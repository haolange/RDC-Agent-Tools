"""Android remote bootstrap helpers for standalone `rdx-tools`."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rdx.runtime_paths import android_binaries_root, runtime_root

DEFAULT_ANDROID_REMOTE_PORT = 38920
DEFAULT_ANDROID_TIMEOUT_MS = 120000
_PACKAGE_MAP = {
    "arm32": "org.renderdoc.renderdoccmd.arm32",
    "arm64": "org.renderdoc.renderdoccmd.arm64",
}
_REMOTE_SOCKET_TEMPLATE = "renderdoc_{port}"
_REMOTE_SOCKET_RE = re.compile(r"@renderdoc_(\d+)")


class AndroidRemoteBootstrapError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Optional[dict[str, object]] = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


@dataclass
class AdbDevice:
    serial: str
    state: str
    detail: str = ""


@dataclass
class AndroidBootstrapResult:
    adb_path: str
    device_serial: str
    package_name: str
    activity_name: str
    abi: str
    host: str
    port: int
    remote_port: int
    apk_path: str
    forward_spec: str
    config_local_path: str = ""
    config_remote_path: str = ""
    started_activity: bool = False
    installed_apk: bool = False
    pushed_config: bool = False
    created_forward: bool = False
    install_mode: str = ""
    install_reason: str = ""
    uninstalled_existing: bool = False
    cleanup_actions: list[str] = field(default_factory=list)


@dataclass
class AndroidBootstrapOptions:
    device_serial: str = ""
    local_port: int = 0
    install_apk: bool = True
    push_config: bool = True


_INSTALL_OVERRIDE_HINTS = {
    "INSTALL_FAILED_VERSION_DOWNGRADE": "version_downgrade",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "signature_mismatch",
    "INSTALL_FAILED_SHARED_USER_INCOMPATIBLE": "signature_mismatch",
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES": "signature_mismatch",
    "INSTALL_FAILED_CONFLICTING_PROVIDER": "mismatched_existing_apk",
    "INSTALL_FAILED_UID_CHANGED": "mismatched_existing_apk",
}


def _candidate_adb_paths() -> list[str]:
    candidates: list[str] = []
    for value in (
        os.environ.get("RDX_ANDROID_ADB_PATH", "").strip(),
        os.environ.get("ADB", "").strip(),
    ):
        if value:
            candidates.append(value)

    for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(env_name, "").strip()
        if not root:
            continue
        candidates.append(str(Path(root) / "platform-tools" / "adb.exe"))
        candidates.append(str(Path(root) / "platform-tools" / "adb"))

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))

    return candidates


def resolve_adb_path() -> str:
    for candidate in _candidate_adb_paths():
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    found = shutil.which("adb")
    if found:
        return str(Path(found).resolve())
    raise AndroidRemoteBootstrapError(
        "adb_unavailable",
        "adb executable not found. Install Android platform-tools or set RDX_ANDROID_ADB_PATH.",
    )


def _run_subprocess(
    cmd: list[str],
    *,
    timeout_s: float,
    error_code: str,
    error_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AndroidRemoteBootstrapError(error_code, error_message, details={"command": cmd, "reason": str(exc)}) from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidRemoteBootstrapError(
            error_code,
            f"{error_message}: timed out after {timeout_s:.1f}s",
            details={"command": cmd},
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise AndroidRemoteBootstrapError(
            error_code,
            f"{error_message}: {stderr or 'command failed'}",
            details={"command": cmd, "returncode": proc.returncode},
        )
    return proc


def _extract_install_reason(stderr: str) -> str:
    text = str(stderr or "").strip()
    for marker, reason in _INSTALL_OVERRIDE_HINTS.items():
        if marker in text:
            return reason
    return ""


def _should_force_replace_install(stderr: str) -> bool:
    return bool(_extract_install_reason(stderr))


def _run_install_command(adb_path: str, device_serial: str, apk_path: str, *, replace_existing: bool) -> subprocess.CompletedProcess[str]:
    install_args = ["install"]
    if replace_existing:
        install_args.append("-r")
    install_args.extend(["-g", "--force-queryable", str(apk_path)])
    return subprocess.run(
        _adb_base_cmd(adb_path, device_serial) + install_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180.0,
        check=False,
    )


def _ensure_android_helper_installed(adb_path: str, device_serial: str, package_name: str, apk_path: str, result: AndroidBootstrapResult) -> None:
    proc = _run_install_command(adb_path, device_serial, apk_path, replace_existing=True)
    if proc.returncode == 0:
        result.installed_apk = True
        result.install_mode = "upgrade"
        result.install_reason = "fresh_install" if "Success" in str(proc.stdout or "") else "mismatched_existing_apk"
        result.cleanup_actions.append("apk-installed")
        return

    stderr = (proc.stderr or proc.stdout or "").strip()
    install_reason = _extract_install_reason(stderr)
    if not _should_force_replace_install(stderr):
        raise AndroidRemoteBootstrapError(
            "android_apk_install_failed",
            f"Failed to install RenderDocCmd APK {Path(apk_path).name}: {stderr or 'command failed'}",
            details={
                "command": _adb_base_cmd(adb_path, device_serial) + ["install", "-r", "-g", "--force-queryable", str(apk_path)],
                "returncode": proc.returncode,
            },
        )

    uninstall_proc = subprocess.run(
        _adb_base_cmd(adb_path, device_serial) + ["uninstall", package_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60.0,
        check=False,
    )
    if uninstall_proc.returncode != 0:
        uninstall_stderr = (uninstall_proc.stderr or uninstall_proc.stdout or "").strip()
        raise AndroidRemoteBootstrapError(
            "android_apk_force_replace_uninstall_failed",
            f"Failed to uninstall mismatched RenderDocCmd package {package_name}: {uninstall_stderr or 'command failed'}",
            details={"package_name": package_name, "reason": install_reason or "mismatched_existing_apk"},
        )

    result.uninstalled_existing = True
    reinstall_proc = _run_install_command(adb_path, device_serial, apk_path, replace_existing=False)
    if reinstall_proc.returncode != 0:
        reinstall_stderr = (reinstall_proc.stderr or reinstall_proc.stdout or "").strip()
        raise AndroidRemoteBootstrapError(
            "android_apk_force_replace_reinstall_failed",
            f"Failed to reinstall RenderDocCmd APK {Path(apk_path).name}: {reinstall_stderr or 'command failed'}",
            details={"package_name": package_name, "reason": install_reason or "mismatched_existing_apk"},
        )

    result.installed_apk = True
    result.install_mode = "force_replace"
    result.install_reason = install_reason or "mismatched_existing_apk"
    result.cleanup_actions.append("apk-force-replaced")


def _adb_base_cmd(adb_path: str, device_serial: str = "") -> list[str]:
    cmd = [adb_path]
    if device_serial:
        cmd.extend(["-s", device_serial])
    return cmd


def parse_adb_devices(output: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0].strip()
        state = parts[1].strip()
        detail = " ".join(parts[2:]).strip()
        devices.append(AdbDevice(serial=serial, state=state, detail=detail))
    return devices


def choose_adb_device(devices: list[AdbDevice], requested_serial: str = "") -> AdbDevice:
    requested_serial = str(requested_serial or "").strip()
    if requested_serial:
        for device in devices:
            if device.serial == requested_serial:
                if device.state != "device":
                    raise AndroidRemoteBootstrapError(
                        "adb_device_unavailable",
                        f"adb device {requested_serial} is not ready: {device.state}",
                        details={"serial": requested_serial, "state": device.state},
                    )
                return device
        raise AndroidRemoteBootstrapError(
            "adb_device_not_found",
            f"adb device {requested_serial} was not found.",
            details={"serial": requested_serial},
        )

    ready = [device for device in devices if device.state == "device"]
    if not ready:
        raise AndroidRemoteBootstrapError("adb_no_device", "No Android device is available in `adb devices -l`.")
    if len(ready) > 1:
        serials = ", ".join(device.serial for device in ready)
        raise AndroidRemoteBootstrapError(
            "adb_multiple_devices",
            "Multiple Android devices are connected; specify `options.device_serial`.",
            details={"serials": serials},
        )
    return ready[0]


def normalize_android_arch(abi_value: str) -> tuple[str, str]:
    abi = str(abi_value or "").strip()
    lowered = abi.lower()
    if "arm64" in lowered:
        return "arm64", abi
    if "armeabi" in lowered or lowered.startswith("arm"):
        return "arm32", abi
    raise AndroidRemoteBootstrapError(
        "android_abi_unsupported",
        f"Unsupported Android ABI for packaged RenderDocCmd APKs: {abi or 'unknown'}",
        details={"abi": abi},
    )


def select_android_package(arch: str) -> tuple[str, Path]:
    key = str(arch or "").strip().lower()
    package = _PACKAGE_MAP.get(key)
    if not package:
        raise AndroidRemoteBootstrapError(
            "android_arch_unsupported",
            f"Unsupported Android RenderDocCmd package arch: {arch}",
            details={"arch": arch},
        )
    apk_path = android_binaries_root() / key / f"{package}.apk"
    if not apk_path.is_file():
        raise AndroidRemoteBootstrapError(
            "android_apk_missing",
            f"Missing Android RenderDocCmd APK: {apk_path}",
            details={"apk_path": str(apk_path)},
        )
    return package, apk_path


def allocate_local_port(requested_port: int = 0) -> int:
    if int(requested_port or 0) > 0:
        return int(requested_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _config_workspace() -> Path:
    path = runtime_root() / "android_remote"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_renderdoc_conf(package_name: str) -> Path:
    config_path = _config_workspace() / f"{package_name}.renderdoc.conf"
    config_path.write_text("", encoding="utf-8")
    return config_path


def _adb_shell(adb_path: str, device_serial: str, *args: str, timeout_s: float, error_code: str, error_message: str) -> subprocess.CompletedProcess[str]:
    cmd = _adb_base_cmd(adb_path, device_serial)
    cmd.extend(["shell", *args])
    return _run_subprocess(cmd, timeout_s=timeout_s, error_code=error_code, error_message=error_message)


def _stop_package(adb_path: str, device_serial: str, package_name: str) -> None:
    try:
        _adb_shell(
            adb_path,
            device_serial,
            "am",
            "force-stop",
            package_name,
            timeout_s=10.0,
            error_code="android_remote_stop_failed",
            error_message="Failed to stop RenderDocCmd activity",
        )
    except AndroidRemoteBootstrapError:
        return


def detect_device_arch(adb_path: str, device_serial: str) -> tuple[str, str]:
    for prop in ("ro.product.cpu.abilist64", "ro.product.cpu.abilist", "ro.product.cpu.abi"):
        proc = _adb_shell(
            adb_path,
            device_serial,
            "getprop",
            prop,
            timeout_s=10.0,
            error_code="android_abi_probe_failed",
            error_message=f"Failed to query Android ABI via {prop}",
        )
        values = [item.strip() for item in (proc.stdout or "").replace(",", "\n").splitlines() if item.strip()]
        for value in values:
            try:
                return normalize_android_arch(value)
            except AndroidRemoteBootstrapError:
                continue
    raise AndroidRemoteBootstrapError(
        "android_abi_unknown",
        "Unable to determine Android device ABI from adb properties.",
        details={"serial": device_serial},
    )


def _is_package_running(adb_path: str, device_serial: str, package_name: str) -> bool:
    try:
        proc = subprocess.run(
            _adb_base_cmd(adb_path, device_serial) + ["shell", "pidof", package_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0 and bool(str(proc.stdout or "").strip())


def _list_renderdoc_socket_ports(adb_path: str, device_serial: str) -> list[int]:
    try:
        proc = _adb_shell(
            adb_path,
            device_serial,
            "cat",
            "/proc/net/unix",
            timeout_s=10.0,
            error_code="android_socket_probe_failed",
            error_message="Failed to inspect Android unix sockets",
        )
    except AndroidRemoteBootstrapError:
        return []
    ports: list[int] = []
    for line in str(proc.stdout or "").splitlines():
        match = _REMOTE_SOCKET_RE.search(line)
        if not match:
            continue
        try:
            ports.append(int(match.group(1)))
        except Exception:
            continue
    return sorted(set(ports))


def _select_remote_socket_port(requested_port: int, before_ports: list[int], after_ports: list[int]) -> int:
    requested = int(requested_port or 0)
    if requested > 0 and requested in after_ports:
        return requested
    before = set(int(port) for port in before_ports)
    added = [int(port) for port in after_ports if int(port) not in before]
    if len(added) == 1:
        return added[0]
    if len(after_ports) == 1:
        return int(after_ports[0])
    if requested > 0:
        raise AndroidRemoteBootstrapError(
            "android_remote_socket_missing",
            f"RenderDoc remote socket renderdoc_{requested} was not found on Android device.",
            details={"requested_port": requested, "discovered_ports": list(after_ports)},
        )
    raise AndroidRemoteBootstrapError(
        "android_remote_socket_ambiguous",
        "Unable to determine Android RenderDoc remote socket port.",
        details={"discovered_ports": list(after_ports), "added_ports": added},
    )


def bootstrap_android_remote(
    *,
    remote_port: int = DEFAULT_ANDROID_REMOTE_PORT,
    options: Optional[AndroidBootstrapOptions] = None,
) -> AndroidBootstrapResult:
    opts = options or AndroidBootstrapOptions()
    adb_path = resolve_adb_path()
    device_proc = _run_subprocess(
        [adb_path, "devices", "-l"],
        timeout_s=10.0,
        error_code="adb_devices_failed",
        error_message="Failed to query adb devices",
    )
    device = choose_adb_device(parse_adb_devices(device_proc.stdout), opts.device_serial)
    arch, abi = detect_device_arch(adb_path, device.serial)
    package_name, apk_path = select_android_package(arch)
    activity_name = f"{package_name}.Loader"
    activity_component = f"{package_name}/.Loader"
    local_port = allocate_local_port(opts.local_port)
    forward_spec = f"tcp:{local_port}"
    result = AndroidBootstrapResult(
        adb_path=adb_path,
        device_serial=device.serial,
        package_name=package_name,
        activity_name=activity_name,
        abi=abi,
        host="127.0.0.1",
        port=local_port,
        remote_port=int(remote_port),
        apk_path=str(apk_path),
        forward_spec=forward_spec,
    )
    existing_socket_ports = _list_renderdoc_socket_ports(adb_path, device.serial)

    if opts.install_apk:
        _ensure_android_helper_installed(adb_path, device.serial, package_name, str(apk_path), result)

    if opts.push_config:
        config_path = _write_renderdoc_conf(package_name)
        remote_dir = f"/sdcard/Android/data/{package_name}/files"
        remote_conf = f"{remote_dir}/renderdoc.conf"
        _adb_shell(
            adb_path,
            device.serial,
            "mkdir",
            "-p",
            remote_dir,
            timeout_s=15.0,
            error_code="android_config_push_failed",
            error_message="Failed to create remote RenderDoc config directory",
        )
        _run_subprocess(
            _adb_base_cmd(adb_path, device.serial) + ["push", str(config_path), remote_conf],
            timeout_s=30.0,
            error_code="android_config_push_failed",
            error_message="Failed to push renderdoc.conf to Android device",
        )
        result.config_local_path = str(config_path)
        result.config_remote_path = remote_conf
        result.pushed_config = True
        result.cleanup_actions.append("config-pushed")

    _stop_package(adb_path, device.serial, package_name)
    time.sleep(0.5)

    launch_error: AndroidRemoteBootstrapError | None = None
    try:
        _adb_shell(
            adb_path,
            device.serial,
            "am",
            "start",
            "-W",
            "-n",
            activity_component,
            '-e',
            'renderdoccmd',
            'remoteserver',
            timeout_s=20.0,
            error_code="android_remote_launch_failed",
            error_message="Failed to launch RenderDocCmd activity",
        )
    except AndroidRemoteBootstrapError as exc:
        launch_error = exc

    if not _is_package_running(adb_path, device.serial, package_name):
        try:
            _adb_shell(
                adb_path,
                device.serial,
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
                timeout_s=20.0,
                error_code="android_remote_launch_failed",
                error_message="Failed to launch RenderDocCmd activity",
            )
        except AndroidRemoteBootstrapError as exc:
            launch_error = exc

    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _is_package_running(adb_path, device.serial, package_name):
            result.started_activity = True
            break
        time.sleep(1.0)

    if not result.started_activity:
        raise launch_error or AndroidRemoteBootstrapError(
            "android_remote_launch_failed",
            "Failed to confirm RenderDocCmd process startup on Android device.",
            details={"package_name": package_name, "device_serial": device.serial},
        )
    result.cleanup_actions.append("activity-started")

    remote_socket_port = _select_remote_socket_port(
        int(remote_port),
        existing_socket_ports,
        _list_renderdoc_socket_ports(adb_path, device.serial),
    )
    result.remote_port = int(remote_socket_port)

    _run_subprocess(
        _adb_base_cmd(adb_path, device.serial)
        + ["forward", forward_spec, f"localabstract:{_REMOTE_SOCKET_TEMPLATE.format(port=int(remote_socket_port))}"],
        timeout_s=10.0,
        error_code="adb_forward_failed",
        error_message="Failed to establish adb forward for RenderDoc remote socket",
    )
    result.created_forward = True
    result.cleanup_actions.append("adb-forward")
    return result


def cleanup_android_remote(result: AndroidBootstrapResult) -> list[str]:
    errors: list[str] = []
    if result.created_forward:
        try:
            _run_subprocess(
                _adb_base_cmd(result.adb_path, result.device_serial) + ["forward", "--remove", result.forward_spec],
                timeout_s=10.0,
                error_code="adb_forward_remove_failed",
                error_message="Failed to remove adb forward",
            )
        except AndroidRemoteBootstrapError as exc:
            errors.append(exc.message)

    if result.started_activity:
        try:
            _adb_shell(
                result.adb_path,
                result.device_serial,
                "am",
                "force-stop",
                result.package_name,
                timeout_s=10.0,
                error_code="android_remote_stop_failed",
                error_message="Failed to stop RenderDocCmd activity",
            )
        except AndroidRemoteBootstrapError as exc:
            errors.append(exc.message)

    if result.config_local_path:
        try:
            Path(result.config_local_path).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Failed to delete local config {result.config_local_path}: {exc}")

    return errors


def describe_android_remote(result: AndroidBootstrapResult) -> dict[str, object]:
    return {
        "transport": "adb_android",
        "device_serial": result.device_serial,
        "package_name": result.package_name,
        "activity_name": result.activity_name,
        "abi": result.abi,
        "apk_path": result.apk_path,
        "host": result.host,
        "port": result.port,
        "remote_port": result.remote_port,
        "forward_spec": result.forward_spec,
        "config_remote_path": result.config_remote_path,
        "installed_apk": result.installed_apk,
        "pushed_config": result.pushed_config,
        "started_activity": result.started_activity,
        "created_forward": result.created_forward,
        "install_mode": result.install_mode,
        "install_reason": result.install_reason,
        "uninstalled_existing": result.uninstalled_existing,
        "cleanup_actions": list(result.cleanup_actions),
    }






