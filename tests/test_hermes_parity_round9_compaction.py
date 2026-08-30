"""Round-9 Hermes-parity regressions: compaction summarizer routing.

GAP H (found while preparing the round-9 live run): the compaction LLM
summarizer was the LAST routing consumer still resolving its model from
``settings.openai_model`` (the ``gpt-4o-mini`` default). The planner,
the chat bridge, and the scheduler tick were all aligned with
``routing.primary_model`` in earlier rounds — but every compaction
summarizer call on the operator's gateway (which no longer serves the
default model) failed, compaction silently degraded to the
deterministic template, and LLM-gated memory deltas (GAP 9) could never
be extracted. ``config_sync`` now pins the summarizer to the SAME
routing truth, and the summarizer honors the pinned override.
"""

from __future__ import annotations

import pytest

from tests.test_dead_bot_regressions import (
    _engine_for,
    _sync,
    _wizard_store_secrets,
    _write_config,
    zero_home,  # noqa: F401 — re-exported fixture dependency
)


def test_config_sync_aligns_compaction_summarizer_with_routing_model(
    zero_home, tmp_path, monkeypatch
):
    """GAP H: ``routing.primary_model`` must reach the compaction
    summarizer exactly like the planner / chat bridge / scheduler tick
    already do."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=["sec_" + "y" * 24])

    # Before the sync, no routing override is pinned.
    assert services.compaction.summarizer_routing is None

    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-RECOVERED")
    _svc, _bindings, providers = _sync(zero_home)
    assert providers, "provider must register for the alignment to fire"
    routing = _svc.compaction.summarizer_routing
    assert routing is not None
    assert routing["provider"] == "openai-compatible"
    assert routing["model"] == "test-model"  # routing.primary_model from _write_config


def test_compaction_summarizer_sends_aligned_provider_and_model(
    zero_home, tmp_path, monkeypatch
):
    """The wired summarizer callable must send its request to the pinned
    provider/model (spy on the provider service), not the gpt-4o-mini
    settings default."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=["sec_" + "y" * 24])

    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-RECOVERED")
    _svc, _bindings, providers = _sync(zero_home)
    assert providers

    captured: dict = {}

    class _FakeResponse:
        content = "Current goal: x\n\nAccepted decisions:\n- d1"

    def _spy_send_request(**kwargs):
        captured["provider"] = kwargs["request"].provider
        captured["model_name"] = kwargs["request"].model_name
        return object(), _FakeResponse()

    compaction = _svc.compaction
    assert compaction.summarizer is not None
    monkeypatch.setattr(
        _svc.providers, "send_request", _spy_send_request, raising=True
    )
    summary = compaction.summarizer(
        project_id=None,
        execution_id=type("E", (), {"value": "exec_round9"})(),
        actor_id=None,
        messages=[{"role": "user", "content": "decide the launch codename"}],
    )
    assert summary and "Accepted decisions" in summary
    assert captured["provider"] == "openai-compatible"
    assert captured["model_name"] == "test-model"


def test_compaction_summarizer_without_override_keeps_settings_default(
    zero_home, tmp_path, monkeypatch
):
    """No pinned override → historical settings-derived behavior must be
    preserved (no silent behavior change for settings-only installs)."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=["sec_" + "y" * 24])

    # Register an adapter directly WITHOUT running config sync (no pin).
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter

    services.providers.register_adapter(
        OpenAICompatibleProviderAdapter(
            api_key="sk-TEST",
            base_url="https://api.example.com/v1",
            timeout_seconds=5,
        )
    )

    assert services.compaction.summarizer_routing is None
    captured: dict = {}

    class _FakeResponse:
        content = "Current goal: x"

    def _spy_send_request(**kwargs):
        captured["provider"] = kwargs["request"].provider
        captured["model_name"] = kwargs["request"].model_name
        return object(), _FakeResponse()

    monkeypatch.setattr(
        services.providers, "send_request", _spy_send_request, raising=True
    )
    services.compaction.summarizer(
        project_id=None,
        execution_id=type("E", (), {"value": "exec_round9b"})(),
        actor_id=None,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert captured["provider"] == "openai-compatible"
    # Settings default (gpt-4o-mini) — the composition-time behavior.
    assert captured["model_name"] == settings.openai_model


def test_summarizer_routing_setter_validation():
    """The routing override setter rejects malformed pins loudly."""
    from zero.app.compaction_service import CompactionService

    service = CompactionService(
        context_repo=object(), artifact_service=object(), authorization_service=object()
    )
    with pytest.raises(TypeError):
        service.summarizer_routing = "openai-compatible"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        service.summarizer_routing = {"model": ""}
    with pytest.raises(ValueError):
        service.summarizer_routing = {"model": "m", "bogus": 1}
    service.summarizer_routing = {"provider": "anthropic", "model": "claude-opus-5"}
    assert service.summarizer_routing == {
        "provider": "anthropic",
        "model": "claude-opus-5",
    }
    service.summarizer_routing = None
    assert service.summarizer_routing is None
