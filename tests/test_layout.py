from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_has_unique_tools_and_declared_count() -> None:
    catalog = ROOT / "spec" / "tool_catalog.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    tools = payload.get("tools", [])
    names = [str(t.get("name", "")).strip() for t in tools]
    declared_count = int(payload.get("tool_count") or len(names))
    assert len(names) == declared_count
    assert len(set(names)) == len(names)
    assert all(n.startswith("rd.") for n in names)


def test_catalog_uses_repo_relative_source_path_and_readable_groups() -> None:
    catalog = ROOT / "spec" / "tool_catalog.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    assert payload.get("source_path") == "spec/doc_extracted.txt"
    groups = payload.get("groups", {})
    assert isinstance(groups, dict)
    assert groups
    for group_name in groups:
        text = str(group_name)
        assert "?" not in text
        assert "\ufffd" not in text
        assert "Context Snapshot Tools" in text or "????" not in text


def test_catalog_boundaries_remove_legacy_surfaces_and_expand_export_params() -> None:
    catalog = ROOT / "spec" / "tool_catalog.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    tools = payload.get("tools", [])
    names = {str(t.get("name", "")).strip() for t in tools}
    assert int(payload.get("tool_count") or 0) == 200

    removed = {
        "rd.app.is_available",
        "rd.app.start_frame_capture",
        "rd.app.end_frame_capture",
        "rd.app.trigger_capture",
        "rd.app.set_capture_option",
        "rd.app.get_capture_options",
        "rd.app.push_marker",
        "rd.app.pop_marker",
        "rd.app.set_marker",
        "rd.texture.save_to_file",
        "rd.buffer.save_to_file",
        "rd.mesh.export",
        "rd.analysis.get_frame_stats",
        "rd.analysis.get_event_stats",
        "rd.analysis.get_warnings",
        "rd.analysis.estimate_overdraw",
        "rd.macro.generate_pass_summary",
        "rd.macro.locate_draw_affecting_pixel",
        "rd.macro.trace_resource_lifetime",
        "rd.macro.find_nan_inf_in_targets",
    }
    assert not (removed & names)

    export_texture = next(tool for tool in tools if tool.get("name") == "rd.export.texture")
    export_buffer = next(tool for tool in tools if tool.get("name") == "rd.export.buffer")
    export_mesh = next(tool for tool in tools if tool.get("name") == "rd.export.mesh")
    core_init = next(tool for tool in tools if tool.get("name") == "rd.core.init")

    assert {"channels", "flip_y", "subresource", "file_format", "remap"} <= set(export_texture.get("param_names", []))
    assert {"buffer_id", "offset", "size", "output_path"} <= set(export_buffer.get("param_names", []))
    assert {"include_attributes", "space", "format", "output_path"} <= set(export_mesh.get("param_names", []))
    assert "enable_app_api" not in set(core_init.get("param_names", []))


def test_required_directories_exist() -> None:
    required = [
        ROOT / "rdx",
        ROOT / "mcp",
        ROOT / "cli",
        ROOT / "spec",
        ROOT / "policy",
        ROOT / "docs",
        ROOT / "tests",
        ROOT / "binaries" / "windows" / "x64" / "python",
        ROOT / "binaries" / "windows" / "x64" / "pymodules",
        ROOT / "intermediate" / "runtime" / "rdx_cli",
        ROOT / "intermediate" / "runtime" / "worker-cache",
        ROOT / "intermediate" / "runtime" / "worker-state",
        ROOT / "intermediate" / "artifacts",
        ROOT / "intermediate" / "pytest",
        ROOT / "intermediate" / "logs",
    ]
    for p in required:
        assert p.is_dir(), str(p)


def test_runtime_manifest_declares_bundled_python_and_worker_materialize_flags() -> None:
    manifest = ROOT / "binaries" / "windows" / "x64" / "manifest.runtime.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    files = payload.get("files")
    assert isinstance(files, list) and files

    bundled_python = payload.get("bundled_python")
    assert isinstance(bundled_python, dict)
    for key in (
        "python_version",
        "python_entry",
        "pythonw_entry",
        "python3_dll",
        "python_dll",
        "stdlib_layout",
        "site_packages",
        "dll_dir",
        "pth_file",
    ):
        assert str(bundled_python.get(key) or "").strip(), key

    indexed = {str(item.get("path") or ""): item for item in files if isinstance(item, dict)}
    assert indexed[str(bundled_python["python_entry"])] ["worker_materialize"] is False
    assert indexed[str(bundled_python["python_dll"])] ["worker_materialize"] is False
    assert indexed["renderdoc.dll"]["worker_materialize"] is True
    assert indexed["pymodules/renderdoc.pyd"]["worker_materialize"] is True