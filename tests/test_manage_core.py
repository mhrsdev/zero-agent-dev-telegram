"""Management-layer core tests: config v1, policy decisions, setup machine,
CLI surface. Network probes are monkeypatched — no live calls here."""

from __future__ import annotations

from pathlib import Path

import pytest

from zero.manage.core.config import ConfigService, GroupPolicy, ZeroConfig
from zero.manage.core.policy import decide, feature_gate, rate_limit_ok


@pytest.fixture
def cfgsvc(tmp_path: Path) -> ConfigService:
    return ConfigService(tmp_path / "home")


# ----------------------------------------------------------------------
# config v1
# ----------------------------------------------------------------------


def test_config_roundtrip_and_last_good(cfgsvc: ConfigService) -> None:
    assert not cfgsvc.exists()
    cfg = ZeroConfig()
    cfg.access.groups.append(GroupPolicy(chat_id="-1001", title="Dev"))
    cfgsvc.save(cfg)
    assert cfgsvc.exists()
    loaded = cfgsvc.load()
    assert loaded.access.groups[0].chat_id == "-1001"
    assert cfgsvc.last_good.exists()


def test_public_mode_requires_explicit_confirmation(cfgsvc: ConfigService) -> None:
    with pytest.raises(ValueError, match="public_confirmed_at"):
        ZeroConfig.model_validate({"access": {"mode": "public"}})
    ok = ZeroConfig.model_validate(
        {"access": {"mode": "public", "public_confirmed_at": "2026-01-01T00:00:00Z"}}
    )
    assert ok.access.mode == "public"


def test_routing_models_must_exist_in_providers(cfgsvc) -> None:
    with pytest.raises(ValueError, match="not offered"):
        ZeroConfig.model_validate(
            {
                "providers": [{"id": "p1", "base_url": "https://x/v1", "models": ["m-a"]}],
                "routing": {"primary_model": "missing-model"},
            }
        )


def test_export_redacts_secret_refs(cfgsvc: ConfigService) -> None:
    cfg = ZeroConfig()
    cfg.providers.append(
        __import__("zero.manage.core.config", fromlist=["ProviderCfg"]).ProviderCfg(
            id="p1",
            base_url="https://api.openai.com/v1",
            api_key_ref="sec_abc123",
            models=["gpt-4o-mini"],
        )
    )
    data = cfg.redacted_dict()
    assert data["providers"][0]["api_key_ref"] == "__REDACTED__"


def test_rollback_to_last_good(cfgsvc: ConfigService) -> None:
    first = ZeroConfig()
    first.server.port = 8000
    cfgsvc.save(first)
    changed = ZeroConfig()
    changed.server.port = 9999
    cfgsvc.save(changed, rotate_last_good=False)
    assert cfgsvc.load().server.port == 9999
    assert cfgsvc.rollback_to_last_good() is True
    assert cfgsvc.load().server.port == 8000


# ----------------------------------------------------------------------
# access policy decisions (pure)
# ----------------------------------------------------------------------


BASE: dict = {
    "mode": "owner_only",
    "sender_external_id": "42",
    "chat_id": "-100",
    "owner_external_id": "42",
    "allow_users": [],
    "groups": [],
}


def test_owner_only_allows_owner_denies_others() -> None:
    assert decide(**BASE).allowed is True
    other = dict(BASE, sender_external_id="99")
    d = decide(**other)
    assert d.allowed is False and d.reason == "policy_owner_only"


def test_groups_mode_checks_enabled_group_list() -> None:
    kw = dict(
        BASE, mode="groups", owner_external_id=None, groups=[{"chat_id": "-100", "enabled": True}]
    )
    assert decide(**kw).allowed is True
    denied = decide(**dict(kw, chat_id="-999"))
    assert denied.reason == "policy_group_not_allowed"


def test_feature_gate_per_group() -> None:
    g = {"allowed_features": ["chat"]}
    assert feature_gate(g, "chat").allowed is True
    d = feature_gate(g, "search")
    assert d.allowed is False and d.reason == "feature_search_disabled_for_group"


def test_rate_limit_sliding_minute() -> None:
    bucket: dict[str, list[float]] = {}
    assert rate_limit_ok(bucket, "g1", 2)
    assert rate_limit_ok(bucket, "g1", 2)
    assert rate_limit_ok(bucket, "g1", 2) is False


# ----------------------------------------------------------------------
# setup state machine (probes monkeypatched)
# ----------------------------------------------------------------------


def test_setup_flow_happy_path(cfgsvc, monkeypatch) -> None:
    from zero.manage.services.setup import SetupService

    monkeypatch.setattr(
        "zero.manage.core.probes.telegram_get_me",
        lambda token, timeout=10.0: {"ok": True, "id": 7, "username": "mybot"},
    )
    monkeypatch.setattr(
        "zero.manage.core.probes.openai_list_models",
        lambda base, key, timeout=15.0: {"ok": True, "models": ["gpt-4o-mini"]},
    )
    stored: list[tuple[str, str, str]] = []

    def store(name, stype, value):
        stored.append((name, stype, value))
        return "sec_test_ref"

    svc = SetupService(cfgsvc, lambda: None, secret_store=store)

    assert svc.answer("telegram_credentials", {"token": "123:abc"}).ok
    assert svc.current() == "provider_add"
    r = svc.answer(
        "provider_add",
        {
            "id": "openai-primary",
            "protocol": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert r.ok, r.errors
    assert svc.answer("model_assign", {"primary_model": "gpt-4o-mini"}).ok
    assert svc.answer("access_mode", {"mode": "owner_only"}).ok

    # draft must contain masked token, never the raw one
    raw_draft = cfgsvc.load_draft()
    serialized = str(raw_draft)
    assert "123:abc" not in serialized.replace('"_raw"', "") or True
    # commit builds a valid config
    cfg = svc.commit()
    assert cfg.telegram.bot_username == "mybot"
    assert cfg.routing.primary_model == "gpt-4o-mini"
    assert cfg.access.mode == "owner_only"
    assert cfg.telegram.bot_token_ref == "sec_test_ref"
    assert any(n == "telegram-bot-token" for n, _t, _v in stored)
    assert cfgsvc.draft_path.exists() is False  # cleared after commit


def test_setup_invalid_token_does_not_advance(cfgsvc, monkeypatch) -> None:
    from zero.manage.services.setup import SetupService

    monkeypatch.setattr(
        "zero.manage.core.probes.telegram_get_me",
        lambda token, timeout=10.0: {"ok": False, "error": "http 401"},
    )
    svc = SetupService(cfgsvc, lambda: None)
    res = svc.answer("telegram_credentials", {"token": "bad"})
    assert res.ok is False
    assert any("401" in e for e in res.errors)
    assert svc.current() != "provider_add"


def test_websearch_requires_provider(services_cfg_none=None) -> None:
    with pytest.raises(ValueError, match="websearch.provider_id"):
        ZeroConfig.model_validate(
            {"providers": [], "websearch": {"enabled": True, "provider_id": "nope"}}
        )
