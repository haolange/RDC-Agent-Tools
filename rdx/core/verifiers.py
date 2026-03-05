"""
RDX-MCP 的可插拔 verifier engine。

每个 verifier 接收 experiment 输入（context），并返回包含 metrics、
异常信息与 artifact 引用的结构化 verdict。

默认注册的 verifiers：
    naninf       —— 检测渲染输出中的 NaN / Inf 像素
    image_diff   —— 与参考图像对比渲染输出
    pixel_stats  —— 检查区域内的像素统计
    binding_diff —— 比较两个 event 的资源绑定
"""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from rdx.models import (
    AnomalyInfo,
    ArtifactRef,
    BBox,
    VerifierType,
)

if TYPE_CHECKING:
    pass  # 仅用于 forward references；避免模块级重依赖导入

logger = logging.getLogger("rdx.core.verifiers")


# ---------------------------------------------------------------------------
# Data-transfer objects（DTO）
# ---------------------------------------------------------------------------

@dataclass
class VerifyContext:
    """verifier 执行所需的全部输入。"""

    session_id: str
    capture_id: str
    event_id: int
    session_manager: Any          # rdx.core.session.SessionManager
    artifact_store: Any           # rdx.core.artifacts.ArtifactStore
    render_service: Any           # rdx.core.render.RenderService
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    """每个 verifier 返回的结构化 verdict。"""

    passed: bool
    metrics: Dict[str, Any] = field(default_factory=dict)
    anomaly: Optional[AnomalyInfo] = None
    artifacts: List[ArtifactRef] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Abstract base（抽象基类）
# ---------------------------------------------------------------------------

class BaseVerifier(abc.ABC):
    """每个 verifier 必须实现的接口。"""

    @abc.abstractmethod
    def name(self) -> str:
        """机器可读标识（如 ``"naninf"``）。"""
        ...

    @abc.abstractmethod
    async def verify(self, context: VerifyContext) -> VerifyResult:
        """执行验证并返回结果。"""
        ...


# ---------------------------------------------------------------------------
# NaN / Inf verifier
# ---------------------------------------------------------------------------

