"""Pipeline state inspection and shader artifact export service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
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


@dataclass
class ShaderBindingResolution:
    """Resolved event-bound shader identity shared by pipeline/shader/patch tools."""

    shader_id: Any = None
    reflection: Any = None
    entry_point: str = "main"
    stage: Optional[ShaderStage] = None
    pipeline_id: Any = None
    resolution_source: str = "unresolved"
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    stage_object: Any = None

    @property
    def found(self) -> bool:
        return not _is_null_id(self.shader_id)

    def to_diagnostics(self) -> Dict[str, Any]:
        payload = dict(self.diagnostics)
        payload.update(
            {
                "shader_id": "" if _is_null_id(self.shader_id) else str(self.shader_id),
                "pipeline_id": "" if _is_null_id(self.pipeline_id) else str(self.pipeline_id),
                "entry_point": str(self.entry_point or "main"),
                "stage": str((self.stage.value if self.stage is not None else "")),
                "resolution_source": self.resolution_source,
            },
        )
        return payload

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
    raw_text = str(rd_api or "").strip().lower()
    numeric_map: Dict[int, GraphicsAPI] = {
        0: GraphicsAPI.D3D11,
        1: GraphicsAPI.D3D12,
        2: GraphicsAPI.OPENGL,
        3: GraphicsAPI.VULKAN,
    }
    if raw_text.isdigit():
        mapped = numeric_map.get(int(raw_text))
        if mapped is not None:
            return mapped
    try:
        raw_int = int(rd_api)
    except (TypeError, ValueError):
        raw_int = None
    if raw_int is not None:
        if raw_int in numeric_map:
            return numeric_map[raw_int]
    for key, value in {
        "d3d11": GraphicsAPI.D3D11,
        "d3d12": GraphicsAPI.D3D12,
        "opengl": GraphicsAPI.OPENGL,
        "vulkan": GraphicsAPI.VULKAN,
    }.items():
        if key in raw_text:
            return value
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
    if resource_id is None:
        return True
    text = str(resource_id).strip().lower()
    if text in {"", "0", "none", "resourceid::0"}:
        return True
    rd = _get_rd()
    try:
        return resource_id == rd.ResourceId()
    except Exception:
        return False


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


def _get_api_specific_state_by_api(controller: Any, api: GraphicsAPI) -> Any:
    """Return API-specific state using RDX's normalized API value."""
    if api == GraphicsAPI.D3D11:
        return controller.GetD3D11PipelineState()
    if api == GraphicsAPI.D3D12:
        return controller.GetD3D12PipelineState()
    if api == GraphicsAPI.VULKAN:
        return controller.GetVulkanPipelineState()
    if api == GraphicsAPI.OPENGL:
        return controller.GetOpenGLPipelineState()
    return None


def _api_state_shader_score(api_state: Any, api: GraphicsAPI) -> int:
    if api_state is None:
        return 0
    score = 0
    for rd_stage in _rd_shader_stages():
        stage_obj = _stage_object_from_api_state(api_state, api, rd_stage)
        if not _is_null_id(_shader_id_from_stage_object(stage_obj)):
            score += 100
    for attr in (
        "pipelineResourceId",
        "pipelineComputeLayoutResourceId",
        "pipelinePreRastLayoutResourceId",
        "pipelineFragmentLayoutResourceId",
    ):
        if not _is_null_id(getattr(api_state, attr, None)):
            score += 10
    for attr in ("graphics", "compute"):
        pipeline = getattr(api_state, attr, None)
        if pipeline is not None and not _is_null_id(getattr(pipeline, "pipelineResourceId", None)):
            score += 10
    topology = _extract_topology(api_state, api)
    if topology:
        score += 1
    if _extract_viewport(api_state, api):
        score += 1
    if _extract_scissor(api_state, api):
        score += 1
    return score


