from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional


WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
PM_REMOVE = 0x0001
CW_USEDEFAULT = 0x80000000
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_EX_CLIENTEDGE = 0x00000200
SW_SHOW = 5
IDC_ARROW = 32512
COLOR_WINDOW = 5
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 0x00000002


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_WNDPROC_REGISTRY: dict[int, "PreviewWindowHost"] = {}
_CLASS_LOCK = threading.Lock()
_CLASS_NAME = "RdxPreviewWindow"
_CLASS_READY = False


def fit_size_within_bounds(
    content_width: int,
    content_height: int,
    max_width: int,
    max_height: int,
) -> tuple[int, int]:
    width = max(1, int(content_width or 1))
    height = max(1, int(content_height or 1))
    bound_width = max(1, int(max_width or width))
    bound_height = max(1, int(max_height or height))
    scale = min(1.0, float(bound_width) / float(width), float(bound_height) / float(height))
    return (
        max(1, int(round(float(width) * scale))),
        max(1, int(round(float(height) * scale))),
    )


def fit_content_rect(
    client_width: int,
    client_height: int,
    content_width: int,
    content_height: int,
) -> dict[str, int]:
    target_width = max(1, int(client_width or 1))
    target_height = max(1, int(client_height or 1))
    content_fit_width, content_fit_height = fit_size_within_bounds(
        content_width,
        content_height,
        target_width,
        target_height,
    )
    offset_x = max(0, (target_width - content_fit_width) // 2)
    offset_y = max(0, (target_height - content_fit_height) // 2)
    return {
        "x": int(offset_x),
        "y": int(offset_y),
        "width": int(content_fit_width),
        "height": int(content_fit_height),
    }


def _rect_size(rect: RECT) -> tuple[int, int]:
    return (
        max(0, int(rect.right) - int(rect.left)),
        max(0, int(rect.bottom) - int(rect.top)),
    )


def _primary_work_area() -> tuple[int, int]:
    rect = RECT()
    if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return _rect_size(rect)
    return (1920, 1080)


def _ensure_window_class() -> None:
    global _CLASS_READY
    with _CLASS_LOCK:
        if _CLASS_READY:
            return
        hinstance = _kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW()
        window_class.style = 0
        window_class.lpfnWndProc = _window_proc
        window_class.cbClsExtra = 0
        window_class.cbWndExtra = 0
        window_class.hInstance = hinstance
        window_class.hIcon = None
        window_class.hCursor = _user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        window_class.hbrBackground = wintypes.HBRUSH(COLOR_WINDOW + 1)
        window_class.lpszMenuName = None
        window_class.lpszClassName = _CLASS_NAME
        atom = _user32.RegisterClassW(ctypes.byref(window_class))
        if atom == 0:
            error = ctypes.GetLastError()
            if error != 1410:
                raise OSError(f"RegisterClassW failed: {error}")
        _CLASS_READY = True


@WNDPROC
def _window_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    host = _WNDPROC_REGISTRY.get(int(hwnd))
    if msg == WM_CLOSE:
        _user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        if host is not None:
            host._destroyed.set()
        _user32.PostQuitMessage(0)
        return 0
    if host is not None:
        if msg == WM_SIZE:
            host._handle_size()
        elif msg == WM_ENTERSIZEMOVE:
            host._begin_user_resize()
        elif msg == WM_EXITSIZEMOVE:
            host._end_user_resize()
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class PreviewWindowHost:
    def __init__(
        self,
        *,
        title: str,
        width: int = 1280,
        height: int = 720,
        on_closed: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.title = str(title or "RDX Preview")
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.on_closed = on_closed
        self.hwnd: int = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._destroyed = threading.Event()
        self._stop_requested = threading.Event()
        self._close_reason = "user"
        self._startup_error = ""
        self._state_lock = threading.Lock()
        self._client_size = (self.width, self.height)
        self._window_size = (self.width, self.height)
        self._framebuffer_extent = (0, 0)
        self._manual_size_override = False
        self._user_resize_active = False
        self._auto_resize_depth = 0
        self._geometry_initialized = False

    def start(self, *, timeout_s: float = 5.0) -> int:
        if self._thread is not None and self._thread.is_alive() and self.hwnd:
            return int(self.hwnd)
        self._ready.clear()
        self._destroyed.clear()
        self._stop_requested.clear()
        self._close_reason = "user"
        self._startup_error = ""
        self._thread = threading.Thread(target=self._run, name="rdx-preview-window", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=max(0.1, float(timeout_s))):
            raise TimeoutError("Preview window did not become ready")
        if self._startup_error:
            raise RuntimeError(self._startup_error)
        if not self.hwnd:
            raise RuntimeError("Preview window was not created")
        self._refresh_window_metrics()
        return int(self.hwnd)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and bool(self.hwnd)

    def close(self, *, by_user: bool = False, timeout_s: float = 2.0) -> None:
        self._close_reason = "user" if by_user else "runtime"
        self._stop_requested.set()
        hwnd = int(self.hwnd or 0)
        if hwnd:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout_s)))

    def set_title(self, title: str) -> None:
        self.title = str(title or self.title)
        hwnd = int(self.hwnd or 0)
        if hwnd:
            _user32.SetWindowTextW(hwnd, self.title)

    def geometry_snapshot(self) -> dict[str, object]:
        self._refresh_window_metrics()
        with self._state_lock:
            return {
                "window_rect": {
                    "width": int(self._window_size[0]),
                    "height": int(self._window_size[1]),
                },
                "client_rect": {
                    "width": int(self._client_size[0]),
                    "height": int(self._client_size[1]),
                },
                "manual_size_override": bool(self._manual_size_override),
            }

    def apply_framebuffer_geometry(
        self,
        framebuffer_width: int,
        framebuffer_height: int,
        *,
        screen_cap_ratio: float = 0.5,
        force: bool = False,
    ) -> dict[str, object]:
        width = max(1, int(framebuffer_width or 1))
        height = max(1, int(framebuffer_height or 1))
        hwnd = int(self.hwnd or 0)
        current_work_width, current_work_height = self._work_area_size(hwnd)
        max_width = max(1, int(round(float(current_work_width) * float(screen_cap_ratio or 0.5))))
        max_height = max(1, int(round(float(current_work_height) * float(screen_cap_ratio or 0.5))))
        desired_client = fit_size_within_bounds(width, height, max_width, max_height)
        with self._state_lock:
            extent_changed = self._framebuffer_extent != (width, height)
            should_resize = bool(force or extent_changed or not self._geometry_initialized or not self._manual_size_override)
            self._framebuffer_extent = (width, height)
            if extent_changed or force:
                self._manual_size_override = False
        if should_resize and hwnd:
            self._apply_client_size(hwnd, desired_client)
        else:
            self._refresh_window_metrics()
        with self._state_lock:
            self._geometry_initialized = True
        return self.geometry_snapshot()

    def _handle_size(self) -> None:
        self._refresh_window_metrics()
        with self._state_lock:
            if self._geometry_initialized and self._auto_resize_depth <= 0:
                self._manual_size_override = True

    def _begin_user_resize(self) -> None:
        with self._state_lock:
            self._user_resize_active = True

    def _end_user_resize(self) -> None:
        self._refresh_window_metrics()
        with self._state_lock:
            if self._geometry_initialized and self._auto_resize_depth <= 0:
                self._manual_size_override = True
            self._user_resize_active = False

    def _work_area_size(self, hwnd: int) -> tuple[int, int]:
        if hwnd > 0:
            try:
                monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                if monitor:
                    info = MONITORINFO()
                    info.cbSize = ctypes.sizeof(MONITORINFO)
                    if _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                        return _rect_size(info.rcWork)
            except Exception:
                pass
        return _primary_work_area()

    def _refresh_window_metrics(self) -> None:
        hwnd = int(self.hwnd or 0)
        if hwnd <= 0:
            return
        client_rect = RECT()
        window_rect = RECT()
        try:
            _user32.GetClientRect(hwnd, ctypes.byref(client_rect))
            _user32.GetWindowRect(hwnd, ctypes.byref(window_rect))
        except Exception:
            return
        client_size = _rect_size(client_rect)
        window_size = _rect_size(window_rect)
        with self._state_lock:
            self._client_size = client_size
            self._window_size = window_size

    def _apply_client_size(self, hwnd: int, desired_client: tuple[int, int]) -> None:
        desired_client_width = max(1, int(desired_client[0]))
        desired_client_height = max(1, int(desired_client[1]))
        current_client_rect = RECT()
        current_window_rect = RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(current_client_rect))
        _user32.GetWindowRect(hwnd, ctypes.byref(current_window_rect))
        current_client_width, current_client_height = _rect_size(current_client_rect)
        current_window_width, current_window_height = _rect_size(current_window_rect)
        delta_width = max(0, current_window_width - current_client_width)
        delta_height = max(0, current_window_height - current_client_height)
        target_window_width = max(1, desired_client_width + delta_width)
        target_window_height = max(1, desired_client_height + delta_height)
        left = int(current_window_rect.left)
        top = int(current_window_rect.top)
        with self._state_lock:
            self._auto_resize_depth += 1
        try:
            _user32.MoveWindow(
                hwnd,
                left,
                top,
                target_window_width,
                target_window_height,
                True,
            )
        finally:
            with self._state_lock:
                self._auto_resize_depth = max(0, self._auto_resize_depth - 1)
        self._refresh_window_metrics()

    def _run(self) -> None:
        hwnd = 0
        try:
            _ensure_window_class()
            hinstance = _kernel32.GetModuleHandleW(None)
            hwnd = _user32.CreateWindowExW(
                WS_EX_CLIENTEDGE,
                _CLASS_NAME,
                self.title,
                WS_OVERLAPPEDWINDOW,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                self.width,
                self.height,
                None,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise OSError(f"CreateWindowExW failed: {ctypes.GetLastError()}")
            self.hwnd = int(hwnd)
            _WNDPROC_REGISTRY[int(hwnd)] = self
            _user32.ShowWindow(hwnd, SW_SHOW)
            _user32.UpdateWindow(hwnd)
            self._refresh_window_metrics()
        except Exception as exc:  # noqa: BLE001
            self._startup_error = f"{exc.__class__.__name__}: {exc}"
            self._ready.set()
            return

        self._ready.set()
        msg = MSG()
        try:
            while True:
                while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                    if msg.message == 0x0012:
                        return
                if self._stop_requested.wait(0.02):
                    if int(self.hwnd or 0):
                        _user32.PostMessageW(int(self.hwnd), WM_CLOSE, 0, 0)
                if self._destroyed.is_set():
                    return
                time.sleep(0.02)
        finally:
            if hwnd:
                _WNDPROC_REGISTRY.pop(int(hwnd), None)
            closed_by_user = self._close_reason == "user"
            self.hwnd = 0
            if self.on_closed is not None:
                try:
                    self.on_closed(closed_by_user)
                except Exception:
                    pass