class NaNInfVerifier(BaseVerifier):
    """检测指定 event 的渲染输出中的 NaN 与 Inf。

    Workflow
    --------
    1.  将 replay 导航到 *event_id* 并渲染输出。
    2.  读回像素缓冲（float32 RGBA）。
    3.  使用 ``image_utils.compute_naninf_mask()`` 计算 NaN/Inf mask。
    4.  构建 metrics，并在异常时生成包含 bounding box 与 mask artifact 的
        ``AnomalyInfo``。
    """

    def name(self) -> str:
        return "naninf"

    async def verify(self, context: VerifyContext) -> VerifyResult:
        # 延迟导入 —— renderdoc / numpy 可能在模块加载时不在 sys.path 中。
        try:
            from rdx.utils import image_utils
        except ImportError:
            logger.error("image_utils not available; cannot run NaNInfVerifier")
            return VerifyResult(
                passed=False,
                notes="image_utils module unavailable",
            )

        render_svc = context.render_service
        artifact_store = context.artifact_store

        # 1. Render the output --------------------------------------------------
        try:
            render_result = await render_svc.render_event(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=context.event_id,
            )
            if render_result is None:
                return VerifyResult(
                    passed=False,
                    notes="render_event returned None",
                )
        except Exception as exc:
            logger.exception("Failed to render event %d", context.event_id)
            return VerifyResult(
                passed=False,
                notes=f"Render failed: {exc}",
            )

        # 2. Readback raw pixel data -------------------------------------------
        try:
            pixel_data = await render_svc.readback_texture(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=context.event_id,
            )
            if pixel_data is None:
                return VerifyResult(
                    passed=False,
                    notes="Texture readback returned None",
                )
            width = pixel_data.get("width", 0)
            height = pixel_data.get("height", 0)
            pixels = pixel_data.get("data")  # expected numpy float32 array
            if pixels is None or width == 0 or height == 0:
                return VerifyResult(
                    passed=False,
                    notes="Invalid pixel data from readback",
                )
        except Exception as exc:
            logger.exception("Texture readback failed for event %d",
                             context.event_id)
            return VerifyResult(
                passed=False,
                notes=f"Readback failed: {exc}",
            )

        # 3. Compute NaN/Inf mask -----------------------------------------------
        try:
            mask_result = image_utils.compute_naninf_mask(
                pixels, width, height,
            )
        except Exception as exc:
            logger.exception("compute_naninf_mask failed")
            return VerifyResult(
                passed=False,
                notes=f"NaN/Inf mask computation failed: {exc}",
            )

        nan_count: int = mask_result.get("nan_count", 0)
        inf_count: int = mask_result.get("inf_count", 0)
        total_pixels: int = width * height
        density = (nan_count + inf_count) / max(total_pixels, 1)
        mask_image = mask_result.get("mask")       # uint8 image or None
        bbox_raw = mask_result.get("bbox")         # (x0, y0, x1, y1) or None

        passed = nan_count == 0 and inf_count == 0

        metrics: Dict[str, Any] = {
            "nan_count": nan_count,
            "inf_count": inf_count,
            "total_pixels": total_pixels,
            "density": density,
        }

        # 4. Build anomaly & artifacts if something was detected ----------------
        anomaly: Optional[AnomalyInfo] = None
        artifacts: List[ArtifactRef] = []

        if not passed:
            bbox: Optional[BBox] = None
            if bbox_raw is not None:
                try:
                    bbox = BBox(
                        x0=int(bbox_raw[0]),
                        y0=int(bbox_raw[1]),
                        x1=int(bbox_raw[2]),
                        y1=int(bbox_raw[3]),
                    )
                except (IndexError, TypeError, ValueError):
                    logger.warning("Could not parse NaN/Inf bounding box")

            # Store mask image as artifact
            mask_artifact_ref: Optional[ArtifactRef] = None
            if mask_image is not None and artifact_store is not None:
                try:
                    mask_artifact_ref = await artifact_store.store_image(
                        image=mask_image,
                        name=f"naninf_mask_evt{context.event_id}",
                        session_id=context.session_id,
                        meta={
                            "verifier": "naninf",
                            "event_id": context.event_id,
                            "nan_count": nan_count,
                            "inf_count": inf_count,
                        },
                    )
                    artifacts.append(mask_artifact_ref)
                except Exception as exc:
                    logger.warning("Failed to store NaN/Inf mask artifact: %s",
                                   exc)

            anomaly = AnomalyInfo(
                type="naninf",
                bbox=bbox,
                nan_count=nan_count,
                inf_count=inf_count,
                total_pixels=total_pixels,
                density=density,
                mask_artifact=mask_artifact_ref,
            )

        return VerifyResult(
            passed=passed,
            metrics=metrics,
            anomaly=anomaly,
            artifacts=artifacts,
            notes="" if passed else (
                f"Detected {nan_count} NaN and {inf_count} Inf pixels "
                f"({density:.4%} of {total_pixels} total)"
            ),
        )


# ---------------------------------------------------------------------------
# Image-diff verifier
# ---------------------------------------------------------------------------