def _select_api_specific_state(controller: Any, preferred_api: GraphicsAPI) -> Tuple[GraphicsAPI, Any]:
    """Pick the API state that actually exposes the loaded capture pipeline."""
    candidates: List[GraphicsAPI] = []
    for api in (GraphicsAPI.VULKAN, GraphicsAPI.D3D12, GraphicsAPI.OPENGL, GraphicsAPI.D3D11):
        if api not in candidates:
            candidates.append(api)
    if preferred_api != GraphicsAPI.UNKNOWN and preferred_api not in candidates:
        candidates.append(preferred_api)

    best_api = preferred_api
    best_state = None
    best_score = -1
    for api in candidates:
        try:
            state = _get_api_specific_state_by_api(controller, api)
        except Exception:
            continue
        score = _api_state_shader_score(state, api)
        if score > best_score:
            best_api = api
            best_state = state
            best_score = score
        if score > 0:
            break
    return best_api, best_state


def _stage_object_from_api_state(api_state: Any, api: GraphicsAPI, rd_stage: Any) -> Any:
    """Find an API-specific stage object when generic PipelineState is empty."""
    rd = _get_rd()
    if api == GraphicsAPI.VULKAN:
        stage_attrs = {
            rd.ShaderStage.Vertex: "vertexShader",
            rd.ShaderStage.Hull: "tessControlShader",
            rd.ShaderStage.Domain: "tessEvalShader",
            rd.ShaderStage.Geometry: "geometryShader",
            rd.ShaderStage.Pixel: "fragmentShader",
            rd.ShaderStage.Compute: "computeShader",
        }
    else:
        stage_attrs = {
            rd.ShaderStage.Vertex: "vertexShader",
            rd.ShaderStage.Hull: "hullShader",
            rd.ShaderStage.Domain: "domainShader",
            rd.ShaderStage.Geometry: "geometryShader",
            rd.ShaderStage.Pixel: "pixelShader",
            rd.ShaderStage.Compute: "computeShader",
        }
    attr = stage_attrs.get(rd_stage)
    if not attr or api_state is None:
        return None
    return getattr(api_state, attr, None)


def _shader_id_from_stage_object(stage_obj: Any) -> Any:
    if stage_obj is None:
        return None
    for attr in ("shaderResourceId", "resourceId", "shaderId"):
        rid = getattr(stage_obj, attr, None)
        if not _is_null_id(rid):
            return rid
    return _shader_id_from_reflection(getattr(stage_obj, "reflection", None))


def _shader_id_from_reflection(refl: Any) -> Any:
    if refl is None:
        return None
    for attr in ("resourceId", "shaderResourceId", "shaderId"):
        rid = getattr(refl, attr, None)
        if not _is_null_id(rid):
            return rid
    return None


def _shader_reflection_from_stage_object(stage_obj: Any) -> Any:
    if stage_obj is None:
        return None
    return getattr(stage_obj, "reflection", None)


def _shader_entry_from_stage_object(stage_obj: Any) -> str:
    if stage_obj is None:
        return "main"
    return str(getattr(stage_obj, "entryPoint", "") or "main")


def _api_specific_pipeline_object(api_state: Any, api: GraphicsAPI, stage: ShaderStage) -> Any:
    rd = _get_rd()
    if api_state is None:
        return rd.ResourceId()
    if stage == ShaderStage.CS:
        for attr in ("pipelineResourceId", "pipelineComputeLayoutResourceId"):
            rid = getattr(api_state, attr, None)
            if not _is_null_id(rid):
                return rid
        compute = getattr(api_state, "compute", None)
        rid = getattr(compute, "pipelineResourceId", None) if compute is not None else None
        if not _is_null_id(rid):
            return rid
    for attr in (
        "pipelineResourceId",
        "pipelineFragmentLayoutResourceId",
        "pipelinePreRastLayoutResourceId",
    ):
        rid = getattr(api_state, attr, None)
        if not _is_null_id(rid):
            return rid
    graphics = getattr(api_state, "graphics", None)
    rid = getattr(graphics, "pipelineResourceId", None) if graphics is not None else None
    if not _is_null_id(rid):
        return rid
    return rd.ResourceId()


def _pipeline_object_from_pipe(pipe: Any, stage: ShaderStage) -> Any:
    rd = _get_rd()
    try:
        if stage == ShaderStage.CS and hasattr(pipe, "GetComputePipelineObject"):
            rid = pipe.GetComputePipelineObject()
            if not _is_null_id(rid):
                return rid
        if hasattr(pipe, "GetGraphicsPipelineObject"):
            rid = pipe.GetGraphicsPipelineObject()
            if not _is_null_id(rid):
                return rid
    except Exception:
        pass
    return rd.ResourceId()


