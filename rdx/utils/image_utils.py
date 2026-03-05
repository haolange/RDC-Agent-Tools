"""
GPU debug 可视化的图像处理工具。

所有函数均在 ``(H, W, C)`` 布局的 NumPy 数组和/或 PIL Images 上工作。
同时支持 HDR（``float32``）与 LDR（``uint8``）像素格式。
"""

from __future__ import annotations

import io
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Internal helpers（内部辅助）
# ---------------------------------------------------------------------------


def _ensure_float32(pixels: np.ndarray) -> np.ndarray:
    """若 *pixels* 为整数类型，则提升为 ``float32``。

    ``uint8`` 数值会被归一化到 ``[0, 1]`` 区间。
    """
    if pixels.dtype == np.float32:
        return pixels
    if pixels.dtype == np.float64:
        return pixels.astype(np.float32)
    if np.issubdtype(pixels.dtype, np.integer):
        return pixels.astype(np.float32) / np.float32(np.iinfo(pixels.dtype).max)
    return pixels.astype(np.float32)


def _ensure_uint8(pixels: np.ndarray) -> np.ndarray:
    """将 *pixels* 转换为 ``uint8`` 并裁剪到 ``[0, 255]``。"""
    if pixels.dtype == np.uint8:
        return pixels
    arr = np.clip(pixels, 0.0, 1.0) * 255.0
    return arr.astype(np.uint8)


def _ensure_3ch(pixels: np.ndarray) -> np.ndarray:
    """确保 *pixels* 拥有 3 个颜色通道（H, W, 3）。

    - 单通道 (H, W) 或 (H, W, 1) 会复制为灰度 RGB。
    - RGBA (H, W, 4) 会去除 alpha 通道。
    """
    if pixels.ndim == 2:
        return np.stack([pixels, pixels, pixels], axis=-1)
    c = pixels.shape[2]
    if c == 1:
        return np.concatenate([pixels, pixels, pixels], axis=-1)
    if c == 4:
        return pixels[:, :, :3]
    return pixels


