"""Provider 测试：能力矩阵、模型自动切换、缓存与预算。"""
from __future__ import annotations

from godfather.providers.base import COST_FREE, COST_UNKNOWN, ProviderRequest
from godfather.providers.manual import ManualImportProvider, validate_import_payload
from godfather.providers.ollama import OllamaProvider
from godfather.providers.registry import TeacherService


def test_ollama_capabilities():
    provider = OllamaProvider(base_url="http://127.0.0.1:11434")
    caps = provider.capabilities()
    assert caps.provider == "ollama"
    assert caps.text is True
    assert caps.usage_visible is True
    assert provider.estimate_cost(ProviderRequest(prompt="x"))[1] == COST_FREE


def test_ollama_model_auto_switch(monkeypatch):
    provider = OllamaProvider(base_url="http://127.0.0.1:11434", default_model="qwen2.5:7b")
    monkeypatch.setattr(provider, "list_models", lambda: ["qwen3:0.6b", "llama3:8b"])
    model, switched = provider.resolve_model("qwen2.5:7b")
    assert model == "qwen3:0.6b"
    assert switched is True


def test_ollama_model_keeps_requested(monkeypatch):
    provider = OllamaProvider(base_url="http://127.0.0.1:11434", default_model="qwen2.5:7b")
    monkeypatch.setattr(provider, "list_models", lambda: ["qwen2.5:7b", "qwen3:0.6b"])
    model, switched = provider.resolve_model("qwen2.5:7b")
    assert model == "qwen2.5:7b"
    assert switched is False


def test_ollama_model_matches_base_name(monkeypatch):
    provider = OllamaProvider(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(provider, "list_models", lambda: ["qwen3:0.6b"])
    model, switched = provider.resolve_model("qwen3")
    assert model == "qwen3:0.6b"
    assert switched is True


def test_teacher_cache_avoids_second_call(ctx, project, monkeypatch):
    class FakeProvider:
        name = "ollama"

        def capabilities(self):
            from godfather.providers.base import Capabilities

            return Capabilities(provider="ollama", name="fake")

        def healthcheck(self):
            return True, "ok"

        def estimate_cost(self, request):
            return 0.0, COST_FREE

        def generate(self, request):
            from godfather.providers.base import ProviderResult, ProviderUsage

            return ProviderResult(
                ok=True,
                content="\\section{x}",
                provider="ollama",
                model="m",
                usage=ProviderUsage(cost_status=COST_FREE, usage_visible=True),
            )

    service = TeacherService(ctx.db, ctx.settings, ctx.registry)
    monkeypatch.setattr(ctx.registry, "_providers", {"ollama": FakeProvider()})

    first = service.call(project, "ollama", ProviderRequest(prompt="hello"), model="m")
    assert first.ok is True
    assert first.cached is False

    second = service.call(project, "ollama", ProviderRequest(prompt="hello"), model="m")
    assert second.ok is True
    assert second.cached is True

    calls = ctx.db.query_all("SELECT * FROM provider_calls WHERE project_id = ?", (project,))
    assert len(calls) == 1


def test_teacher_cache_invalidated_by_policy_version(ctx, project, monkeypatch):
    class FakeProvider:
        name = "ollama"

        def capabilities(self):
            from godfather.providers.base import Capabilities

            return Capabilities(provider="ollama", name="fake")

        def healthcheck(self):
            return True, "ok"

        def estimate_cost(self, request):
            return 0.0, COST_FREE

        def generate(self, request):
            from godfather.providers.base import ProviderResult, ProviderUsage

            return ProviderResult(
                ok=True,
                content="\\section{x}",
                provider="ollama",
                model="m",
                usage=ProviderUsage(cost_status=COST_FREE, usage_visible=True),
            )

    service = TeacherService(ctx.db, ctx.settings, ctx.registry)
    monkeypatch.setattr(ctx.registry, "_providers", {"ollama": FakeProvider()})
    service.call(project, "ollama", ProviderRequest(prompt="hello"), model="m", policy_version="policy-001")
    second = service.call(project, "ollama", ProviderRequest(prompt="hello"), model="m", policy_version="policy-002")
    assert second.cached is False


def test_manual_provider_reports_unknown_cost():
    provider = ManualImportProvider()
    assert provider.estimate_cost(ProviderRequest(prompt="x"))[1] == COST_UNKNOWN
    result = provider.generate(ProviderRequest(prompt="x"))
    assert result.ok is False
    assert result.error_kind == "manual_required"


def test_manual_import_validation_reports_problems():
    ok, problems = validate_import_payload({"samples": [{"sample_id": "a"}]})
    assert ok is False
    assert any("input_hash" in problem for problem in problems)
