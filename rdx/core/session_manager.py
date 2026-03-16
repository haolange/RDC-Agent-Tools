from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rdx.core.renderdoc_status import build_renderdoc_error_details, status_ok as _rd_status_ok, status_text as _rd_status_text
from rdx.models import (
    BackendType,
    CaptureInfo,
    ErrorDetail,
    GraphicsAPI,
    SessionCapabilities,
    SessionInfo,
    _new_id,
)

logger = logging.getLogger(__name__)


def _get_rd() -> Any:
    import renderdoc as rd

    return rd


_GRAPHICS_API_MAP: Dict[str, GraphicsAPI] = {
    "d3d11": GraphicsAPI.D3D11,
    "d3d12": GraphicsAPI.D3D12,
    "vulkan": GraphicsAPI.VULKAN,
    "opengl": GraphicsAPI.OPENGL,
    "opengles": GraphicsAPI.OPENGLES,
}


def _map_graphics_api(api_props: Any) -> GraphicsAPI:
    try:
        raw = str(api_props.pipelineType).lower()
    except (AttributeError, TypeError):
        return GraphicsAPI.UNKNOWN
    for key, value in _GRAPHICS_API_MAP.items():
        if key in raw:
            return value
    return GraphicsAPI.UNKNOWN


def _count_actions(actions: Any) -> int:
    total = 0
    for action in actions or []:
        total += 1
        children = getattr(action, "children", None)
        if children:
            total += _count_actions(children)
    return total


def _status_ok(status: Any) -> bool:
    return _rd_status_ok(status, _get_rd())


def _status_text(status: Any) -> str:
    return _rd_status_text(status)


def _check_status(
    status: Any,
    operation: str,
    *,
    backend_type: str = "local",
    capture_context: Optional[Dict[str, Any]] = None,
    source_layer: str = "renderdoc_status",
    classification: str = "renderdoc_status",
    fix_hint: str = "Inspect the RenderDoc status and capture context before retrying.",
) -> None:
    if not _status_ok(status):
        details = build_renderdoc_error_details(
            status,
            operation=operation,
            source_layer=source_layer,
            backend_type=backend_type,
            capture_context=capture_context,
            classification=classification,
            fix_hint=fix_hint,
        )
        raise SessionError(
            code="renderdoc_error",
            message=f"{operation} failed with status: {details['renderdoc_status']['status_text']}",
            details=details,
        )


@dataclass
class SessionState:
    session_id: str
    backend_type: BackendType
    controller: Any = None
    output: Any = None
    capture_file: Any = None
    remote_server: Any = None
    capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)
    capture_id: Optional[str] = None
    is_initialized: bool = False
    rdc_path: Optional[str] = None
    replay_config: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class SessionError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(code=code, message=message, details=details)


