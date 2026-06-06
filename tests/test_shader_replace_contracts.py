from __future__ import annotations

import asyncio
import difflib
import json
from types import SimpleNamespace

import pytest

from rdx import server
from rdx.core import patch_engine as patch_engine_mod
from rdx.models import PatchOp, PatchResult, PatchSpec, ShaderStage


class _FakePipe:
    def GetShader(self, stage: object) -> str:
        if stage == "ps":
            return "ResourceId::77"
        return "ResourceId::0"

    def GetShaderReflection(self, stage: object) -> SimpleNamespace:
        return SimpleNamespace(entryPoint="main")


class _SupportedController:
    def GetPipelineState(self) -> _FakePipe:
        return _FakePipe()

    def BuildTargetShader(self, *args, **kwargs) -> tuple[str, str]:  # type: ignore[no-untyped-def]
        return "ResourceId::99", ""

    def ReplaceResource(self, original: object, replacement: object) -> None:
        return None

    def RemoveReplacement(self, shader_id: object) -> None:
        return None

    def FreeTargetResource(self, shader_id: object) -> None:
        return None


class _EncodingPipe(_FakePipe):
    def __init__(self, encoding: object) -> None:
        self.encoding = encoding

    def GetShaderReflection(self, stage: object) -> SimpleNamespace:
        return SimpleNamespace(entryPoint="main", encoding=self.encoding)


class _EncodingController(_SupportedController):
    def __init__(self, encoding: object) -> None:
        self.encoding = encoding

    def GetPipelineState(self) -> _EncodingPipe:
        return _EncodingPipe(self.encoding)


class _UnsupportedController:
    def GetPipelineState(self) -> _FakePipe:
        return _FakePipe()


class _FakePatchEngine:
    def __init__(
        self,
        *,
        success: bool,
        error_code: str = "",
        error_category: str = "",
        error_details: dict[str, object] | None = None,
        messages: list[str] | None = None,
    ) -> None:
        self.success = success
        self.error_code = error_code
        self.error_category = error_category
        self.error_details = dict(error_details or {})
        self.messages = list(messages or [])
        self.calls: list[dict[str, object]] = []

    async def apply_patch(
        self,
        *,
        session_id: str,
        event_id: int,
        stage: object,
        session_manager: object,
        patch_spec: object,
    ) -> PatchResult:
        self.calls.append(
            {
                "session_id": session_id,
                "event_id": int(event_id),
                "stage": stage,
                "session_manager": session_manager,
                "patch_spec": patch_spec,
            }
        )
        if self.success:
            return PatchResult(
                patch_id=str(getattr(patch_spec, "patch_id", "")),
                applied_to_shader_hash="hash_after",
                original_shader_hash="hash_before",
                success=True,
                messages=list(self.messages),
                source_before_text="before",
                source_after_text="after",
                disassembly_target="SPIR-V (RenderDoc)",
                encoding="spirvasm",
                entry_point="main",
                compile_flags=[{"name": "optimization", "value": "0"}],
            )
        return PatchResult(
            patch_id=str(getattr(patch_spec, "patch_id", "")),
            success=False,
            error_message="patch failed",
            error_code=self.error_code,
            error_category=self.error_category,
            error_details=dict(self.error_details),
            messages=list(self.messages),
        )


class _FakeSessionManager:
    def __init__(self) -> None:
        self.state = SimpleNamespace(capabilities=SimpleNamespace(patch_supported=False))

    def get_state(self, session_id: str) -> SimpleNamespace:
        return self.state


@pytest.fixture(autouse=True)
def _reset_shader_replace_runtime() -> None:
    original_patch_engine = server.server_runtime._patch_engine
    original_session_manager = server.server_runtime._session_manager
    original_replacements = dict(server._runtime.shader_replacements)
    try:
        server._runtime.shader_replacements.clear()
        yield
    finally:
        server.server_runtime._patch_engine = original_patch_engine
        server.server_runtime._session_manager = original_session_manager
        server._runtime.shader_replacements = original_replacements


def _install_shader_replace_env(monkeypatch: pytest.MonkeyPatch, controller: object) -> None:
    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> object:
        return controller

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 101)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_rd_stage", lambda stage: stage)
    monkeypatch.setattr(server.server_runtime, "_is_null_resource_id", lambda rid: str(rid) in {"", "ResourceId::0", "0"})


