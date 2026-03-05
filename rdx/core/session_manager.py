"""
本地与远程 RenderDoc replay sessions 的生命周期管理。

提供 :class:`SessionManager` 单例，持有所有活跃 replay session。
每个 session 封装 RenderDoc ``IReplayController`` 以及关联的
``IReplayOutput``、``ICaptureFile``，并在远程 backend 时包含
``IRemoteServer``。

Typical usage::

    mgr = SessionManager()
    info = await mgr.create_session(
        backend_config={"type": "local"},
        replay_config={"width": 1920, "height": 1080},
    )
    cap = await mgr.open_capture(info.session_id, "/path/to/capture.rdc")
    controller = mgr.get_controller(info.session_id)
    # ... drive the controller ...
    await mgr.close_session(info.session_id)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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

# ---------------------------------------------------------------------------
# Lazy import（renderdoc 仅在 replay host process 中可用）
# ---------------------------------------------------------------------------


def _get_rd():
    """返回 ``renderdoc`` module，并在首次调用时导入。"""
    import renderdoc as rd
    return rd


# ---------------------------------------------------------------------------
# Mapping helpers（映射辅助）
# ---------------------------------------------------------------------------

_GRAPHICS_API_MAP: Dict[str, GraphicsAPI] = {
    "d3d11":    GraphicsAPI.D3D11,
    "d3d12":    GraphicsAPI.D3D12,
    "vulkan":   GraphicsAPI.VULKAN,
    "opengl":   GraphicsAPI.OPENGL,
    "opengles":  GraphicsAPI.OPENGLES,
}


def _map_graphics_api(api_props: Any) -> GraphicsAPI:
    """将 RenderDoc ``APIProperties.pipelineType`` 转换为我们的 enum。"""
    try:
        raw = str(api_props.pipelineType).lower()
        for key, value in _GRAPHICS_API_MAP.items():
            if key in raw:
                return value
    except (AttributeError, TypeError):
        pass
    return GraphicsAPI.UNKNOWN


def _count_actions(actions: Any) -> int:
    """递归统计树中的每个 ``ActionDescription``。"""
    total = 0
    for action in actions:
        total += 1
        children = getattr(action, "children", None)
        if children:
            total += _count_actions(children)
    return total


def _check_status(status: Any, operation: str) -> None:
    """当 *status* 不是 ``Succeeded`` 时抛出 :class:`SessionError`。"""
    rd = _get_rd()
    if status != rd.ResultCode.Succeeded:
        raise SessionError(
            code="renderdoc_error",
            message=f"{operation} failed with status: {status}",
            details={"status": str(status), "operation": operation},
        )


# ---------------------------------------------------------------------------
# Session state container（session 状态容器）
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """单个 replay session 的内部可变状态。

    保存 session 生命周期内需保持存活的 RenderDoc handle，以及
    相关的 book-keeping metadata。
    """

    session_id: str
    backend_type: BackendType

    # RenderDoc handles（C++ 类型不透明，故标注为 Any）
    controller: Any = None        # IReplayController
    output: Any = None            # IReplayOutput
    capture_file: Any = None      # ICaptureFile
    remote_server: Any = None     # IRemoteServer (remote sessions only)

    # Metadata
    capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)
    capture_id: Optional[str] = None
    is_initialized: bool = False
    rdc_path: Optional[str] = None
    replay_config: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Errors（错误）
# ---------------------------------------------------------------------------

class SessionError(Exception):
    """当 session 操作失败时抛出。

    携带 :class:`~rdx.models.ErrorDetail` 负载，便于调用方将结构化错误
    信息传递给 MCP clients。
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(code=code, message=message, details=details)


# ---------------------------------------------------------------------------
# Session Manager（单例）
# ---------------------------------------------------------------------------

