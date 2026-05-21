from __future__ import annotations

from types import SimpleNamespace

from rdx.core import pipeline_service
from rdx.models import GraphicsAPI


class _FakeController:
    def __init__(self) -> None:
        self.d3d11 = SimpleNamespace()
        self.vulkan = SimpleNamespace(
            graphics=SimpleNamespace(pipelineResourceId="ResourceId::pipe"),
            vertexShader=SimpleNamespace(shaderResourceId="ResourceId::0"),
            tessControlShader=SimpleNamespace(shaderResourceId="ResourceId::0"),
            tessEvalShader=SimpleNamespace(shaderResourceId="ResourceId::0"),
            geometryShader=SimpleNamespace(shaderResourceId="ResourceId::0"),
            fragmentShader=SimpleNamespace(shaderResourceId="ResourceId::frag", entryPoint="main", reflection=SimpleNamespace()),
            computeShader=SimpleNamespace(shaderResourceId="ResourceId::0"),
            inputAssembly=SimpleNamespace(topology="TriangleList"),
        )

    def GetD3D11PipelineState(self) -> SimpleNamespace:
        return self.d3d11

    def GetD3D12PipelineState(self) -> None:
        return None

    def GetOpenGLPipelineState(self) -> None:
        return None

    def GetVulkanPipelineState(self) -> SimpleNamespace:
        return self.vulkan


def _fake_renderdoc() -> SimpleNamespace:
    return SimpleNamespace(
        ResourceId=lambda: "ResourceId::0",
        ShaderStage=SimpleNamespace(
            Vertex="vs",
            Hull="hs",
            Domain="ds",
            Geometry="gs",
            Pixel="ps",
            Compute="cs",
        ),
        GraphicsAPI=SimpleNamespace(
            D3D11="d3d11",
            D3D12="d3d12",
            OpenGL="opengl",
            Vulkan="vulkan",
        ),
    )


def test_null_resource_id_includes_renderdoc_zero_string(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    assert pipeline_service._is_null_id(None) is True
    assert pipeline_service._is_null_id("ResourceId::0") is True
    assert pipeline_service._is_null_id("0") is True
    assert pipeline_service._is_null_id("ResourceId::frag") is False


def test_remote_renderer_api_does_not_hide_vulkan_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    api, api_state = pipeline_service._select_api_specific_state(
        _FakeController(),
        GraphicsAPI.D3D11,
    )

    assert api == GraphicsAPI.VULKAN
    assert pipeline_service._shader_id_from_stage_object(api_state.fragmentShader) == "ResourceId::frag"


def test_vulkan_nested_graphics_pipeline_resource_is_used(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    rid = pipeline_service._api_specific_pipeline_object(
        _FakeController().vulkan,
        GraphicsAPI.VULKAN,
        pipeline_service.ShaderStage.PS,
    )

    assert rid == "ResourceId::pipe"


def test_stage_shader_id_falls_back_to_reflection_resource(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    stage = SimpleNamespace(
        resourceId="ResourceId::0",
        reflection=SimpleNamespace(resourceId="ResourceId::reflected-shader"),
    )

    assert pipeline_service._shader_id_from_stage_object(stage) == "ResourceId::reflected-shader"


def test_shader_id_from_reflection_resource(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    refl = SimpleNamespace(resourceId="ResourceId::reflection-only")

    assert pipeline_service._shader_id_from_reflection(refl) == "ResourceId::reflection-only"


def test_vulkan_shader_resolver_uses_pipeline_parent_shader_module(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pipeline_service, "_get_rd", _fake_renderdoc)

    class _Pipe:
        def GetShader(self, stage: object) -> str:
            return "ResourceId::0"

        def GetShaderReflection(self, stage: object) -> None:
            return None

        def GetGraphicsPipelineObject(self) -> str:
            return "ResourceId::pipe"

    class _Controller(_FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.vulkan.fragmentShader = SimpleNamespace(resourceId="ResourceId::0", reflection=None, entryPoint="main")
            self.shader_calls: list[tuple[object, object, object]] = []

        def GetResources(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    resourceId="ResourceId::pipe",
                    parentResources=["ResourceId::layout", "ResourceId::frag-module"],
                )
            ]

        def GetShaderEntryPoints(self, shader_id: object) -> list[SimpleNamespace]:
            if str(shader_id) == "ResourceId::frag-module":
                return [SimpleNamespace(name="main", stage="ps")]
            return []

        def GetShader(self, pipeline_id: object, shader_id: object, entry: object) -> SimpleNamespace:
            self.shader_calls.append((pipeline_id, shader_id, entry))
            if str(shader_id) != "ResourceId::frag-module":
                return None
            return SimpleNamespace(entryPoint="main", resourceId="ResourceId::frag-module")

    controller = _Controller()
    resolution = pipeline_service.resolve_shader_binding(
        controller,
        _Pipe(),
        controller.vulkan,
        GraphicsAPI.VULKAN,
        _fake_renderdoc().ShaderStage.Pixel,
        pipeline_service.ShaderStage.PS,
    )

    assert resolution.found is True
    assert str(resolution.shader_id) == "ResourceId::frag-module"
    assert str(resolution.pipeline_id) == "ResourceId::pipe"
    assert resolution.resolution_source == "pipeline_parent_shader_module"
    assert controller.shader_calls[0][0] == "ResourceId::pipe"