def test_edit_and_replace_records_real_applied_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_engine = _FakePatchEngine(success=True)
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [
                        {
                            "op": "force_full_precision",
                            "variables": [],
                        }
                    ],
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["status"] == "applied"
    assert payload["resolved_event_id"] == 101
    assert payload["replacement"]["status"] == "applied"
    assert payload["replacement"]["original_shader_id"] == "ResourceId::77"
    assert "mock_applied" not in json.dumps(payload)
    assert len(patch_engine.calls) == 1
    patch_spec = patch_engine.calls[0]["patch_spec"]
    assert getattr(patch_spec, "target_event_id") == 101
    assert str(getattr(patch_spec, "target_shader_id")) == "ResourceId::77"
    assert server._runtime.shader_replacements["sess_demo"][0]["status"] == "applied"
    persisted_spec = server._runtime.shader_replacements["sess_demo"][0]["patch_spec"]
    assert persisted_spec["patch_id"] == payload["replacement_id"]
    assert persisted_spec["target_event_id"] == 101
    assert persisted_spec["target_stage"] == "ps"
    assert persisted_spec["target_shader_id"] == "ResourceId::77"
    assert persisted_spec["ops"][0]["op"] == "force_full_precision"
    assert session_manager.state.capabilities.patch_supported is True


def test_edit_and_replace_noop_patch_is_not_recorded_as_active_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_engine = _FakePatchEngine(
        success=True,
        messages=["Patch operations produced no source changes before recompilation."],
    )
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    async def _noop_apply_patch(**kwargs) -> PatchResult:  # type: ignore[no-untyped-def]
        patch_engine.calls.append(dict(kwargs))
        return PatchResult(
            patch_id=str(getattr(kwargs.get("patch_spec"), "patch_id", "")),
            applied_to_shader_hash="hash_same",
            original_shader_hash="hash_same",
            success=True,
            messages=["Patch operations produced no source changes before recompilation."],
        )

    patch_engine.apply_patch = _noop_apply_patch  # type: ignore[method-assign]

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [
                        {
                            "op": "force_full_precision",
                            "variables": [],
                        }
                    ],
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["status"] == "noop"
    assert payload["replacement"]["status"] == "noop"
    assert server._runtime.shader_replacements.get("sess_demo", []) == []


def test_edit_and_replace_can_emit_patch_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_engine = _FakePatchEngine(success=True)
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    async def _fake_store_text_artifact_payload(text: str, **kwargs) -> dict[str, object]:
        stem = str(kwargs.get("stem") or "")
        return {
            "title": str(kwargs.get("title") or stem),
            "saved_path": f"H:/fake/{stem}{kwargs.get('suffix')}",
            "type": "saved_path",
        }

    monkeypatch.setattr(
        server.server_runtime,
        "_store_text_artifact_payload",
        _fake_store_text_artifact_payload,
    )

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "emit_patch_artifacts": True,
                    "ops": [
                        {
                            "op": "force_full_precision",
                            "variables": [],
                        }
                    ],
                },
            )
        )
    )

    assert payload["success"] is True
    assert len(payload["artifacts"]) == 3
    replacement = payload["replacement"]
    assert replacement["compile"]["encoding"] == "spirvasm"
    assert replacement["compile"]["disassembly_target"] == "SPIR-V (RenderDoc)"
    assert replacement["artifacts"]["source_before"]["saved_path"].endswith("_before.txt")
    assert replacement["artifacts"]["source_after"]["saved_path"].endswith("_after.txt")
    assert replacement["artifacts"]["patch_diff"]["saved_path"].endswith("_diff.diff")


def test_edit_and_replace_accepts_source_text_workflow_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_engine = _FakePatchEngine(success=True)
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "source_text": "OpCapability Shader\n",
                    "source_target": "SPIR-V ASM",
                    "source_encoding": "spirvasm",
                    "expected_source_hash": "hash_demo",
                },
            )
        )
    )

    assert payload["success"] is True
    patch_spec = patch_engine.calls[0]["patch_spec"]
    assert getattr(patch_spec, "source_text") == "OpCapability Shader\n"
    assert getattr(patch_spec, "source_target") == "SPIR-V ASM"
    assert getattr(patch_spec, "source_encoding") == "spirvasm"
    assert getattr(patch_spec, "expected_source_hash") == "hash_demo"
    assert getattr(patch_spec, "ops") == []


