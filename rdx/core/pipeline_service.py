"""Pipeline state 妫€鏌ヤ笌 shader artifact 瀵煎嚭 service銆?

灏?RenderDoc 鐨?pipeline state introspection 涓?shader disassembly
APIs 灏佽涓?async 鎿嶄綔锛岃繑鍥炵粨鏋勫寲鐨?Pydantic models 涓庣増鏈寲 artifacts銆?

鎵€鏈夐樆濉炵殑 RenderDoc 璋冪敤閮戒細閫氳繃 ``asyncio.to_thread`` 鍒嗘淳鍒扮嚎绋嬨€?
``renderdoc`` module 閲囩敤寤惰繜瀵煎叆鈥斺€斾粎鍦?RenderDoc replay context 涓彲鐢ㄣ€?
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from rdx.models import (
    ArtifactRef,
    BlendState,
    DepthStencilState,
    GraphicsAPI,
    PipelineSnapshot,
    RenderTargetInfo,
    ResourceBindingEntry,
    ShaderExportBundle,
    ShaderInfo,
    ShaderStage,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy renderdoc import锛堝欢杩熷鍏ワ級
# ---------------------------------------------------------------------------

_rd_module: Any = None


def _get_rd() -> Any:
    """杩斿洖 ``renderdoc`` module锛屽苟鍦ㄩ娆¤闂椂瀵煎叆銆?"""
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
# Dependency protocols锛堜緷璧栧崗璁級
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionManager(Protocol):
    """session lifecycle manager 鐨勬渶灏忕粨鏋勫寲绾﹀畾銆?"""

    def get_controller(self, session_id: str) -> Any:
        """杩斿洖缁戝畾鍒?*session_id* 鐨?``ReplayController``銆?"""
        ...

    def get_output(self, session_id: str) -> Any:
        """杩斿洖缁戝畾鍒?*session_id* 鐨?``ReplayOutput``銆?"""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """artifact persistence layer 鐨勬渶灏忕粨鏋勫寲绾﹀畾銆?"""

    async def store(
        self,
        data: bytes,
        *,
        mime: str,
        suffix: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """鎸佷箙鍖?*data* 骞惰繑鍥炶拷韪敤鐨?:class:`ArtifactRef`銆?"""
        ...


# ---------------------------------------------------------------------------
# Shader stage enumeration helpers锛坰hader stage 鏋氫妇杈呭姪锛?
# ---------------------------------------------------------------------------

# 鏋氫妇缁戝畾 shader 鏃堕亶鍘嗙殑 graphics stages锛岄『搴忎笌鍏稿瀷 graphics pipeline 涓€鑷淬€?
_GRAPHICS_STAGES: Tuple[str, ...] = (
    "Vertex",
    "Hull",
    "Domain",
    "Geometry",
    "Pixel",
)

_COMPUTE_STAGES: Tuple[str, ...] = ("Compute",)


def _rd_shader_stages() -> List[Any]:
    """杩斿洖鎵€鏈?graphics 涓?compute stages 鐨?``rd.ShaderStage`` 鍒楄〃銆?"""
    rd = _get_rd()
    return [
        rd.ShaderStage.Vertex,
        rd.ShaderStage.Hull,
        rd.ShaderStage.Domain,
        rd.ShaderStage.Geometry,
        rd.ShaderStage.Pixel,
        rd.ShaderStage.Compute,
    ]


def _map_shader_stage(rd_stage: Any) -> ShaderStage:
    """灏?RenderDoc ``ShaderStage`` enum 鏄犲皠涓烘垜浠殑 ``ShaderStage``銆?"""
    rd = _get_rd()
    mapping: Dict[Any, ShaderStage] = {
        rd.ShaderStage.Vertex: ShaderStage.VS,
        rd.ShaderStage.Hull: ShaderStage.HS,
        rd.ShaderStage.Domain: ShaderStage.DS,
        rd.ShaderStage.Geometry: ShaderStage.GS,
        rd.ShaderStage.Pixel: ShaderStage.PS,
        rd.ShaderStage.Compute: ShaderStage.CS,
    }
    return mapping.get(rd_stage, ShaderStage.PS)


def _our_stage_to_rd(stage: ShaderStage) -> Any:
    """灏嗘垜浠殑 ``ShaderStage`` 鏄犲皠鍥?RenderDoc ``ShaderStage``銆?"""
    rd = _get_rd()
    mapping: Dict[ShaderStage, Any] = {
        ShaderStage.VS: rd.ShaderStage.Vertex,
        ShaderStage.HS: rd.ShaderStage.Hull,
        ShaderStage.DS: rd.ShaderStage.Domain,
        ShaderStage.GS: rd.ShaderStage.Geometry,
        ShaderStage.PS: rd.ShaderStage.Pixel,
        ShaderStage.CS: rd.ShaderStage.Compute,
    }
    result = mapping.get(stage)
    if result is None:
        raise ValueError(f"Unsupported shader stage for RenderDoc: {stage}")
    return result


# ---------------------------------------------------------------------------
# Graphics API mapping锛圙raphics API 鏄犲皠锛?
# ---------------------------------------------------------------------------


def _map_graphics_api(rd_api: Any) -> GraphicsAPI:
    """灏?``rd.GraphicsAPI`` 鏄犲皠涓烘垜浠殑 ``GraphicsAPI`` enum銆?"""
    rd = _get_rd()
    mapping: Dict[Any, GraphicsAPI] = {
        rd.GraphicsAPI.D3D11: GraphicsAPI.D3D11,
        rd.GraphicsAPI.D3D12: GraphicsAPI.D3D12,
        rd.GraphicsAPI.Vulkan: GraphicsAPI.VULKAN,
        rd.GraphicsAPI.OpenGL: GraphicsAPI.OPENGL,
    }
    return mapping.get(rd_api, GraphicsAPI.UNKNOWN)


# ---------------------------------------------------------------------------
# Null ResourceId helper锛堢┖ ResourceId 鍒ゆ柇锛?
# ---------------------------------------------------------------------------


def _is_null_id(resource_id: Any) -> bool:
    """褰?*resource_id* 涓虹┖/null 鏃惰繑鍥?``True``銆?"""
    rd = _get_rd()
    try:
        return resource_id == rd.ResourceId()
    except Exception:
        return resource_id is None


# ---------------------------------------------------------------------------
# API-specific state retrieval锛圓PI 鐗瑰畾 state 鑾峰彇锛?
# ---------------------------------------------------------------------------


def _get_api_specific_state(controller: Any, rd_api: Any) -> Any:
    """杩斿洖 API 鐗瑰畾鐨?pipeline state 瀵硅薄銆?

    鑻?API 鏃犳硶璇嗗埆鍒欒繑鍥?``None``銆?
    """
    rd = _get_rd()
    if rd_api == rd.GraphicsAPI.D3D11:
        return controller.GetD3D11PipelineState()
    if rd_api == rd.GraphicsAPI.D3D12:
        return controller.GetD3D12PipelineState()
    if rd_api == rd.GraphicsAPI.Vulkan:
        return controller.GetVulkanPipelineState()
    if rd_api == rd.GraphicsAPI.OpenGL:
        return controller.GetOpenGLPipelineState()
    return None


# ---------------------------------------------------------------------------
# Blend state extraction锛圓PI 鐗瑰畾锛?
# ---------------------------------------------------------------------------


def _extract_blend_state(api_state: Any, api: GraphicsAPI) -> List[BlendState]:
    """浠?API 鐗瑰畾 state 涓彁鍙栨瘡涓?RT 鐨?blend 閰嶇疆銆?

    姣忎釜 render target slot 杩斿洖涓€涓?:class:`BlendState`銆?
    """
    blends: List[BlendState] = []
    if api_state is None:
        return blends

    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12):
            raw_blends = api_state.outputMerger.blendState.blends
        elif api == GraphicsAPI.VULKAN:
            raw_blends = api_state.colorBlend.blends
        elif api == GraphicsAPI.OPENGL:
            raw_blends = api_state.framebuffer.blendState.blends
        else:
            return blends

        for b in raw_blends:
            blends.append(BlendState(
                enabled=bool(b.enabled),
                src_color=str(b.colorBlend.source),
                dst_color=str(b.colorBlend.destination),
                color_op=str(b.colorBlend.operation),
                src_alpha=str(b.alphaBlend.source),
                dst_alpha=str(b.alphaBlend.destination),
                alpha_op=str(b.alphaBlend.operation),
            ))
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_blend_state: %s", exc)

    return blends


# ---------------------------------------------------------------------------
# Depth / stencil extraction锛圓PI 鐗瑰畾锛?
# ---------------------------------------------------------------------------


def _extract_depth_stencil(
    api_state: Any,
    api: GraphicsAPI,
) -> DepthStencilState:
    """浠?API 鐗瑰畾 state 涓彁鍙?depth/stencil 閰嶇疆銆?"""
    ds = DepthStencilState()
    if api_state is None:
        return ds

    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12):
            raw = api_state.outputMerger.depthStencilState
            ds.depth_test_enabled = bool(raw.depthEnable)
            ds.depth_write_enabled = bool(raw.depthWrites)
            ds.depth_func = str(raw.depthFunction)
            ds.stencil_enabled = bool(raw.stencilEnable)
        elif api == GraphicsAPI.VULKAN:
            raw = api_state.depthStencil
            ds.depth_test_enabled = bool(raw.depthTestEnable)
            ds.depth_write_enabled = bool(raw.depthWriteEnable)
            ds.depth_func = str(raw.depthFunction)
            ds.stencil_enabled = bool(raw.stencilTestEnable)
        elif api == GraphicsAPI.OPENGL:
            ds.depth_test_enabled = bool(api_state.depthState.depthEnable)
            ds.depth_write_enabled = bool(api_state.depthState.depthWrites)
            ds.depth_func = str(api_state.depthState.depthFunction)
            ds.stencil_enabled = bool(api_state.stencilState.stencilEnable)
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_depth_stencil: %s", exc)

    return ds


# ---------------------------------------------------------------------------
# Render target extraction锛堟覆鏌撶洰鏍囨彁鍙栵級
# ---------------------------------------------------------------------------


async def _extract_render_targets(
    pipe_state: Any,
    api: GraphicsAPI,
    controller: Any,
) -> Tuple[List[RenderTargetInfo], Optional[RenderTargetInfo]]:
    """鎻愬彇 colour render targets 涓?depth target銆?

    浣跨敤鎶借薄鎺ュ彛 ``PipeState.GetOutputTargets()`` 涓?
    ``PipeState.GetDepthTarget()``锛屽洜姝ら€昏緫涓?API 鏃犲叧銆?

    杩斿洖 ``(colour_targets, depth_target)``銆?
    """
    colour_targets: List[RenderTargetInfo] = []
    depth_target: Optional[RenderTargetInfo] = None

    textures = await asyncio.to_thread(controller.GetTextures)
    tex_by_id: Dict[Any, Any] = {tex.resourceId: tex for tex in textures}

    # ---- colour outputs ----
    try:
        output_descriptors = pipe_state.GetOutputTargets()
        for desc in output_descriptors:
            rid = desc.resourceId
            if _is_null_id(rid):
                continue
            tex = tex_by_id.get(rid)
            rt = RenderTargetInfo(resource_id=str(rid))
            if tex is not None:
                rt.format = str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format)
                rt.width = int(tex.width)
                rt.height = int(tex.height)
                # 缁忛獙鍒ゆ柇锛氭鏌?format 鍚嶇О涓槸鍚﹀寘鍚?sRGB銆?
                rt.is_srgb = "srgb" in rt.format.lower()
            colour_targets.append(rt)
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_render_targets (colour): %s", exc)

    # ---- depth target ----
    try:
        depth_desc = pipe_state.GetDepthTarget()
        rid = depth_desc.resourceId
        if not _is_null_id(rid):
            tex = tex_by_id.get(rid)
            dt = RenderTargetInfo(resource_id=str(rid))
            if tex is not None:
                dt.format = str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format)
                dt.width = int(tex.width)
                dt.height = int(tex.height)
            depth_target = dt
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_render_targets (depth): %s", exc)

    return colour_targets, depth_target


# ---------------------------------------------------------------------------
# Viewport / scissor extraction锛圓PI 鐗瑰畾锛?
# ---------------------------------------------------------------------------


def _extract_viewport(api_state: Any, api: GraphicsAPI) -> Dict[str, float]:
    """杩斿洖绗竴涓?viewport锛屾牸寮忎负 ``{x, y, width, height, minDepth, maxDepth}``銆?"""
    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12, GraphicsAPI.OPENGL):
            vp = api_state.rasterizer.viewports[0]
            return {
                "x": float(vp.x),
                "y": float(vp.y),
                "width": float(vp.width),
                "height": float(vp.height),
                "min_depth": float(vp.minDepth),
                "max_depth": float(vp.maxDepth),
            }
        if api == GraphicsAPI.VULKAN:
            vs = api_state.viewportScissor.viewportScissors[0]
            vp = vs.vp
            return {
                "x": float(vp.x),
                "y": float(vp.y),
                "width": float(vp.width),
                "height": float(vp.height),
                "min_depth": float(vp.minDepth),
                "max_depth": float(vp.maxDepth),
            }
    except (AttributeError, IndexError, TypeError) as exc:
        logger.debug("_extract_viewport: %s", exc)
    return {}


def _extract_scissor(api_state: Any, api: GraphicsAPI) -> Dict[str, int]:
    """杩斿洖绗竴涓?scissor rect锛屾牸寮忎负 ``{x, y, width, height}``銆?"""
    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12, GraphicsAPI.OPENGL):
            sc = api_state.rasterizer.scissors[0]
            return {
                "x": int(sc.x),
                "y": int(sc.y),
                "width": int(sc.width),
                "height": int(sc.height),
            }
        if api == GraphicsAPI.VULKAN:
            vs = api_state.viewportScissor.viewportScissors[0]
            sc = vs.scissor
            return {
                "x": int(sc.x),
                "y": int(sc.y),
                "width": int(sc.width),
                "height": int(sc.height),
            }
    except (AttributeError, IndexError, TypeError) as exc:
        logger.debug("_extract_scissor: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Topology extraction锛圓PI 鐗瑰畾锛?
# ---------------------------------------------------------------------------


def _extract_topology(api_state: Any, api: GraphicsAPI) -> str:
    """杩斿洖鍙鐨?primitive topology 瀛楃涓层€?"""
    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12, GraphicsAPI.VULKAN):
            return str(api_state.inputAssembly.topology)
        if api == GraphicsAPI.OPENGL:
            return str(api_state.vertexInput.topology)
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_topology: %s", exc)
    return ""


# ---------------------------------------------------------------------------
# Resource binding helpers锛堣祫婧愮粦瀹氳緟鍔╋級
# ---------------------------------------------------------------------------


def _collect_bindings_for_stage(
    pipe: Any,
    rd_stage: Any,
    our_stage: ShaderStage,
) -> List[ResourceBindingEntry]:
    """Extract resource bindings for one shader stage across API variants."""
    rd = _get_rd()
    entries: List[ResourceBindingEntry] = []
    seen: set[tuple[int, int, str, str]] = set()

    def _descriptor_kind(raw_type: Any, default_kind: str) -> str:
        try:
            dt = int(raw_type)
        except Exception:
            dt = None
        if dt is None:
            return default_kind
        try:
            if dt == int(rd.DescriptorType.ConstantBuffer):
                return "CBV"
            if dt == int(rd.DescriptorType.Sampler):
                return "Sampler"
            if dt in {
                int(rd.DescriptorType.ReadWriteImage),
                int(rd.DescriptorType.ReadWriteTypedBuffer),
                int(rd.DescriptorType.ReadWriteBuffer),
            }:
                return "UAV"
            if dt in {
                int(rd.DescriptorType.ImageSampler),
                int(rd.DescriptorType.Image),
                int(rd.DescriptorType.Buffer),
                int(rd.DescriptorType.TypedBuffer),
                int(rd.DescriptorType.AccelerationStructure),
            }:
                return "SRV"
        except Exception:
            pass
        return default_kind

    def _append_entry(
        *,
        set_or_space: int,
        binding: int,
        resource_id: str,
        binding_type: str,
        fmt: str = "",
    ) -> None:
        key = (set_or_space, binding, binding_type, resource_id)
        if key in seen:
            return
        seen.add(key)
        entries.append(
            ResourceBindingEntry(
                set_or_space=set_or_space,
                binding=binding,
                resource_id=resource_id,
                resource_name="",
                type=binding_type,
                format=fmt,
            ),
        )

    def _iter_new_descriptors(raw_items: Any, default_kind: str) -> None:
        try:
            items = list(raw_items or [])
        except Exception:
            return
        for item in items:
            access = getattr(item, "access", None)
            descriptor = getattr(item, "descriptor", None)
            if access is None or descriptor is None:
                continue
            try:
                binding = int(getattr(access, "index", 0))
            except Exception:
                binding = 0
            try:
                set_or_space = int(getattr(access, "type", 0))
            except Exception:
                set_or_space = 0
            rid_raw = getattr(descriptor, "resource", None)
            resource_id = ""
            if rid_raw is not None and not _is_null_id(rid_raw):
                resource_id = str(rid_raw)
            kind = _descriptor_kind(getattr(descriptor, "type", None), default_kind)
            fmt = str(getattr(descriptor, "format", "")) if hasattr(descriptor, "format") else ""
            if resource_id or kind in {"Sampler", "CBV"}:
                _append_entry(
                    set_or_space=set_or_space,
                    binding=binding,
                    resource_id=resource_id,
                    binding_type=kind,
                    fmt=fmt,
                )

    def _iter_old_arrays(raw_items: Any, default_kind: str, resource_attr: str) -> None:
        try:
            arrays = list(raw_items or [])
        except Exception:
            return
        for bound_array in arrays:
            bind_point = getattr(bound_array, "bindPoint", None)
            if bind_point is None:
                continue
            set_or_space = int(getattr(bind_point, "bindset", 0)) if hasattr(bind_point, "bindset") else 0
            binding = int(getattr(bind_point, "bind", 0)) if hasattr(bind_point, "bind") else 0
            resources = getattr(bound_array, resource_attr, None)
            if resources is None:
                continue
            for res in resources:
                rid_raw = getattr(res, "resourceId", None)
                resource_id = "" if rid_raw is None or _is_null_id(rid_raw) else str(rid_raw)
                if not resource_id and default_kind not in {"Sampler", "CBV"}:
                    continue
                _append_entry(
                    set_or_space=set_or_space,
                    binding=binding,
                    resource_id=resource_id,
                    binding_type=default_kind,
                )

    # Newer RenderDoc returns UsedDescriptor entries.
    try:
        _iter_new_descriptors(pipe.GetReadOnlyResources(rd_stage), "SRV")
    except Exception:
        pass
    try:
        _iter_new_descriptors(pipe.GetReadWriteResources(rd_stage), "UAV")
    except Exception:
        pass
    try:
        _iter_new_descriptors(pipe.GetConstantBlocks(rd_stage), "CBV")
    except Exception:
        pass

    # Fallback for older bindings arrays.
    if not entries:
        try:
            _iter_old_arrays(pipe.GetReadOnlyResources(rd_stage), "SRV", "resources")
        except Exception:
            pass
        try:
            _iter_old_arrays(pipe.GetReadWriteResources(rd_stage), "UAV", "resources")
        except Exception:
            pass
        try:
            _iter_old_arrays(pipe.GetConstantBlocks(rd_stage), "CBV", "buffers")
        except Exception:
            pass

    return entries
# ---------------------------------------------------------------------------
# Shader reflection serialisation锛堝簭鍒楀寲锛?
# ---------------------------------------------------------------------------


def _reflection_to_dict(refl: Any) -> Dict[str, Any]:
    """灏?``ShaderReflection`` 杞崲涓?JSON-safe 鐨勫瓧鍏搞€?

    浠呬繚鐣欏璋冭瘯鏈€鏈変环鍊肩殑閮ㄥ垎锛歴ignatures銆乧onstant blocks锛?
    浠ュ強 resource bindings锛坣ames銆乼ypes銆乥ind points锛夈€?
    """
    result: Dict[str, Any] = {}

    # ---- Input / output signatures ----
    for sig_name in ("inputSignature", "outputSignature"):
        try:
            sig_list = getattr(refl, sig_name, None)
            if sig_list is not None:
                result[sig_name] = [
                    {
                        "varName": str(s.varName),
                        "semanticName": str(s.semanticName),
                        "semanticIndex": int(s.semanticIndex),
                        "regIndex": int(s.regIndex),
                        "compCount": int(s.compCount),
                        "compType": str(s.compType),
                    }
                    for s in sig_list
                ]
        except (AttributeError, TypeError):
            pass

    # ---- Constant blocks ----
    try:
        cb_list = refl.constantBlocks
        result["constantBlocks"] = [
            {
                "name": str(cb.name),
                "bindPoint": int(cb.bindPoint),
                "byteSize": int(cb.byteSize),
                "variables": [
                    {
                        "name": str(v.name),
                        "type": str(v.type.descriptor.name) if hasattr(v.type, "descriptor") else str(v.type),
                        "byteOffset": int(v.byteOffset),
                    }
                    for v in (cb.variables or [])
                ],
            }
            for cb in cb_list
        ]
    except (AttributeError, TypeError):
        pass

    # ---- Read-only resources ----
    try:
        ro_list = refl.readOnlyResources
        result["readOnlyResources"] = [
            {
                "name": str(r.name),
                "bindPoint": int(r.bindPoint),
                "isTexture": bool(r.isTexture),
                "resType": str(r.resType),
            }
            for r in ro_list
        ]
    except (AttributeError, TypeError):
        pass

    # ---- Read-write resources ----
    try:
        rw_list = refl.readWriteResources
        result["readWriteResources"] = [
            {
                "name": str(r.name),
                "bindPoint": int(r.bindPoint),
                "isTexture": bool(r.isTexture),
                "resType": str(r.resType),
            }
            for r in rw_list
        ]
    except (AttributeError, TypeError):
        pass

    return result


# ---------------------------------------------------------------------------
# PipelineService
# ---------------------------------------------------------------------------


class PipelineService:
    """Pipeline state 妫€鏌ヤ笌 shader artifact 瀵煎嚭銆?

    鎵€鏈夊叕寮€鏂规硶鍧囦负 ``async``锛屾帴鏀舵樉寮忎緷璧栵紝骞惰繑鍥炵粨鏋勫寲鐨?Pydantic models銆?
    """

    # ------------------------------------------------------------------
    # snapshot_pipeline
    # ------------------------------------------------------------------

    async def snapshot_pipeline(
        self,
        session_id: str,
        event_id: int,
        session_manager: SessionManager,
    ) -> PipelineSnapshot:
        """鍦ㄦ寚瀹?event 鎹曡幏瀹屾暣鐨?pipeline state snapshot銆?

        Parameters
        ----------
        session_id:
            娲昏穬 replay session id銆?
        event_id:
            闇€瑕佹鏌ョ殑 API event銆?
        session_manager:
            鎻愪緵 ``ReplayController``銆?

        Returns
        -------
        PipelineSnapshot
            鍖呭惈 shaders銆乺ender targets銆乥lend銆乨epth/stencil銆?
            viewport銆乻cissor銆乼opology 涓?bindings 鐨勫畬鏁?pipeline state銆?
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        pipe = await asyncio.to_thread(controller.GetPipelineState)
        api_props = await asyncio.to_thread(controller.GetAPIProperties)
        api = _map_graphics_api(api_props.pipelineType)

        # API-specific state for blend / depth / viewport / topology.
        api_state = await asyncio.to_thread(
            _get_api_specific_state, controller, api_props.pipelineType,
        )

        # ---- Shaders锛堢潃鑹插櫒锛?----------------------------------------
        shaders: List[ShaderInfo] = []
        for rd_stage in _rd_shader_stages():
            try:
                shader_id = pipe.GetShader(rd_stage)
                if _is_null_id(shader_id):
                    continue

                refl = pipe.GetShaderReflection(rd_stage)
                entry_point = "main"
                encoding = ""
                if refl is not None:
                    if hasattr(refl, "entryPoint"):
                        entry_point = str(refl.entryPoint)
                    if hasattr(refl, "encoding"):
                        encoding = str(refl.encoding)

                shaders.append(ShaderInfo(
                    resource_id=str(shader_id),
                    stage=_map_shader_stage(rd_stage),
                    entry_point=entry_point,
                    encoding=encoding,
                ))
            except Exception as exc:
                logger.debug(
                    "snapshot_pipeline: skipping stage %s: %s",
                    rd_stage, exc,
                )

        # ---- Render targets 涓?depth target -------------------------
        render_targets, depth_target = await _extract_render_targets(
            pipe, api, controller,
        )

        # ---- Blend state锛堟贩鍚堢姸鎬侊級-----------------------------------
        blend_states = _extract_blend_state(api_state, api)

        # ---- Depth / stencil state -----------------------------------
        depth_stencil = _extract_depth_stencil(api_state, api)

        # ---- Viewport / scissor --------------------------------------
        viewport = _extract_viewport(api_state, api)
        scissor = _extract_scissor(api_state, api)

        # ---- Topology ------------------------------------------------
        topology = _extract_topology(api_state, api)

        # ---- Resource bindings锛坅ll stages锛?-------------------------
        bindings: List[ResourceBindingEntry] = []
        for rd_stage in _rd_shader_stages():
            try:
                shader_id = pipe.GetShader(rd_stage)
                if _is_null_id(shader_id):
                    continue
                stage_bindings = _collect_bindings_for_stage(
                    pipe, rd_stage, _map_shader_stage(rd_stage),
                )
                bindings.extend(stage_bindings)
            except Exception:
                pass

        return PipelineSnapshot(
            event_id=event_id,
            api=api,
            shaders=shaders,
            render_targets=render_targets,
            depth_target=depth_target,
            blend_states=blend_states,
            depth_stencil=depth_stencil,
            bindings=bindings,
            viewport=viewport,
            scissor=scissor,
            topology=topology,
        )

    # ------------------------------------------------------------------
    # export_shader
    # ------------------------------------------------------------------

    async def export_shader(
        self,
        session_id: str,
        event_id: int,
        stage: ShaderStage,
        session_manager: SessionManager,
        artifact_store: ArtifactStore,
    ) -> ShaderExportBundle:
        """瀵煎嚭 *stage* 缁戝畾鐨?shader锛氬寘鍚?reflection + disassembly artifacts銆?

        Parameters
        ----------
        session_id:
            娲昏穬 replay session id銆?
        event_id:
            闇€瑕佹鏌ョ殑 API event銆?
        stage:
            鎴戜滑鐨?``ShaderStage`` enum 鍊硷紙渚嬪 ``ShaderStage.PS``锛夈€?
        session_manager:
            鎻愪緵 ``ReplayController``銆?
        artifact_store:
            鐢熸垚 artifacts 鐨勬寔涔呭寲灞傘€?

        Returns
        -------
        ShaderExportBundle
            鍖呭惈 shader reflection JSON 涓?disassembly 鏂囨湰鐨?artifact 寮曠敤锛?
            浠ュ強鐩稿叧 metadata銆?
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        pipe = await asyncio.to_thread(controller.GetPipelineState)
        rd_stage = _our_stage_to_rd(stage)

        shader_id = pipe.GetShader(rd_stage)
        if _is_null_id(shader_id):
            raise ValueError(
                f"No shader bound at stage {stage.value} for event {event_id}"
            )

        refl = pipe.GetShaderReflection(rd_stage)
        if refl is None:
            raise RuntimeError(
                f"Shader reflection unavailable for stage {stage.value} "
                f"at event {event_id}"
            )

        # 纭畾鐢ㄤ簬 disassembly 鐨?pipeline object ResourceId銆?
        pipeline_rid = rd.ResourceId()
        try:
            if stage == ShaderStage.CS:
                pipeline_rid = pipe.GetComputePipelineObject()
            else:
                pipeline_rid = pipe.GetGraphicsPipelineObject()
        except (AttributeError, TypeError):
            # 鍥為€€锛歯ull pipeline锛堝鏁?API 鍙敤锛夈€?
            pass

        # 瑙ｆ瀽 entry point銆?
        entry_point = "main"
        encoding = ""
        if hasattr(refl, "entryPoint"):
            entry_point = str(refl.entryPoint)
        if hasattr(refl, "encoding"):
            encoding = str(refl.encoding)

        # ---- Reflection JSON artifact锛堝弽灏勶級---------------------------
        refl_dict = _reflection_to_dict(refl)
        refl_dict["_meta"] = {
            "event_id": event_id,
            "stage": stage.value,
            "shader_id": str(shader_id),
            "entry_point": entry_point,
            "encoding": encoding,
        }
        refl_bytes = json.dumps(refl_dict, indent=2, default=str).encode()
        refl_artifact = await artifact_store.store(
            refl_bytes,
            mime="application/json",
            suffix=".refl.json",
            meta={
                "event_id": event_id,
                "stage": stage.value,
                "kind": "shader_reflection",
            },
        )

        # ---- Disassembly artifact锛堝弽姹囩紪锛?-----------------------------
        disasm_artifact: Optional[ArtifactRef] = None
        try:
            targets: List[str] = await asyncio.to_thread(
                controller.GetDisassemblyTargets, True,
            )

            if targets:
                # 浼樺厛浣跨敤绗竴涓彲鐢ㄧ殑 disassembly target锛涘悓鏃跺皢鎵€鏈?
                # 鎴愬姛鐨勫弽姹囩紪鍚堝苟鎴愪竴涓甫鍒嗗尯鏍囬鐨勬枃鏈紝渚夸簬涓嬫父閫夋嫨銆?
                sections: List[str] = []
                for target_name in targets:
                    try:
                        disasm_text: str = await asyncio.to_thread(
                            controller.DisassembleShader,
                            pipeline_rid,
                            refl,
                            target_name,
                        )
                        if disasm_text:
                            sections.append(
                                f";;; === {target_name} ===\n"
                                f"{disasm_text}"
                            )
                    except Exception as exc:
                        logger.debug(
                            "export_shader: disassembly target %r failed: %s",
                            target_name, exc,
                        )

                if sections:
                    combined = "\n\n".join(sections)
                    disasm_bytes = combined.encode("utf-8")
                    disasm_artifact = await artifact_store.store(
                        disasm_bytes,
                        mime="text/plain",
                        suffix=".disasm.txt",
                        meta={
                            "event_id": event_id,
                            "stage": stage.value,
                            "kind": "shader_disassembly",
                            "targets": [t for t in targets],
                        },
                    )
        except Exception as exc:
            logger.warning("export_shader: disassembly failed: %s", exc)

        # ---- Compute a content hash for the shader -------------------
        shader_hash = ""
        try:
            # 灏?reflection JSON 鐨?hash 浣滀负绋冲畾 identity銆?
            shader_hash = hashlib.sha256(refl_bytes).hexdigest()[:16]
        except Exception:
            pass

        return ShaderExportBundle(
            shader_id=str(shader_id),
            stage=stage,
            entry_point=entry_point,
            encoding=encoding,
            reflection_artifact=refl_artifact,
            disasm_artifact=disasm_artifact,
        )

    # ------------------------------------------------------------------
    # get_resource_bindings
    # ------------------------------------------------------------------

    async def get_resource_bindings(
        self,
        session_id: str,
        event_id: int,
        session_manager: SessionManager,
    ) -> List[ResourceBindingEntry]:
        """杩斿洖鎵€鏈夋椿璺?shader stages 鐨勮祫婧愮粦瀹氥€?

        Parameters
        ----------
        session_id:
            娲昏穬 replay session id銆?
        event_id:
            闇€瑕佹鏌ョ殑 API event銆?
        session_manager:
            鎻愪緵 ``ReplayController``銆?

        Returns
        -------
        list[ResourceBindingEntry]
            鎵€鏈夋椿璺?stages 鐨勭粦瀹氳祫婧愭墎骞冲垪琛ㄣ€?
        """
        rd = _get_rd()
        controller = session_manager.get_controller(session_id)

        await asyncio.to_thread(controller.SetFrameEvent, event_id, True)

        pipe = await asyncio.to_thread(controller.GetPipelineState)

        entries: List[ResourceBindingEntry] = []
        for rd_stage in _rd_shader_stages():
            try:
                shader_id = pipe.GetShader(rd_stage)
                if _is_null_id(shader_id):
                    continue

                our_stage = _map_shader_stage(rd_stage)
                stage_entries = _collect_bindings_for_stage(
                    pipe, rd_stage, our_stage,
                )

                # 鑻ュ彲鐢紝鍒欑敤 reflection 濉厖 resource names銆?
                refl = pipe.GetShaderReflection(rd_stage)
                if refl is not None:
                    _enrich_binding_names(stage_entries, refl)

                entries.extend(stage_entries)
            except Exception as exc:
                logger.debug(
                    "get_resource_bindings: stage %s: %s", rd_stage, exc,
                )

        return entries


# ---------------------------------------------------------------------------
# Post-processing helpers锛堝悗澶勭悊杈呭姪锛?
# ---------------------------------------------------------------------------


def _enrich_binding_names(
    entries: List[ResourceBindingEntry],
    refl: Any,
) -> None:
    """浠?shader reflection 濉厖 ``resource_name`` 涓?``format``銆?

    閫氳繃 binding index 涓?reflection 鐨勮祫婧愬垪琛ㄥ尮閰嶏紝骞跺師鍦颁慨鏀?*entries*銆?
    """
    # 浠?reflection 鏋勫缓蹇€熸煡鎵捐〃銆?
    ro_by_bind: Dict[int, Any] = {}
    rw_by_bind: Dict[int, Any] = {}
    cb_by_bind: Dict[int, Any] = {}

    try:
        for r in (refl.readOnlyResources or []):
            ro_by_bind[int(r.bindPoint)] = r
    except (AttributeError, TypeError):
        pass
    try:
        for r in (refl.readWriteResources or []):
            rw_by_bind[int(r.bindPoint)] = r
    except (AttributeError, TypeError):
        pass
    try:
        for cb in (refl.constantBlocks or []):
            cb_by_bind[int(cb.bindPoint)] = cb
    except (AttributeError, TypeError):
        pass

    for entry in entries:
        b = entry.binding
        if entry.type == "SRV" and b in ro_by_bind:
            r = ro_by_bind[b]
            entry.resource_name = str(r.name)
            if hasattr(r, "resType"):
                entry.format = str(r.resType)
        elif entry.type == "UAV" and b in rw_by_bind:
            r = rw_by_bind[b]
            entry.resource_name = str(r.name)
            if hasattr(r, "resType"):
                entry.format = str(r.resType)
        elif entry.type == "CBV" and b in cb_by_bind:
            entry.resource_name = str(cb_by_bind[b].name)

