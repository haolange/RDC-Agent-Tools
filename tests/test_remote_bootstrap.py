from __future__ import annotations

from types import SimpleNamespace

import pytest

from rdx import remote_bootstrap
from rdx.remote_bootstrap import AdbDevice, AndroidBootstrapResult


def test_choose_adb_device_requires_serial_when_multiple() -> None:
    devices = [
        AdbDevice(serial="device-a", state="device"),
        AdbDevice(serial="device-b", state="device"),
    ]

    with pytest.raises(remote_bootstrap.AndroidRemoteBootstrapError) as excinfo:
        remote_bootstrap.choose_adb_device(devices)

    assert excinfo.value.code == "adb_multiple_devices"


def test_select_android_package_matches_packaged_apk() -> None:
    package_name, apk_path = remote_bootstrap.select_android_package("arm64")

    assert package_name == "org.renderdoc.renderdoccmd.arm64"
    assert apk_path.is_file()


def test_cleanup_android_remote_removes_forward_and_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        commands.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _fake_shell(adb_path: str, device_serial: str, *args: str, **_: object) -> SimpleNamespace:
        commands.append([adb_path, "-s", device_serial, "shell", *args])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remote_bootstrap, "_run_subprocess", _fake_run)
    monkeypatch.setattr(remote_bootstrap, "_adb_shell", _fake_shell)

    config_path = tmp_path / "renderdoc.conf"
    config_path.write_text("", encoding="utf-8")

    result = AndroidBootstrapResult(
        adb_path="adb",
        device_serial="serial-1",
        package_name="org.renderdoc.renderdoccmd.arm64",
        activity_name="org.renderdoc.renderdoccmd.arm64.Loader",
        abi="arm64-v8a",
        host="127.0.0.1",
        port=38960,
        remote_port=38920,
        apk_path="apk",
        forward_spec="tcp:38960",
        config_local_path=str(config_path),
        config_remote_path="/sdcard/Android/data/org.renderdoc.renderdoccmd.arm64/files/renderdoc.conf",
        started_activity=True,
        created_forward=True,
    )

    errors = remote_bootstrap.cleanup_android_remote(result)

    assert errors == []
    assert not config_path.exists()
    assert any(cmd[:3] == ["adb", "-s", "serial-1"] and "forward" in cmd for cmd in commands)
    assert any(cmd[:3] == ["adb", "-s", "serial-1"] and "force-stop" in cmd for cmd in commands)

def test_select_remote_socket_port_prefers_requested_port() -> None:
    assert remote_bootstrap._select_remote_socket_port(38920, [39920], [38920, 39920]) == 38920


def test_select_remote_socket_port_falls_back_to_detected_single_port() -> None:
    assert remote_bootstrap._select_remote_socket_port(38920, [], [39920]) == 39920
