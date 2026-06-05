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


def test_resolve_adb_path_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    adb_path = tmp_path / "adb.exe"
    adb_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("RDX_ANDROID_ADB_PATH", str(adb_path))
    monkeypatch.setattr(remote_bootstrap.shutil, "which", lambda _: None)

    assert remote_bootstrap.resolve_adb_path() == str(adb_path.resolve())


def test_describe_android_remote_exposes_bootstrap_flags() -> None:
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
        config_remote_path="/sdcard/Android/data/org.renderdoc.renderdoccmd.arm64/files/renderdoc.conf",
        installed_apk=True,
        pushed_config=True,
        started_activity=True,
        created_forward=True,
        install_mode="force_replace",
        install_reason="signature_mismatch",
        uninstalled_existing=True,
        cleanup_actions=["apk-installed", "adb-forward"],
    )

    payload = remote_bootstrap.describe_android_remote(result)

    assert payload["installed_apk"] is True
    assert payload["pushed_config"] is True
    assert payload["started_activity"] is True
    assert payload["created_forward"] is True
    assert payload["install_mode"] == "force_replace"
    assert payload["install_reason"] == "signature_mismatch"
    assert payload["uninstalled_existing"] is True
    assert payload["cleanup_actions"] == ["apk-installed", "adb-forward"]


def test_install_helper_force_replaces_mismatched_apk(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        commands.append(list(cmd))
        if "install" in cmd and "-r" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="INSTALL_FAILED_UPDATE_INCOMPATIBLE")
        return SimpleNamespace(returncode=0, stdout="Success", stderr="")

    monkeypatch.setattr(remote_bootstrap.subprocess, "run", _fake_run)

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
    )

    remote_bootstrap._ensure_android_helper_installed("adb", "serial-1", result.package_name, "apk", result)

    assert result.installed_apk is True
    assert result.install_mode == "force_replace"
    assert result.install_reason == "signature_mismatch"
    assert result.uninstalled_existing is True
    assert any(cmd[:4] == ["adb", "-s", "serial-1", "uninstall"] for cmd in commands)


def test_install_helper_reports_uninstall_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        if "install" in cmd and "-r" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="INSTALL_FAILED_VERSION_DOWNGRADE")
        if "uninstall" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="DELETE_FAILED_INTERNAL_ERROR")
        return SimpleNamespace(returncode=0, stdout="Success", stderr="")

    monkeypatch.setattr(remote_bootstrap.subprocess, "run", _fake_run)

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
    )

    with pytest.raises(remote_bootstrap.AndroidRemoteBootstrapError) as excinfo:
        remote_bootstrap._ensure_android_helper_installed("adb", "serial-1", result.package_name, "apk", result)

    assert excinfo.value.code == "android_apk_force_replace_uninstall_failed"


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