class SessionManager:
    _instance: Optional["SessionManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "SessionManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if SessionManager._initialized:
            return
        self._sessions: Dict[str, SessionState] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._replay_initialized: bool = False
        SessionManager._initialized = True

    @classmethod
    def reset(cls) -> None:
        cls._instance = None
        cls._initialized = False

    @staticmethod
    async def _offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def create_session(
        self,
        backend_config: Dict[str, Any],
        replay_config: Dict[str, Any],
        *,
        preferred_session_id: Optional[str] = None,
    ) -> SessionInfo:
        backend_type = BackendType.REMOTE if backend_config.get("type") == "remote" else BackendType.LOCAL
        async with self._lock:
            session_id = str(preferred_session_id or "").strip() or _new_id("sess")
            if session_id in self._sessions:
                raise SessionError(
                    code="session_conflict",
                    message=f"Session {session_id} already exists",
                )
            state = SessionState(
                session_id=session_id,
                backend_type=backend_type,
                replay_config=dict(replay_config or {}),
            )
            if backend_type == BackendType.LOCAL:
                await self._ensure_local_replay_initialized()
            else:
                await self._init_remote(state, backend_config)
                state.capabilities.remote = True
            self._sessions[session_id] = state
            return SessionInfo(
                session_id=session_id,
                backend_type=backend_type,
                capabilities=state.capabilities,
                created_at=state.created_at,
            )

    async def open_capture(self, session_id: str, rdc_path: str) -> CaptureInfo:
        async with self._lock:
            state = self._require_state(session_id)
            if state.is_initialized:
                raise SessionError(
                    code="capture_already_open",
                    message=f"Session {session_id} already has an opened capture",
                )
            if state.backend_type == BackendType.LOCAL:
                await self._open_local_capture(state, rdc_path)
            else:
                await self._open_remote_capture(state, rdc_path)

            controller = state.controller
            if controller is None:
                raise SessionError(code="controller_missing", message=f"Session {session_id} has no replay controller")

            try:
                props = await self._offload(controller.GetAPIProperties)
            except Exception:
                props = None
            state.capabilities.api = _map_graphics_api(props)
            state.capabilities.remote = state.backend_type == BackendType.REMOTE
            state.capabilities.shader_debug_supported = bool(getattr(props, "shaderDebugging", False)) if props is not None else False
            state.capabilities.counters_supported = True

            try:
                roots = await self._offload(controller.GetRootActions)
            except Exception:
                roots = []

            state.capture_id = _new_id("cap")
            state.rdc_path = str(rdc_path)
            state.is_initialized = True

            return CaptureInfo(
                capture_id=state.capture_id,
                session_id=session_id,
                rdc_path=str(rdc_path),
                api=state.capabilities.api,
                frame_count=1,
                total_events=_count_actions(roots),
            )

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is None:
            raise SessionError(code="session_not_found", message=f"Unknown session_id: {session_id}")
        await self._cleanup(state)

    def get_controller(self, session_id: str) -> Any:
        state = self._require_state(session_id)
        if state.controller is None:
            raise SessionError(code="controller_missing", message=f"Session {session_id} has no replay controller")
        return state.controller

    def get_output(self, session_id: str) -> Any:
        state = self._require_state(session_id)
        if state.output is None:
            raise SessionError(code="output_missing", message=f"Session {session_id} has no replay output")
        return state.output

    def get_state(self, session_id: str) -> SessionState:
        return self._require_state(session_id)

    def list_sessions(self) -> List[SessionInfo]:
        return [
            SessionInfo(
                session_id=state.session_id,
                backend_type=state.backend_type,
                capabilities=state.capabilities,
                created_at=state.created_at,
            )
            for state in self._sessions.values()
        ]

    def _require_state(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionError(code="session_not_found", message=f"Unknown session_id: {session_id}")
        return state

    async def _ensure_local_replay_initialized(self) -> None:
        if self._replay_initialized:
            return
        rd = _get_rd()
        await self._offload(rd.InitialiseReplay, rd.GlobalEnvironment(), [])
        self._replay_initialized = True
        logger.debug("RenderDoc replay subsystem initialised")

    async def _init_remote(self, state: SessionState, backend_config: Dict[str, Any]) -> None:
        rd = _get_rd()
        host = str(backend_config.get("host") or "").strip()
        port = backend_config.get("port")
        if not host:
            raise SessionError(
                code="remote_endpoint_required",
                message="Remote session backend requires an explicit host from options.remote_id",
            )
        url = f"{host}:{port}" if port else host

        remote = backend_config.get("remote_server")
        if remote is not None:
            state.remote_server = remote
            logger.debug("Reusing remote RenderDoc server connection at %s", url)
            return

        status, remote = await self._offload(rd.CreateRemoteServerConnection, url)
        _check_status(
            status,
            f"CreateRemoteServerConnection({url})",
            backend_type="remote",
            capture_context={"session_id": state.session_id, "endpoint": url},
            classification="remote_replay_runtime",
            fix_hint="Confirm the remote endpoint is reachable and still owns a valid replay runtime.",
        )
        state.remote_server = remote
        logger.debug("Connected to remote RenderDoc server at %s", url)

    async def _open_local_capture(self, state: SessionState, rdc_path: str) -> None:
        rd = _get_rd()
        cap = await self._offload(rd.OpenCaptureFile)
        status = await self._offload(cap.OpenFile, rdc_path, "", None)
        _check_status(
            status,
            f"OpenFile({rdc_path})",
            backend_type="local",
            capture_context={"session_id": state.session_id, "capture_path": str(rdc_path)},
            classification="rdc_invalid_or_unsupported",
            fix_hint="Verify that the .rdc file is valid and supported by this RenderDoc runtime.",
        )
        state.capture_file = cap

        status, controller = await self._offload(cap.OpenCapture, rd.ReplayOptions(), None)
        _check_status(
            status,
            "OpenCapture",
            backend_type="local",
            capture_context={"session_id": state.session_id, "capture_path": str(rdc_path)},
            classification="rdc_invalid_or_unsupported",
            fix_hint="Verify the capture can be replayed locally with the current RenderDoc runtime.",
        )
        state.controller = controller
        await self._create_headless_output(state, controller)

    async def _open_remote_capture(self, state: SessionState, rdc_path: str) -> None:
        rd = _get_rd()
        if state.remote_server is None:
            raise SessionError(code="no_remote_server", message="Remote session has no active server connection")
        remote_rdc_path = await self._offload(state.remote_server.CopyCaptureToRemote, rdc_path, None)
        status, controller = await self._offload(
            state.remote_server.OpenCapture,
            0,
            remote_rdc_path,
            rd.ReplayOptions(),
            None,
        )
        _check_status(
            status,
            f"remote.OpenCapture({remote_rdc_path})",
            backend_type="remote",
            capture_context={
                "session_id": state.session_id,
                "capture_path": str(rdc_path),
                "remote_capture_path": str(remote_rdc_path),
            },
            classification="remote_replay_runtime",
            fix_hint="Verify the remote endpoint can open the copied capture and has a compatible replay environment.",
        )
        state.controller = controller
        await self._create_headless_output(state, controller)

    async def _create_headless_output(self, state: SessionState, controller: Any) -> None:
        rd = _get_rd()
        width = int(state.replay_config.get("width", 1920) or 1920)
        height = int(state.replay_config.get("height", 1080) or 1080)
        windowing_data = await self._offload(rd.CreateHeadlessWindowingData, width, height)
        state.output = await self._offload(controller.CreateOutput, windowing_data, rd.ReplayOutputType.Texture)
        logger.debug("Created headless output (%dx%d) for session %s", width, height, state.session_id)

    async def _cleanup(self, state: SessionState) -> None:
        errors: List[str] = []

        if state.output is not None:
            try:
                await self._offload(state.output.Shutdown)
            except Exception as exc:
                errors.append(f"output.Shutdown: {exc}")
            state.output = None

        if state.controller is not None:
            try:
                if state.backend_type == BackendType.REMOTE and state.remote_server is not None:
                    await self._offload(state.remote_server.CloseCapture, state.controller)
                else:
                    await self._offload(state.controller.Shutdown)
            except Exception as exc:
                errors.append(f"controller.Shutdown: {exc}")
            state.controller = None

        if state.capture_file is not None:
            try:
                if hasattr(state.capture_file, "CloseFile"):
                    await self._offload(state.capture_file.CloseFile)
                elif hasattr(state.capture_file, "Shutdown"):
                    await self._offload(state.capture_file.Shutdown)
            except Exception as exc:
                errors.append(f"capture_file.CloseFile/Shutdown: {exc}")
            state.capture_file = None

        if state.remote_server is not None:
            try:
                await self._offload(state.remote_server.ShutdownConnection)
            except Exception as exc:
                errors.append(f"remote.ShutdownConnection: {exc}")
            state.remote_server = None

        if state.backend_type == BackendType.LOCAL:
            remaining_local = any(s.backend_type == BackendType.LOCAL for s in self._sessions.values())
            if not remaining_local and self._replay_initialized:
                try:
                    rd = _get_rd()
                    await self._offload(rd.ShutdownReplay)
                    self._replay_initialized = False
                    logger.debug("RenderDoc replay subsystem shut down")
                except Exception as exc:
                    errors.append(f"ShutdownReplay: {exc}")

        if errors:
            logger.warning("Errors during cleanup of session %s: %s", state.session_id, '; '.join(errors))