def test_edit_and_replace_rejects_multiple_edit_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = _FakePatchEngine(success=True)
    server.server_runtime._session_manager = _FakeSessionManager()

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [{"op": "force_full_precision", "variables": []}],
                    "source_text": "OpCapability Shader\n",
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "validation_error"


def test_edit_and_replace_rejects_replace_expr_patch_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = _FakePatchEngine(success=True)
    server.server_runtime._session_manager = _FakeSessionManager()

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [{"op": "replace_expr", "expr_from": "a", "expr_to": "b"}],
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "validation_error"
    assert payload["details"]["unsupported_ops"] == ["replace_expr"]
    assert payload["details"]["agent_text_edit_inputs"] == ["source_text", "diff_text"]


def test_revert_replacement_refreshes_remote_session_when_last_patch_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRevertPatchEngine:
        async def revert_patch(self, session_id: str, patch_id: str, session_manager: object) -> bool:
            return True

    refresh_calls: list[dict[str, object]] = []

    async def _fake_refresh(session_id: str, *, remaining_replacements: list[dict[str, object]]) -> None:
        refresh_calls.append(
            {
                "session_id": session_id,
                "remaining_replacements": list(remaining_replacements),
            }
        )

    monkeypatch.setattr(
        server.server_runtime,
        "_maybe_refresh_remote_session_after_revert",
        _fake_refresh,
    )
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = _FakeRevertPatchEngine()
    server.server_runtime._session_manager = _FakeSessionManager()
    server._runtime.shader_replacements["sess_demo"] = [
        {
            "replacement_id": "repl_demo",
            "stage": "PS",
            "resolved_event_id": 314,
            "original_shader_id": "ResourceId::77",
            "status": "applied",
        }
    ]

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "revert_replacement",
                {
                    "session_id": "sess_demo",
                    "replacement_id": "repl_demo",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["reverted"] is True
    assert server._runtime.shader_replacements["sess_demo"] == []
    assert refresh_calls == [
        {
            "session_id": "sess_demo",
            "remaining_replacements": [],
        }
    ]


def test_revert_replacement_surfaces_remote_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRevertPatchEngine:
        async def revert_patch(self, session_id: str, patch_id: str, session_manager: object) -> bool:
            return True

    async def _failing_refresh(session_id: str, *, remaining_replacements: list[dict[str, object]]) -> None:
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        server.server_runtime,
        "_maybe_refresh_remote_session_after_revert",
        _failing_refresh,
    )
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = _FakeRevertPatchEngine()
    server.server_runtime._session_manager = _FakeSessionManager()
    server._runtime.shader_replacements["sess_demo"] = [
        {
            "replacement_id": "repl_demo",
            "stage": "PS",
            "resolved_event_id": 314,
            "original_shader_id": "ResourceId::77",
            "status": "applied",
        }
    ]

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "revert_replacement",
                {
                    "session_id": "sess_demo",
                    "replacement_id": "repl_demo",
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "replacement_revert_recovery_failed"
    assert payload["details"]["remaining_replacements"] == 0
    assert payload["details"]["exception_type"] == "RuntimeError"


def test_edit_and_replace_returns_capability_failure_without_mock_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_engine = _FakePatchEngine(success=True)
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _UnsupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [
                        {
                            "op": "force_full_precision",
                            "variables": [],
                        }
                    ],
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_replace_backend_unsupported"
    assert payload["category"] == "capability"
    assert payload["details"]["capability"] == "shader_replace"
    assert "mock_applied" not in json.dumps(payload)
    assert "status" not in payload
    assert patch_engine.calls == []
    assert session_manager.state.capabilities.patch_supported is False


def test_edit_and_replace_preserves_patch_engine_error_details(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_engine = _FakePatchEngine(
        success=False,
        error_code="shader_build_failed",
        error_category="runtime",
        error_details={
            "failure_stage": "build",
            "compiler_output": "compile error",
        },
    )
    session_manager = _FakeSessionManager()
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = patch_engine
    server.server_runtime._session_manager = session_manager

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "edit_and_replace",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "ops": [
                        {
                            "op": "force_full_precision",
                            "variables": [],
                        }
                    ],
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_build_failed"
    assert payload["category"] == "runtime"
    assert payload["details"]["failure_stage"] == "build"
    assert payload["details"]["compiler_output"] == "compile error"


def test_get_source_reports_ir_fallback_when_debug_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _EncodingController("SPIRV"))
    server.server_runtime._session_manager = _FakeSessionManager()

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_source",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "shader_id": "ResourceId::77",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["source"] is None
    assert payload["files"] == []
    assert payload["source_available"] is False
    assert payload["failure_reason"] == "source_debug_info_unavailable"
    assert payload["fallback_tool"] == "rd.shader.get_disassembly"
    assert payload["fallback_args"]["target"] == "SPIR-V ASM"
    assert payload["fallback_args"]["source_encoding"] == "spirvasm"
    assert payload["fallback_args"]["shader_id"] == "ResourceId::77"
    assert payload["edit_plan"]["input_kind"] == "text_ir"
    assert payload["edit_plan"]["recommended_next_tool"] == "rd.shader.get_disassembly"
    assert payload["edit_plan"]["allowed_ops"] == ["force_full_precision"]


def test_get_source_reports_dxil_readonly_fallback_when_debug_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _EncodingController("DXIL"))
    server.server_runtime._session_manager = _FakeSessionManager()

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_source",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "shader_id": "ResourceId::77",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["source_available"] is False
    assert payload["fallback_tool"] == "rd.shader.get_disassembly"
    assert payload["fallback_args"]["target"] == "auto"
    assert "source_encoding" not in payload["fallback_args"]
    assert payload["edit_plan"]["shader_format"]["container"] == "dxil"
    assert payload["edit_plan"]["input_kind"] == "renderdoc_disassembly"
    assert payload["edit_plan"]["can_replace"] is False
    assert payload["edit_plan"]["allowed_edit_inputs"] == []
    assert "rd.shader.extract_binary" in payload["edit_plan"]["fallback_tools"]


