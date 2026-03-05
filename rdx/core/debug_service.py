"""
RDX-MCP 的 shader debugging service。

将 RenderDoc 的 pixel、vertex、compute-thread shader debugging APIs
封装为 async 接口。该 service 采用延迟导入 ``renderdoc`` module，
以便在缺少它时仍可加载其余包。

关键能力：
    * 逐步执行 pixel shader，查找第一个 NaN/Inf 输出。
    * 采集完整 shader trace（可配置步数上限）。
    * 调试指定 vertex invocation。
    * 将 trace 持久化为 JSON artifacts 便于后续分析。
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from rdx.models import (
    ArtifactRef,
    DebugStep,
    GraphicsAPI,
    PixelDebugResult,
)

if TYPE_CHECKING:
    pass  # avoid heavyweight imports at module scope

logger = logging.getLogger("rdx.core.debug_service")

# RenderDoc 中已知支持 shader debugging 的 API。
_DEBUG_SUPPORTED_APIS = frozenset({
    GraphicsAPI.D3D11,
    GraphicsAPI.D3D12,
    GraphicsAPI.VULKAN,
})


class DebugService:
    """围绕 RenderDoc shader-debug primitives 的高层 async wrapper。

    所有公开方法均接收松耦合的 service 引用（session_manager、
    artifact_store），便于保持无状态并易于测试。
    """

    # ------------------------------------------------------------------
    # Public API（公共接口）
    # ------------------------------------------------------------------

    async def debug_pixel(
        self,
        session_id: str,
        event_id: int,
        x: int,
        y: int,
        session_manager: Any,
        artifact_store: Any,
        *,
        sample: int = 0,
        mode: str = "run_to_naninf",
        max_steps: int = 20_000,
    ) -> PixelDebugResult:
        """调试位于 *(x, y)* 的 pixel shader invocation。

        Parameters
        ----------
        session_id:
            活跃 session identifier。
        event_id:
            需要调试的 draw-call event。
        x, y:
            render target 中的像素坐标。
        session_manager:
            session manager 引用（提供 replay controller）。
        artifact_store:
            artifact store 引用（用于 trace 持久化）。
        sample:
            MSAA sample index（默认 0）。
        mode:
            ``"run_to_naninf"`` -- 在第一个 NaN/Inf variable 处停止。
            ``"full_trace"``    -- 收集所有 step，直到 *max_steps*。
        max_steps:
            shader step 硬上限，防止 trace 失控。

        Returns
        -------
        PixelDebugResult
            包含 trace artifact、总步数，以及首个 NaN/Inf step（如有）。
        """
        # -- Guard：是否支持 shader debugging -------------------------------
        if not await self._check_debug_support(session_id, session_manager):
            return PixelDebugResult(
                ok=False,
                notes=(
                    "Shader debugging is not supported for the current "
                    "graphics API or driver."
                ),
            )

        # 延迟导入 renderdoc module。
        rd = _lazy_import_renderdoc()
        if rd is None:
            return PixelDebugResult(
                ok=False,
                notes="renderdoc Python module is not available.",
            )

        # -- 获取 replay controller -----------------------------------------
        controller = await self._get_controller(
            session_id, session_manager,
        )
        if controller is None:
            return PixelDebugResult(
                ok=False,
                notes="Could not obtain replay controller.",
            )

        trace = None
        try:
            # -- 跳转到目标 event ------------------------------------------
            controller.SetFrameEvent(event_id, True)

            # -- 启动 pixel debug session ----------------------------------
            inputs = rd.DebugPixelInputs()
            inputs.sample = sample

            trace = controller.DebugPixel(x, y, inputs)

            if trace is None or not trace.valid:
                return PixelDebugResult(
                    ok=False,
                    notes=(
                        f"DebugPixel({x}, {y}) at event {event_id} "
                        f"returned an invalid trace. Shader debugging "
                        f"may not be available for this draw call."
                    ),
                )

            # -- 迭代 debug states -----------------------------------------
            debugger = trace.debugger
            steps: List[DebugStep] = []
            naninf_step: Optional[DebugStep] = None
            step_index = 0

            while step_index < max_steps:
                try:
                    states = controller.ContinueDebug(debugger)
                except Exception as exc:
                    logger.warning(
                        "ContinueDebug raised at step %d: %s",
                        step_index,
                        exc,
                    )
                    break

                if not states:
                    # 没有更多 state —— shader 执行完成。
                    break

                for state in states:
                    variables = self._extract_variables(state)
                    has_nan_inf = any(
                        self._has_naninf(v)
                        for v in variables.values()
                        if isinstance(v, (int, float))
                    )

                    step = DebugStep(
                        step_index=getattr(state, "stepIndex", step_index),
                        registers=variables,
                        is_naninf=has_nan_inf,
                    )
                    steps.append(step)
                    step_index += 1

                    if has_nan_inf and naninf_step is None:
                        naninf_step = step

                    if mode == "run_to_naninf" and naninf_step is not None:
                        break

                    if step_index >= max_steps:
                        break

                # 如果已找到目标，提前退出。
                if mode == "run_to_naninf" and naninf_step is not None:
                    break

        except Exception as exc:
            logger.exception(
                "Pixel debug failed for event %d at (%d, %d)",
                event_id,
                x,
                y,
            )
            return PixelDebugResult(
                ok=False,
                notes=f"Pixel debug failed: {exc}",
            )
        finally:
            # 始终释放 trace 以回收 driver 资源。
            if trace is not None:
                try:
                    controller.FreeTrace(trace)
                except Exception:
                    logger.debug("FreeTrace raised (non-critical)", exc_info=True)

        # -- 将 trace 持久化为 JSON artifact --------------------------------
        trace_artifact: Optional[ArtifactRef] = None
        if artifact_store is not None and steps:
            try:
                trace_payload = {
                    "session_id": session_id,
                    "event_id": event_id,
                    "pixel": {"x": x, "y": y},
                    "sample": sample,
                    "mode": mode,
                    "total_steps": len(steps),
                    "naninf_step_index": (
                        naninf_step.step_index if naninf_step else None
                    ),
                    "steps": [
                        {
                            "step_index": s.step_index,
                            "instruction": s.instruction,
                            "registers": _sanitize_for_json(s.registers),
                            "is_naninf": s.is_naninf,
                        }
                        for s in steps
                    ],
                }
                trace_artifact = await artifact_store.store_json(
                    data=trace_payload,
                    name=(
                        f"pixel_trace_evt{event_id}_"
                        f"x{x}_y{y}"
                    ),
                    session_id=session_id,
                    meta={
                        "type": "pixel_debug_trace",
                        "event_id": event_id,
                        "x": x,
                        "y": y,
                        "mode": mode,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to persist trace artifact: %s", exc)

        return PixelDebugResult(
            ok=True,
            trace_artifact=trace_artifact,
            total_steps=len(steps),
            naninf_step=naninf_step,
            notes=(
                f"Traced {len(steps)} steps. "
                + (
                    f"First NaN/Inf at step {naninf_step.step_index}."
                    if naninf_step
                    else "No NaN/Inf detected."
                )
            ),
        )

    # ------------------------------------------------------------------

    async def debug_vertex(
        self,
        session_id: str,
        event_id: int,
        vertex_id: int,
        instance_id: int,
        session_manager: Any,
        artifact_store: Any,
        *,
        max_steps: int = 20_000,
    ) -> Dict[str, Any]:
        """调试 vertex shader invocation。

        Parameters
        ----------
        session_id:
            活跃 session identifier。
        event_id:
            包含该 vertex 的 draw-call event。
        vertex_id:
            需要调试的 vertex index。
        instance_id:
            instance index（用于 instanced draws；通常为 0）。
        session_manager:
            session manager 引用。
        artifact_store:
            artifact store 引用。
        max_steps:
            shader step 硬上限。

        Returns
        -------
        dict
            概要信息，包含 ``ok``、``total_steps``、``trace_artifact``、
            以及 ``notes``。
        """
        if not await self._check_debug_support(session_id, session_manager):
            return {
                "ok": False,
                "notes": (
                    "Shader debugging is not supported for the current "
                    "graphics API or driver."
                ),
            }

        rd = _lazy_import_renderdoc()
        if rd is None:
            return {
                "ok": False,
                "notes": "renderdoc Python module is not available.",
            }

        controller = await self._get_controller(session_id, session_manager)
        if controller is None:
            return {"ok": False, "notes": "Could not obtain replay controller."}

        trace = None
        try:
            controller.SetFrameEvent(event_id, True)

            # DebugVertex(vertexId, instanceId, idx, view)
            # idx  = 0（provoking vertex index）
            # view = 0（multiview index）
            trace = controller.DebugVertex(vertex_id, instance_id, 0, 0)

            if trace is None or not trace.valid:
                return {
                    "ok": False,
                    "notes": (
                        f"DebugVertex({vertex_id}, inst={instance_id}) "
                        f"at event {event_id} returned an invalid trace."
                    ),
                }

            debugger = trace.debugger
            steps: List[Dict[str, Any]] = []
            step_index = 0

            while step_index < max_steps:
                try:
                    states = controller.ContinueDebug(debugger)
                except Exception as exc:
                    logger.warning(
                        "ContinueDebug raised at step %d: %s",
                        step_index,
                        exc,
                    )
                    break

                if not states:
                    break

                for state in states:
                    variables = self._extract_variables(state)
                    steps.append({
                        "step_index": getattr(state, "stepIndex", step_index),
                        "registers": _sanitize_for_json(variables),
                    })
                    step_index += 1
                    if step_index >= max_steps:
                        break

        except Exception as exc:
            logger.exception(
                "Vertex debug failed for event %d, vtx %d",
                event_id,
                vertex_id,
            )
            return {"ok": False, "notes": f"Vertex debug failed: {exc}"}
        finally:
            if trace is not None:
                try:
                    controller.FreeTrace(trace)
                except Exception:
                    logger.debug("FreeTrace raised (non-critical)", exc_info=True)

        # 持久化 artifact
        trace_artifact: Optional[ArtifactRef] = None
        if artifact_store is not None and steps:
            try:
                payload = {
                    "session_id": session_id,
                    "event_id": event_id,
                    "vertex_id": vertex_id,
                    "instance_id": instance_id,
                    "total_steps": len(steps),
                    "steps": steps,
                }
                trace_artifact = await artifact_store.store_json(
                    data=payload,
                    name=(
                        f"vertex_trace_evt{event_id}_"
                        f"vtx{vertex_id}_inst{instance_id}"
                    ),
                    session_id=session_id,
                    meta={
                        "type": "vertex_debug_trace",
                        "event_id": event_id,
                        "vertex_id": vertex_id,
                        "instance_id": instance_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to persist vertex trace artifact: %s", exc)

        return {
            "ok": True,
            "total_steps": len(steps),
            "trace_artifact": trace_artifact,
            "notes": f"Traced {len(steps)} vertex shader steps.",
        }

    # ------------------------------------------------------------------
    # Support-check（能力检测）
    # ------------------------------------------------------------------

    async def _check_debug_support(
        self,
        session_id: str,
        session_manager: Any,
    ) -> bool:
        """若当前 API/driver 支持 shader debugging 则返回 ``True``。

        支持 D3D11、D3D12、Vulkan。
        不支持 OpenGL 和 OpenGL ES。
        """
        try:
            session_info = await session_manager.get_session(session_id)
            if session_info is None:
                logger.warning("Session %s not found", session_id)
                return False

            capabilities = getattr(session_info, "capabilities", None)
            if capabilities is not None:
                # 若 session 已暴露标志，则优先使用。
                if hasattr(capabilities, "shader_debug_supported"):
                    return bool(capabilities.shader_debug_supported)

            # 退回到基于 API 的启发式判断。
            api = GraphicsAPI.UNKNOWN
            if capabilities is not None and hasattr(capabilities, "api"):
                api = capabilities.api
            elif hasattr(session_info, "api"):
                api = session_info.api

            return api in _DEBUG_SUPPORTED_APIS

        except Exception as exc:
            logger.warning(
                "Could not determine debug support for session %s: %s",
                session_id,
                exc,
            )
            # 乐观策略：先尝试，若不支持则由 DebugPixel/DebugVertex
            # 以更温和方式失败。
            return True

    # ------------------------------------------------------------------
    # Internal helpers（内部辅助）
    # ------------------------------------------------------------------

    @staticmethod
    async def _get_controller(
        session_id: str,
        session_manager: Any,
    ) -> Any:
        """获取 *session_id* 对应的 replay controller。

        失败时返回 ``None``。
        """
        try:
            controller = await session_manager.get_controller(session_id)
            return controller
        except Exception as exc:
            logger.error(
                "Failed to get controller for session %s: %s",
                session_id,
                exc,
            )
            return None

    @staticmethod
    def _extract_variables(state: Any) -> Dict[str, Any]:
        """从 ``ShaderDebugState`` 中提取变量名与数值。

        state 中的每个 ``change`` 都包含带 ``name`` 与 ``value`` 的
        ``SourceVariableMapping``。这里将其扁平化为 ``{name: value}``。
        """
        result: Dict[str, Any] = {}

        changes = getattr(state, "changes", None)
        if changes is None:
            return result

        for change in changes:
            name = getattr(change, "name", None)
            if name is None:
                continue

            # RenderDoc 会根据类型通过不同 accessor 暴露 value，
            # 先尝试最常见的 float 路径。
            value: Any = None

            # 先尝试 .value（SourceVariableMapping 存储的是新值）。
            raw_value = getattr(change, "value", None)
            if raw_value is not None:
                # ShaderVariable 风格：floatValue, uintValue, sintValue
                float_val = getattr(raw_value, "floatValue", None)
                if float_val is not None:
                    # floatValue 通常是 4 元数组（xyzw）。
                    try:
                        value = [float(float_val[i]) for i in range(4)]
                    except (IndexError, TypeError):
                        value = float(float_val)
                else:
                    uint_val = getattr(raw_value, "uintValue", None)
                    if uint_val is not None:
                        try:
                            value = [int(uint_val[i]) for i in range(4)]
                        except (IndexError, TypeError):
                            value = int(uint_val)
                    else:
                        sint_val = getattr(raw_value, "sintValue", None)
                        if sint_val is not None:
                            try:
                                value = [int(sint_val[i]) for i in range(4)]
                            except (IndexError, TypeError):
                                value = int(sint_val)
                        else:
                            # 兜底：使用 repr。
                            value = repr(raw_value)

            result[str(name)] = value

        return result

    @staticmethod
    def _has_naninf(value: Any) -> bool:
        """若 *value* 是 NaN 或 Inf 则返回 ``True``。

        支持单个 float 与 float 列表/元组。
        """
        if isinstance(value, (list, tuple)):
            return any(
                DebugService._has_naninf(v)
                for v in value
            )
        if isinstance(value, float):
            return math.isnan(value) or math.isinf(value)
        if isinstance(value, int):
            return False
        return False


# ---------------------------------------------------------------------------
# Module-level helpers（模块级辅助）
# ---------------------------------------------------------------------------

_rd_module: Any = None
_rd_import_attempted: bool = False


def _lazy_import_renderdoc() -> Any:
    """首次使用时导入 ``renderdoc``。

    返回 module 对象；若无法加载则返回 ``None``。
    """
    global _rd_module, _rd_import_attempted

    if _rd_import_attempted:
        return _rd_module

    _rd_import_attempted = True
    try:
        import renderdoc as rd  # type: ignore[import-not-found]
        _rd_module = rd
        logger.debug("renderdoc module loaded successfully")
    except ImportError:
        logger.warning(
            "renderdoc Python module not found. Shader debugging 将不可用。"
            "请确保该 module 在 sys.path 中或设置 RDX_RENDERDOC_PATH。"
        )
        _rd_module = None

    return _rd_module


def _sanitize_for_json(data: Any) -> Any:
    """Make *data* JSON-serializable.

    Replaces ``float('nan')`` and ``float('inf')`` with string sentinels so
    that ``json.dumps`` does not raise.
    """
    if isinstance(data, dict):
        return {k: _sanitize_for_json(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_sanitize_for_json(v) for v in data]
    if isinstance(data, float):
        if math.isnan(data):
            return "__NaN__"
        if math.isinf(data):
            return "__Inf__" if data > 0 else "__-Inf__"
    return data