def _resource_id_keys(resource_id: Any) -> set[str]:
    if resource_id is None:
        return set()
    text = str(resource_id).strip()
    keys = {text}
    if "::" in text:
        keys.add(text.split("::", 1)[1])
    try:
        keys.add(str(int(resource_id)))
    except Exception:
        pass
    return {key for key in keys if key and key != "0"}


def _resource_ids_equal(left: Any, right: Any) -> bool:
    if _is_null_id(left) or _is_null_id(right):
        return False
    return bool(_resource_id_keys(left) & _resource_id_keys(right))


def _entry_point_name(entry: Any) -> str:
    for attr in ("name", "entryPoint"):
        value = getattr(entry, attr, None)
        if value:
            return str(value)
    if isinstance(entry, str):
        return entry
    return ""


def _stage_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "0": "vertex",
        "1": "hull",
        "2": "domain",
        "3": "geometry",
        "4": "pixel",
        "5": "compute",
        "fragment": "pixel",
        "frag": "pixel",
        "ps": "pixel",
        "pixel": "pixel",
        "vs": "vertex",
        "cs": "compute",
    }
    if text in aliases:
        return aliases[text]
    for key, alias in aliases.items():
        if key and key in text:
            return alias
    return text


def _entry_stage_matches(entry: Any, rd_stage: Any) -> bool:
    stage_value = getattr(entry, "stage", None)
    if stage_value is None:
        return True
    return _stage_key(stage_value) == _stage_key(rd_stage)


def _make_entry_point(name: str, rd_stage: Any) -> Any:
    rd = _get_rd()
    entry_name = str(name or "main") or "main"
    try:
        entry = rd.ShaderEntryPoint()
        entry.name = entry_name
        entry.stage = rd_stage
        return entry
    except Exception:
        return {"name": entry_name, "stage": rd_stage}


def _resource_parent_ids(controller: Any, pipeline_id: Any) -> Tuple[List[Any], Dict[str, Any]]:
    diag: Dict[str, Any] = {"pipeline_resource_found": False, "parent_shader_candidates": []}
    if _is_null_id(pipeline_id) or not hasattr(controller, "GetResources"):
        return [], diag
    try:
        resources = list(controller.GetResources() or [])
    except Exception as exc:
        diag["resource_query_error"] = f"{type(exc).__name__}: {exc}"
        return [], diag
    for resource in resources:
        rid = getattr(resource, "resourceId", None)
        if not _resource_ids_equal(rid, pipeline_id):
            continue
        diag["pipeline_resource_found"] = True
        parents = list(getattr(resource, "parentResources", None) or [])
        diag["parent_shader_candidates"] = [str(parent) for parent in parents if not _is_null_id(parent)]
        return [parent for parent in parents if not _is_null_id(parent)], diag
    return [], diag