def test_remote_replacement_validation_uses_live_outputs_without_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Controller:
        def __init__(self) -> None:
            self.set_frame_event_calls: list[tuple[int, bool]] = []

        def SetFrameEvent(self, event_id: int, force: bool) -> None:
            self.set_frame_event_calls.append((event_id, force))

    controller = _Controller()
    previous_replays = dict(server._runtime.replays)
    try:
        server._runtime.replays["sess_remote"] = server.ReplayHandle(
            session_id="sess_remote",
            capture_file_id="capf_remote",
            frame_index=0,
            active_event_id=1248,
        )
        monkeypatch.setattr(
            server.server_runtime,
            "_context_state",
            lambda ctx: {
                "sessions": {
                    "sess_remote": {
                        "backend_type": "remote",
                        "remote": {"endpoint": "127.0.0.1:38920", "remote_id": "remote_demo"},
                    }
                }
            },
        )
        monkeypatch.setattr(
            server.server_runtime,
            "_session_record_remote_metadata",
            lambda record: {"endpoint": "127.0.0.1:38920"},
        )

        async def _fake_get_controller(session_id: str) -> object:
            return controller

        async def _fake_output_target_resource_ids(session_id: str, event_id: int) -> list[tuple[str, int]]:
            return [("ResourceId::208592", 0)]

        async def _fail_recovery(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("live replacement validation must not recover/reopen the remote capture")

        async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
            return fn(*args, **kwargs)

        monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
        monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", _fake_output_target_resource_ids)
        monkeypatch.setattr(server.server_runtime, "_recover_single_session_from_state", _fail_recovery)
        monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)

        asyncio.run(
            server.server_runtime._validate_remote_replacement_persistence(
                "sess_remote",
                {
                    "replacement_id": "repl_demo",
                    "resolved_event_id": 1248,
                    "patch_spec": {
                        "target_event_id": 1248,
                        "preserve_outputs": True,
                    },
                },
            )
        )

        assert controller.set_frame_event_calls == [(1248, True)]
    finally:
        server._runtime.replays = previous_replays


class _FakeShaderCompileFlag:
    def __init__(self) -> None:
        self.name = ""
        self.value = ""