class ImageDiffVerifier(BaseVerifier):
    """将渲染输出与参考图像进行对比。

    Required *params* keys:
        reference_image_path (str)：参考图像文件路径。
    Optional:
        threshold (float)：均值差异阈值，低于该值则通过。
            默认 ``0.01``。
    """

    DEFAULT_THRESHOLD: float = 0.01

    def name(self) -> str:
        return "image_diff"

    async def verify(self, context: VerifyContext) -> VerifyResult:
        try:
            from rdx.utils import image_utils
        except ImportError:
            return VerifyResult(
                passed=False,
                notes="image_utils module unavailable",
            )

        reference_path: Optional[str] = context.params.get(
            "reference_image_path",
        )
        if not reference_path:
            return VerifyResult(
                passed=False,
                notes="reference_image_path not provided in params",
            )

        threshold: float = float(
            context.params.get("threshold", self.DEFAULT_THRESHOLD),
        )

        render_svc = context.render_service
        artifact_store = context.artifact_store

        # 1. 渲染当前输出 ----------------------------------------------
        try:
            pixel_data = await render_svc.readback_texture(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=context.event_id,
            )
            if pixel_data is None:
                return VerifyResult(
                    passed=False,
                    notes="Texture readback returned None",
                )
            current_image = pixel_data.get("data")
            width = pixel_data.get("width", 0)
            height = pixel_data.get("height", 0)
            if current_image is None or width == 0 or height == 0:
                return VerifyResult(
                    passed=False,
                    notes="Invalid pixel data from readback",
                )
        except Exception as exc:
            logger.exception("Render/readback failed for event %d",
                             context.event_id)
            return VerifyResult(
                passed=False,
                notes=f"Render/readback failed: {exc}",
            )

        # 2. 加载参考图像 -----------------------------------------------
        try:
            ref_image = image_utils.load_image(reference_path)
            if ref_image is None:
                return VerifyResult(
                    passed=False,
                    notes=f"Could not load reference image: {reference_path}",
                )
        except Exception as exc:
            logger.exception("Failed to load reference image %s",
                             reference_path)
            return VerifyResult(
                passed=False,
                notes=f"Failed to load reference image: {exc}",
            )

        # 3. 计算差异 ---------------------------------------------------
        try:
            diff_result = image_utils.compute_diff_map(
                current_image, ref_image, width, height,
            )
        except Exception as exc:
            logger.exception("compute_diff_map failed")
            return VerifyResult(
                passed=False,
                notes=f"Diff computation failed: {exc}",
            )

        mean_diff: float = diff_result.get("mean_diff", 1.0)
        max_diff: float = diff_result.get("max_diff", 1.0)
        diff_pixel_count: int = diff_result.get("diff_pixel_count", 0)
        total_pixels = width * height
        diff_ratio = diff_pixel_count / max(total_pixels, 1)
        heatmap = diff_result.get("heatmap")  # uint8 RGB image

        passed = mean_diff < threshold

        metrics: Dict[str, Any] = {
            "mean_diff": mean_diff,
            "max_diff": max_diff,
            "diff_pixel_count": diff_pixel_count,
            "diff_ratio": diff_ratio,
            "threshold": threshold,
        }

        artifacts: List[ArtifactRef] = []

        # 4. 存储差异 heatmap artifact ---------------------------------
        if heatmap is not None and artifact_store is not None:
            try:
                heatmap_ref = await artifact_store.store_image(
                    image=heatmap,
                    name=f"diff_heatmap_evt{context.event_id}",
                    session_id=context.session_id,
                    meta={
                        "verifier": "image_diff",
                        "event_id": context.event_id,
                        "mean_diff": mean_diff,
                        "max_diff": max_diff,
                        "threshold": threshold,
                    },
                )
                artifacts.append(heatmap_ref)
            except Exception as exc:
                logger.warning("Failed to store diff heatmap artifact: %s", exc)

        anomaly: Optional[AnomalyInfo] = None
        if not passed:
            anomaly = AnomalyInfo(
                type="image_diff",
                stats={
                    "mean_diff": mean_diff,
                    "max_diff": max_diff,
                    "diff_pixel_count": diff_pixel_count,
                    "diff_ratio": diff_ratio,
                },
            )

        return VerifyResult(
            passed=passed,
            metrics=metrics,
            anomaly=anomaly,
            artifacts=artifacts,
            notes="" if passed else (
                f"Image diff exceeds threshold: mean={mean_diff:.6f} "
                f"(threshold={threshold:.6f}), {diff_pixel_count} differing "
                f"pixels ({diff_ratio:.4%})"
            ),
        )


# ---------------------------------------------------------------------------
# Pixel-statistics verifier
# ---------------------------------------------------------------------------

