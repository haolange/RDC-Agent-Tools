"""
RDX-MCP 配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rdx.runtime_paths import artifacts_dir, logs_dir, runtime_root


@dataclass
class BackendConfig:
    type: str = "local"  # "local" 或 "remote"
    gpu_vendor: str = "any"  # nvidia, amd, intel, arm, any
    gpu_index: int = 0
    remote_host: Optional[str] = None
    remote_port: int = 38920
    remote_protocol: str = "renderdoc"  # 远程协议：renderdoc, adb, ssh
    remote_auth_mode: str = "none"  # 认证方式：none, key, token
    remote_auth_value: Optional[str] = None


@dataclass
class ReplayConfig:
    headless: bool = True
    optimisation_level: str = "balanced"  # 优化等级：balanced, fast_seek, max_accurate
    default_output_width: int = 1920
    default_output_height: int = 1080
    max_texture_readback_bytes: int = 256 * 1024 * 1024  # 256MB
    replay_timeout_seconds: int = 120


@dataclass
class WorkerConfig:
    max_workers_per_gpu: int = 1
    max_remote_controllers: int = 1
    task_queue_size: int = 64
    worker_timeout_seconds: int = 300


@dataclass
class ArtifactConfig:
    store_path: Path = field(default_factory=lambda: artifacts_dir())
    max_store_size_gb: float = 50.0
    hash_algorithm: str = "sha256"
    compress_artifacts: bool = True


@dataclass
class DatabaseConfig:
    path: Path = field(default_factory=lambda: runtime_root() / "metadata.db")
    fingerprint_path: Path = field(default_factory=lambda: runtime_root() / "fingerprints.db")


@dataclass
class BisectConfig:
    default_strategy: str = "binary"  # 搜索策略：binary, ddmin
    max_iterations: int = 60
    default_confidence_threshold: float = 0.85
    early_stop_on_clear_boundary: bool = True


@dataclass
class PatchConfig:
    max_patch_ops: int = 50
    auto_revert_on_crash: bool = True
    preserve_original_shaders: bool = True
    spirv_tools_path: Optional[str] = None  # SPIRV-Tools binaries 的路径
    dxc_path: Optional[str] = None  # DXC compiler 的路径


@dataclass
class ReportConfig:
    output_path: Path = field(default_factory=lambda: logs_dir())
    generate_html: bool = True
    generate_markdown: bool = True
    generate_json: bool = True
    embed_thumbnails: bool = True
    max_embedded_image_width: int = 1024


@dataclass
class RdxConfig:
    backend: BackendConfig = field(default_factory=BackendConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    artifact: ArtifactConfig = field(default_factory=ArtifactConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    bisect: BisectConfig = field(default_factory=BisectConfig)
    patch: PatchConfig = field(default_factory=PatchConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    renderdoc_module_path: Optional[str] = None  # renderdoc module 的 sys.path 补充路径
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> RdxConfig:
        cfg = cls()
        if p := os.environ.get("RDX_RENDERDOC_PATH"):
            cfg.renderdoc_module_path = p
        if p := os.environ.get("RDX_ARTIFACT_STORE"):
            cfg.artifact.store_path = Path(p)
        if p := os.environ.get("RDX_DATA_DIR"):
            cfg.database.path = Path(p) / "metadata.db"
            cfg.database.fingerprint_path = Path(p) / "fingerprints.db"
        if p := os.environ.get("RDX_REPORT_DIR"):
            cfg.report.output_path = Path(p)
        if p := os.environ.get("RDX_LOG_LEVEL"):
            cfg.log_level = p
        if p := os.environ.get("RDX_GPU_VENDOR"):
            cfg.backend.gpu_vendor = p
        if p := os.environ.get("RDX_SPIRV_TOOLS_PATH"):
            cfg.patch.spirv_tools_path = p
        if p := os.environ.get("RDX_HEADLESS"):
            cfg.replay.headless = p.lower() in ("1", "true", "yes")
        return cfg
