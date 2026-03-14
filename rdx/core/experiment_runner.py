"""
RDX-MCP 的原子化 experiment runner。

负责编排 GPU-debug experiment 的完整生命周期：

1. 将 replay 导航到指定 draw-call event。
2. 在 *baseline* 状态（未打补丁前）运行 verifier。
3. 可选：应用 shader patch 并重新验证。
4. 比较 before / after metrics 产生 verdict。
5. 回滚 patch，确保后续实验从干净状态开始。

模块还提供 *bisect* 能力：对 event 范围进行二分搜索，定位
verifier 首次失败的 draw call；以及 *batch* runner，用于顺序
执行多个 experiment 定义。
"""

from __future__ import annotations

import logging
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from rdx.config import ConfidenceWeightsConfig, RdxConfig
from rdx.models import (
    ArtifactRef,
    BisectResult,
    BisectRange,
    ConfidenceBreakdown,
    ConfidenceWeights,
    ExperimentDef,
    ExperimentEvidence,
    ExperimentResult,
    ExperimentStatus,
    PatchSpec,
    VerdictResult,
    VerifierConfig,
    VerifierType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers（内部辅助）
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    """生成带指定 *prefix* 的短唯一标识符。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _ts() -> float:
    """当前墙钟时间（POSIX timestamp）。"""
    return time.time()


# 从 verifier type 映射到最能代表结果“badness”的 metric key。
# 对这些主指标而言，数值越低越 *好*。
_PRIMARY_METRIC_KEY: Dict[str, str] = {
    VerifierType.NANINF:          "nan_inf_count",
    VerifierType.IMAGE_DIFF:      "diff_score",
    VerifierType.PIXEL_STATS:     "anomaly_score",
    VerifierType.BINDING_DIFF:    "mismatch_count",
    VerifierType.COUNTER_ANOMALY: "anomaly_score",
    VerifierType.CUSTOM:          "score",
}

# 判定为 ``IMPROVED`` 的最小相对改进比例（fraction）。
_IMPROVEMENT_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# ExperimentRunner
# ---------------------------------------------------------------------------

class ExperimentRunner:
    """执行自包含、可审计的 GPU-debug experiments。

    每个 experiment 都会采集 baseline metrics，可选应用 patch，
    重新测量并给出包含完整 evidence 的 verdict。experiment 是 *atomic*：
    无论成功或失败，方法返回前都会回滚 patch。

    Parameters
    ----------
    session_manager:
        提供 ``get_controller(session_id)`` 以获得指定 session 的
        replay controller。
    render_service:
        提供 ``async capture_render(session_id, event_id)``，返回
        该 event 渲染输出的 :class:`ArtifactRef`。
    verifier_engine:
        提供 ``async verify(session_id, event_id, config)``，返回
        包含 ``"passed"`` 布尔字段的 metric ``dict``。
    patch_engine:
        :class:`~rdx.core.patch_engine.PatchEngine` 实例。
    artifact_store:
        提供 ``async store(data, metadata)``，返回 :class:`ArtifactRef`。
    """

    def __init__(
        self,
        session_manager: Any,
        render_service: Any,
        verifier_engine: Any,
        patch_engine: Any,
        artifact_store: Any,
        config: Optional[RdxConfig] = None,
    ) -> None:
        self._session_manager = session_manager
        self._render_service = render_service
        self._verifier_engine = verifier_engine
        self._patch_engine = patch_engine
        self._artifact_store = artifact_store
        self._config = config or RdxConfig()

        # 以 patch_id 为键的 PatchSpec 注册表。调用方必须在
        # ExperimentDef 引用之前注册 spec。
        self._patch_specs: Dict[str, PatchSpec] = {}

    # ------------------------------------------------------------------
    # Patch-spec registry（PatchSpec 注册表）
    # ------------------------------------------------------------------

    def register_patch_spec(self, spec: PatchSpec) -> None:
        """注册 :class:`PatchSpec`，供 experiment 引用。"""
        self._patch_specs[spec.patch_id] = spec

    def get_patch_spec(self, patch_id: str) -> Optional[PatchSpec]:
        """查询已注册的 :class:`PatchSpec`。"""
        return self._patch_specs.get(patch_id)

    # ------------------------------------------------------------------
    # Single experiment（单实验）
    # ------------------------------------------------------------------

    async def run_experiment(
        self,
        experiment_def: ExperimentDef,
    ) -> ExperimentResult:
        """执行由 *experiment_def* 定义的单个 experiment。

        Workflow
        -------
        1. 将 replay 导航到目标 event。
        2. 运行配置的 verifier，采集 **baseline** metrics，并可选捕获渲染 artifact。
        3. 若定义中设置了 ``patch_id``：
           a. 从注册表解析 :class:`PatchSpec`。
           b. 通过 patch engine 应用 patch。
           c. 在同一 event 强制重新渲染。
           d. 再次运行 verifier 采集 **after** metrics。
        4. 用两组 metrics 与渲染 artifacts 构建 :class:`ExperimentEvidence`。
        5. 比较 before / after metrics 决定 verdict。
        6. 回滚 patch（如有），使 session 回到原始状态。
        7. 返回 :class:`ExperimentResult`。
        """
        t0 = _ts()
        session_id = experiment_def.session_id
        event_id = experiment_def.event_id
        patch_applied = False
        patch_id_used: Optional[str] = None

        try:
            controller = self._session_manager.get_controller(session_id)

            # 1 -- 导航到 event
            controller.SetFrameEvent(event_id, True)

            # 2 -- baseline 验证
            before_artifact = await self._safe_capture(session_id, event_id)
            before_metrics = await self._safe_verify(
                session_id, event_id, experiment_def.verifier,
            )

            after_artifact: Optional[ArtifactRef] = None
            after_metrics: Dict[str, Any] = {}

            # 3 -- 可选 patch 应用
            if experiment_def.patch_id is not None:
                spec = self._resolve_patch_spec(experiment_def.patch_id)
                if spec is None:
                    return self._make_error_result(
                        experiment_def,
                        t0,
                        f"PatchSpec '{experiment_def.patch_id}' not found "
                        f"in registry or active patches",
                    )

                patch_result = await self._patch_engine.apply_patch(
                    session_id,
                    spec.target_event_id or event_id,
                    spec.target_stage,
                    self._session_manager,
                    spec,
                )
                if not patch_result.success:
                    return self._make_error_result(
                        experiment_def,
                        t0,
                        f"Patch application failed: {patch_result.error_message}",
                    )
                patch_applied = True
                patch_id_used = spec.patch_id

                # 3c -- 使用已打补丁的 shader 强制重新渲染
                controller.SetFrameEvent(event_id, True)

                # 3d -- patch 后验证
                after_artifact = await self._safe_capture(
                    session_id, event_id,
                )
                after_metrics = await self._safe_verify(
                    session_id, event_id, experiment_def.verifier,
                )

            # 4 -- 构建 evidence
            verdict = self._determine_verdict(
                before_metrics,
                after_metrics,
                experiment_def.verifier.type,
            )
            evidence = ExperimentEvidence(
                experiment_id=experiment_def.experiment_id,
                before_artifact=before_artifact,
                after_artifact=after_artifact,
                before_metrics=before_metrics,
                after_metrics=after_metrics,
                verifier_passed=after_metrics.get("passed", False)
                    if after_metrics else before_metrics.get("passed", False),
                verdict=verdict,
                notes=self._build_evidence_notes(
                    before_metrics, after_metrics, verdict,
                ),
            )

            # 5 -- 结果
            status = ExperimentStatus.COMPLETED

        except Exception as exc:
            logger.exception(
                "Experiment %s failed", experiment_def.experiment_id,
            )
            evidence = ExperimentEvidence(
                experiment_id=experiment_def.experiment_id,
                verdict=VerdictResult.ERROR,
                notes=str(exc),
            )
            status = ExperimentStatus.FAILED

        finally:
            # 6 -- 始终回滚 patch
            if patch_applied and patch_id_used is not None:
                try:
                    await self._patch_engine.revert_patch(
                        session_id, patch_id_used, self._session_manager,
                    )
                except Exception:
                    logger.exception(
                        "Failed to revert patch %s after experiment %s",
                        patch_id_used, experiment_def.experiment_id,
                    )

        return ExperimentResult(
            experiment_id=experiment_def.experiment_id,
            status=status,
            evidence=evidence,
            duration_seconds=_ts() - t0,
        )

    # ------------------------------------------------------------------
    # Bisect（二分定位）
    # ------------------------------------------------------------------

    async def run_bisect(
        self,
        session_id: str,
        capture_id: str,
        range_lo: int,
        range_hi: int,
        verifier_config: VerifierConfig,
        strategy: str = "",
        max_iters: int = 0,
        confidence_threshold: float = 0.0,
        confidence_weights: Optional[Dict[str, float]] = None,
        confidence_profile: str = "",
    ) -> BisectResult:
        """对 event 范围进行二分搜索，找到第一个 "bad" event。

        Parameters
        ----------
        session_id:
            活跃 replay session identifier。
        capture_id:
            当前 replay 的 capture（仅用于日志）。
        range_lo, range_hi:
            要搜索的 event-ID 闭区间。假设 ``range_lo`` 为 *good*
            （verifier 通过），``range_hi`` 为 *bad*（verifier 失败）。
        verifier_config:
            每个探测点传给 verifier 的配置。
        strategy:
            ``"binary"`` 表示经典二分搜索。
            ``"ddmin"`` 表示二分后追加边界验证，以提高置信度。
        max_iters:
            verifier 调用次数的硬上限。
        confidence_threshold:
            目标置信度；当达到阈值且边界相邻时可提前停止搜索。

        Returns
        -------
        BisectResult
            标识第一个 bad event、最后一个已知 good event、evidence chain
            （每次探测的 experiment IDs）、计算得到的 confidence 以及迭代次数。
        """
        if range_hi <= range_lo:
            raise ValueError(
                f"Invalid bisect range: lo={range_lo} hi={range_hi}; "
                f"hi must be greater than lo"
            )

        runtime_strategy = str(strategy or self._config.bisect.default_strategy or "binary")
        runtime_max_iters = int(max_iters or self._config.bisect.max_iterations or 60)
        runtime_threshold = float(
            confidence_threshold or self._config.bisect.default_confidence_threshold or 0.85
        )
        weights = self._resolve_confidence_weights(confidence_weights, self._config)
        profile = str(confidence_profile or self._config.bisect.confidence_profile or "default")

        total_range = range_hi - range_lo
        lo = range_lo
        hi = range_hi
        iterations = 0
        evidence_chain: List[str] = []
        boundary_consistent_count = 0
        last_good = lo
        last_bad = hi

        # -- Phase 1: binary search（经典二分）------------------------------
        while lo + 1 < hi and iterations < runtime_max_iters:
            mid = (lo + hi) // 2

            exp_id = _new_id("bexp")
            metrics = await self._safe_verify(
                session_id, mid, verifier_config,
            )
            evidence_chain.append(exp_id)
            iterations += 1

            is_bad = not metrics.get("passed", True)

            if is_bad:
                last_bad = mid
                hi = mid
            else:
                last_good = mid
                lo = mid

            # 跟踪相邻边界的一致性。
            if abs(last_bad - last_good) <= 1:
                boundary_consistent_count += 1

            # 当置信度足够时提前退出。
            confidence = self._calculate_confidence(
                last_good, last_bad, total_range,
                boundary_consistent_count,
                weights=weights,
            )
            if confidence >= runtime_threshold and hi - lo <= 1:
                break

        # -- Phase 2 (ddmin): 边界强化 -------------------------------------
        if runtime_strategy == "ddmin":
            # 重新验证边界 events，并探测相邻点以提高置信度。
            verification_points: List[tuple] = [
                (last_good, True),   # expect good
                (last_bad,  False),  # expect bad
            ]
            if last_good - 1 >= range_lo:
                verification_points.append((last_good - 1, True))
            if last_bad + 1 <= range_hi:
                verification_points.append((last_bad + 1, False))

            for point, expect_good in verification_points:
                if iterations >= runtime_max_iters:
                    break
                exp_id = _new_id("bexp")
                metrics = await self._safe_verify(
                    session_id, point, verifier_config,
                )
                evidence_chain.append(exp_id)
                iterations += 1

                is_good = metrics.get("passed", True)
                if is_good == expect_good:
                    boundary_consistent_count += 1
                else:
                    logger.warning(
                        "Bisect boundary probe at event %d returned "
                        "unexpected result (expected good=%s, got good=%s)",
                        point, expect_good, is_good,
                    )

        # -- 最终 confidence 计算 ------------------------------------------
        breakdown = self._calculate_confidence_breakdown(
            last_good,
            last_bad,
            total_range,
            boundary_consistent_count,
            weights=weights,
        )
        confidence = breakdown.weighted_total
        self._record_bisect_history(
            capture_id=capture_id,
            strategy=runtime_strategy,
            confidence_profile=profile,
            weights=weights,
            total_range=total_range,
            boundary_consistent_count=boundary_consistent_count,
            iterations=iterations,
            confidence=confidence,
            verifier_config=verifier_config,
        )

        logger.info(
            "Bisect complete for capture %s: first_bad=%d, last_good=%d, "
            "confidence=%.3f, iterations=%d, strategy=%s",
            capture_id, last_bad, last_good, confidence, iterations, runtime_strategy,
        )

        return BisectResult(
            first_bad_event_id=last_bad,
            first_good_event_id=last_good,
            evidence_chain=evidence_chain,
            confidence=confidence,
            iterations=iterations,
            confidence_breakdown=breakdown,
            confidence_weights=ConfidenceWeights(
                sharpness=weights.sharpness,
                consistency=weights.consistency,
                range_factor=weights.range_factor,
            ),
            confidence_profile=profile,
            boundary_consistent_count=boundary_consistent_count,
        )

    # ------------------------------------------------------------------
    # Batch execution（批量执行）
    # ------------------------------------------------------------------

    async def batch_experiments(
        self,
        experiments: List[ExperimentDef],
    ) -> List[ExperimentResult]:
        """顺序执行多个 experiment，并在每个之间保持干净状态。

        每次 experiment 结束后会回滚该 session 的所有 patch，作为
        保障措施，确保下一个 experiment 处于干净 baseline。
        """
        results: List[ExperimentResult] = []

        for exp_def in experiments:
            result = await self.run_experiment(exp_def)
            results.append(result)

            # 双保险：回滚任何残留 patch，确保下一个 experiment
            # 从未修改状态开始。
            try:
                await self._patch_engine.revert_all(
                    exp_def.session_id, self._session_manager,
                )
            except Exception:
                logger.exception(
                    "Failed to revert patches between batch experiments "
                    "(after experiment %s)",
                    exp_def.experiment_id,
                )

        return results

    # ------------------------------------------------------------------
    # Private helpers（私有辅助）
    # ------------------------------------------------------------------

    def _resolve_patch_spec(self, patch_id: str) -> Optional[PatchSpec]:
        """通过 *patch_id* 查找 :class:`PatchSpec`。

        先查 runner 自身注册表，再回退到 patch engine 已追踪的 spec
        （适用于在 experiment 之外应用的 patch）。
        """
        spec = self._patch_specs.get(patch_id)
        if spec is not None:
            return spec

        # 回退路径：patch engine 会存储已应用 patch 的 spec，
        # 当调用方直接应用 patch 时很有用。
        for active_spec in self._patch_engine.list_patches():
            if active_spec.patch_id == patch_id:
                return active_spec

        return None

    async def _safe_verify(
        self,
        session_id: str,
        event_id: int,
        config: VerifierConfig,
    ) -> Dict[str, Any]:
        """运行 verifier，失败时返回近似空的 dict。"""
        try:
            controller = self._session_manager.get_controller(session_id)
            controller.SetFrameEvent(event_id, True)
            return await self._verifier_engine.verify(
                session_id, event_id, config,
            )
        except Exception:
            logger.exception(
                "Verifier failed at event %d (session %s)",
                event_id, session_id,
            )
            return {"passed": False, "error": True}

    async def _safe_capture(
        self,
        session_id: str,
        event_id: int,
    ) -> Optional[ArtifactRef]:
        """捕获渲染帧，失败时返回 ``None``。"""
        try:
            return await self._render_service.capture_render(
                session_id, event_id,
            )
        except Exception:
            logger.exception(
                "Render capture failed at event %d (session %s)",
                event_id, session_id,
            )
            return None

    @staticmethod
    def _make_error_result(
        exp_def: ExperimentDef,
        t0: float,
        message: str,
    ) -> ExperimentResult:
        """构造错误场景下的 :class:`ExperimentResult`。"""
        return ExperimentResult(
            experiment_id=exp_def.experiment_id,
            status=ExperimentStatus.FAILED,
            evidence=ExperimentEvidence(
                experiment_id=exp_def.experiment_id,
                verdict=VerdictResult.ERROR,
                notes=message,
            ),
            duration_seconds=_ts() - t0,
        )

    @staticmethod
    def _build_evidence_notes(
        before: Dict[str, Any],
        after: Dict[str, Any],
        verdict: VerdictResult,
    ) -> str:
        """构建 metric 对比的可读摘要。"""
        parts: List[str] = [f"Verdict: {verdict.value}"]

        before_passed = before.get("passed", False)
        parts.append(f"Baseline passed: {before_passed}")

        if after:
            after_passed = after.get("passed", False)
            parts.append(f"Post-patch passed: {after_passed}")

            # 汇总发生变化的数值型 metrics。
            for key in sorted(set(before) | set(after)):
                if key in ("passed", "error"):
                    continue
                bv = before.get(key)
                av = after.get(key)
                if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
                    if bv != av:
                        parts.append(f"  {key}: {bv} -> {av}")

        return "; ".join(parts)

    # ------------------------------------------------------------------
    # Confidence calculation（置信度计算）
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_confidence_weights(
        override: Optional[Dict[str, float]],
        config: Optional[RdxConfig] = None,
    ) -> ConfidenceWeightsConfig:
        cfg = config or RdxConfig()
        source = override if isinstance(override, dict) else {}
        sharpness = float(source.get("sharpness", cfg.confidence_weights.sharpness))
        consistency = float(source.get("consistency", cfg.confidence_weights.consistency))
        range_factor = float(source.get("range_factor", cfg.confidence_weights.range_factor))
        total = sharpness + consistency + range_factor
        if sharpness < 0 or consistency < 0 or range_factor < 0 or total <= 0:
            raise ValueError("confidence weights must be positive and sum to a non-zero value")
        return ConfidenceWeightsConfig(
            sharpness=sharpness / total,
            consistency=consistency / total,
            range_factor=range_factor / total,
        )

    @classmethod
    def _calculate_confidence_breakdown(
        good_id: int,
        bad_id: int,
        total_range: int,
        boundary_consistent_count: int,
        *,
        weights: ConfidenceWeightsConfig,
    ) -> ConfidenceBreakdown:
        """计算 bisect 边界的 ``[0, 1]`` 置信度分数。

        置信度更高的条件：

        * good / bad 边界更 *sharp*（event ID 相邻）。
        * 多次一致验证确认边界。
        * 搜索范围更小（异常机会更少）。
        """
        if total_range <= 0:
            return ConfidenceBreakdown()

        boundary_gap = abs(bad_id - good_id)

        # Sharpness：边界相邻时为 1.0，间隔增大则以双曲方式衰减。
        sharpness = 1.0 / (1.0 + max(boundary_gap - 1, 0))

        # Consistency：在 3 次一致探测后饱和到 1.0。
        consistency = min(boundary_consistent_count / 3.0, 1.0)

        # Range factor：范围越小越可信。
        range_factor = min(50.0 / max(total_range, 1), 1.0)

        confidence = (
            weights.sharpness * sharpness
            + weights.consistency * consistency
            + weights.range_factor * range_factor
        )
        return ConfidenceBreakdown(
            sharpness=sharpness,
            consistency=consistency,
            range_factor=range_factor,
            weighted_total=max(0.0, min(confidence, 1.0)),
        )

    @classmethod
    def _calculate_confidence(
        cls,
        good_id: int,
        bad_id: int,
        total_range: int,
        boundary_consistent_count: int,
        *,
        weights: ConfidenceWeightsConfig,
    ) -> float:
        return cls._calculate_confidence_breakdown(
            good_id,
            bad_id,
            total_range,
            boundary_consistent_count,
            weights=weights,
        ).weighted_total

    def _record_bisect_history(
        self,
        *,
        capture_id: str,
        strategy: str,
        confidence_profile: str,
        weights: ConfidenceWeightsConfig,
        total_range: int,
        boundary_consistent_count: int,
        iterations: int,
        confidence: float,
        verifier_config: VerifierConfig,
    ) -> None:
        if str(self._config.adaptive_bisect.mode or "off").strip().lower() == "off":
            return
        history_path = Path(self._config.adaptive_bisect.history_store_path)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "capture_id": str(capture_id),
            "strategy": str(strategy),
            "confidence_profile": str(confidence_profile),
            "weights": {
                "sharpness": weights.sharpness,
                "consistency": weights.consistency,
                "range_factor": weights.range_factor,
            },
            "total_range": int(total_range),
            "boundary_consistent_count": int(boundary_consistent_count),
            "iterations": int(iterations),
            "confidence": float(confidence),
            "verifier_type": str(verifier_config.type),
            "ts": _ts(),
        }
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Verdict determination
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_verdict(
        before_metrics: Dict[str, Any],
        after_metrics: Dict[str, Any],
        verifier_type: VerifierType,
    ) -> VerdictResult:
        """Compare before / after metrics and assign a verdict.

        Decision matrix
        ----------------
        +-----------------+--------------+-----------------------------------+
        | Baseline        | Post-patch   | Verdict                           |
        +=================+==============+===================================+
        | passing         | passing      | INCONCLUSIVE (patch unnecessary)  |
        | passing         | failing      | REJECTED (patch broke things)     |
        | failing         | passing      | FIXED                             |
        | failing         | failing      | IMPROVED / REJECTED / INCONCLUSIVE|
        +-----------------+--------------+-----------------------------------+

        When both baseline and post-patch are failing, the primary metric
        for the given *verifier_type* is compared.  An improvement of at
        least ``_IMPROVEMENT_THRESHOLD`` (10 %) qualifies as ``IMPROVED``;
        a regression of the same magnitude yields ``REJECTED``; anything
        in between is ``INCONCLUSIVE``.
        """
        if not after_metrics:
            # No patch was applied -- baseline-only run.
            if before_metrics.get("passed", False):
                return VerdictResult.INCONCLUSIVE
            return VerdictResult.INCONCLUSIVE

        before_passed = before_metrics.get("passed", False)
        after_passed = after_metrics.get("passed", False)

        # Simple boolean transitions.
        if before_passed and after_passed:
            return VerdictResult.INCONCLUSIVE
        if before_passed and not after_passed:
            return VerdictResult.REJECTED
        if not before_passed and after_passed:
            return VerdictResult.FIXED

        # Both failing -- compare the primary metric.
        metric_key = _PRIMARY_METRIC_KEY.get(verifier_type, "score")

        before_score = _extract_numeric(before_metrics, metric_key)
        after_score = _extract_numeric(after_metrics, metric_key)

        if before_score is not None and after_score is not None:
            if before_score == 0:
                # Avoid division by zero; no baseline signal to compare.
                return VerdictResult.INCONCLUSIVE

            # For all primary metrics, *lower* is better.
            relative_change = (before_score - after_score) / abs(before_score)

            if relative_change >= _IMPROVEMENT_THRESHOLD:
                return VerdictResult.IMPROVED
            if relative_change <= -_IMPROVEMENT_THRESHOLD:
                return VerdictResult.REJECTED

        return VerdictResult.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------

def _extract_numeric(
    metrics: Dict[str, Any],
    key: str,
) -> Optional[float]:
    """Safely extract a numeric value from a metrics dictionary.

    Returns ``None`` if *key* is absent or its value is not numeric.
    Supports combined keys like ``"nan_inf_count"`` by also checking
    component keys (``"nan_count"`` + ``"inf_count"``).
    """
    value = metrics.get(key)
    if isinstance(value, (int, float)):
        return float(value)

    # Special-case: ``nan_inf_count`` may be stored as separate fields.
    if key == "nan_inf_count":
        nan_val = metrics.get("nan_count")
        inf_val = metrics.get("inf_count")
        if isinstance(nan_val, (int, float)) and isinstance(inf_val, (int, float)):
            return float(nan_val) + float(inf_val)

    return None
