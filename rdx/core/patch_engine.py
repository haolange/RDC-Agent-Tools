"""
RDX-MCP 的 shader patch engine。

在 RenderDoc replay 环境中对 shader 执行源级别修改，管理替换资源，
并跟踪活动 patch 以便干净回滚。每个已应用的 patch 都会被记录，
以便随时恢复原始 shader。

该引擎操作的是从 replay controller 获取的反汇编/反编译 shader 文本。
支持三类 patch 操作：

* **force_full_precision** —— 提升低精度类型并添加 ``precise`` 关键字（HLSL），
  升级精度限定符（GLSL），或移除 ``RelaxedPrecision`` 装饰（SPIR-V assembly）。
* **insert_guard** —— 用 ``isnan`` / ``isinf`` guards 包裹表达式，
  使 NaN 或 Inf 替换为安全的 fallback。
* **replace_expr** —— 在 shader 源码中直接进行文本替换。

修改后会通过 replay controller（``BuildTargetShader``）重新编译，
并通过 ``ReplaceResource`` 进行热替换。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rdx.models import (
    PatchOp,
    PatchResult,
    PatchSpec,
    ShaderStage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy renderdoc import（延迟导入）
# ---------------------------------------------------------------------------

_rd_module: Any = None


def _get_rd() -> Any:
    """返回 ``renderdoc`` module，并在需要时延迟导入。

    该 module 仅在 RenderDoc host process 中可用，或当库路径已加入
    ``sys.path``。在模块加载阶段提前导入会影响仅做包探查的工具。
    """
    global _rd_module
    if _rd_module is None:
        import renderdoc as rd  # type: ignore[import-untyped]
        _rd_module = rd
    return _rd_module


# ---------------------------------------------------------------------------
# Internal helpers（内部辅助）
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    """生成带 *prefix* 的短唯一标识符。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# 将字符串枚举 ShaderStage 映射到 renderdoc.ShaderStage C++ enum 的整数值。
_STAGE_TO_RD_INDEX: Dict[ShaderStage, int] = {
    ShaderStage.VS: 0,   # Vertex
    ShaderStage.HS: 1,   # Hull / Tessellation Control
    ShaderStage.DS: 2,   # Domain / Tessellation Evaluation
    ShaderStage.GS: 3,   # Geometry
    ShaderStage.PS: 4,   # Pixel / Fragment
    ShaderStage.CS: 5,   # Compute
}


def _to_rd_stage(stage: ShaderStage) -> Any:
    """将 ``rdx.models.ShaderStage`` 转换为 ``renderdoc.ShaderStage``。

    若 RenderDoc enum 不覆盖该 stage（例如 mesh / amplification shaders），
    将抛出 ``ValueError``。
    """
    rd = _get_rd()
    idx = _STAGE_TO_RD_INDEX.get(stage)
    if idx is None:
        raise ValueError(
            f"Shader stage '{stage.value}' is not supported for patching. "
            f"Supported stages: {sorted(s.value for s in _STAGE_TO_RD_INDEX)}"
        )
    return rd.ShaderStage(idx)


def _shader_id_str(shader_id: Any) -> str:
    """将 ``renderdoc.ResourceId`` 或兼容对象稳定转换为字符串。"""
    return str(shader_id or "")


# ---------------------------------------------------------------------------
# PatchRecord
# ---------------------------------------------------------------------------

@dataclass
class PatchRecord:
    """单个已应用 shader patch 的内部记录。

    保存回滚 patch 与释放 replay controller 分配的替换资源所需信息。
    """

    patch_id: str
    session_id: str
    original_shader_id: Any       # renderdoc.ResourceId
    replacement_shader_id: Any    # renderdoc.ResourceId
    original_shader_hash: str     # SHA-256 of the original source text
    spec: PatchSpec
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# PatchEngine
# ---------------------------------------------------------------------------