class PixelStatsVerifier(BaseVerifier):
    """验证渲染输出区域内的像素统计。

    检查异常值（NaN、Inf、极端幅度），并验证像素值是否落在期望范围内。

    Optional *params* keys:
        region (dict)：``{x0, y0, x1, y1}`` bounding box。省略则使用全图。
        expected_range (list[float])：合法像素值范围 ``[min_val, max_val]``。
            默认 ``[0.0, 1.0]``。
        channels (list[int])：要检查的通道索引（0=R, 1=G, 2=B, 3=A）。
            默认 ``[0, 1, 2]``。
    """

    def name(self) -> str:
        return "pixel_stats"

    async def verify(self, context: VerifyContext) -> VerifyResult:
        import math

        render_svc = context.render_service

        # 解析 params --------------------------------------------------
        region = context.params.get("region")  # {x0, y0, x1, y1} or None
        expected_range = context.params.get("expected_range", [0.0, 1.0])
        channels: List[int] = context.params.get("channels", [0, 1, 2])

        range_min = float(expected_range[0])
        range_max = float(expected_range[1])

        # 1. 读回像素 ---------------------------------------------------
        try:
            pixel_data = await render_svc.readback_texture(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=context.event_id,
            )
            if pixel_data is None:
                return VerifyResult(
                    passed=False,
                    notes="Texture readback returned None",
                )
            pixels = pixel_data.get("data")
            width = pixel_data.get("width", 0)
            height = pixel_data.get("height", 0)
            if pixels is None or width == 0 or height == 0:
                return VerifyResult(
                    passed=False,
                    notes="Invalid pixel data from readback",
                )
        except Exception as exc:
            logger.exception("Readback failed for event %d", context.event_id)
            return VerifyResult(
                passed=False,
                notes=f"Readback failed: {exc}",
            )

        # 2. 确定区域边界 -----------------------------------------------
        x0 = 0
        y0 = 0
        x1 = width
        y1 = height
        if region is not None:
            x0 = max(0, int(region.get("x0", 0)))
            y0 = max(0, int(region.get("y0", 0)))
            x1 = min(width, int(region.get("x1", width)))
            y1 = min(height, int(region.get("y1", height)))

        # 3. 遍历区域像素并收集统计 ------------------------------------
        nan_count = 0
        inf_count = 0
        out_of_range_count = 0
        total_checked = 0
        channel_sums: Dict[int, float] = {ch: 0.0 for ch in channels}
        channel_mins: Dict[int, float] = {ch: float("inf") for ch in channels}
        channel_maxs: Dict[int, float] = {ch: float("-inf") for ch in channels}

        try:
            # pixels 可能是扁平数组或二维数组（每像素 RGBA）。
            # 这里兼容 numpy arrays 与普通 lists。
            for row in range(y0, y1):
                for col in range(x0, x1):
                    base_idx = (row * width + col) * 4
                    for ch in channels:
                        try:
                            val = float(pixels[base_idx + ch])
                        except (IndexError, TypeError):
                            continue

                        total_checked += 1

                        if math.isnan(val):
                            nan_count += 1
                            continue
                        if math.isinf(val):
                            inf_count += 1
                            continue

                        channel_sums[ch] += val
                        if val < channel_mins[ch]:
                            channel_mins[ch] = val
                        if val > channel_maxs[ch]:
                            channel_maxs[ch] = val

                        if val < range_min or val > range_max:
                            out_of_range_count += 1
        except Exception as exc:
            logger.exception("Error iterating pixel data")
            return VerifyResult(
                passed=False,
                notes=f"Pixel iteration error: {exc}",
            )

        total_region_pixels = (x1 - x0) * (y1 - y0)
        anomalous = nan_count + inf_count + out_of_range_count
        passed = anomalous == 0

        # 构建每通道均值 -----------------------------------------------
        channel_means: Dict[str, float] = {}
        valid_count = total_checked - nan_count - inf_count
        for ch in channels:
            if valid_count > 0:
                channel_means[f"ch{ch}_mean"] = channel_sums[ch] / valid_count
            else:
                channel_means[f"ch{ch}_mean"] = 0.0

        metrics: Dict[str, Any] = {
            "nan_count": nan_count,
            "inf_count": inf_count,
            "out_of_range_count": out_of_range_count,
            "total_checked": total_checked,
            "total_region_pixels": total_region_pixels,
            "anomalous_total": anomalous,
            **channel_means,
        }

        # 追加每通道 min/max（仅限有限值）
        for ch in channels:
            min_v = channel_mins[ch]
            max_v = channel_maxs[ch]
            metrics[f"ch{ch}_min"] = min_v if math.isfinite(min_v) else None
            metrics[f"ch{ch}_max"] = max_v if math.isfinite(max_v) else None

        anomaly: Optional[AnomalyInfo] = None
        if not passed:
            anomaly = AnomalyInfo(
                type="pixel_stats",
                bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1) if region else None,
                nan_count=nan_count,
                inf_count=inf_count,
                total_pixels=total_region_pixels,
                density=anomalous / max(total_region_pixels, 1),
                stats={
                    "out_of_range_count": out_of_range_count,
                    "expected_range": [range_min, range_max],
                },
            )

        return VerifyResult(
            passed=passed,
            metrics=metrics,
            anomaly=anomaly,
            artifacts=[],
            notes="" if passed else (
                f"Region ({x0},{y0})-({x1},{y1}): "
                f"{nan_count} NaN, {inf_count} Inf, "
                f"{out_of_range_count} out-of-range "
                f"[{range_min}, {range_max}]"
            ),
        )


