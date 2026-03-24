from __future__ import annotations

import asyncio
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
                            "op": "replace_expr",
                            "expr_from": "foo",
                            "expr_to": "bar",
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
    assert session_manager.state.capabilities.patch_supported is True


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
                            "op": "replace_expr",
                            "expr_from": "foo",
                            "expr_to": "bar",
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
                            "op": "replace_expr",
                            "expr_from": "foo",
                            "expr_to": "bar",
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


class _FakeShaderCompileFlag:
    def __init__(self) -> None:
        self.name = ""
        self.value = ""


class _FakeShaderCompileFlags:
    def __init__(self) -> None:
        self.flags: list[_FakeShaderCompileFlag] = []


class _PatchEnginePipe:
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


class _PatchEngineController:
    def __init__(self, *, build_errors: str = "", replace_error: Exception | None = None) -> None:
        self.build_errors = build_errors
        self.replace_error = replace_error
        self.build_calls: list[tuple[object, object, bytes, object, object]] = []
        self.replace_calls: list[tuple[object, object]] = []

    def SetFrameEvent(self, event_id: int, force: bool) -> None:
        return None

    def GetPipelineState(self) -> _PatchEnginePipe:
        return _PatchEnginePipe()

    def GetDisassemblyTargets(self, include_unsupported: bool) -> list[str]:
        return ["SPIR-V (RenderDoc)"]

    def GetTargetShaderEncodings(self) -> list[str]:
        return ["SPIRVAsm"]

    def DisassembleShader(self, pipeline: object, refl: object, target: str) -> str:
        return "OpEntryPoint Fragment %main \"main\""

    def BuildTargetShader(self, *args) -> tuple[str, str]:  # type: ignore[no-untyped-def]
        self.build_calls.append(args)
        if self.build_errors:
            return "ResourceId::0", self.build_errors
        return "ResourceId::99", ""

    def ReplaceResource(self, original: object, replacement: object) -> None:
        if self.replace_error is not None:
            raise self.replace_error
        self.replace_calls.append((original, replacement))

    def FreeTargetResource(self, shader_id: object) -> None:
        return None


class _PatchEngineSessionManager:
    def __init__(self, controller: _PatchEngineController) -> None:
        self.controller = controller

    def get_controller(self, session_id: str) -> _PatchEngineController:
        return self.controller


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
                ops=[PatchOp(op="replace_expr", expr_from="main", expr_to="main")],
            ),
        )
    )

    assert result.success is True
    assert len(controller.build_calls) == 1
    compile_flags = controller.build_calls[0][3]
    assert isinstance(compile_flags, _FakeShaderCompileFlags)
    assert [(item.name, item.value) for item in compile_flags.flags] == [
        ("optimization", "0"),
        ("target", "spirv"),
    ]
    assert controller.replace_calls == [("ResourceId::77", "ResourceId::99")]


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
                ops=[PatchOp(op="replace_expr", expr_from="main", expr_to="main")],
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
