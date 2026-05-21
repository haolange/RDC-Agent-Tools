from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SUPPORTED_SCRIPTS = (
    "scripts/README.md",
    "scripts/check_markdown_health.py",
    "scripts/cleanup_workspace.py",
    "scripts/generate_release_checksums.py",
    "scripts/package_runtime.py",
    "scripts/preview_geometry_smoke.py",
    "scripts/rdx_bat_command_smoke.py",
    "scripts/rdx_bat_launcher.ps1",
    "scripts/release_gate.py",
    "scripts/smoke_report_aggregator.py",
    "scripts/tool_contract_check.py",
    "scripts/tool_contract_remote_smoke.py",
)

INTERNAL_SCRIPT_HELPERS = (
    "scripts/_shared.py",
)

DELETED_ENTRIES = (
    "scripts/android_remote_whitehair_investigation.py",
    "scripts/build_offline_replay_detailed_report.py",
    "scripts/native_smoke.py",
    "scripts/run_smoke_196_dual_sample.py",
    "whitehair_remote_regression.py",
    "whitehair_pybind_probe.py",
    "scripts/arg_test.ps1",
    "docs/whitehair-android-remote-retrospective.md",
)

FORMAL_REFERENCES = (
    "AGENTS.md",
    "README.md",
    "docs/README.md",
    "docs/android-remote-cli-smoke-prompt.md",
    "docs/doc-governance.md",
    "docs/troubleshooting.md",
    *SUPPORTED_SCRIPTS,
    *INTERNAL_SCRIPT_HELPERS,
)

FORBIDDEN_SNIPPETS = (
    "Desktop",
    "platform-tools\\adb.exe",
    "e38b8019",
)


def test_supported_scripts_exist() -> None:
    for rel in (*SUPPORTED_SCRIPTS, *INTERNAL_SCRIPT_HELPERS):
        assert (ROOT / rel).is_file(), rel


def test_supported_scripts_do_not_embed_machine_local_defaults() -> None:
    for rel in (*SUPPORTED_SCRIPTS, *INTERNAL_SCRIPT_HELPERS):
        text = (ROOT / rel).read_text(encoding="utf-8-sig")
        lower_text = text.lower()
        assert "path.home()" not in text, rel
        for snippet in FORBIDDEN_SNIPPETS:
            assert snippet not in text, f"{rel}: {snippet}"
        assert "c:\\users\\" not in lower_text, rel
        assert "d:\\utility\\" not in lower_text, rel


def test_deleted_entries_are_not_referenced_from_formal_surface() -> None:
    for rel in FORMAL_REFERENCES:
        text = (ROOT / rel).read_text(encoding="utf-8-sig")
        for deleted in DELETED_ENTRIES:
            assert deleted not in text, f"{rel}: {deleted}"