class _FakeShaderCompileFlags:
    def __init__(self) -> None:
        self.flags: list[_FakeShaderCompileFlag] = []


class _PatchEnginePipe:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = list(outputs if outputs is not None else ["ResourceId::208592"])

    def GetShader(self, stage: object) -> str:
        return "ResourceId::77"

    def GetShaderReflection(self, stage: object) -> SimpleNamespace:
        return SimpleNamespace(
            entryPoint="main",
            debugInfo=SimpleNamespace(
                compileFlags=[
                    SimpleNamespace(name="optimization", value="0"),
                    SimpleNamespace(name="target", value="spirv"),
                ]
            ),
        )

    def GetGraphicsPipelineObject(self) -> str:
        return "ResourceId::9001"

    def GetOutputTargets(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(resourceId=rid) for rid in self.outputs]


class _PatchEngineController:
    def __init__(
        self,
        *,
        build_errors: str = "",
        build_shader_id: str = "ResourceId::99",
        replace_error: Exception | None = None,
        output_sequences: list[list[str]] | None = None,
    ) -> None:
        self.build_errors = build_errors
        self.build_shader_id = build_shader_id
        self.replace_error = replace_error
        self.build_calls: list[tuple[object, object, bytes, object, object]] = []
        self.replace_calls: list[tuple[object, object]] = []
        self.set_frame_event_calls: list[tuple[int, bool]] = []
        self.free_calls: list[object] = []
        self.remove_calls: list[object] = []
        self.output_sequences = list(output_sequences or [])

    def SetFrameEvent(self, event_id: int, force: bool) -> None:
        self.set_frame_event_calls.append((event_id, force))
        return None

    def GetPipelineState(self) -> _PatchEnginePipe:
        outputs = self.output_sequences.pop(0) if self.output_sequences else None
        return _PatchEnginePipe(outputs)

    def GetDisassemblyTargets(self, include_unsupported: bool) -> list[str]:
        return ["SPIR-V (RenderDoc)"]

    def GetTargetShaderEncodings(self) -> list[str]:
        return ["SPIRVAsm"]

    def DisassembleShader(self, pipeline: object, refl: object, target: str) -> str:
        return "OpEntryPoint Fragment %main \"main\""

    def BuildTargetShader(self, *args) -> tuple[str, str]:  # type: ignore[no-untyped-def]
        self.build_calls.append(args)
        if self.build_errors:
            return self.build_shader_id, self.build_errors
        return self.build_shader_id, ""

    def ReplaceResource(self, original: object, replacement: object) -> None:
        if self.replace_error is not None:
            raise self.replace_error
        self.replace_calls.append((original, replacement))

    def RemoveReplacement(self, shader_id: object) -> None:
        self.remove_calls.append(shader_id)
        return None

    def FreeTargetResource(self, shader_id: object) -> None:
        self.free_calls.append(shader_id)
        return None


class _PatchEngineSessionManager:
    def __init__(self, controller: _PatchEngineController) -> None:
        self.controller = controller

    def get_controller(self, session_id: str) -> _PatchEngineController:
        return self.controller


def test_list_replacements_filters_stale_metadata_when_patch_engine_has_no_live_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ListingPatchEngine:
        def list_patches(self, session_id: str):  # type: ignore[no-untyped-def]
            return []

    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._patch_engine = _ListingPatchEngine()
    server.server_runtime._session_manager = _FakeSessionManager()
    server._runtime.shader_replacements["sess_demo"] = [
        {
            "replacement_id": "repl_stale",
            "stage": "PS",
            "resolved_event_id": 314,
            "original_shader_id": "ResourceId::77",
            "status": "applied",
        }
    ]

    payload = json.loads(asyncio.run(server._dispatch_shader("list_replacements", {"session_id": "sess_demo"})))

    assert payload["success"] is True
    assert payload["replacements"] == []
    assert server._runtime.shader_replacements["sess_demo"] == []


def test_patch_engine_rebinds_target_event_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text='OpEntryPoint Fragment %main_changed "main"',
            ),
        )
    )

    assert result.success is True
    assert controller.set_frame_event_calls == [(314, True), (314, True), (314, True)]


