"""Pipeline state inspection and shader artifact export service."""

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
# ---------------------------------------------------------------------------

_rd_module: Any = None


def _get_rd() -> Any:
    """Internal helper."""
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
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionManager(Protocol):
    """Internal helper."""

    def get_controller(self, session_id: str) -> Any:
        """Internal helper."""
        ...

    def get_output(self, session_id: str) -> Any:
        """Internal helper."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Internal helper."""

    async def store(
        self,
        data: bytes,
        *,
        mime: str,
        suffix: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """Internal helper."""
        ...


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_GRAPHICS_STAGES: Tuple[str, ...] = (
    "Vertex",
    "Hull",
    "Domain",
    "Geometry",
    "Pixel",
)

_COMPUTE_STAGES: Tuple[str, ...] = ("Compute",)


def _rd_shader_stages() -> List[Any]:
    """Internal helper."""
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
    """Internal helper."""
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
    """Internal helper."""
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
# ---------------------------------------------------------------------------


def _map_graphics_api(rd_api: Any) -> GraphicsAPI:
    """Internal helper."""
    rd = _get_rd()
    mapping: Dict[Any, GraphicsAPI] = {
        rd.GraphicsAPI.D3D11: GraphicsAPI.D3D11,
        rd.GraphicsAPI.D3D12: GraphicsAPI.D3D12,
        rd.GraphicsAPI.Vulkan: GraphicsAPI.VULKAN,
        rd.GraphicsAPI.OpenGL: GraphicsAPI.OPENGL,
    }
    return mapping.get(rd_api, GraphicsAPI.UNKNOWN)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _is_null_id(resource_id: Any) -> bool:
    """Internal helper."""
    rd = _get_rd()
    try:
        return resource_id == rd.ResourceId()
    except Exception:
        return resource_id is None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _get_api_specific_state(controller: Any, rd_api: Any) -> Any:
    """Internal helper."""
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
# ---------------------------------------------------------------------------


def _extract_blend_state(api_state: Any, api: GraphicsAPI) -> List[BlendState]:
    """Internal helper."""
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
# ---------------------------------------------------------------------------


def _extract_depth_stencil(
    api_state: Any,
    api: GraphicsAPI,
) -> DepthStencilState:
    """Internal helper."""
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
# ---------------------------------------------------------------------------


async def _extract_render_targets(
    pipe_state: Any,
    api: GraphicsAPI,
    controller: Any,
) -> Tuple[List[RenderTargetInfo], Optional[RenderTargetInfo]]:
    """Internal helper."""
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
# ---------------------------------------------------------------------------


def _extract_viewport(api_state: Any, api: GraphicsAPI) -> Dict[str, float]:
    """Internal helper."""
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
    """Internal helper."""
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
# ---------------------------------------------------------------------------


def _extract_topology(api_state: Any, api: GraphicsAPI) -> str:
    """Internal helper."""
    try:
        if api in (GraphicsAPI.D3D11, GraphicsAPI.D3D12, GraphicsAPI.VULKAN):
            return str(api_state.inputAssembly.topology)
        if api == GraphicsAPI.OPENGL:
            return str(api_state.vertexInput.topology)
    except (AttributeError, TypeError) as exc:
        logger.debug("_extract_topology: %s", exc)
    return ""


# ---------------------------------------------------------------------------
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
# ---------------------------------------------------------------------------


def _reflection_to_dict(refl: Any) -> Dict[str, Any]:
    """Internal helper."""
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
    """Internal helper."""

    # ------------------------------------------------------------------
    # snapshot_pipeline
    # ------------------------------------------------------------------

    async def snapshot_pipeline(
        self,
        session_id: str,
        event_id: int,
        session_manager: SessionManager,
    ) -> PipelineSnapshot:
        """Internal helper."""
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

        render_targets, depth_target = await _extract_render_targets(
            pipe, api, controller,
        )

        blend_states = _extract_blend_state(api_state, api)

        # ---- Depth / stencil state -----------------------------------
        depth_stencil = _extract_depth_stencil(api_state, api)

        # ---- Viewport / scissor --------------------------------------
        viewport = _extract_viewport(api_state, api)
        scissor = _extract_scissor(api_state, api)

        # ---- Topology ------------------------------------------------
        topology = _extract_topology(api_state, api)

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
        """Internal helper."""
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

        pipeline_rid = rd.ResourceId()
        try:
            if stage == ShaderStage.CS:
                pipeline_rid = pipe.GetComputePipelineObject()
            else:
                pipeline_rid = pipe.GetGraphicsPipelineObject()
        except (AttributeError, TypeError):
            pass

        entry_point = "main"
        encoding = ""
        if hasattr(refl, "entryPoint"):
            entry_point = str(refl.entryPoint)
        if hasattr(refl, "encoding"):
            encoding = str(refl.encoding)

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

        disasm_artifact: Optional[ArtifactRef] = None
        try:
            targets: List[str] = await asyncio.to_thread(
                controller.GetDisassemblyTargets, True,
            )

            if targets:
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
        """Internal helper."""
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
# ---------------------------------------------------------------------------


def _enrich_binding_names(
    entries: List[ResourceBindingEntry],
    refl: Any,
) -> None:
    """Internal helper."""
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

