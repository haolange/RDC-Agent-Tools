"""
RDX-MCP 的核心数据模型。
供 MCP tools 与内部服务使用的结构化类型集合。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _ts() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BackendType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class GraphicsAPI(str, Enum):
    D3D11 = "D3D11"
    D3D12 = "D3D12"
    VULKAN = "Vulkan"
    OPENGL = "OpenGL"
    OPENGLES = "OpenGLES"
    UNKNOWN = "Unknown"


class ShaderStage(str, Enum):
    VS = "vs"
    HS = "hs"
    DS = "ds"
    GS = "gs"
    PS = "ps"
    CS = "cs"
    MS = "ms"
    AS = "as"


class BugType(str, Enum):
    NANINF = "naninf"
    PRECISION = "precision"
    BINDING_ERROR = "binding_error"
    TRANSPARENCY = "transparency"
    VERTEX_DEFORM = "vertex_deform"
    COLORSPACE = "colorspace"
    PERFORMANCE = "performance"
    UNKNOWN = "unknown"


class VerifierType(str, Enum):
    NANINF = "naninf"
    IMAGE_DIFF = "image_diff"
    PIXEL_STATS = "pixel_stats"
    BINDING_DIFF = "binding_diff"
    COUNTER_ANOMALY = "counter_anomaly"
    CUSTOM = "custom"


class PatchType(str, Enum):
    PRECISION = "precision"
    GUARD = "guard"
    EXPR_REPLACE = "expr_replace"


class ExperimentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class VerdictResult(str, Enum):
    IMPROVED = "improved"
    FIXED = "fixed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class BisectStrategy(str, Enum):
    BINARY = "binary"
    DDMIN = "ddmin"


# ---------------------------------------------------------------------------
# 通用响应封装（response envelope）
# ---------------------------------------------------------------------------

class ArtifactRef(BaseModel):
    uri: str
    sha256: str
    mime: str
    bytes: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ToolResponse(BaseModel):
    ok: bool
    trace_id: str = Field(default_factory=lambda: _new_id("trc"))
    error: Optional[ErrorDetail] = None
    artifact: Optional[ArtifactRef] = None


# ---------------------------------------------------------------------------
# Session（会话）
# ---------------------------------------------------------------------------

class SessionCapabilities(BaseModel):
    api: GraphicsAPI = GraphicsAPI.UNKNOWN
    shader_debug_supported: bool = False
    counters_supported: bool = False
    patch_supported: bool = False
    remote: bool = False


class SessionInfo(BaseModel):
    session_id: str
    backend_type: BackendType
    capabilities: SessionCapabilities = Field(default_factory=SessionCapabilities)
    created_at: float = Field(default_factory=_ts)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class CaptureInfo(BaseModel):
    capture_id: str
    session_id: str
    rdc_path: str
    api: GraphicsAPI = GraphicsAPI.UNKNOWN
    driver_name: str = ""
    driver_version: str = ""
    frame_count: int = 1
    total_events: int = 0
    thumbnails: List[ArtifactRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Event tree（事件树）
# ---------------------------------------------------------------------------

class EventFlags(BaseModel):
    is_draw: bool = False
    is_dispatch: bool = False
    is_marker: bool = False
    is_copy: bool = False
    is_resolve: bool = False
    is_clear: bool = False
    is_pass_boundary: bool = False


class EventNode(BaseModel):
    event_id: int
    name: str = ""
    flags: EventFlags = Field(default_factory=EventFlags)
    children: List[EventNode] = Field(default_factory=list)
    depth: int = 0
    output_targets: List[str] = Field(default_factory=list)  # ResourceId strings
    inferred_pass: Optional[str] = None


# ---------------------------------------------------------------------------
# Anomaly（异常）
# ---------------------------------------------------------------------------

class BBox(BaseModel):
    x0: int
    y0: int
    x1: int
    y1: int


class AnomalyInfo(BaseModel):
    anomaly_id: str = Field(default_factory=lambda: _new_id("anom"))
    type: str = "naninf"
    bbox: Optional[BBox] = None
    nan_count: int = 0
    inf_count: int = 0
    total_pixels: int = 0
    density: float = 0.0
    stats: Dict[str, Any] = Field(default_factory=dict)
    mask_artifact: Optional[ArtifactRef] = None


# ---------------------------------------------------------------------------
# Hypothesis（假设）
# ---------------------------------------------------------------------------

class Hypothesis(BaseModel):
    hypothesis_id: str = Field(default_factory=lambda: _new_id("hyp"))
    title: str
    description: str = ""
    bug_types: List[BugType] = Field(default_factory=list)
    proposed_patches: List[str] = Field(default_factory=list)  # PatchSpec references
    priority_score: float = 0.0
    result: Optional[VerdictResult] = None


# ---------------------------------------------------------------------------
# Pipeline / Shader
# ---------------------------------------------------------------------------

class ShaderInfo(BaseModel):
    resource_id: str
    stage: ShaderStage
    entry_point: str = "main"
    hash: str = ""
    encoding: str = ""  # SPIR-V, DXBC, DXIL, GLSL, etc.


class ResourceBindingEntry(BaseModel):
    set_or_space: int = 0
    binding: int = 0
    resource_id: str = ""
    resource_name: str = ""
    type: str = ""  # SRV, UAV, CBV, sampler, etc.
    format: str = ""


class BlendState(BaseModel):
    enabled: bool = False
    src_color: str = ""
    dst_color: str = ""
    color_op: str = ""
    src_alpha: str = ""
    dst_alpha: str = ""
    alpha_op: str = ""


class DepthStencilState(BaseModel):
    depth_test_enabled: bool = False
    depth_write_enabled: bool = False
    depth_func: str = ""
    stencil_enabled: bool = False


class RenderTargetInfo(BaseModel):
    resource_id: str
    format: str = ""
    width: int = 0
    height: int = 0
    is_srgb: bool = False


class PipelineSnapshot(BaseModel):
    event_id: int
    api: GraphicsAPI = GraphicsAPI.UNKNOWN
    shaders: List[ShaderInfo] = Field(default_factory=list)
    render_targets: List[RenderTargetInfo] = Field(default_factory=list)
    depth_target: Optional[RenderTargetInfo] = None
    blend_states: List[BlendState] = Field(default_factory=list)
    depth_stencil: DepthStencilState = Field(default_factory=DepthStencilState)
    bindings: List[ResourceBindingEntry] = Field(default_factory=list)
    viewport: Dict[str, float] = Field(default_factory=dict)
    scissor: Dict[str, int] = Field(default_factory=dict)
    topology: str = ""
    vertex_inputs: List[Dict[str, Any]] = Field(default_factory=list)


class ShaderExportBundle(BaseModel):
    shader_id: str
    stage: ShaderStage
    entry_point: str = "main"
    encoding: str = ""
    reflection_artifact: Optional[ArtifactRef] = None
    disasm_artifact: Optional[ArtifactRef] = None
    ir_artifact: Optional[ArtifactRef] = None
    source_artifact: Optional[ArtifactRef] = None
    compile_flags: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shader debug trace（Shader 调试轨迹）
# ---------------------------------------------------------------------------

class DebugStep(BaseModel):
    step_index: int
    instruction: str = ""
    registers: Dict[str, Any] = Field(default_factory=dict)
    is_naninf: bool = False


class PixelDebugResult(BaseModel):
    ok: bool = True
    trace_artifact: Optional[ArtifactRef] = None
    total_steps: int = 0
    naninf_step: Optional[DebugStep] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Patch（补丁）
# ---------------------------------------------------------------------------

class PatchOp(BaseModel):
    op: str  # force_full_precision, insert_guard, replace_expr
    variables: List[str] = Field(default_factory=list)
    range_from: Optional[int] = None  # instruction index
    range_to: Optional[int] = None
    expr_from: Optional[str] = None
    expr_to: Optional[str] = None
    guard_expr: Optional[str] = None
    guard_replacement: Optional[str] = None


class PatchSpec(BaseModel):
    patch_id: str = Field(default_factory=lambda: _new_id("patch"))
    target_event_id: int = 0
    target_stage: ShaderStage = ShaderStage.PS
    target_shader_id: str = ""
    intent: str = "fix_naninf"
    ops: List[PatchOp] = Field(default_factory=list)
    max_diff_ops: int = 20
    preserve_outputs: bool = True


class PatchResult(BaseModel):
    patch_id: str
    applied_to_shader_hash: str = ""
    original_shader_hash: str = ""
    success: bool = True
    error_message: str = ""
    error_code: str = ""
    error_category: str = ""
    error_details: Dict[str, Any] = Field(default_factory=dict)
    messages: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Experiment（实验）
# ---------------------------------------------------------------------------

class VerifierConfig(BaseModel):
    type: VerifierType = VerifierType.NANINF
    params: Dict[str, Any] = Field(default_factory=dict)


class ExperimentDef(BaseModel):
    experiment_id: str = Field(default_factory=lambda: _new_id("exp"))
    session_id: str = ""
    capture_id: str = ""
    event_id: int = 0
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    patch_id: Optional[str] = None
    description: str = ""


class ExperimentEvidence(BaseModel):
    experiment_id: str
    before_artifact: Optional[ArtifactRef] = None
    after_artifact: Optional[ArtifactRef] = None
    before_metrics: Dict[str, Any] = Field(default_factory=dict)
    after_metrics: Dict[str, Any] = Field(default_factory=dict)
    verifier_passed: bool = False
    verdict: VerdictResult = VerdictResult.INCONCLUSIVE
    notes: str = ""


class ExperimentResult(BaseModel):
    experiment_id: str
    status: ExperimentStatus = ExperimentStatus.COMPLETED
    evidence: Optional[ExperimentEvidence] = None
    duration_seconds: float = 0.0
    created_at: float = Field(default_factory=_ts)


# ---------------------------------------------------------------------------
# Bisect（二分定位）
# ---------------------------------------------------------------------------

class BisectRange(BaseModel):
    lo: int
    hi: int


class ConfidenceWeights(BaseModel):
    sharpness: float = 0.50
    consistency: float = 0.35
    range_factor: float = 0.15


class ConfidenceBreakdown(BaseModel):
    sharpness: float = 0.0
    consistency: float = 0.0
    range_factor: float = 0.0
    weighted_total: float = 0.0


class BisectResult(BaseModel):
    first_bad_event_id: int
    first_good_event_id: int
    evidence_chain: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    iterations: int = 0
    confidence_breakdown: ConfidenceBreakdown = Field(default_factory=ConfidenceBreakdown)
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    confidence_profile: str = "default"
    boundary_consistent_count: int = 0


# ---------------------------------------------------------------------------
# Performance counters（性能计数器）
# ---------------------------------------------------------------------------

class CounterSample(BaseModel):
    event_id: int
    counter_id: int
    counter_name: str = ""
    value: float = 0.0


class CounterSummary(BaseModel):
    counter_name: str
    min_val: float = 0.0
    max_val: float = 0.0
    mean_val: float = 0.0
    p95_val: float = 0.0
    hotspot_event_id: Optional[int] = None


class PerfResult(BaseModel):
    samples: List[CounterSample] = Field(default_factory=list)
    summaries: List[CounterSummary] = Field(default_factory=list)
    anomaly_events: List[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Report（报告）
# ---------------------------------------------------------------------------

class ReportBundle(BaseModel):
    task_id: str
    bug_types: List[BugType] = Field(default_factory=list)
    capture_info: Optional[CaptureInfo] = None
    first_bad_event_id: Optional[int] = None
    first_good_event_id: Optional[int] = None
    bbox: Optional[BBox] = None
    verifier_metrics: Dict[str, Any] = Field(default_factory=dict)
    hypotheses_tried: List[Hypothesis] = Field(default_factory=list)
    fix_candidate: Optional[Dict[str, Any]] = None
    confidence: float = 0.0
    evidence_artifacts: Dict[str, str] = Field(default_factory=dict)
    pipeline_snapshot: Optional[PipelineSnapshot] = None
    experiments: List[ExperimentResult] = Field(default_factory=list)
    created_at: float = Field(default_factory=_ts)


# ---------------------------------------------------------------------------
# Knowledge / Fingerprint（知识/指纹）
# ---------------------------------------------------------------------------

class PassFingerprint(BaseModel):
    fingerprint_id: str = Field(default_factory=lambda: _new_id("fp"))
    rt_formats: List[str] = Field(default_factory=list)
    blend_modes: List[str] = Field(default_factory=list)
    depth_mode: str = ""
    binding_pattern: List[str] = Field(default_factory=list)
    output_change_pattern: str = ""
    tags: List[str] = Field(default_factory=list)


class ShaderFingerprint(BaseModel):
    fingerprint_id: str = Field(default_factory=lambda: _new_id("sfp"))
    shader_hash: str = ""
    resource_names: List[str] = Field(default_factory=list)
    slot_pattern: List[int] = Field(default_factory=list)
    ir_kgram_hashes: List[str] = Field(default_factory=list)
    constant_signatures: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


class FingerprintRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: _new_id("fpr"))
    task_id: str = ""
    pass_fp: Optional[PassFingerprint] = None
    shader_fp: Optional[ShaderFingerprint] = None
    bug_type: BugType = BugType.UNKNOWN
    verdict: VerdictResult = VerdictResult.INCONCLUSIVE
    project_id: str = ""
    engine_branch: str = ""
    fingerprint_version: int = 1
    created_at: float = Field(default_factory=_ts)


class RegressionEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: _new_id("reg"))
    capture_hash: str = ""
    first_bad_event_id: int = 0
    verifier_config: VerifierConfig = Field(default_factory=VerifierConfig)
    patch_id: Optional[str] = None
    expected_metrics: Dict[str, Any] = Field(default_factory=dict)
    project_id: str = ""
    created_at: float = Field(default_factory=_ts)


# ---------------------------------------------------------------------------
# Task（顶层）
# ---------------------------------------------------------------------------

class TaskInput(BaseModel):
    rdc_path: str
    description: str
    reference_image_path: Optional[str] = None
    expected_image_path: Optional[str] = None
    bug_type_hints: List[BugType] = Field(default_factory=list)
    backend_type: BackendType = BackendType.LOCAL
    project_id: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


class TaskState(BaseModel):
    task_id: str = Field(default_factory=lambda: _new_id("task"))
    input: TaskInput
    session_id: Optional[str] = None
    capture_id: Optional[str] = None
    current_skill: str = ""
    anomalies: List[AnomalyInfo] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    experiments: List[ExperimentResult] = Field(default_factory=list)
    bisect_result: Optional[BisectResult] = None
    pipeline: Optional[PipelineSnapshot] = None
    report: Optional[ReportBundle] = None
    status: str = "created"
    created_at: float = Field(default_factory=_ts)
    updated_at: float = Field(default_factory=_ts)