def test_patch_engine_rolls_back_when_replacement_removes_event_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController(
        output_sequences=[
            ["ResourceId::208592"],
            [],
            [],
        ],
    )
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text='OpEntryPoint Fragment %main_changed "main"',
                preserve_outputs=True,
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_replace_preserve_outputs_failed"
    assert result.error_details["output_targets_before"] == ["ResourceId::208592"]
    assert result.error_details["output_targets_after"] == []
    assert controller.replace_calls == [("ResourceId::77", "ResourceId::99")]
    assert controller.remove_calls == ["ResourceId::77"]
    assert controller.free_calls == ["ResourceId::99"]


def test_patch_engine_build_target_shader_uses_shader_compile_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
                patch_spec=PatchSpec(
                    patch_id="repl_demo",
                    target_event_id=314,
                    target_stage=ShaderStage.PS,
                    target_shader_id="ResourceId::77",
                    source_text='OpEntryPoint Fragment %main_changed "main"',
                ),
            )
    )

    assert result.success is True
    assert result.messages == ["Applied source_text replacement."]
    assert len(controller.build_calls) == 1
    compile_flags = controller.build_calls[0][3]
    assert isinstance(compile_flags, _FakeShaderCompileFlags)
    assert [(item.name, item.value) for item in compile_flags.flags] == [
        ("optimization", "0"),
        ("target", "spirv"),
    ]
    assert controller.replace_calls == [("ResourceId::77", "ResourceId::99")]


def test_patch_engine_assembles_raw_spirv_asm_when_backend_accepts_binary_spirv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_assemble_spirv_asm",
        staticmethod(lambda source: b"\x03\x02\x23\x07assembled"),
    )

    class _BinarySpirvController(_PatchEngineController):
        def GetDisassemblyTargets(self, include_unsupported: bool) -> list[str]:
            return ["SPIR-V ASM"]

        def GetTargetShaderEncodings(self) -> list[str]:
            return ["SPIRV"]

        def DisassembleShader(self, pipeline: object, refl: object, target: str) -> str:
            return "; SPIR-V\nOpCapability Shader\nOpEntryPoint Fragment %main \"main\"\n"

    controller = _BinarySpirvController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text="; SPIR-V\nOpCapability Shader\nOpEntryPoint Fragment %main_changed \"main\"\n",
                source_target="SPIR-V ASM",
                source_encoding="spirvasm",
            ),
        )
    )

    assert result.success is True
    build_call = controller.build_calls[0]
    assert build_call[1] == "SPIRV"
    assert build_call[2] == b"\x03\x02\x23\x07assembled"


def test_patch_engine_build_compile_flags_supports_renderdoc_flag_container(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)

    refl = SimpleNamespace(
        debugInfo=SimpleNamespace(
            compileFlags=SimpleNamespace(
                flags=[
                    SimpleNamespace(name="optimization", value="0"),
                    SimpleNamespace(name="target", value="spirv"),
                ]
            )
        )
    )

    compile_flags = patch_engine_mod.PatchEngine._build_compile_flags(refl)

    assert [(item.name, item.value) for item in compile_flags.flags] == [
        ("optimization", "0"),
        ("target", "spirv"),
    ]


def test_patch_engine_skips_build_and_replace_when_patch_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
                patch_spec=PatchSpec(
                    patch_id="repl_demo",
                    target_event_id=314,
                    target_stage=ShaderStage.PS,
                    target_shader_id="ResourceId::77",
                    source_text='OpEntryPoint Fragment %main "main"',
                ),
            )
        )

    assert result.success is True
    assert result.applied_to_shader_hash == result.original_shader_hash
    assert controller.build_calls == []
    assert controller.replace_calls == []


def test_patch_engine_supports_source_text_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text="OpCapability Shader\nOpEntryPoint Fragment %main_changed \"main\"\n",
            ),
        )
    )

    assert result.success is True
    assert controller.build_calls
    assert controller.build_calls[0][2] == b"OpCapability Shader\nOpEntryPoint Fragment %main_changed \"main\"\n"


def test_patch_engine_supports_diff_text_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()
    before = "OpEntryPoint Fragment %main \"main\""
    after = "OpEntryPoint Fragment %main_changed \"main\""
    diff_text = "".join(
        difflib.unified_diff(
            [before + "\n"],
            [after + "\n"],
            fromfile="before",
            tofile="after",
        )
    )

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                diff_text=diff_text,
            ),
        )
    )

    assert result.success is True
    assert controller.build_calls[0][2] == (after + "\n").encode("utf-8")