def resolve_shader_binding(
    controller: Any,
    pipe: Any,
    api_state: Any,
    api: GraphicsAPI,
    rd_stage: Any,
    our_stage: Optional[ShaderStage] = None,
) -> ShaderBindingResolution:
    """Resolve the shader bound at the current event using all RenderDoc state layers."""
    rd = _get_rd()
    stage = our_stage or _map_shader_stage(rd_stage)
    diagnostics: Dict[str, Any] = {
        "selected_api": str(getattr(api, "value", api)),
        "api_state_type": type(api_state).__name__ if api_state is not None else "",
        "shader_id_candidates": [],
        "candidate_errors": [],
    }

    pipeline_id = _pipeline_object_from_pipe(pipe, stage)
    if _is_null_id(pipeline_id):
        pipeline_id = _api_specific_pipeline_object(api_state, api, stage)
    diagnostics["pipeline_id"] = "" if _is_null_id(pipeline_id) else str(pipeline_id)

    stage_obj = _stage_object_from_api_state(api_state, api, rd_stage)
    diagnostics["stage_object_type"] = type(stage_obj).__name__ if stage_obj is not None else ""
    diagnostics["stage_attrs"] = [
        attr
        for attr in ("resourceId", "shaderResourceId", "shaderId", "reflection", "entryPoint")
        if stage_obj is not None and hasattr(stage_obj, attr)
    ]

    shader_id = None
    reflection = None
    try:
        shader_id = pipe.GetShader(rd_stage)
        diagnostics["shader_id_candidates"].append({"source": "pipeline_state", "shader_id": str(shader_id or "")})
    except Exception as exc:
        diagnostics["candidate_errors"].append(f"PipelineState.GetShader: {type(exc).__name__}: {exc}")
    try:
        reflection = pipe.GetShaderReflection(rd_stage)
    except Exception as exc:
        diagnostics["candidate_errors"].append(f"PipelineState.GetShaderReflection: {type(exc).__name__}: {exc}")
    if _is_null_id(shader_id):
        shader_id = _shader_id_from_reflection(reflection)
        if not _is_null_id(shader_id):
            diagnostics["shader_id_candidates"].append({"source": "pipeline_state_reflection", "shader_id": str(shader_id)})
    if not _is_null_id(shader_id):
        if reflection is None:
            reflection = _shader_reflection_from_stage_object(stage_obj)
        entry = str(getattr(reflection, "entryPoint", "") or _shader_entry_from_stage_object(stage_obj) or "main")
        return ShaderBindingResolution(
            shader_id=shader_id,
            reflection=reflection,
            entry_point=entry,
            stage=stage,
            pipeline_id=pipeline_id,
            resolution_source="pipeline_state_shader",
            diagnostics=diagnostics,
            stage_object=stage_obj,
        )

    stage_reflection = _shader_reflection_from_stage_object(stage_obj)
    stage_shader_id = _shader_id_from_stage_object(stage_obj)
    if not _is_null_id(stage_shader_id):
        diagnostics["shader_id_candidates"].append({"source": "api_stage_object", "shader_id": str(stage_shader_id)})
        return ShaderBindingResolution(
            shader_id=stage_shader_id,
            reflection=stage_reflection,
            entry_point=_shader_entry_from_stage_object(stage_obj),
            stage=stage,
            pipeline_id=pipeline_id,
            resolution_source="api_specific_stage_object",
            diagnostics=diagnostics,
            stage_object=stage_obj,
        )

    stage_entry = str(
        getattr(stage_reflection, "entryPoint", "")
        or _shader_entry_from_stage_object(stage_obj)
        or "main"
    )
    parent_ids, parent_diag = _resource_parent_ids(controller, pipeline_id)
    diagnostics.update(parent_diag)
    get_entry_points = getattr(controller, "GetShaderEntryPoints", None)
    get_shader = getattr(controller, "GetShader", None)
    if callable(get_shader):
        for candidate_id in parent_ids:
            entries: List[Any] = []
            if callable(get_entry_points):
                try:
                    entries = [
                        entry
                        for entry in list(get_entry_points(candidate_id) or [])
                        if _entry_stage_matches(entry, rd_stage)
                    ]
                except Exception as exc:
                    diagnostics["candidate_errors"].append(
                        f"GetShaderEntryPoints({candidate_id}): {type(exc).__name__}: {exc}",
                    )
            if not entries:
                entries = [_make_entry_point(stage_entry or "main", rd_stage)]
            preferred_entries = [
                entry
                for entry in entries
                if _entry_point_name(entry) in {stage_entry, "main", ""}
            ] or entries
            for entry in preferred_entries:
                entry_name = _entry_point_name(entry) or stage_entry or "main"
                diagnostics["shader_id_candidates"].append(
                    {
                        "source": "pipeline_parent_shader_module",
                        "shader_id": str(candidate_id),
                        "entry_point": entry_name,
                    },
                )
                try:
                    refl = get_shader(pipeline_id, candidate_id, entry)
                except TypeError:
                    try:
                        refl = get_shader(pipeline_id, candidate_id, entry_name)
                    except Exception as exc:
                        diagnostics["candidate_errors"].append(
                            f"GetShader({candidate_id}, {entry_name}): {type(exc).__name__}: {exc}",
                        )
                        continue
                except Exception as exc:
                    diagnostics["candidate_errors"].append(
                        f"GetShader({candidate_id}, {entry_name}): {type(exc).__name__}: {exc}",
                    )
                    continue
                if refl is None:
                    continue
                refl_shader_id = _shader_id_from_reflection(refl)
                return ShaderBindingResolution(
                    shader_id=candidate_id if not _is_null_id(candidate_id) else refl_shader_id,
                    reflection=refl,
                    entry_point=str(getattr(refl, "entryPoint", "") or entry_name or "main"),
                    stage=stage,
                    pipeline_id=pipeline_id,
                    resolution_source="pipeline_parent_shader_module",
                    diagnostics=diagnostics,
                    stage_object=stage_obj,
                )

    return ShaderBindingResolution(
        shader_id=rd.ResourceId(),
        reflection=stage_reflection,
        entry_point=stage_entry or "main",
        stage=stage,
        pipeline_id=pipeline_id,
        resolution_source="unresolved",
        diagnostics=diagnostics,
        stage_object=stage_obj,
    )