class PatchEngine:
    """在 RenderDoc replay session 中应用、跟踪并回滚 shader patches。

    Usage::

        engine = PatchEngine()
        result = await engine.apply_patch(
            session_id, event_id, ShaderStage.PS, session_mgr, spec,
        )
        ...
        await engine.revert_patch(session_id, result.patch_id, session_mgr)
    """

    def __init__(self) -> None:
        # patch_id -> PatchRecord
        self._patches: Dict[str, PatchRecord] = {}

    # ------------------------------------------------------------------
    # Public async API（公开异步接口）
    # ------------------------------------------------------------------

    async def apply_patch(
        self,
        session_id: str,
        event_id: int,
        stage: ShaderStage,
        session_manager: Any,
        patch_spec: PatchSpec,
    ) -> PatchResult:
        """将 *patch_spec* 应用到 *event_id* 上 *stage* 绑定的 shader。

        Steps
        -----
        1. 将 replay 导航到 *event_id*。
        2. 读取 pipeline state 并获取绑定的 shader + reflection。
        3. 以最佳可编辑编码反汇编 shader。
        4. 逐个应用 *patch_spec* 中的 ``PatchOp`` 到源码文本。
        5. 通过 ``BuildTargetShader`` 编译修改后的源码。
        6. 使用 ``ReplaceResource`` 热替换 shader。
        7. 记录 patch 以便后续回滚。

        返回 :class:`PatchResult` 表示成功或失败。
        """
        try:
            controller = session_manager.get_controller(session_id)

            # 1 -- 导航到目标 event
            controller.SetFrameEvent(event_id, True)

            # 2 -- pipeline state 与 shader 标识
            pipe = controller.GetPipelineState()
            rd_stage = _to_rd_stage(stage)
            shader_id = pipe.GetShader(rd_stage)
            refl = pipe.GetShaderReflection(rd_stage)

            if shader_id is None or _shader_id_str(shader_id) in {"", "0", "ResourceId::0"}:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    success=False,
                    error_message=(
                        f"No shader bound at stage {stage.value} "
                        f"for event {event_id}"
                    ),
                    error_code="shader_binding_lookup_failed",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "failure_stage": "resolve_binding",
                        "failure_reason": "stage_unbound",
                        "expected_shader_id": str(patch_spec.target_shader_id or ""),
                        "bound_shader_id": _shader_id_str(shader_id),
                    },
                )

            if patch_spec.target_shader_id and _shader_id_str(shader_id) != str(patch_spec.target_shader_id):
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    success=False,
                    error_message=(
                        "Current event binding does not match the expected shader identity "
                        f"for stage {stage.value}"
                    ),
                    error_code="shader_binding_lookup_failed",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "failure_stage": "resolve_binding",
                        "failure_reason": "shader_id_mismatch",
                        "expected_shader_id": str(patch_spec.target_shader_id or ""),
                        "bound_shader_id": _shader_id_str(shader_id),
                    },
                )

            if refl is None:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    success=False,
                    error_message=(
                        f"No shader bound at stage {stage.value} "
                        f"for event {event_id}"
                    ),
                    error_code="shader_not_bound",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "failure_stage": "resolve_binding",
                        "failure_reason": "reflection_unavailable",
                        "expected_shader_id": str(patch_spec.target_shader_id or ""),
                        "bound_shader_id": _shader_id_str(shader_id),
                    },
                )

            # 3 -- 以最佳可编辑编码进行反汇编
            try:
                source, encoding, disasm_target, _ = self._resolve_source(
                    controller,
                    pipe,
                    refl,
                    stage,
                    session_id,
                    requested_target=str(patch_spec.source_target or ""),
                    requested_encoding=str(patch_spec.source_encoding or ""),
                )
            except RuntimeError as exc:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    success=False,
                    error_message=str(exc),
                    error_code="shader_disassembly_unavailable",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "requested_target": str(patch_spec.source_target or ""),
                        "requested_source_encoding": str(patch_spec.source_encoding or ""),
                        "failure_stage": "disassembly",
                        "failure_reason": "source_unavailable",
                        "expected_shader_id": str(patch_spec.target_shader_id or ""),
                        "bound_shader_id": _shader_id_str(shader_id),
                    },
                )
            rd = _get_rd()

            if not source:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    success=False,
                    error_message=(
                        f"Disassembly returned empty source for "
                        f"target '{disasm_target}'"
                    ),
                    error_code="shader_disassembly_unavailable",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "disassembly_target": str(disasm_target),
                        "failure_stage": "disassembly",
                        "failure_reason": "empty_source",
                        "expected_shader_id": str(patch_spec.target_shader_id or ""),
                        "bound_shader_id": _shader_id_str(shader_id),
                    },
                )

            original_hash = hashlib.sha256(
                source.encode("utf-8"),
            ).hexdigest()
            encoding_name = self._encoding_name(encoding)
            messages: List[str] = []
            if patch_spec.expected_source_hash and patch_spec.expected_source_hash != original_hash:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    original_shader_hash=original_hash,
                    success=False,
                    error_message="Current shader source hash does not match expected_source_hash",
                    error_code="shader_source_mismatch",
                    error_category="validation",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "shader_id": _shader_id_str(shader_id),
                        "expected_source_hash": str(patch_spec.expected_source_hash),
                        "actual_source_hash": original_hash,
                        "disassembly_target": str(disasm_target),
                        "failure_stage": "validate_source",
                        "failure_reason": "source_hash_mismatch",
                    },
                    source_before_text=source,
                    source_after_text=source,
                    disassembly_target=str(disasm_target),
                    encoding=encoding_name,
                    entry_point=str(getattr(refl, "entryPoint", "") or "main"),
                )

            # 4 -- 顺序应用每个 PatchOp
            modified = source
            if patch_spec.source_text:
                modified = patch_spec.source_text
                messages.append("Applied source_text replacement.")
            elif patch_spec.diff_text:
                try:
                    modified = self._apply_unified_diff(source, patch_spec.diff_text)
                except ValueError as exc:
                    return PatchResult(
                        patch_id=patch_spec.patch_id,
                        original_shader_hash=original_hash,
                        success=False,
                        error_message=f"Failed to apply diff_text: {exc}",
                        error_code="shader_patch_diff_failed",
                        error_category="validation",
                        error_details={
                            "event_id": int(event_id),
                            "stage": stage.value.upper(),
                            "session_id": str(session_id),
                            "shader_id": _shader_id_str(shader_id),
                            "disassembly_target": str(disasm_target),
                            "failure_stage": "apply_patch",
                            "failure_reason": "diff_apply_failed",
                        },
                        source_before_text=source,
                        source_after_text=source,
                        disassembly_target=str(disasm_target),
                        encoding=encoding_name,
                        entry_point=str(getattr(refl, "entryPoint", "") or "main"),
                    )
                messages.append("Applied diff_text replacement.")
            else:
                for op in patch_spec.ops:
                    if op.op == "force_full_precision" and ("spirv" in encoding_name or "spv" in encoding_name):
                        precision_matches = self._collect_spirv_precision_targets(
                            modified,
                            op.variables,
                        )
                        if precision_matches:
                            matched_lines = ", ".join(
                                str(line_no) for line_no, _ in precision_matches[:12]
                            )
                            if len(precision_matches) > 12:
                                matched_lines = f"{matched_lines}, ..."
                            messages.append(
                                f"force_full_precision matched {len(precision_matches)} RelaxedPrecision line(s)"
                                f" at {matched_lines}",
                            )
                        elif op.variables:
                            messages.append(
                                "force_full_precision matched no RelaxedPrecision lines for variables: "
                                + ", ".join(str(item) for item in op.variables),
                            )
                    modified = self._apply_op(modified, encoding_name, op)

            if modified == source:
                logger.warning(
                    "Patch %s: operations produced no changes to the shader "
                    "source (stage=%s, event=%d)",
                    patch_spec.patch_id, stage.value, event_id,
                )
                messages.append(
                    "Patch operations produced no source changes before recompilation.",
                )
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    applied_to_shader_hash=original_hash,
                    original_shader_hash=original_hash,
                    success=True,
                    messages=messages,
                    source_before_text=source,
                    source_after_text=modified,
                    disassembly_target=str(disasm_target),
                    encoding=encoding_name,
                    entry_point=str(getattr(refl, "entryPoint", "") or "main"),
                )

            # 5 -- 编译修改后的源码
            entry_point = refl.entryPoint if refl.entryPoint else "main"
            source_bytes = modified.encode("utf-8")
            compile_flags = self._build_compile_flags(refl)
            compile_flag_payload = self._compile_flag_payload(compile_flags)
            try:
                new_id, errors = controller.BuildTargetShader(
                    entry_point,
                    encoding,
                    source_bytes,
                    compile_flags,
                    rd_stage,
                )
            except Exception as exc:
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    original_shader_hash=original_hash,
                    success=False,
                    error_message=f"BuildTargetShader failed: {exc}",
                    error_code="shader_build_runtime_error",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "shader_id": _shader_id_str(shader_id),
                        "entry_point": str(entry_point),
                        "encoding": encoding_name,
                        "disassembly_target": str(disasm_target),
                        "compile_flags": compile_flag_payload,
                        "exception_type": type(exc).__name__,
                        "failure_stage": "build",
                        "failure_reason": "build_runtime_error",
                    },
                    messages=messages,
                )

            if errors:
                # 区分硬失败（null resource）与仅有警告
                # （资源已分配但 compiler 输出诊断信息）。
                null_id = rd.ResourceId()
                if new_id == null_id or new_id is None:
                    return PatchResult(
                        patch_id=patch_spec.patch_id,
                        original_shader_hash=original_hash,
                        success=False,
                        error_message=f"Shader build failed: {errors}",
                        error_code="shader_build_failed",
                        error_category="runtime",
                        error_details={
                            "event_id": int(event_id),
                            "stage": stage.value.upper(),
                            "session_id": str(session_id),
                            "shader_id": _shader_id_str(shader_id),
                            "entry_point": str(entry_point),
                            "encoding": encoding_name,
                            "disassembly_target": str(disasm_target),
                            "compile_flags": compile_flag_payload,
                            "compiler_output": str(errors),
                            "failure_stage": "build",
                            "failure_reason": "compiler_failed",
                        },
                        messages=messages,
                        source_before_text=source,
                        source_after_text=modified,
                        disassembly_target=str(disasm_target),
                        encoding=encoding_name,
                        entry_point=str(entry_point),
                        compile_flags=compile_flag_payload,
                    )
                # 非致命警告 —— 记录日志并继续。
                logger.warning(
                    "Shader build for patch %s produced warnings: %s",
                    patch_spec.patch_id, errors,
                )
                messages.append(str(errors))

            # 6 -- 热替换资源
            try:
                controller.ReplaceResource(shader_id, new_id)
            except Exception as exc:
                try:
                    controller.FreeTargetResource(new_id)
                except Exception:
                    logger.debug(
                        "Failed to free replacement resource after ReplaceResource failure",
                        exc_info=True,
                    )
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    original_shader_hash=original_hash,
                    success=False,
                    error_message=f"ReplaceResource failed: {exc}",
                    error_code="shader_replace_apply_failed",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "shader_id": _shader_id_str(shader_id),
                        "replacement_shader_id": _shader_id_str(new_id),
                        "entry_point": str(entry_point),
                        "encoding": encoding_name,
                        "compile_flags": compile_flag_payload,
                        "exception_type": type(exc).__name__,
                        "failure_stage": "apply_replacement",
                        "failure_reason": "replace_resource_failed",
                    },
                    messages=messages,
                    source_before_text=source,
                    source_after_text=modified,
                    disassembly_target=str(disasm_target),
                    encoding=encoding_name,
                    entry_point=str(entry_point),
                    compile_flags=compile_flag_payload,
                )

            try:
                controller.SetFrameEvent(event_id, True)
            except Exception as exc:
                try:
                    controller.RemoveReplacement(shader_id)
                except Exception:
                    logger.debug(
                        "Failed to remove replacement after rebind failure",
                        exc_info=True,
                    )
                try:
                    controller.FreeTargetResource(new_id)
                except Exception:
                    logger.debug(
                        "Failed to free replacement resource after rebind failure",
                        exc_info=True,
                    )
                return PatchResult(
                    patch_id=patch_spec.patch_id,
                    original_shader_hash=original_hash,
                    success=False,
                    error_message=f"Replacement applied but rebind failed: {exc}",
                    error_code="shader_replace_rebind_failed",
                    error_category="runtime",
                    error_details={
                        "event_id": int(event_id),
                        "stage": stage.value.upper(),
                        "session_id": str(session_id),
                        "shader_id": _shader_id_str(shader_id),
                        "replacement_shader_id": _shader_id_str(new_id),
                        "entry_point": str(entry_point),
                        "encoding": encoding_name,
                        "compile_flags": compile_flag_payload,
                        "exception_type": type(exc).__name__,
                        "failure_stage": "rebind_event",
                        "failure_reason": "set_frame_event_failed",
                    },
                    messages=messages,
                    source_before_text=source,
                    source_after_text=modified,
                    disassembly_target=str(disasm_target),
                    encoding=encoding_name,
                    entry_point=str(entry_point),
                    compile_flags=compile_flag_payload,
                )

            applied_hash = hashlib.sha256(
                modified.encode("utf-8"),
            ).hexdigest()

            # 7 -- 记录以便未来回滚
            record = PatchRecord(
                patch_id=patch_spec.patch_id,
                session_id=session_id,
                original_shader_id=shader_id,
                replacement_shader_id=new_id,
                original_shader_hash=original_hash,
                spec=patch_spec,
            )
            self._patches[patch_spec.patch_id] = record

            logger.info(
                "Applied patch %s to shader %s (stage=%s, event=%d, "
                "encoding=%s)",
                patch_spec.patch_id, shader_id, stage.value, event_id,
                encoding_name,
            )

            return PatchResult(
                patch_id=patch_spec.patch_id,
                applied_to_shader_hash=applied_hash,
                original_shader_hash=original_hash,
                success=True,
                messages=messages,
                source_before_text=source,
                source_after_text=modified,
                disassembly_target=str(disasm_target),
                encoding=encoding_name,
                entry_point=str(entry_point),
                compile_flags=compile_flag_payload,
            )

        except Exception as exc:
            logger.exception(
                "apply_patch failed for patch %s", patch_spec.patch_id,
            )
            return PatchResult(
                patch_id=patch_spec.patch_id,
                success=False,
                error_message=str(exc),
                error_code="shader_replace_runtime_error",
                error_category="runtime",
                error_details={
                    "event_id": int(event_id),
                    "stage": stage.value.upper(),
                    "patch_id": patch_spec.patch_id,
                    "exception_type": type(exc).__name__,
                    "failure_stage": "runtime",
                    "failure_reason": type(exc).__name__,
                },
                source_before_text=source if 'source' in locals() else "",
                source_after_text=modified if 'modified' in locals() else "",
                disassembly_target=str(disasm_target) if 'disasm_target' in locals() else "",
                encoding=encoding_name if 'encoding_name' in locals() else "",
                entry_point=str(entry_point) if 'entry_point' in locals() else "",
                compile_flags=compile_flag_payload if 'compile_flag_payload' in locals() else [],
            )

    async def revert_patch(
        self,
        session_id: str,
        patch_id: str,
        session_manager: Any,
    ) -> bool:
        """回滚先前应用的 patch。

        移除资源替换、释放已编译的替换资源，并删除内部记录。

        成功返回 ``True``；若 patch 不存在或回滚失败则返回 ``False``。
        """
        record = self._patches.get(patch_id)
        if record is None:
            logger.warning("revert_patch: patch %s not found", patch_id)
            return False

        if record.session_id != session_id:
            logger.warning(
                "revert_patch: patch %s belongs to session %s, not %s",
                patch_id, record.session_id, session_id,
            )
            return False

        try:
            controller = session_manager.get_controller(session_id)
            controller.RemoveReplacement(record.original_shader_id)
            target_event_id = int(getattr(record.spec, "target_event_id", 0) or 0)
            if target_event_id > 0:
                controller.SetFrameEvent(target_event_id, True)
            controller.FreeTargetResource(record.replacement_shader_id)
            del self._patches[patch_id]
            logger.info("Reverted patch %s", patch_id)
            return True
        except Exception:
            logger.exception("Failed to revert patch %s", patch_id)
            return False

    async def revert_all(
        self,
        session_id: str,
        session_manager: Any,
    ) -> int:
        """回滚 *session_id* 的所有活动 patch。

        返回成功回滚的 patch 数量。回滚失败的 patch 会记录日志，
        但不会阻止其它 patch 的回滚尝试。
        """
        target_ids = [
            pid for pid, rec in self._patches.items()
            if rec.session_id == session_id
        ]
        reverted = 0
        for pid in target_ids:
            if await self.revert_patch(session_id, pid, session_manager):
                reverted += 1
        if reverted:
            logger.info(
                "Reverted %d / %d patches for session %s",
                reverted, len(target_ids), session_id,
            )
        return reverted

    def list_patches(
        self,
        session_id: Optional[str] = None,
    ) -> List[PatchSpec]:
        """返回所有活动 patch 的 :class:`PatchSpec`。

        若提供 *session_id*，则仅返回该 session 的 patch。
        """
        return [
            rec.spec
            for rec in self._patches.values()
            if session_id is None or rec.session_id == session_id
        ]

    # ------------------------------------------------------------------
    # Patch-op dispatch（PatchOp 分派）
    # ------------------------------------------------------------------

    def _apply_op(
        self,
        source: str,
        encoding_name: str,
        op: PatchOp,
    ) -> str:
        """将单个 :class:`PatchOp` 分派到对应处理器。"""
        if op.op == "force_full_precision":
            return self._apply_precision_patch(
                source, encoding_name, op.variables,
            )
        if op.op == "insert_guard":
            return self._apply_guard_patch(
                source,
                encoding_name,
                op.guard_expr or "",
                op.guard_replacement or "0.0",
            )
        if op.op == "replace_expr":
            return self._apply_expr_replace(
                source,
                op.expr_from or "",
                op.expr_to or "",
            )
        logger.warning("Unknown patch op type '%s'; skipping", op.op)
        return source

    # ------------------------------------------------------------------
    # Precision patch（精度提升）
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_precision_patch(
        source: str,
        encoding: str,
        variables: List[str],
    ) -> str:
        """对列出的 *variables*（或全局）强制 full precision。

        **HLSL**
            * 将低精度类型（``min16float``, ``half``, ``min10float``,
              ``min16int``, ``min12int``, ``min16uint``）替换为全精度等价类型。
            * 在 *variables* 中每个变量声明前插入 ``precise`` 关键字。

        **GLSL**
            * 对列出的 *variables* 将 ``mediump``/``lowp`` 替换为 ``highp``，
              若未提供变量则全局替换。

        **SPIR-V assembly**
            * 移除列出的 *variables* 对应的
              ``OpDecorate %<var> RelaxedPrecision`` 行；若未提供变量，
              则移除所有此类装饰。
        """
        modified = source
        enc = encoding.lower()

        # ---- HLSL ---------------------------------------------------------
        if "hlsl" in enc:
            # 将低精度类型提升为全精度。
            _hlsl_type_map = {
                "min16float": "float",
                "min10float": "float",
                "min16int":   "int",
                "min12int":   "int",
                "min16uint":  "uint",
                "half":       "float",
            }
            for old_type, new_type in _hlsl_type_map.items():
                modified = modified.replace(old_type, new_type)

            # 为目标变量声明添加 ``precise``。
            for var in variables:
                # 匹配：<type> <var>（且前面未有 ``precise``）
                # <type> 是常见 HLSL 数值类型，可能带向量/矩阵维度后缀
                #（如 float4, float4x4）。
                pattern = re.compile(
                    r"(?<!\bprecise\s)"
                    r"(\b(?:float|double|int|uint|dword)"
                    r"(?:[1-4](?:x[1-4])?)?)"
                    rf"(\s+{re.escape(var)}\b)",
                )
                modified = pattern.sub(r"precise \1\2", modified)

        # ---- GLSL ---------------------------------------------------------
        elif "glsl" in enc:
            if variables:
                for var in variables:
                    # 替换声明 *var* 的那一行上的精度限定符。
                    pattern = re.compile(
                        rf"(\b(?:mediump|lowp)\b)"
                        rf"(\s+\w+\s+{re.escape(var)}\b)",
                    )
                    modified = pattern.sub(r"highp\2", modified)
            else:
                # 全局提升 —— 替换所有精度限定符。
                modified = re.sub(r"\bmediump\b", "highp", modified)
                modified = re.sub(r"\blowp\b", "highp", modified)

        # ---- SPIR-V assembly ----------------------------------------------
        elif "spirv" in enc or "spv" in enc:
            if variables:
                for var in variables:
                    pattern = re.compile(
                        rf"^\s*OpDecorate\s+%{re.escape(var)}"
                        r"\s+RelaxedPrecision\s*$",
                        re.MULTILINE,
                    )
                    modified = pattern.sub("", modified)
                    renderdoc_pattern = re.compile(
                        rf"^([^\n]*(?:%{re.escape(var)}\b|_{re.escape(var)}\b)[^\n]*?)"
                        r"\s*:\s*\[\[RelaxedPrecision\]\](\s*;)\s*$",
                        re.MULTILINE,
                    )
                    modified = renderdoc_pattern.sub(r"\1\2", modified)
            else:
                # 移除 *所有* RelaxedPrecision 装饰。
                modified = re.sub(
                    r"^\s*OpDecorate\s+%\w+\s+RelaxedPrecision\s*$",
                    "",
                    modified,
                    flags=re.MULTILINE,
                )
                modified = re.sub(
                    r"\s*:\s*\[\[RelaxedPrecision\]\](\s*;)",
                    r"\1",
                    modified,
                )

        return modified

    @staticmethod
    def _collect_spirv_precision_targets(
        source: str,
        variables: List[str],
    ) -> List[Tuple[int, str]]:
        matches: List[Tuple[int, str]] = []
        exact_token_patterns = [
            re.compile(rf"(?:%{re.escape(var)}\b|_{re.escape(var)}\b)")
            for var in variables
            if str(var or "").strip()
        ]
        for line_no, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or "RelaxedPrecision" not in stripped:
                continue
            if not exact_token_patterns:
                matches.append((line_no, stripped))
                continue
            if any(pattern.search(stripped) for pattern in exact_token_patterns):
                matches.append((line_no, stripped))
        return matches

    # ------------------------------------------------------------------
    # Guard patch（防护插入）
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_guard_patch(
        source: str,
        encoding: str,
        expr: str,
        guard: str,
    ) -> str:
        """对 *expr* 的每次出现加上 NaN / Inf guard。

        The guarded form replaces *expr* with::

            (isnan(expr) || isinf(expr)) ? guard : expr

        适用于 HLSL 与 GLSL 源码。对于 SPIR-V assembly，会输出注释 marker，
        因为指令级改写需要外部 SPIR-V tooling。

        Parameters
        ----------
        source:
            Shader 源码文本。
        encoding:
            小写 encoding 标识（``"hlsl"``, ``"glsl"`` 等）。
        expr:
            要加 guard 的表达式。
        guard:
            当 *expr* 为 NaN 或 Inf 时使用的替代值
            （如 ``"0.0"`` 或 ``"float3(0,0,0)"``）。
        """
        if not expr:
            return source

        enc = encoding.lower()
        escaped = re.escape(expr)
        # 负向环视避免在标识符内部被替换。
        token_pattern = rf"(?<![a-zA-Z0-9_.]){escaped}(?![a-zA-Z0-9_.])"

        if "hlsl" in enc or "glsl" in enc:
            replacement = (
                f"(isnan({expr}) || isinf({expr}) ? {guard} : {expr})"
            )
            return re.sub(token_pattern, replacement, source)

        if "spirv" in enc or "spv" in enc:
            # 指令级改写超出文本 patch 范畴。输出结构化注释，便于外部
            # SPIR-V assembler pass 识别处理。
            marker = f"; RDX_GUARD: {expr} -> {guard}\n"
            if marker not in source:
                idx = source.find("OpFunction")
                if idx >= 0:
                    return source[:idx] + marker + source[idx:]
                return marker + source
            return source

        # 未知/通用 encoding —— 尽力做字面替换。
        replacement = (
            f"(isnan({expr}) || isinf({expr}) ? {guard} : {expr})"
        )
        return source.replace(expr, replacement)

    # ------------------------------------------------------------------
    # Expression replacement（表达式替换）
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_expr_replace(
        source: str,
        expr_from: str,
        expr_to: str,
    ) -> str:
        """在 shader 源码中执行直接文本替换。

        将所有 *expr_from* 替换为 *expr_to*。若 *expr_from* 为空，
        则返回原始 *source* 不变。
        """
        if not expr_from:
            return source
        return source.replace(expr_from, expr_to)

    @classmethod
    def _resolve_source(
        cls,
        controller: Any,
        pipe: Any,
        refl: Any,
        stage: ShaderStage,
        session_id: str,
        *,
        requested_target: str = "",
        requested_encoding: str = "",
    ) -> Tuple[str, Any, str, bool]:
        target_name = str(requested_target or "").strip()
        encoding_name = str(requested_encoding or "").strip().lower()
        raw_requested = cls._is_raw_spirv_asm_request(target_name, encoding_name)

        rd = _get_rd()
        pipeline_obj = rd.ResourceId()
        try:
            if stage == ShaderStage.CS:
                pipeline_obj = pipe.GetComputePipelineObject()
            else:
                pipeline_obj = pipe.GetGraphicsPipelineObject()
        except Exception:
            pipeline_obj = rd.ResourceId()

        if not target_name and not encoding_name:
            encoding, disasm_target = cls._get_best_encoding(controller, session_id)
            source = controller.DisassembleShader(pipeline_obj, refl, disasm_target)
            return str(source or ""), encoding, str(disasm_target), cls._looks_like_raw_spirv_asm(str(source or ""))

        targets = [str(t) for t in controller.GetDisassemblyTargets(True)]
        supported_encodings = list(controller.GetTargetShaderEncodings())

        if raw_requested:
            encoding = cls._find_requested_encoding(supported_encodings, encoding_name or "spirvasm")
            if encoding is None:
                raise RuntimeError(
                    "Raw SPIR-V ASM editing requires a replay backend that supports SPIRVAsm source encoding"
                )
            raw_target = cls._find_raw_spirv_asm_target(targets)
            if raw_target:
                source = controller.DisassembleShader(pipeline_obj, refl, raw_target)
                if source:
                    return str(source), encoding, str(raw_target), True
            source = cls._disassemble_raw_spirv_bytes(refl)
            if source:
                return source, encoding, "SPIR-V ASM", True
            raise RuntimeError(
                "Raw SPIR-V ASM disassembly is unavailable: no raw SPIR-V target was exposed and external spirv-dis was not found"
            )

        target = ""
        if target_name and target_name.lower() != "auto":
            target = cls._find_matching_target(targets, target_name)
            if not target:
                raise RuntimeError(f"Requested disassembly target is unavailable: {target_name}")
        if not target:
            _, target = cls._get_best_encoding(controller, session_id)
        source = controller.DisassembleShader(pipeline_obj, refl, target)
        if not source:
            raise RuntimeError(f"Disassembly returned empty source for target '{target}'")
        encoding = cls._find_requested_encoding(supported_encodings, encoding_name) if encoding_name else None
        if encoding is None:
            encoding, _ = cls._get_best_encoding(controller, session_id)
        return str(source), encoding, str(target), cls._looks_like_raw_spirv_asm(str(source))

    @staticmethod
    def _is_raw_spirv_asm_request(target: str, encoding: str) -> bool:
        target_key = str(target or "").strip().lower().replace("_", " ").replace("-", " ")
        encoding_key = str(encoding or "").strip().lower().replace("_", "")
        if encoding_key == "spirvasm":
            return True
        if not target_key:
            return False
        return target_key in {"spirv asm", "spir v asm", "spirvdis", "spirv dis", "spir v dis"} or (
            "spir" in target_key and ("asm" in target_key or "dis" in target_key)
        )

    @staticmethod
    def _find_matching_target(targets: List[str], requested_target: str) -> str:
        requested = str(requested_target or "").strip().lower()
        for target in targets:
            if str(target).strip().lower() == requested:
                return str(target)
        return ""

    @staticmethod
    def _find_raw_spirv_asm_target(targets: List[str]) -> str:
        for target in targets:
            lowered = str(target).strip().lower()
            if "spir" not in lowered:
                continue
            if "renderdoc" in lowered:
                continue
            if "asm" in lowered or "dis" in lowered or lowered == "spir-v":
                return str(target)
        return ""

    @classmethod
    def _find_requested_encoding(
        cls,
        encodings: List[Any],
        requested_encoding: str,
    ) -> Optional[Any]:
        normalized = str(requested_encoding or "").strip().lower().replace("_", "")
        if not normalized:
            return None
        aliases = {
            "spirvasm": {"spirvasm", "spirvdis"},
            "hlsl": {"hlsl"},
            "glsl": {"glsl"},
            "dxbc": {"dxbc"},
            "dxil": {"dxil"},
            "slang": {"slang"},
        }
        wanted = aliases.get(normalized, {normalized})
        for item in encodings:
            item_name = cls._encoding_name(item).replace("_", "")
            if item_name in wanted:
                return item
        return None

    @staticmethod
    def _looks_like_raw_spirv_asm(source: str) -> bool:
        text = str(source or "")
        if not text:
            return False
        return "OpCapability" in text or "OpDecorate" in text or text.lstrip().startswith("; SPIR-V")

    @staticmethod
    def _disassemble_raw_spirv_bytes(refl: Any) -> str:
        raw_bytes = getattr(refl, "rawBytes", None)
        if raw_bytes is None:
            return ""
        try:
            payload = bytes(raw_bytes)
        except Exception:
            try:
                payload = raw_bytes.tobytes()
            except Exception:
                return ""
        tool = shutil.which("spirv-dis") or shutil.which("spirv-dis.exe")
        if not tool:
            return ""
        runtime_dir = Path(__file__).resolve().parents[2] / "intermediate" / "runtime" / "rdx_cli" / "patch_engine"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stem = f"spirv_{uuid.uuid4().hex[:10]}"
        source_path = runtime_dir / f"{stem}.spv"
        output_path = runtime_dir / f"{stem}.spvasm"
        try:
            source_path.write_bytes(payload)
            proc = subprocess.run(
                [tool, str(source_path), "--raw-id", "-o", str(output_path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "spirv-dis failed with exit code %s: %s",
                    proc.returncode,
                    (proc.stderr or proc.stdout or "").strip(),
                )
                return ""
            return output_path.read_text(encoding="utf-8")
        finally:
            for path in (source_path, output_path):
                try:
                    path.unlink()
                except Exception:
                    pass

    @staticmethod
    def _apply_unified_diff(source: str, diff_text: str) -> str:
        if not diff_text.strip():
            return source
        source_lines = source.splitlines(keepends=True)
        diff_lines = diff_text.splitlines(keepends=True)
        result: List[str] = []
        source_index = 0
        line_index = 0
        current_hunk = False

        def lines_match(source_line: str, diff_payload: str) -> bool:
            if source_line == diff_payload:
                return True
            return source_line.rstrip("\r\n") == diff_payload.rstrip("\r\n")

        while line_index < len(diff_lines):
            line = diff_lines[line_index]
            if line.startswith("---") or line.startswith("+++"):
                line_index += 1
                continue
            if not line.startswith("@@"):
                raise ValueError(f"Unsupported diff header line: {line.rstrip()}")
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match is None:
                raise ValueError(f"Malformed diff hunk header: {line.rstrip()}")
            old_start = int(match.group(1))
            while source_index < old_start - 1 and source_index < len(source_lines):
                result.append(source_lines[source_index])
                source_index += 1
            line_index += 1
            current_hunk = True
            while line_index < len(diff_lines):
                diff_line = diff_lines[line_index]
                if diff_line.startswith("@@"):
                    break
                if diff_line.startswith("\\ No newline at end of file"):
                    line_index += 1
                    continue
                if not diff_line:
                    line_index += 1
                    continue
                marker = diff_line[0]
                payload = diff_line[1:]
                if marker == " ":
                    if source_index >= len(source_lines) or not lines_match(source_lines[source_index], payload):
                        raise ValueError("Diff context does not match the current shader source")
                    result.append(source_lines[source_index])
                    source_index += 1
                elif marker == "-":
                    if source_index >= len(source_lines) or not lines_match(source_lines[source_index], payload):
                        raise ValueError("Diff removal does not match the current shader source")
                    source_index += 1
                elif marker == "+":
                    result.append(payload)
                else:
                    raise ValueError(f"Unsupported diff marker: {marker}")
                line_index += 1

        if not current_hunk:
            raise ValueError("Unified diff did not contain any hunk")
        result.extend(source_lines[source_index:])
        return "".join(result)

    # ------------------------------------------------------------------
    # Encoding selection（编码选择）
    # ------------------------------------------------------------------

    @staticmethod
    def _get_best_encoding(
        controller: Any,
        session_id: str,
    ) -> Tuple[Any, str]:
        """为当前 session 选择最易编辑的 shader encoding。

        该方法向 replay controller 查询可用的 disassembly targets
        （如 ``"HLSL"``, ``"GLSL 460"``, ``"SPIR-V (Human-readable)"``），
        以及 ``BuildTargetShader`` 可接受的 shader encodings。

        优先级为 **HLSL > GLSL > SPIRVAsm**，因为高层语言更易文本化修改。

        Returns
        -------
        tuple[ShaderEncoding, str]
            ``(ShaderEncoding, disassembly_target_name)``，分别用于
            ``DisassembleShader`` 与 ``BuildTargetShader``。

        Raises
        ------
        RuntimeError
            当不存在合适的 encoding / target 组合时抛出。
        """
        rd = _get_rd()

        targets: List[str] = [str(t) for t in controller.GetDisassemblyTargets(True)]
        encodings = list(controller.GetTargetShaderEncodings())

        # 构建 encoding 集合用于快速 membership 测试。
        encoding_set = set(encodings)

        # (ShaderEncoding, 用于匹配 disassembly target name 的关键字)
        preferences = [
            (rd.ShaderEncoding.HLSL,     "hlsl"),
            (rd.ShaderEncoding.GLSL,     "glsl"),
            (rd.ShaderEncoding.SPIRVAsm, "spir"),
        ]

        for enc, keyword in preferences:
            if enc not in encoding_set:
                continue
            for target_name in targets:
                if keyword in target_name.lower():
                    return enc, target_name

        # 未找到首选项时，回退到可用项。
        if encodings and targets:
            logger.warning(
                "No preferred encoding matched for session %s; falling "
                "back to encoding=%s target='%s'",
                session_id, encodings[0], targets[0],
            )
            return encodings[0], targets[0]

        raise RuntimeError(
            f"No shader encodings available for session {session_id}. "
            f"Disassembly targets={targets!r}, "
            f"Encodings={[str(e) for e in encodings]!r}"
        )

    @staticmethod
    def _build_compile_flags(refl: Any) -> Any:
        """构造 ``BuildTargetShader`` 需要的真实 ``ShaderCompileFlags`` 对象。"""
        rd = _get_rd()
        compile_flags = rd.ShaderCompileFlags()
        debug_info = getattr(refl, "debugInfo", None)
        raw_flags = getattr(debug_info, "compileFlags", None) if debug_info is not None else None
        if raw_flags is None:
            return compile_flags
        try:
            raw_values = getattr(raw_flags, "flags", raw_flags)
            cloned_flags = []
            for item in list(raw_values):
                flag = rd.ShaderCompileFlag()
                flag.name = str(getattr(item, "name", "") or "")
                flag.value = str(getattr(item, "value", "") or "")
                cloned_flags.append(flag)
            compile_flags.flags = cloned_flags
        except Exception:
            logger.warning("Failed to clone shader compile flags from reflection", exc_info=True)
        return compile_flags

    @staticmethod
    def _compile_flag_payload(compile_flags: Any) -> List[Dict[str, str]]:
        """将 ``ShaderCompileFlags`` 转成诊断友好的结构化载荷。"""
        raw_flags = getattr(compile_flags, "flags", None)
        if raw_flags is None:
            return []
        try:
            values = list(raw_flags)
        except Exception:
            return []
        payload: List[Dict[str, str]] = []
        for item in values:
            payload.append(
                {
                    "name": str(getattr(item, "name", "") or ""),
                    "value": str(getattr(item, "value", "") or ""),
                }
            )
        return payload

    @staticmethod
    def _encoding_name(encoding: Any) -> str:
        """从 ``ShaderEncoding`` enum 派生小写名称字符串。

        兼容不同 renderdoc 版本暴露的 ``ShaderEncoding.HLSL`` 或裸
        ``"HLSL"`` 形式。
        """
        try:
            numeric = int(encoding)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None:
            numeric_map = {
                0: "unknown",
                1: "hlsl",
                2: "glsl",
                3: "spirvasm",
                4: "dxil",
                5: "slang",
            }
            if numeric in numeric_map:
                return numeric_map[numeric]
        name = str(encoding)
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        return name.lower()
