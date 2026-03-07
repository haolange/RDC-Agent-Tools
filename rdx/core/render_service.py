"""Headless render 与 readback service。

将 RenderDoc 的 texture display、readback 和 pixel inspection APIs
封装为 async 操作，生成版本化 artifacts，并通过 artifact-store 抽象存储。

所有阻塞的 RenderDoc 调用都会通过 ``asyncio.to_thread`` 分派到线程，
确保服务可在 async event loop 中运行而不阻塞其他协程。

``renderdoc`` module 采用延迟导入——仅在 RenderDoc replay context 中可用，
或当 shared library 已放入 ``sys.path`` 时可用。
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import numpy as np

from rdx.core.errors import RuntimeToolError
from rdx.core.renderdoc_status import status_code_name, status_code_raw
from rdx.models import ArtifactRef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy renderdoc import（延迟导入）
# ---------------------------------------------------------------------------

_rd_module: Any = None


def _get_rd() -> Any:
    """返回 ``renderdoc`` module，并在首次访问时导入。

    当 module 无法加载时抛出带描述信息的 ``ImportError``。
    """
    global _rd_module
    if _rd_module is None:
        try:
            import renderdoc as _rd  # type: ignore[import-not-found]
        except ImportError:
            raise ImportError(
                "The 'renderdoc' Python module is not available.  "
                "Make sure you are running inside a RenderDoc replay "
                "context or that the renderdoc shared library directory "
                "is on sys.path / PYTHONPATH."
            ) from None
        _rd_module = _rd
    return _rd_module


# ---------------------------------------------------------------------------
# Dependency protocols（依赖协议）
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionManager(Protocol):
    """session lifecycle manager 的最小结构化约定。"""

    def get_controller(self, session_id: str) -> Any:
        """返回绑定到 *session_id* 的 ``ReplayController``。"""
        ...

    def get_output(self, session_id: str) -> Any:
        """返回绑定到 *session_id* 的 ``ReplayOutput``。"""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """artifact persistence layer 的最小结构化约定。"""

    async def store(
        self,
        data: bytes,
        *,
        mime: str,
        suffix: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """持久化 *data* 并返回追踪用的 :class:`ArtifactRef`。"""
        ...


# ---------------------------------------------------------------------------
# Constants / look-up tables（常量/查找表）
# ---------------------------------------------------------------------------

_MIME_MAP: Dict[str, str] = {
    "png": "image/png",
    "exr": "image/x-exr",
    "hdr": "image/vnd.radiance",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "dds": "image/vnd-ms.dds",
    "tga": "image/x-tga",
    "bmp": "image/bmp",
    "raw": "application/octet-stream",
    "npz": "application/x-npz",
}

_SUFFIX_MAP: Dict[str, str] = {
    "png": ".png",
    "exr": ".exr",
    "hdr": ".hdr",
    "jpg": ".jpg",
    "jpeg": ".jpg",
    "dds": ".dds",
    "tga": ".tga",
    "bmp": ".bmp",
    "raw": ".raw",
    "npz": ".npz",
}

_FILETYPE_NAME_MAP: Dict[str, str] = {
    "png": "PNG",
    "jpg": "JPG",
    "jpeg": "JPG",
    "dds": "DDS",
    "exr": "EXR",
    "hdr": "HDR",
    "tga": "TGA",
    "bmp": "BMP",
    "raw": "Raw",
}


def _normalize_file_format(file_format: str) -> str:
    fmt = str(file_format or "png").strip().lower()
    if fmt == "jpeg":
        return "jpg"
    return fmt


def _resolve_file_type(file_format: str) -> Tuple[str, Any]:
    rd = _get_rd()
    fmt = _normalize_file_format(file_format)
    enum_name = _FILETYPE_NAME_MAP.get(fmt)
    if not enum_name:
        supported = sorted(k for k in _FILETYPE_NAME_MAP.keys() if k != "jpeg")
        raise ValueError(
            f"Unsupported texture export format '{file_format}'. "
            f"Supported: {', '.join(supported)}"
        )
    file_type = getattr(rd.FileType, enum_name, None)
    if file_type is None:
        raise ValueError(
            f"RenderDoc FileType '{enum_name}' is unavailable in this runtime"
        )
    return fmt, file_type


def _save_texture_result(result: Any) -> Tuple[bool, Dict[str, str]]:
    rd = _get_rd()
    if isinstance(result, bool):
        return result, {
            "result_code_raw": str(result),
            "result_code_name": "bool",
            "status_text": "",
        }

    detail = str(getattr(result, "message", "") or getattr(result, "details", ""))

    for attr in ("code", "result", "status"):
        code = getattr(result, attr, None)
        if code is None:
            continue
        try:
            ok = code == rd.ResultCode.Succeeded
        except Exception:
            ok = "Succeeded" in str(code)
        return ok, {
            "result_code_raw": status_code_raw(code),
            "result_code_name": status_code_name(code),
            "status_text": detail or str(code),
        }

    try:
        ok = bool(result)
    except Exception:
        ok = False
    return ok, {
        "result_code_raw": str(result),
        "result_code_name": type(result).__name__,
        "status_text": detail or str(result),
    }


def _normalize_rgba8_bytes(rgba_bytes: bytes, width: int, height: int) -> bytes:
    expected_rgba8_size = width * height * 4
    if expected_rgba8_size <= 0:
        raise ValueError(f"Invalid output dimensions: {width}x{height}")

    if len(rgba_bytes) == expected_rgba8_size:
        return rgba_bytes

    expected_rgb8_size = width * height * 3
    if len(rgba_bytes) == expected_rgb8_size:
        rgb = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape(height, width, 3)
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        return np.concatenate([rgb, alpha], axis=2).tobytes()

    expected_rgba32f_size = width * height * 16
    if len(rgba_bytes) == expected_rgba32f_size:
        arr = np.frombuffer(rgba_bytes, dtype=np.float32).reshape(height, width, 4)
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
        arr = np.clip(arr, 0.0, 1.0)
        return (arr * 255.0).astype(np.uint8).tobytes()

    if len(rgba_bytes) < expected_rgba8_size:
        logger.warning(
            "Readback buffer too small (%d < %d), padding to RGBA8 size",
            len(rgba_bytes),
            expected_rgba8_size,
        )
        return rgba_bytes + b"\x00" * (expected_rgba8_size - len(rgba_bytes))

    logger.warning(
        "Readback buffer too large (%d > %d), truncating to RGBA8 size",
        len(rgba_bytes),
        expected_rgba8_size,
    )
    return rgba_bytes[:expected_rgba8_size]


def _resolve_overlay(name: str) -> Any:
    """将易读的 overlay 名称映射到 ``rd.DebugOverlay`` 值。"""
    rd = _get_rd()
    no_overlay = getattr(rd.DebugOverlay, "NoOverlay")

    def _pick(*candidates: str) -> Any:
        for candidate in candidates:
            value = getattr(rd.DebugOverlay, candidate, None)
            if value is not None:
                return value
        return no_overlay

    key = name.lower().replace("-", "_").replace(" ", "_")
    table: Dict[str, Any] = {
        "none": no_overlay,
        "nan": _pick("NaN"),
        "clipping": _pick("Clipping"),
        "drawcall": _pick("Drawcall"),
        "wireframe": _pick("Wireframe"),
        "depth": _pick("DepthTest", "Depth"),
        "stencil": _pick("StencilTest", "Stencil"),
        "backface_cull": _pick("BackfaceCull"),
        "viewport_scissor": _pick("ViewportScissor"),
        "quad_overdraw": _pick("QuadOverdrawDraw", "QuadOverdrawPass"),
        "triangle_size": _pick("TriangleSizeDraw", "TriangleSizePass"),
    }
    return table.get(key, no_overlay)


def _is_null_resource_id(resource_id: Any) -> bool:
    """当 *resource_id* 为空/null ID 时返回 ``True``。"""
    rd = _get_rd()
    try:
        return resource_id == rd.ResourceId()
    except Exception:
        return resource_id is None


# ---------------------------------------------------------------------------
# Image encoding（图像编码）
# ---------------------------------------------------------------------------


def _encode_image(
    rgba_bytes: bytes,
    width: int,
    height: int,
    fmt: str,
) -> Tuple[bytes, str]:
    """将原始 RGBA-8 *rgba_bytes* 编码为指定图像格式。

    返回 ``(encoded_bytes, actual_format_used)``。当可选 codec
    （如 EXR / HDR 的 imageio）未安装时，实际格式可能与请求不同。
    """
    from PIL import Image  # type: ignore[import-untyped]

    rgba8_bytes = _normalize_rgba8_bytes(rgba_bytes, width, height)
    img = Image.frombytes("RGBA", (width, height), rgba8_bytes)
    buf = io.BytesIO()
    upper = fmt.upper()

    if upper == "PNG":
        img.save(buf, format="PNG", compress_level=6)
        return buf.getvalue(), "png"

    if upper in ("JPG", "JPEG"):
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "jpg"

    if upper == "EXR":
        try:
            import imageio.v3 as iio  # type: ignore[import-untyped]

            arr = np.frombuffer(rgba8_bytes, dtype=np.uint8).reshape(
                height, width, 4,
            )
            arr_f = arr.astype(np.float32) / 255.0
            encoded = iio.imwrite("<bytes>", arr_f, extension=".exr")
            return bytes(encoded), "exr"
        except Exception:
            logger.warning("EXR encoding unavailable; falling back to PNG")
            img.save(buf, format="PNG")
            return buf.getvalue(), "png"

    if upper == "HDR":
        try:
            import imageio.v3 as iio  # type: ignore[import-untyped]

            arr = np.frombuffer(rgba8_bytes, dtype=np.uint8).reshape(
                height, width, 4,
            )
            # HDR 仅支持 3 通道（RGB）。
            arr_f = arr[:, :, :3].astype(np.float32) / 255.0
            encoded = iio.imwrite("<bytes>", arr_f, extension=".hdr")
            return bytes(encoded), "hdr"
        except Exception:
            logger.warning("HDR encoding unavailable; falling back to PNG")
            img.save(buf, format="PNG")
            return buf.getvalue(), "png"

    # Unknown format -- default to PNG.
    logger.warning("Unknown output format %r; falling back to PNG", fmt)
    img.save(buf, format="PNG")
    return buf.getvalue(), "png"


# ---------------------------------------------------------------------------
# RenderService
# ---------------------------------------------------------------------------


class RenderService:
    """Headless render、readback 与 pixel inspection service。

    所有公开方法均为 ``async``，并显式接收 *session_manager* /
    *artifact_store* 依赖，因此服务本身无状态且易于用 fakes 进行单元测试。
    """

    # ------------------------------------------------------------------
    # render_event
    # ------------------------------------------------------------------

    async def render_event(
        self,
        session_id: str,
        event_id: int,
        session_manager: SessionManager,
        artifact_store: ArtifactStore,
        source_config: Optional[Dict[str, Any]] = None,
        view_config: Optional[Dict[str, Any]] = None,
        output_format: str = "png",
    ) -> Tuple[ArtifactRef, Dict[str, Any]]:
        """渲染指定 event 并将结果保存为图像 artifact。

        Parameters
        ----------
        session_id:
            活跃 replay session id。
        event_id:
            需要导航到的 draw-call / API event。
        session_manager:
            提供 ``ReplayController`` 与 ``ReplayOutput``。
        artifact_store:
            生成图像的持久化层。
        source_config:
            选择 **渲染对象**：

            * ``{"source": "final_output"}`` *(默认)* —— 当前 draw 的最后一个
              bound colour output render-target。
            * ``{"source": "texture", "texture_id": <int|ResourceId>}``
              —— 指定 texture。
        view_config:
            可视化展示配置（均可选）：

            * ``scale`` *(float)* —— 缩放因子，``0`` 表示 fit。
            * ``channels`` *(dict)* —— ``{"r": bool, "g": bool, ...}``。
            * ``overlay`` *(str)* —— debug overlay 名称（``nan``、
              ``clipping``、``wireframe`` 等）。
            * ``hdr`` *(bool)* —— 启用 HDR multiplier。
            * ``hdr_multiplier`` *(float)* —— HDR multiplier 值。
            * ``range_min`` / ``range_max`` *(float)* —— 显示范围。
            * ``flip_y`` *(bool)* —— 垂直翻转。
            * ``raw_output`` *(bool)* —— 跳过 sRGB 转换。
        output_format:
            图像格式（``png``, ``exr``, ``hdr``, ``jpg``）。

        Returns
        -------
        tuple[ArtifactRef, dict]
            存储的 artifact 引用与 view-metadata 字典。
        """
        rd = _get_rd()
        source_config = source_config or {"source": "final_output"}
        view_config = view_config or {}

        controller = session_manager.get_controller(session_id)
        output = session_manager.get_output(session_id)
        backend_type = "unknown"
        get_state = getattr(session_manager, "get_state", None)
        if callable(get_state):
            try:
                state = get_state(session_id)
                backend_type = str(getattr(state, "backend_type", "unknown")).lower()
            except Exception:
                backend_type = "unknown"

        # 导航到指定 event。
        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        # 确定目标 texture。
        tex_id = await self._resolve_source_texture(controller, source_config)

        # ---- 构建 TextureDisplay ------------------------------------
        tex_display = rd.TextureDisplay()
        tex_display.resourceId = tex_id
        tex_display.subresource = rd.Subresource()

        # Scale: 0 = fit to output window.
        tex_display.scale = float(view_config.get("scale", 0))

        # Display range (maps to visualised min/max).
        tex_display.rangeMin = float(view_config.get("range_min", 0.0))
        tex_display.rangeMax = float(view_config.get("range_max", 1.0))

        # HDR multiplier.  A negative value disables HDR display.
        tex_display.hdrMultiplier = float(
            view_config.get("hdr_multiplier", -1.0)
        )
        if view_config.get("hdr", False) and tex_display.hdrMultiplier < 0:
            tex_display.hdrMultiplier = 4.0

        # Flip / raw output.
        tex_display.flipY = bool(view_config.get("flip_y", False))
        tex_display.rawOutput = bool(view_config.get("raw_output", False))

        # Channel visibility.
        channels = view_config.get("channels", {})
        tex_display.red = bool(channels.get("r", True))
        tex_display.green = bool(channels.get("g", True))
        tex_display.blue = bool(channels.get("b", True))
        tex_display.alpha = bool(channels.get("a", True))

        # Debug overlay.
        overlay_name = str(view_config.get("overlay", "none"))
        tex_display.overlay = _resolve_overlay(overlay_name)

        # ---- Render and readback -------------------------------------
        await asyncio.to_thread(output.SetTextureDisplay, tex_display)
        await asyncio.to_thread(output.Display)

        rgba_bytes: bytes = await asyncio.to_thread(
            output.ReadbackOutputTexture,
        )
        width, height = await asyncio.to_thread(output.GetDimensions)

        # ---- Encode --------------------------------------------------
        fmt_lower = output_format.lower()
        image_bytes, actual_fmt = await asyncio.to_thread(
            _encode_image, rgba_bytes, width, height, fmt_lower,
        )

        mime = _MIME_MAP.get(actual_fmt, "image/png")
        suffix = _SUFFIX_MAP.get(actual_fmt, ".png")

        view_meta: Dict[str, Any] = {
            "event_id": event_id,
            "texture_id": str(tex_id),
            "width": width,
            "height": height,
            "format": actual_fmt,
            "overlay": overlay_name,
            "channels": {
                "r": tex_display.red,
                "g": tex_display.green,
                "b": tex_display.blue,
                "a": tex_display.alpha,
            },
            "scale": tex_display.scale,
        }

        artifact_ref = await artifact_store.store(
            image_bytes,
            mime=mime,
            suffix=suffix,
            meta=view_meta,
        )

        logger.debug(
            "render_event: event=%d tex=%s %dx%d fmt=%s -> %s",
            event_id, tex_id, width, height, actual_fmt, artifact_ref.uri,
        )
        return artifact_ref, view_meta

    async def save_texture_file(
        self,
        session_id: str,
        event_id: int,
        texture_id: Any,
        session_manager: SessionManager,
        artifact_store: ArtifactStore,
        *,
        output_format: str = "png",
        output_path: Optional[str] = None,
        subresource: Optional[Dict[str, int]] = None,
    ) -> Tuple[ArtifactRef, Dict[str, Any], Optional[str]]:
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)
        backend_type = "unknown"
        get_state = getattr(session_manager, "get_state", None)
        if callable(get_state):
            try:
                state = get_state(session_id)
                backend_type = str(getattr(state, "backend_type", "unknown")).lower()
            except Exception:
                backend_type = "unknown"

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)
        resolved_id = await self._resolve_texture_id(controller, texture_id)
        fmt, file_type = _resolve_file_type(output_format)

        save_data = rd.TextureSave()
        save_data.resourceId = resolved_id
        save_data.destType = file_type
        sub = subresource or {}
        save_data.mip = int(sub.get("mip", 0))
        try:
            save_data.slice.sliceIndex = max(0, int(sub.get("slice", 0)))
        except Exception:
            pass
        try:
            save_data.sample.sampleIndex = max(0, int(sub.get("sample", 0)))
        except Exception:
            pass

        suffix = _SUFFIX_MAP.get(fmt, f".{fmt}")
        mime = _MIME_MAP.get(fmt, "application/octet-stream")
        target_path: Path
        owns_temp_file = False
        if output_path:
            target_path = Path(output_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            fd, temp_path = tempfile.mkstemp(prefix="rdx_tex_", suffix=suffix)
            os.close(fd)
            target_path = Path(temp_path)
            owns_temp_file = True

        try:
            if fmt == "raw":
                sub = rd.Subresource()
                sub.mip = int(subresource.get("mip", 0) if subresource else 0)
                sub.slice = int(subresource.get("slice", 0) if subresource else 0)
                sub.sample = int(subresource.get("sample", 0) if subresource else 0)
                raw_data = await asyncio.to_thread(
                    controller.GetTextureData,
                    resolved_id,
                    sub,
                )
                await asyncio.to_thread(target_path.write_bytes, bytes(raw_data))
            else:
                save_result = await asyncio.to_thread(
                    controller.SaveTexture,
                    save_data,
                    str(target_path),
                )
                ok, status_payload = _save_texture_result(save_result)
                if not ok:
                    details = {
                        "source_layer": "renderdoc_status",
                        "operation": "SaveTexture",
                        "backend_type": backend_type,
                        "capture_context": {
                            "session_id": session_id,
                            "event_id": event_id,
                            "texture_id": str(resolved_id),
                            "subresource": dict(subresource or {}),
                            "file_format": fmt,
                        },
                        "renderdoc_status": status_payload,
                        "classification": "renderdoc_status",
                        "fix_hint": "Inspect the RenderDoc status and texture export inputs before retrying SaveTexture.",
                        "failure_stage": "controller.SaveTexture",
                    }
                    raise RuntimeToolError(
                        f"SaveTexture failed: {status_payload['status_text'] or 'unknown error'}",
                        details=details,
                    )
            if not target_path.is_file():
                raise ValueError(
                    f"SaveTexture succeeded but file was not created: {target_path}"
                )
            byte_size = int(target_path.stat().st_size)
            if byte_size <= 0:
                raise ValueError(
                    f"SaveTexture created an empty file: {target_path}"
                )

            meta: Dict[str, Any] = {
                "event_id": event_id,
                "texture_id": str(resolved_id),
                "file_format": fmt,
                "byte_size": byte_size,
                "subresource": {
                    "mip": int(getattr(save_data, "mip", 0)),
                    "slice": int(getattr(save_data.slice, "sliceIndex", 0)),
                    "sample": int(getattr(save_data.sample, "sampleIndex", 0)),
                },
            }
            artifact_ref = await artifact_store.store_file(
                target_path,
                mime=mime,
                suffix=suffix,
                meta=meta,
            )
            return artifact_ref, meta, str(target_path) if output_path else None
        finally:
            if owns_temp_file:
                try:
                    target_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # readback_texture
    # ------------------------------------------------------------------

    async def readback_texture(
        self,
        session_id: str,
        event_id: int,
        texture_id: Any,
        session_manager: SessionManager,
        artifact_store: ArtifactStore,
        subresource: Optional[Dict[str, int]] = None,
        region: Optional[Dict[str, int]] = None,
    ) -> Tuple[ArtifactRef, Dict[str, Any]]:
        """读取原始 texture 数据并保存为 NumPy ``.npz``。

        Parameters
        ----------
        session_id, event_id:
            Replay 坐标。
        texture_id:
            要读取的 texture 的 ``ResourceId``（或其整数形式）。
        session_manager, artifact_store:
            注入的依赖。
        subresource:
            ``{"mip": int, "slice": int, "sample": int}`` —— 默认
            mip 0 / slice 0 / sample 0。
        region:
            ``{"x": int, "y": int, "width": int, "height": int}`` 的
            texel 裁剪矩形。省略时返回完整 mip level。

        Returns
        -------
        tuple[ArtifactRef, dict]
            artifact 引用与统计信息字典（shape、dtype、每通道 min/max、
            nan_count、inf_count）。
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        # 构建 subresource 描述符。
        sub = rd.Subresource()
        if subresource:
            sub.mip = int(subresource.get("mip", 0))
            sub.slice = int(subresource.get("slice", 0))
            sub.sample = int(subresource.get("sample", 0))

        # 从 capture 元数据解析 texture 尺寸。
        resolved_id = await self._resolve_texture_id(controller, texture_id)
        tex_desc = await self._find_texture_desc(controller, resolved_id)
        if tex_desc is None:
            raise ValueError(
                f"Texture {texture_id} not found in capture resources"
            )

        tex_width = max(1, tex_desc.width >> sub.mip)
        tex_height = max(1, tex_desc.height >> sub.mip)

        # 从 GPU readback 获取原始数据。
        raw_data: bytes = await asyncio.to_thread(
            controller.GetTextureData, resolved_id, sub,
        )

        # ---- 解析字节布局 ---------------------------------------------
        # RenderDoc 返回紧密打包的像素数据，其分量布局与资源格式一致。
        # 这里使用简单启发式基于 buffer 大小选择 float32-RGBA 或 uint8-RGBA。
        expected_pixels = tex_width * tex_height
        bytes_per_pixel_f32 = 16  # 4 channels x 4 bytes
        bytes_per_pixel_u8 = 4   # 4 channels x 1 byte

        if len(raw_data) >= expected_pixels * bytes_per_pixel_f32:
            arr = np.frombuffer(
                raw_data[: expected_pixels * bytes_per_pixel_f32],
                dtype=np.float32,
            ).reshape(tex_height, tex_width, 4)
        elif len(raw_data) >= expected_pixels * bytes_per_pixel_u8:
            arr = np.frombuffer(
                raw_data[: expected_pixels * bytes_per_pixel_u8],
                dtype=np.uint8,
            ).reshape(tex_height, tex_width, 4)
        else:
            # Unknown / compressed layout -- store the flat byte array.
            logger.warning(
                "readback_texture: unexpected buffer size %d for %dx%d "
                "texture; storing flat uint8 array",
                len(raw_data), tex_width, tex_height,
            )
            arr = np.frombuffer(raw_data, dtype=np.uint8)

        # ---- Optional crop -------------------------------------------
        if region and arr.ndim == 3:
            rx = max(0, min(int(region.get("x", 0)), arr.shape[1] - 1))
            ry = max(0, min(int(region.get("y", 0)), arr.shape[0] - 1))
            rw = max(
                1,
                min(int(region.get("width", arr.shape[1] - rx)),
                    arr.shape[1] - rx),
            )
            rh = max(
                1,
                min(int(region.get("height", arr.shape[0] - ry)),
                    arr.shape[0] - ry),
            )
            arr = arr[ry: ry + rh, rx: rx + rw]

        # ---- Statistics ----------------------------------------------
        stats: Dict[str, Any] = {
            "event_id": event_id,
            "texture_id": str(texture_id),
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
        }

        if np.issubdtype(arr.dtype, np.floating):
            stats["nan_count"] = int(np.isnan(arr).sum())
            stats["inf_count"] = int(np.isinf(arr).sum())
            finite = arr[np.isfinite(arr)]
            if finite.size > 0:
                stats["min"] = float(finite.min())
                stats["max"] = float(finite.max())
                stats["mean"] = float(finite.mean())
            else:
                stats["min"] = None
                stats["max"] = None
                stats["mean"] = None
        else:
            stats["nan_count"] = 0
            stats["inf_count"] = 0
            stats["min"] = int(arr.min()) if arr.size > 0 else None
            stats["max"] = int(arr.max()) if arr.size > 0 else None
            stats["mean"] = float(arr.mean()) if arr.size > 0 else None

        # ---- Serialize to .npz --------------------------------------
        npz_buf = io.BytesIO()
        np.savez_compressed(npz_buf, pixels=arr)
        npz_bytes = npz_buf.getvalue()

        artifact_ref = await artifact_store.store(
            npz_bytes,
            mime="application/x-npz",
            suffix=".npz",
            meta=stats,
        )

        logger.debug(
            "readback_texture: event=%d tex=%s shape=%s dtype=%s -> %s",
            event_id, texture_id, arr.shape, arr.dtype, artifact_ref.uri,
        )
        return artifact_ref, stats

    # ------------------------------------------------------------------
    # pick_pixel
    # ------------------------------------------------------------------

    async def pick_pixel(
        self,
        session_id: str,
        event_id: int,
        texture_id: Any,
        x: int,
        y: int,
        session_manager: SessionManager,
    ) -> Dict[str, Any]:
        """在指定 event 的 texture 中读取单个像素。

        Returns
        -------
        dict
            ``{x, y, event_id, texture_id, r, g, b, a,
            value_type, has_nan, has_inf}``
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        resolved_id = await self._resolve_texture_id(controller, texture_id)
        sub = rd.Subresource()

        pixel_value = await asyncio.to_thread(
            controller.PickPixel,
            resolved_id,
            int(x),
            int(y),
            sub,
            rd.CompType.Typeless,
        )

        # PixelValue 暴露 .floatValue、.uintValue、.intValue 数组；
        # Float 覆盖绝大多数用例。
        fv: List[float] = list(pixel_value.floatValue[:4])

        result: Dict[str, Any] = {
            "x": x,
            "y": y,
            "event_id": event_id,
            "texture_id": str(texture_id),
            "r": fv[0],
            "g": fv[1],
            "b": fv[2],
            "a": fv[3],
            "value_type": "float",
            "has_nan": any(math.isnan(v) for v in fv),
            "has_inf": any(math.isinf(v) for v in fv),
        }

        # 对整数格式的 texture 也提供整数解释。
        try:
            uv = list(pixel_value.uintValue[:4])
            result["r_uint"] = uv[0]
            result["g_uint"] = uv[1]
            result["b_uint"] = uv[2]
            result["a_uint"] = uv[3]
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # get_texture_stats
    # ------------------------------------------------------------------

    async def get_texture_stats(
        self,
        session_id: str,
        event_id: int,
        texture_id: Any,
        session_manager: SessionManager,
    ) -> Dict[str, Any]:
        """计算 texture 的逐通道 min/max 统计值。

        使用 RenderDoc 的 GPU 加速 ``GetMinMax``，避免完整的 CPU 端 readback。

        Returns
        -------
        dict
            ``{event_id, texture_id, channels: {r,g,b,a: {min,max,...}},
            overall_min, overall_max, has_any_nan, has_any_inf}``
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        resolved_id = await self._resolve_texture_id(controller, texture_id)
        sub = rd.Subresource()

        min_val, max_val = await asyncio.to_thread(
            controller.GetMinMax,
            resolved_id,
            sub,
            rd.CompType.Typeless,
        )

        min_f: List[float] = list(min_val.floatValue[:4])
        max_f: List[float] = list(max_val.floatValue[:4])

        channel_names = ("r", "g", "b", "a")
        channels: Dict[str, Dict[str, Any]] = {}
        for i, name in enumerate(channel_names):
            channels[name] = {
                "min": min_f[i],
                "max": max_f[i],
                "has_nan": math.isnan(min_f[i]) or math.isnan(max_f[i]),
                "has_inf": math.isinf(min_f[i]) or math.isinf(max_f[i]),
            }

        # 汇总有限（非 NaN/Inf）通道值的统计。
        finite_mins = [
            v for v in min_f if not (math.isnan(v) or math.isinf(v))
        ]
        finite_maxs = [
            v for v in max_f if not (math.isnan(v) or math.isinf(v))
        ]
        overall_min = min(finite_mins) if finite_mins else float("nan")
        overall_max = max(finite_maxs) if finite_maxs else float("nan")

        return {
            "event_id": event_id,
            "texture_id": str(texture_id),
            "channels": channels,
            "overall_min": overall_min,
            "overall_max": overall_max,
            "has_any_nan": any(
                channels[c]["has_nan"] for c in channel_names
            ),
            "has_any_inf": any(
                channels[c]["has_inf"] for c in channel_names
            ),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolve_source_texture(
        controller: Any,
        source_config: Dict[str, Any],
    ) -> Any:
        """确定需要渲染的 texture ResourceId。

        对于 ``"final_output"``，会检查 pipeline state 的 output targets，
        并返回最后一个非空 colour attachment。
        对于 ``"texture"``，会使用显式的 *texture_id*。
        """
        rd = _get_rd()
        source = source_config.get("source", "final_output")

        if source == "texture":
            tex_id_raw = source_config.get("texture_id")
            if tex_id_raw is None:
                raise ValueError(
                    "source_config with source='texture' requires a "
                    "'texture_id' field"
                )
            # 若调用方传入整数，则在 texture 列表中查找。
            if isinstance(tex_id_raw, int):
                textures = await asyncio.to_thread(controller.GetTextures)
                for tex in textures:
                    if int(tex.resourceId) == tex_id_raw:
                        return tex.resourceId
                raise ValueError(
                    f"No texture found matching id {tex_id_raw}"
                )
            # 否则假定已是 ResourceId。
            return tex_id_raw

        # "final_output" —— 当前 draw call 的最后一个非空 colour output。
        pipe_state = await asyncio.to_thread(controller.GetPipelineState)
        targets = pipe_state.GetOutputTargets()
        for target in reversed(targets):
            resource_id = getattr(target, "resourceId", None)
            if resource_id is None:
                descriptor_resource = getattr(target, "resource", None)
                resource_id = getattr(descriptor_resource, "resourceId", descriptor_resource)
            if resource_id is None:
                continue
            if not _is_null_resource_id(resource_id):
                return resource_id

        # 回退：使用 capture 中的第一张 texture。
        textures = await asyncio.to_thread(controller.GetTextures)
        if textures:
            logger.warning(
                "_resolve_source_texture: no output targets found; "
                "falling back to first capture texture",
            )
            return textures[0].resourceId

        raise RuntimeError(
            "No output render target or texture found for the current event"
        )

    @staticmethod
    async def _resolve_texture_id(
        controller: Any,
        texture_id: Any,
    ) -> Any:
        """将 *texture_id* 转换为 RenderDoc ``ResourceId``。

        支持 ``int``（扫描 textures）或已解析的 ``ResourceId``。
        """
        if isinstance(texture_id, int):
            textures = await asyncio.to_thread(controller.GetTextures)
            for tex in textures:
                if int(tex.resourceId) == texture_id:
                    return tex.resourceId
            raise ValueError(f"No texture found matching id {texture_id}")
        return texture_id

    @staticmethod
    async def _find_texture_desc(
        controller: Any,
        resource_id: Any,
    ) -> Any:
        """查找 *resource_id* 对应的 ``TextureDescription``。

        若该 texture 不在 capture 中，则返回 ``None``。
        """
        textures = await asyncio.to_thread(controller.GetTextures)
        for tex in textures:
            if tex.resourceId == resource_id:
                return tex
        return None