def _resolve_bound_stage(
    pipe: Any,
    api_state: Any,
    api: GraphicsAPI,
    rd_stage: Any,
    controller: Any = None,
) -> Tuple[Any, Any, Any]:
    resolution = resolve_shader_binding(controller, pipe, api_state, api, rd_stage)
    return resolution.shader_id, resolution.reflection, resolution.stage_object


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

        # APIProperties can describe the replay renderer on remote sessions.
        # Probe API-specific states and use the one that exposes real stages.
        api, api_state = await asyncio.to_thread(
            _select_api_specific_state, controller, api,
        )

        shaders: List[ShaderInfo] = []
        for rd_stage in _rd_shader_stages():
            try:
                resolution = await asyncio.to_thread(
                    resolve_shader_binding,
                    controller,
                    pipe,
                    api_state,
                    api,
                    rd_stage,
                    _map_shader_stage(rd_stage),
                )
                if not resolution.found:
                    continue
                refl = resolution.reflection
                entry_point = resolution.entry_point or "main"
                encoding = ""
                if refl is not None:
                    if hasattr(refl, "encoding"):
                        encoding = str(refl.encoding)

                shaders.append(ShaderInfo(
                    resource_id=str(resolution.shader_id),
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
                resolution = await asyncio.to_thread(
                    resolve_shader_binding,
                    controller,
                    pipe,
                    api_state,
                    api,
                    rd_stage,
                    _map_shader_stage(rd_stage),
                )
                if not resolution.found:
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
        api_props = await asyncio.to_thread(controller.GetAPIProperties)
        api = _map_graphics_api(api_props.pipelineType)
        api, api_state = await asyncio.to_thread(
            _select_api_specific_state, controller, api,
        )
        rd_stage = _our_stage_to_rd(stage)

        resolution = await asyncio.to_thread(
            resolve_shader_binding,
            controller,
            pipe,
            api_state,
            api,
            rd_stage,
            stage,
        )
        shader_id, refl = resolution.shader_id, resolution.reflection
        if _is_null_id(shader_id):
            raise ValueError(
                f"No shader bound at stage {stage.value} for event {event_id}"
            )

        if refl is None:
            raise RuntimeError(
                f"Shader reflection unavailable for stage {stage.value} "
                f"at event {event_id}"
            )

        pipeline_rid = resolution.pipeline_id
        if _is_null_id(pipeline_rid):
            pipeline_rid = rd.ResourceId()

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
            "pipeline_id": str(pipeline_rid),
            "resolution_source": resolution.resolution_source,
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
        api_props = await asyncio.to_thread(controller.GetAPIProperties)
        api = _map_graphics_api(api_props.pipelineType)
        api, api_state = await asyncio.to_thread(
            _select_api_specific_state, controller, api,
        )

        entries: List[ResourceBindingEntry] = []
        for rd_stage in _rd_shader_stages():
            try:
                our_stage = _map_shader_stage(rd_stage)
                resolution = await asyncio.to_thread(
                    resolve_shader_binding,
                    controller,
                    pipe,
                    api_state,
                    api,
                    rd_stage,
                    our_stage,
                )
                if not resolution.found:
                    continue

                stage_entries = _collect_bindings_for_stage(
                    pipe, rd_stage, our_stage,
                )

                if resolution.reflection is not None:
                    _enrich_binding_names(stage_entries, resolution.reflection)

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