def test_patch_engine_reports_source_hash_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text="OpCapability Shader\n",
                expected_source_hash="mismatch",
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_source_mismatch"
    assert result.error_category == "validation"
    assert controller.build_calls == []


def test_apply_precision_patch_supports_renderdoc_spirv_relaxedprecision_annotations() -> None:
    source = (
        'float3 _213 = CompositeConstruct({_1192, _1192, _1192}) : [[RelaxedPrecision]];\n'
        'float _421 = Dot(_404, _404) : [[RelaxedPrecision]];\n'
    )

    modified = patch_engine_mod.PatchEngine._apply_precision_patch(
        source,
        "spirv",
        ["213", "404"],
    )

    assert "[[RelaxedPrecision]]" not in modified
    assert "float3 _213 = CompositeConstruct({_1192, _1192, _1192});" in modified
    assert "float _421 = Dot(_404, _404);" in modified


def test_apply_precision_patch_removes_spirv_member_relaxedprecision_annotations() -> None:
    source = (
        "OpDecorate %3 RelaxedPrecision\n"
        "OpMemberDecorate %12 22 RelaxedPrecision\n"
        "OpMemberDecorate %12 22 Offset 3732\n"
    )

    modified = patch_engine_mod.PatchEngine._apply_precision_patch(
        source,
        "spirvasm",
        [],
    )

    assert "RelaxedPrecision" not in modified
    assert "OpMemberDecorate %12 22 Offset 3732" in modified


def test_insert_guard_wraps_hlsl_expression() -> None:
    source = "float value = lighting;"

    modified = patch_engine_mod.PatchEngine._apply_guard_patch(
        source,
        "hlsl",
        "lighting",
        "0.0",
    )

    assert modified == "float value = (isnan(lighting) || isinf(lighting) ? 0.0 : lighting);"


def test_insert_guard_wraps_glsl_expression() -> None:
    source = "vec3 color = lighting;"

    modified = patch_engine_mod.PatchEngine._apply_guard_patch(
        source,
        "glsl",
        "lighting",
        "vec3(0.0)",
    )

    assert modified == "vec3 color = (isnan(lighting) || isinf(lighting) ? vec3(0.0) : lighting);"


def test_insert_guard_requires_guard_expr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                ops=[PatchOp(op="insert_guard")],
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "validation_error"
    assert result.error_details["failure_reason"] == "missing_guard_expr"
    assert controller.build_calls == []


def test_insert_guard_rejects_spirv_asm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                ops=[PatchOp(op="insert_guard", guard_expr="lighting")],
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_patch_op_unsupported_for_encoding"
    assert result.error_details["agent_text_edit_inputs"] == ["source_text", "diff_text"]
    assert controller.build_calls == []


def test_collect_spirv_precision_targets_matches_exact_tokens_only() -> None:
    source = (
        "float3 _213 = CompositeConstruct({_1192, _1192, _1192}) : [[RelaxedPrecision]];\n"
        "float _2130 = Dot(_2138, {0.3000, 0.5898, 0.1100}) : [[RelaxedPrecision]];\n"
    )

    matches = patch_engine_mod.PatchEngine._collect_spirv_precision_targets(
        source,
        ["213"],
    )

    assert matches == [
        (
            1,
            "float3 _213 = CompositeConstruct({_1192, _1192, _1192}) : [[RelaxedPrecision]];",
        )
    ]


def test_patch_engine_encoding_name_maps_numeric_spirv_encoding() -> None:
    assert patch_engine_mod.PatchEngine._encoding_name(3) == "spirv"
    assert patch_engine_mod.PatchEngine._encoding_name(4) == "spirvasm"


def test_patch_engine_reports_structured_build_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController(build_errors="compile error")
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text='OpEntryPoint Fragment %main_changed "main"',
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_build_failed"
    assert result.error_category == "runtime"
    assert result.error_details["compiler_output"] == "compile error"
    assert result.error_details["compile_flags"] == [
        {"name": "optimization", "value": "0"},
        {"name": "target", "value": "spirv"},
    ]