# ---------------------------------------------------------------------------
# Binding-diff verifier
# ---------------------------------------------------------------------------

class BindingDiffVerifier(BaseVerifier):
    """比较两个 event（good vs. bad）之间的资源绑定差异。

    Required *params* keys:
        good_event_id (int)：渲染正确的 event。
        bad_event_id  (int)：渲染错误的 event。

    Optional:
        pipeline_service：显式 pipeline-service 引用；若省略，
            verifier 会尝试从 *context.params* 获取。
    """

    def name(self) -> str:
        return "binding_diff"

    async def verify(self, context: VerifyContext) -> VerifyResult:
        good_event_id: Optional[int] = context.params.get("good_event_id")
        bad_event_id: Optional[int] = context.params.get("bad_event_id")

        if good_event_id is None or bad_event_id is None:
            return VerifyResult(
                passed=False,
                notes="Both good_event_id and bad_event_id must be provided",
            )

        pipeline_service = context.params.get("pipeline_service")

        if pipeline_service is None:
            return VerifyResult(
                passed=False,
                notes="pipeline_service is required for binding_diff verifier",
            )

        # 1. 获取两个 event 的 snapshot -------------------------------
        try:
            good_snap = await pipeline_service.snapshot(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=int(good_event_id),
            )
            bad_snap = await pipeline_service.snapshot(
                session_id=context.session_id,
                capture_id=context.capture_id,
                event_id=int(bad_event_id),
            )
        except Exception as exc:
            logger.exception("Pipeline snapshot failed")
            return VerifyResult(
                passed=False,
                notes=f"Pipeline snapshot failed: {exc}",
            )

        if good_snap is None or bad_snap is None:
            return VerifyResult(
                passed=False,
                notes="One or both pipeline snapshots returned None",
            )

        # 2. 比较 bindings --------------------------------------------
        good_bindings = {
            (b.set_or_space, b.binding, b.type): b
            for b in getattr(good_snap, "bindings", [])
        }
        bad_bindings = {
            (b.set_or_space, b.binding, b.type): b
            for b in getattr(bad_snap, "bindings", [])
        }

        all_keys = set(good_bindings.keys()) | set(bad_bindings.keys())

        added: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        changed: List[Dict[str, Any]] = []

        for key in sorted(all_keys):
            good_b = good_bindings.get(key)
            bad_b = bad_bindings.get(key)

            if good_b is None and bad_b is not None:
                added.append({
                    "set_or_space": key[0],
                    "binding": key[1],
                    "type": key[2],
                    "resource_id": bad_b.resource_id,
                    "resource_name": bad_b.resource_name,
                })
            elif good_b is not None and bad_b is None:
                removed.append({
                    "set_or_space": key[0],
                    "binding": key[1],
                    "type": key[2],
                    "resource_id": good_b.resource_id,
                    "resource_name": good_b.resource_name,
                })
            elif good_b is not None and bad_b is not None:
                if (good_b.resource_id != bad_b.resource_id
                        or good_b.format != bad_b.format):
                    changed.append({
                        "set_or_space": key[0],
                        "binding": key[1],
                        "type": key[2],
                        "good_resource_id": good_b.resource_id,
                        "bad_resource_id": bad_b.resource_id,
                        "good_format": good_b.format,
                        "bad_format": bad_b.format,
                    })

        total_diffs = len(added) + len(removed) + len(changed)
        passed = total_diffs == 0

        metrics: Dict[str, Any] = {
            "good_event_id": good_event_id,
            "bad_event_id": bad_event_id,
            "bindings_added": len(added),
            "bindings_removed": len(removed),
            "bindings_changed": len(changed),
            "total_diffs": total_diffs,
        }

        # Store the detailed diff as an artifact --------------------------------
        artifacts: List[ArtifactRef] = []
        artifact_store = context.artifact_store
        if total_diffs > 0 and artifact_store is not None:
            diff_payload = {
                "good_event_id": good_event_id,
                "bad_event_id": bad_event_id,
                "added": added,
                "removed": removed,
                "changed": changed,
            }
            try:
                diff_ref = await artifact_store.store_json(
                    data=diff_payload,
                    name=(
                        f"binding_diff_evt{good_event_id}_"
                        f"vs_evt{bad_event_id}"
                    ),
                    session_id=context.session_id,
                    meta={"verifier": "binding_diff"},
                )
                artifacts.append(diff_ref)
            except Exception as exc:
                logger.warning("Failed to store binding diff artifact: %s", exc)

        anomaly: Optional[AnomalyInfo] = None
        if not passed:
            anomaly = AnomalyInfo(
                type="binding_diff",
                stats={
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                },
            )

        return VerifyResult(
            passed=passed,
            metrics=metrics,
            anomaly=anomaly,
            artifacts=artifacts,
            notes="" if passed else (
                f"Binding differences between event {good_event_id} "
                f"(good) and {bad_event_id} (bad): "
                f"{len(added)} added, {len(removed)} removed, "
                f"{len(changed)} changed"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier engine（registry + dispatcher）
# ---------------------------------------------------------------------------

class VerifierEngine:
    """verifier 注册表与便捷分发器。

    构造时预注册四个内置 verifier。

    Usage::

        engine = VerifierEngine()
        result = await engine.run_verifier("naninf", context)
    """

    DEFAULT_VERIFIER = "naninf"

    def __init__(self) -> None:
        self._registry: Dict[str, BaseVerifier] = {}
        self._register_builtins()

    # -- public API ----------------------------------------------------------

    def register(self, name: str, verifier: BaseVerifier) -> None:
        """以 *name* 注册 verifier（会覆盖已有条目）。"""
        if not isinstance(verifier, BaseVerifier):
            raise TypeError(
                f"Expected BaseVerifier instance, got {type(verifier).__name__}"
            )
        self._registry[name] = verifier
        logger.debug("Registered verifier %r", name)

    def get(self, name: str) -> BaseVerifier:
        """按名称获取已注册的 verifier。

        若不存在则抛出 ``KeyError``。
        """
        try:
            return self._registry[name]
        except KeyError:
            available = ", ".join(sorted(self._registry)) or "(none)"
            raise KeyError(
                f"No verifier registered under {name!r}.  "
                f"Available: {available}"
            ) from None

    async def run_verifier(
        self,
        name: str,
        context: VerifyContext,
    ) -> VerifyResult:
        """查找 *name* 对应的 verifier，并用 *context* 执行。

        捕获意外异常，确保调用方始终获得 ``VerifyResult``（失败时 ``passed=False``）。
        """
        try:
            verifier = self.get(name)
        except KeyError as exc:
            return VerifyResult(
                passed=False,
                notes=str(exc),
            )

        t0 = time.monotonic()
        try:
            result = await verifier.verify(context)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception(
                "Verifier %r raised after %.2fs", name, elapsed,
            )
            return VerifyResult(
                passed=False,
                notes=f"Verifier {name!r} raised: {exc}",
            )

        elapsed = time.monotonic() - t0
        logger.info(
            "Verifier %r completed in %.2fs -- passed=%s",
            name,
            elapsed,
            result.passed,
        )
        return result

    # -- internals -----------------------------------------------------------

    def _register_builtins(self) -> None:
        self.register("naninf", NaNInfVerifier())
        self.register("image_diff", ImageDiffVerifier())
        self.register("pixel_stats", PixelStatsVerifier())
        self.register("binding_diff", BindingDiffVerifier())
