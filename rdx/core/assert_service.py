"""Assertion helpers with stable 0/1/2 exit semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class AssertOutcome:
    passed: bool
    reason: str
    details: Dict[str, Any]


class AssertService:
    @staticmethod
    def assert_pipeline_diff(
        payload: Dict[str, Any],
        *,
        max_changes: int = 0,
    ) -> AssertOutcome:
        if not bool(payload.get("ok")):
            err = payload.get("error") or {}
            return AssertOutcome(
                passed=False,
                reason=str(err.get("message") or "pipeline diff failed"),
                details={"error": err},
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        diff = data.get("diff", [])
        count = len(diff) if isinstance(diff, list) else 0
        passed = count <= int(max_changes)
        reason = f"pipeline changes={count}, threshold={int(max_changes)}"
        return AssertOutcome(passed=passed, reason=reason, details={"changes": count, "max_changes": int(max_changes)})

    @staticmethod
    def assert_image_metrics(
        payload: Dict[str, Any],
        *,
        mse_max: Optional[float] = None,
        max_abs_max: Optional[float] = None,
        psnr_min: Optional[float] = None,
    ) -> AssertOutcome:
        if not bool(payload.get("ok")):
            err = payload.get("error") or {}
            return AssertOutcome(
                passed=False,
                reason=str(err.get("message") or "image diff failed"),
                details={"error": err},
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        metrics = data.get("metrics", {}) if isinstance(data.get("metrics"), dict) else {}
        mse = float(metrics.get("mse", 0.0))
        max_abs = float(metrics.get("max_abs", 0.0))
        psnr = float(metrics.get("psnr", 0.0))

        checks = []
        if mse_max is not None:
            checks.append(("mse", mse <= float(mse_max), mse, float(mse_max), "<="))
        if max_abs_max is not None:
            checks.append(("max_abs", max_abs <= float(max_abs_max), max_abs, float(max_abs_max), "<="))
        if psnr_min is not None:
            checks.append(("psnr", psnr >= float(psnr_min), psnr, float(psnr_min), ">="))

        failed = [c for c in checks if not c[1]]
        if failed:
            reasons = [f"{name} {actual} not {op} {threshold}" for name, _, actual, threshold, op in failed]
            return AssertOutcome(
                passed=False,
                reason="; ".join(reasons),
                details={"metrics": metrics},
            )
        return AssertOutcome(
            passed=True,
            reason="image metrics within thresholds",
            details={"metrics": metrics},
        )