def _bbox_from_mask(mask: np.ndarray) -> Optional[Dict[str, int]]:
    """从布尔 mask 计算紧致的轴对齐 bounding box。

    当 mask 全为 ``False`` 时返回 ``None``。
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    return {
        "x0": int(xs.min()),
        "y0": int(ys.min()),
        "x1": int(xs.max()),
        "y1": int(ys.max()),
    }


# ---------------------------------------------------------------------------
# NaN / Inf detection
# ---------------------------------------------------------------------------


def compute_naninf_mask(
    pixels: np.ndarray,
) -> Tuple[np.ndarray, Dict]:
    """生成用于高亮 NaN 与 Inf 像素的彩色 mask。

    Parameters
    ----------
    pixels:
        任意数值 dtype 的图像数组 ``(H, W, C)``。整数图像不会出现 NaN/Inf，
        因此结果将是空 mask。

    Returns
    -------
    mask_image:
        ``uint8`` RGBA 数组 ``(H, W, 4)``：NaN 像素绘制为红色
        ``(255, 0, 0, 255)``，Inf 像素为蓝色 ``(0, 0, 255, 255)``，
        其余像素完全透明 ``(0, 0, 0, 0)``。
    stats:
        包含 ``nan_count``、``inf_count``、``total_pixels``、
        ``density``（坏像素占比）与 ``bbox``（紧致 bounding box
        ``{x0, y0, x1, y1}`` 或 ``None``）的字典。
    """
    h, w = pixels.shape[:2]
    total_pixels = h * w

    mask_image = np.zeros((h, w, 4), dtype=np.uint8)

    fpix = pixels.astype(np.float64, copy=False)

    # 跨通道汇总：只要 *任一* 通道为 NaN，该像素即为 NaN。
    if fpix.ndim == 3:
        nan_mask = np.any(np.isnan(fpix), axis=2)
        inf_mask = np.any(np.isinf(fpix), axis=2)
    else:
        nan_mask = np.isnan(fpix)
        inf_mask = np.isinf(fpix)

    nan_count = int(nan_mask.sum())
    inf_count = int(inf_mask.sum())

    # Paint the mask (NaN takes precedence over Inf when both are present).
    mask_image[inf_mask] = [0, 0, 255, 255]
    mask_image[nan_mask] = [255, 0, 0, 255]

    bad_mask = nan_mask | inf_mask
    density = float(bad_mask.sum()) / total_pixels if total_pixels > 0 else 0.0
    bbox = _bbox_from_mask(bad_mask)

    stats = {
        "nan_count": nan_count,
        "inf_count": inf_count,
        "total_pixels": total_pixels,
        "density": density,
        "bbox": bbox,
    }
    return mask_image, stats


# ---------------------------------------------------------------------------
# Diff map
# ---------------------------------------------------------------------------


def compute_diff_map(
    img_a: np.ndarray,
    img_b: np.ndarray,
    threshold: float = 0.01,
) -> Tuple[np.ndarray, Dict]:
    """计算两张图像的逐像素 L2 距离 heatmap。

    两张图像会提升到 ``float32`` 且必须具有相同的空间尺寸。
    若通道数不同，则先统一为 3 通道。

    Parameters
    ----------
    img_a, img_b:
        图像数组 ``(H, W, C)``。
    threshold:
        逐像素 L2 距离阈值，低于该值认为像素一致。

    Returns
    -------
    heatmap:
        ``uint8`` RGB heatmap ``(H, W, 3)``，从黑色（无差异）到亮红（最大差异）。
    stats:
        包含 ``mean_diff``、``max_diff``、``diff_pixel_count``、
        ``diff_ratio`` 与 ``bbox`` 的字典。

    Raises
    ------
    ValueError
        当两张图像的空间尺寸不匹配时抛出。
    """
    a = _ensure_float32(img_a)
    b = _ensure_float32(img_b)

    # 统一通道数。
    a = _ensure_3ch(a)
    b = _ensure_3ch(b)

    if a.shape[:2] != b.shape[:2]:
        raise ValueError(
            f"Spatial dimensions must match: {a.shape[:2]} vs {b.shape[:2]}"
        )

    h, w = a.shape[:2]
    total_pixels = h * w

    # Per-pixel L2 distance across channels.
    diff = np.sqrt(np.sum((a - b) ** 2, axis=2))  # (H, W)
    max_diff = float(diff.max()) if diff.size > 0 else 0.0
    mean_diff = float(diff.mean()) if diff.size > 0 else 0.0

    above_threshold = diff > threshold
    diff_pixel_count = int(above_threshold.sum())
    diff_ratio = diff_pixel_count / total_pixels if total_pixels > 0 else 0.0

    # Normalise diff to [0, 1] for the heatmap.
    if max_diff > 0:
        norm = np.clip(diff / max_diff, 0.0, 1.0)
    else:
        norm = np.zeros_like(diff)

    # Build an RGB heatmap: black -> red -> yellow -> white.
    heatmap = np.zeros((h, w, 3), dtype=np.float32)
    heatmap[:, :, 0] = np.clip(norm * 3.0, 0.0, 1.0)             # red ramp
    heatmap[:, :, 1] = np.clip((norm - 0.33) * 3.0, 0.0, 1.0)    # green ramp
    heatmap[:, :, 2] = np.clip((norm - 0.66) * 3.0, 0.0, 1.0)    # blue ramp

    heatmap_u8 = _ensure_uint8(heatmap)

    bbox = _bbox_from_mask(above_threshold)

    stats = {
        "mean_diff": mean_diff,
        "max_diff": max_diff,
        "diff_pixel_count": diff_pixel_count,
        "diff_ratio": diff_ratio,
        "bbox": bbox,
    }
    return heatmap_u8, stats


# ---------------------------------------------------------------------------
# Bounding-box overlay
# ---------------------------------------------------------------------------


def overlay_bbox(
    img: np.ndarray,
    bbox: Dict[str, int],
    color: Tuple[int, ...] = (255, 0, 0, 180),
    thickness: int = 2,
) -> np.ndarray:
    """在图像上绘制轴对齐的 bounding box 矩形。

    Parameters
    ----------
    img:
        ``uint8`` 的源图像 ``(H, W, C)``。不会修改原图，返回副本。
    bbox:
        具有整数键 ``x0``, ``y0``, ``x1``, ``y1`` 的字典。
    color:
        矩形颜色的 RGBA 元组。
    thickness:
        线条厚度（像素）。

    Returns
    -------
    np.ndarray
        绘制矩形后的 *img* 副本。
    """
    out = img.copy()
    h, w = out.shape[:2]
    has_alpha = out.ndim == 3 and out.shape[2] == 4

    x0 = max(0, int(bbox["x0"]))
    y0 = max(0, int(bbox["y0"]))
    x1 = min(w - 1, int(bbox["x1"]))
    y1 = min(h - 1, int(bbox["y1"]))

    # 根据通道数确定绘制颜色。
    if has_alpha:
        draw_color = np.array(color[:4], dtype=np.uint8)
    elif out.ndim == 3 and out.shape[2] == 3:
        draw_color = np.array(color[:3], dtype=np.uint8)
    else:
        # 灰度：使用颜色的亮度值。
        draw_color = np.uint8(
            0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
        )

    # Alpha blending factor (only meaningful when colour has an alpha).
    alpha = color[3] / 255.0 if len(color) >= 4 else 1.0

    def _draw_hline(y: int, xa: int, xb: int) -> None:
        if 0 <= y < h:
            xa = max(0, xa)
            xb = min(w - 1, xb)
            if alpha < 1.0 and out.ndim == 3:
                existing = out[y, xa : xb + 1].astype(np.float32)
                blended = existing * (1.0 - alpha) + draw_color[:existing.shape[1]].astype(np.float32) * alpha
                out[y, xa : xb + 1] = np.clip(blended, 0, 255).astype(np.uint8)
            else:
                out[y, xa : xb + 1] = draw_color

    def _draw_vline(x: int, ya: int, yb: int) -> None:
        if 0 <= x < w:
            ya = max(0, ya)
            yb = min(h - 1, yb)
            if alpha < 1.0 and out.ndim == 3:
                existing = out[ya : yb + 1, x].astype(np.float32)
                blended = existing * (1.0 - alpha) + draw_color[:existing.shape[1]].astype(np.float32) * alpha
                out[ya : yb + 1, x] = np.clip(blended, 0, 255).astype(np.uint8)
            else:
                out[ya : yb + 1, x] = draw_color

    for t in range(thickness):
        _draw_hline(y0 + t, x0, x1)         # top edge
        _draw_hline(y1 - t, x0, x1)         # bottom edge
        _draw_vline(x0 + t, y0, y1)         # left edge
        _draw_vline(x1 - t, y0, y1)         # right edge

    return out


# ---------------------------------------------------------------------------
# Pixel statistics
# ---------------------------------------------------------------------------


def pixel_stats(
    pixels: np.ndarray,
    region: Optional[Dict[str, int]] = None,
) -> Dict:
    """计算像素数据的逐通道统计。

    Parameters
    ----------
    pixels:
        图像数组 ``(H, W, C)`` 或 ``(H, W)``。
    region:
        可选子区域 ``{x0, y0, x1, y1}``。提供后仅统计该轴对齐矩形内的像素。

    Returns
    -------
    dict
        键包括 ``min``、``max``、``mean``、``std``（均为逐通道列表），以及
        ``has_nan``、``has_inf``。
    """
    arr = pixels
    if region is not None:
        x0 = max(0, int(region["x0"]))
        y0 = max(0, int(region["y0"]))
        x1 = min(arr.shape[1], int(region["x1"]) + 1)
        y1 = min(arr.shape[0], int(region["y1"]) + 1)
        arr = arr[y0:y1, x0:x1]

    arr_f = arr.astype(np.float64, copy=False)

    if arr_f.ndim == 2:
        arr_f = arr_f[:, :, np.newaxis]

    num_channels = arr_f.shape[2]
    ch_min = []
    ch_max = []
    ch_mean = []
    ch_std = []

    for c in range(num_channels):
        channel = arr_f[:, :, c]
        # Use nanmin/nanmax so that NaN values do not poison the result.
        ch_min.append(float(np.nanmin(channel)))
        ch_max.append(float(np.nanmax(channel)))
        ch_mean.append(float(np.nanmean(channel)))
        ch_std.append(float(np.nanstd(channel)))

    has_nan = bool(np.any(np.isnan(arr_f)))
    has_inf = bool(np.any(np.isinf(arr_f)))

    return {
        "min": ch_min,
        "max": ch_max,
        "mean": ch_mean,
        "std": ch_std,
        "has_nan": has_nan,
        "has_inf": has_inf,
    }


# ---------------------------------------------------------------------------
# PNG 序列化
# ---------------------------------------------------------------------------


def array_to_png_bytes(arr: np.ndarray) -> bytes:
    """将 NumPy 图像数组转换为 PNG 字节。

    Parameters
    ----------
    arr:
        ``uint8`` 或 ``float32`` 的图像数组
        ``(H, W)``, ``(H, W, 1)``, ``(H, W, 3)``, 或 ``(H, W, 4)``。

    Returns
    -------
    bytes
        PNG 编码图像。
    """
    # 处理 float 图像：将 NaN/Inf 夹到有限范围，再归一化到 [0, 1]。
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
        arr = _ensure_uint8(arr)

    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]

    mode_map = {2: "L", 3: "RGB", 4: "RGBA"}
    mode = mode_map.get(arr.ndim if arr.ndim == 2 else arr.shape[2], "RGB")
    if arr.ndim == 2:
        mode = "L"

    img = Image.fromarray(arr, mode=mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def png_bytes_to_array(data: bytes) -> np.ndarray:
    """将 PNG 字节解码为 NumPy ``uint8`` 数组。

    Parameters
    ----------
    data:
        原始 PNG 字节。

    Returns
    -------
    np.ndarray
        ``dtype=uint8`` 的图像数组 ``(H, W, C)``。
    """
    img = Image.open(io.BytesIO(data))
    arr = np.asarray(img, dtype=np.uint8)
    # 确保为 3-D 以保持一致性（单通道图像变为 (H, W, 1)）。
    if arr.ndim == 2:
        arr = arr[:, :, np.newaxis]
    return arr


# ---------------------------------------------------------------------------
# HDR tonemapping
# ---------------------------------------------------------------------------


def tonemap_hdr(
    pixels: np.ndarray,
    exposure: float = 1.0,
) -> np.ndarray:
    """对 HDR 像素数据应用简单的 Reinhard tonemapping。

    .. math::

        L_{\\text{out}} = \\frac{L_{\\text{in}} \\cdot e}{1 + L_{\\text{in}} \\cdot e}

    其中 *e* 为 *exposure* 乘数。

    Parameters
    ----------
    pixels:
        ``float32`` 的 HDR 图像数组 ``(H, W, C)``。非浮点输入将原样返回
        （已是 LDR）。
    exposure:
        tonemapping 前的曝光倍增系数。

    Returns
    -------
    np.ndarray
        tonemapping 后的 ``uint8`` LDR 图像 ``(H, W, C)``。
    """
    if not np.issubdtype(pixels.dtype, np.floating):
        # 已是 LDR；无需处理。
        return pixels

    fp = pixels.astype(np.float32, copy=True)

    # Replace NaN/Inf with safe values so tonemapping does not propagate them.
    fp = np.nan_to_num(fp, nan=0.0, posinf=1e4, neginf=0.0)

    # Clamp negatives (can occur in HDR data from certain compute shaders).
    fp = np.maximum(fp, 0.0)

    # Apply exposure.
    fp *= exposure

    # Reinhard operator.
    fp = fp / (1.0 + fp)

    return _ensure_uint8(fp)