def test_patch_engine_does_not_replace_when_compiler_reports_fatal_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController(
        build_errors="error: invalid SPIR-V after source edit",
        build_shader_id="ResourceId::99",
    )
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text='OpEntryPoint Fragment %main_changed "main"',
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_build_failed"
    assert result.error_details["replacement_attempted"] is False
    assert result.error_details["cleanup_attempted"] is True
    assert controller.replace_calls == []
    assert controller.free_calls == ["ResourceId::99"]


def test_patch_engine_rejects_dxil_disassembly_before_build_or_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("DXIL", "DXIL")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text="// edited DXIL disassembly",
            ),
        )
    )

    assert result.success is False
    assert result.error_code == "shader_replace_backend_unsupported"
    assert result.error_details["failure_stage"] == "validate_edit_plan"
    assert result.error_details["replacement_attempted"] is False
    assert result.error_details["edit_plan"]["shader_format"]["container"] == "dxil"
    assert controller.build_calls == []
    assert controller.replace_calls == []


def test_revert_patch_rebinds_target_event(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rd = SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderCompileFlags=_FakeShaderCompileFlags,
        ShaderCompileFlag=_FakeShaderCompileFlag,
    )
    monkeypatch.setattr(patch_engine_mod, "_get_rd", lambda: fake_rd)
    monkeypatch.setattr(patch_engine_mod, "_to_rd_stage", lambda stage: "ps")
    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_get_best_encoding",
        staticmethod(lambda controller, session_id: ("SPIRVAsm", "SPIR-V (RenderDoc)")),
    )

    controller = _PatchEngineController()
    session_manager = _PatchEngineSessionManager(controller)
    engine = patch_engine_mod.PatchEngine()

    result = asyncio.run(
        engine.apply_patch(
            session_id="sess_demo",
            event_id=314,
            stage=ShaderStage.PS,
            session_manager=session_manager,
            patch_spec=PatchSpec(
                patch_id="repl_demo",
                target_event_id=314,
                target_stage=ShaderStage.PS,
                target_shader_id="ResourceId::77",
                source_text='OpEntryPoint Fragment %main_changed "main"',
            ),
        )
    )

    assert result.success is True
    assert asyncio.run(engine.revert_patch("sess_demo", "repl_demo", session_manager)) is True
    assert controller.set_frame_event_calls[-1] == (314, True)


def test_get_disassembly_reports_raw_spirv_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _SupportedController())
    server.server_runtime._session_manager = _FakeSessionManager()

    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_resolve_source",
        classmethod(lambda cls, controller, pipe, refl, stage, session_id, **kwargs: ("OpCapability Shader\n", "SPIRVAsm", "SPIR-V ASM", True)),
    )

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_disassembly",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "target": "SPIR-V ASM",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["target"] == "SPIR-V ASM"
    assert payload["source_encoding"] == "spirvasm"
    assert payload["is_raw_spirv_asm"] is True
    assert payload["source_hash"]
    assert payload["edit_plan"]["input_kind"] == "text_ir"
    assert payload["edit_plan"]["can_replace"] is True
    assert payload["edit_plan"]["allowed_ops"] == ["force_full_precision"]


def test_get_disassembly_reports_dxil_readonly_edit_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_shader_replace_env(monkeypatch, _EncodingController("DXIL"))
    server.server_runtime._session_manager = _FakeSessionManager()

    monkeypatch.setattr(
        patch_engine_mod.PatchEngine,
        "_resolve_source",
        classmethod(lambda cls, controller, pipe, refl, stage, session_id, **kwargs: ("// DXIL disassembly\n", "DXIL", "DXIL", False)),
    )

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_disassembly",
                {
                    "session_id": "sess_demo",
                    "event_id": 101,
                    "stage": "ps",
                    "target": "auto",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["target"] == "DXIL"
    assert payload["source_encoding"] == "dxil"
    assert payload["edit_plan"]["shader_format"]["container"] == "dxil"
    assert payload["edit_plan"]["input_kind"] == "renderdoc_disassembly"
    assert payload["edit_plan"]["can_edit_text"] is False
    assert payload["edit_plan"]["can_replace"] is False
    assert payload["edit_plan"]["recommended_next_tool"] == "rd.shader.extract_binary"