class SessionManager:
    """管理所有活跃 replay sessions 的单例。

    所有对外的可变操作方法均为 ``async``，以便将阻塞的 RenderDoc
    C-API 调用透明地转移到默认 :mod:`asyncio` executor，保持事件循环响应。

    内部 session 字典通过 :class:`asyncio.Lock` 保证线程安全。
    """

    _instance: Optional["SessionManager"] = None
    _initialized: bool = False

    # -- Singleton plumbing -------------------------------------------------

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
        """销毁单例实例（用于 test harness）。"""
        cls._instance = None
        cls._initialized = False

    # -- Executor helper ----------------------------------------------------

    @staticmethod
    async def _offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """在默认 thread-pool executor 中运行同步 callable。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(fn, *args, **kwargs),
        )

    # -- Public API: lifecycle ----------------------------------------------

    async def create_session(
        self,
        backend_config: Dict[str, Any],
        replay_config: Dict[str, Any],
    ) -> SessionInfo:
        """创建新的 replay session（local 或 remote）。

        Parameters
        ----------
        backend_config:
            必须包含 ``"type"``（``"local"`` 或 ``"remote"``）。
            远程 backend 还需要 ``"host"``，并可选 ``"port"``（默认 38920）。
        replay_config:
            可选键：``"width"`` 与 ``"height"``，用于 headless 输出面
            （默认 1920x1080）。

        Returns
        -------
        SessionInfo
            新建 session 的元数据。

        Raises
        ------
        SessionError
            当 RenderDoc 子系统无法初始化或远程连接无法建立时抛出。
        """
        async with self._lock:
            session_id = _new_id("sess")
            backend_type_str = backend_config.get("type", "local")
            backend_type = (
                BackendType.REMOTE
                if backend_type_str == "remote"
                else BackendType.LOCAL
            )

            state = SessionState(
                session_id=session_id,
                backend_type=backend_type,
                replay_config=dict(replay_config),
            )

            try:
                if backend_type == BackendType.LOCAL:
                    await self._init_local(state)
                else:
                    await self._init_remote(state, backend_config)
            except SessionError:
                raise
            except Exception as exc:
                raise SessionError(
                    code="session_create_failed",
                    message=f"Failed to create {backend_type.value} session: {exc}",
                    details={"original_error": str(exc)},
                ) from exc

            state.is_initialized = True
            state.capabilities.remote = (backend_type == BackendType.REMOTE)
            self._sessions[session_id] = state

            logger.info(
                "Created %s session %s", backend_type.value, session_id,
            )
            return SessionInfo(
                session_id=session_id,
                backend_type=backend_type,
                capabilities=state.capabilities,
                created_at=state.created_at,
            )

    async def open_capture(
        self,
        session_id: str,
        rdc_path: str,
    ) -> CaptureInfo:
        """在现有 session 中打开 ``.rdc`` capture 文件。

        建立 ``IReplayController`` 与 headless ``IReplayOutput``，
        识别 graphics API 与 driver 信息，并统计 capture 中 action 总数。

        Parameters
        ----------
        session_id:
            由 :meth:`create_session` 创建的活跃 session。
        rdc_path:
            ``.rdc`` capture 文件的路径。

        Returns
        -------
        CaptureInfo
            打开后的 capture 元数据。

        Raises
        ------
        SessionError
            当 capture 无法打开或 replay controller 无法创建时抛出。
        """
        state = self._require_session(session_id)

        try:
            if state.backend_type == BackendType.LOCAL:
                await self._open_local_capture(state, rdc_path)
            else:
                await self._open_remote_capture(state, rdc_path)
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError(
                code="capture_open_failed",
                message=f"Failed to open capture {rdc_path}: {exc}",
                details={"rdc_path": rdc_path, "original_error": str(exc)},
            ) from exc

        # -- 从 live controller 收集元数据 ---------------------------------
        capture_id = _new_id("cap")
        state.capture_id = capture_id
        state.rdc_path = rdc_path

        api = GraphicsAPI.UNKNOWN
        driver_name = ""
        driver_version = ""
        total_events = 0

        try:
            props = await self._offload(state.controller.GetAPIProperties)
            api = _map_graphics_api(props)
            state.capabilities.api = api
            driver_name = str(getattr(props, "localRenderer", ""))
            if hasattr(props, "driverVersion"):
                driver_version = str(props.driverVersion)
        except Exception as exc:
            logger.warning("Could not read API properties: %s", exc)

        try:
            actions = await self._offload(state.controller.GetRootActions)
            total_events = _count_actions(actions)
        except Exception as exc:
            logger.warning("Could not count actions: %s", exc)

        info = CaptureInfo(
            capture_id=capture_id,
            session_id=session_id,
            rdc_path=rdc_path,
            api=api,
            driver_name=driver_name,
            driver_version=driver_version,
            total_events=total_events,
        )
        logger.info(
            "Opened capture %s (%s, %d events) in session %s",
            capture_id,
            api.value,
            total_events,
            session_id,
        )
        return info

    async def close_session(self, session_id: str) -> None:
        """销毁 session 并释放所有相关资源。

        关闭 replay output、controller、capture file，以及（远程 session）
        的服务器连接。当最后一个 local session 关闭时，也会关闭全局
        RenderDoc replay 子系统。

        Raises
        ------
        SessionError
            当 session 不存在时抛出。
        """
        async with self._lock:
            state = self._sessions.pop(session_id, None)

        if state is None:
            raise SessionError(
                code="session_not_found",
                message=f"No session with id {session_id}",
            )

        await self._cleanup(state)
        logger.info("Closed session %s", session_id)

    # -- Public API: accessors ----------------------------------------------

    def get_controller(self, session_id: str) -> Any:
        """返回 *session_id* 对应的 ``IReplayController``。

        Raises
        ------
        SessionError
            当 session 不存在或尚未打开 capture 时抛出。
        """
        state = self._require_session(session_id)
        if state.controller is None:
            raise SessionError(
                code="no_controller",
                message=(
                    f"Session {session_id} has no active replay controller. "
                    "Open a capture first."
                ),
            )
        return state.controller

    def get_output(self, session_id: str) -> Any:
        """返回 *session_id* 对应的 ``IReplayOutput``。

        Raises
        ------
        SessionError
            当 session 不存在或尚未打开 capture 时抛出。
        """
        state = self._require_session(session_id)
        if state.output is None:
            raise SessionError(
                code="no_output",
                message=(
                    f"Session {session_id} has no replay output. "
                    "Open a capture first."
                ),
            )
        return state.output

    def get_session(self, session_id: str) -> SessionState:
        """返回 *session_id* 的完整 :class:`SessionState`。

        Raises
        ------
        SessionError
            当 session 不存在时抛出。
        """
        return self._require_session(session_id)

    def list_sessions(self) -> List[SessionInfo]:
        """返回所有活跃 sessions 的快照列表。"""
        return [
            SessionInfo(
                session_id=s.session_id,
                backend_type=s.backend_type,
                capabilities=s.capabilities,
                created_at=s.created_at,
            )
            for s in self._sessions.values()
        ]

    # -- Internal: session lookup -------------------------------------------

    def _require_session(self, session_id: str) -> SessionState:
        """获取 session；不存在则抛出 :class:`SessionError`。"""
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionError(
                code="session_not_found",
                message=f"No session with id {session_id}",
            )
        return state

    # -- Internal: initialisation -------------------------------------------

    async def _init_local(self, state: SessionState) -> None:
        """初始化全局 RenderDoc replay 子系统（仅一次）。"""
        if not self._replay_initialized:
            rd = _get_rd()
            await self._offload(
                rd.InitialiseReplay, rd.GlobalEnvironment(), [],
            )
            self._replay_initialized = True
            logger.debug("RenderDoc replay subsystem initialised")

    async def _init_remote(
        self,
        state: SessionState,
        backend_config: Dict[str, Any],
    ) -> None:
        """建立远程服务器连接。"""
        rd = _get_rd()
        host = backend_config.get("host", "localhost")
        port = backend_config.get("port")
        url = f"{host}:{port}" if port else str(host)

        status, remote = await self._offload(
            rd.CreateRemoteServerConnection, url,
        )
        _check_status(status, f"CreateRemoteServerConnection({url})")
        state.remote_server = remote
        logger.debug("Connected to remote RenderDoc server at %s", url)

    # -- Internal: capture opening ------------------------------------------

    async def _open_local_capture(
        self,
        state: SessionState,
        rdc_path: str,
    ) -> None:
        """在本地打开 capture 文件并创建 replay controller。"""
        rd = _get_rd()

        # 1. 打开 capture 文件句柄
        cap = await self._offload(rd.OpenCaptureFile)
        status = await self._offload(cap.OpenFile, rdc_path, "", None)
        _check_status(status, f"OpenFile({rdc_path})")
        state.capture_file = cap

        # 2. 创建 replay controller
        status, controller = await self._offload(
            cap.OpenCapture, rd.ReplayOptions(), None,
        )
        _check_status(status, "OpenCapture")
        state.controller = controller

        # 3. 创建 headless replay output
        await self._create_headless_output(state, controller)

    async def _open_remote_capture(
        self,
        state: SessionState,
        rdc_path: str,
    ) -> None:
        """在远程服务器上打开 capture 文件。"""
        rd = _get_rd()

        if state.remote_server is None:
            raise SessionError(
                code="no_remote_server",
                message="Remote session has no active server connection",
            )

        proxy_id = 0
        status, controller = await self._offload(
            state.remote_server.OpenCapture,
            proxy_id,
            rdc_path,
            rd.ReplayOptions(),
            None,
        )
        _check_status(status, f"remote.OpenCapture({rdc_path})")
        state.controller = controller

        # 创建 headless replay output
        await self._create_headless_output(state, controller)

    async def _create_headless_output(
        self,
        state: SessionState,
        controller: Any,
    ) -> None:
        """为 replay controller 绑定 headless texture output。"""
        rd = _get_rd()
        width = state.replay_config.get("width", 1920)
        height = state.replay_config.get("height", 1080)

        windowing_data = await self._offload(
            rd.CreateHeadlessWindowingData, width, height,
        )
        output = await self._offload(
            controller.CreateOutput,
            windowing_data,
            rd.ReplayOutputType.Texture,
        )
        state.output = output
        logger.debug(
            "Created headless output (%dx%d) for session %s",
            width, height, state.session_id,
        )

    # -- Internal: cleanup --------------------------------------------------

    async def _cleanup(self, state: SessionState) -> None:
        """释放 *state* 持有的所有 RenderDoc 资源。

        单个关闭步骤的错误会记录日志，但不会阻止后续步骤执行。
        """
        errors: List[str] = []

        # 1. Replay output
        if state.output is not None:
            try:
                await self._offload(state.output.Shutdown)
            except Exception as exc:
                errors.append(f"output.Shutdown: {exc}")
            state.output = None

        # 2. Replay controller
        if state.controller is not None:
            try:
                await self._offload(state.controller.Shutdown)
            except Exception as exc:
                errors.append(f"controller.Shutdown: {exc}")
            state.controller = None

        # 3. Capture file
        if state.capture_file is not None:
            try:
                if hasattr(state.capture_file, "CloseFile"):
                    await self._offload(state.capture_file.CloseFile)
                elif hasattr(state.capture_file, "Shutdown"):
                    await self._offload(state.capture_file.Shutdown)
                else:
                    raise AttributeError("capture_file has no CloseFile/Shutdown")
            except Exception as exc:
                errors.append(f"capture_file.CloseFile/Shutdown: {exc}")
            state.capture_file = None

        # 4. Remote server connection
        if state.remote_server is not None:
            try:
                await self._offload(state.remote_server.ShutdownConnection)
            except Exception as exc:
                errors.append(f"remote.ShutdownConnection: {exc}")
            state.remote_server = None

        # 5. 全局 replay 子系统（仅在最后一个 local session 关闭时）
        if state.backend_type == BackendType.LOCAL:
            remaining_local = any(
                s.backend_type == BackendType.LOCAL
                for s in self._sessions.values()
            )
            if not remaining_local and self._replay_initialized:
                try:
                    rd = _get_rd()
                    await self._offload(rd.ShutdownReplay)
                    self._replay_initialized = False
                    logger.debug("RenderDoc replay subsystem shut down")
                except Exception as exc:
                    errors.append(f"ShutdownReplay: {exc}")

        if errors:
            logger.warning(
                "Errors during cleanup of session %s: %s",
                state.session_id,
                "; ".join(errors),
            )
