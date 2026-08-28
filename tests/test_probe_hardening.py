"""Regression tests: probes must never crash on bad secrets, and the
wizard must probe the RAW api key — never the draft's mask.

Reported crash (2026-08): `zero setup` died at step 7/18 with
``UnicodeEncodeError: 'ascii' codec can't encode character '\\u2026' in
position 11``. Root cause: the draft stores secrets masked
(``sk-a…xyz`` — position 11 of ``Bearer sk-a…`` is exactly the mask's
ellipsis) and ``provider_test`` probed the masked value read back from
the draft.
"""

from __future__ import annotations

import pytest

from zero.manage.core import probes
from zero.manage.core.config import ConfigService
from zero.manage.services.setup import SetupService

MASKED = "sk-a\u2026xyz"  # '…' = U+2026, what _mask() stores in drafts


def _svc(tmp_path) -> SetupService:
    return SetupService(ConfigService(tmp_path), lambda: None, secret_store=None)


def _seed_provider_draft(tmp_path, *, with_raw: bool) -> SetupService:
    svc = _svc(tmp_path)
    draft = svc.resume()
    draft.setdefault("data", {})
    draft["current_step"] = "provider_test"
    entry = {
        "id": "p1",
        "protocol": "openai_compatible",
        "base_url": "https://x/v1",
        "api_key": MASKED,
        "models": ["m1"],
        "api_key_ref": "sec_test",
    }
    if with_raw:
        entry["_raw"] = {"api_key": "sk-live-key-123"}
    draft["data"]["provider_add"] = entry
    svc.cfg.save_draft(draft)
    return svc


# ----------------------------------------------------------------------
# probe layer: dirty secrets fail cleanly, no network call, no raise
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("probe_name", "args"),
    [
        ("openai_completion_probe", ("https://x/v1", MASKED, "m1")),
        ("openai_list_models", ("https://x/v1", MASKED)),
        ("anthropic_ping", ("https://x/v1", MASKED, "m1")),
        ("telegram_get_me", ("123:abc\u2026",)),
        ("telegram_recent_chats", ("123:abc\u2026",)),
    ],
)
def test_probes_reject_dirty_secrets_without_network(monkeypatch, probe_name, args):
    def _no_call(*a, **k):
        raise AssertionError("network must not be reached with a dirty secret")

    monkeypatch.setattr(probes.httpx, "get", _no_call)
    monkeypatch.setattr(probes.httpx, "post", _no_call)
    res = getattr(probes, probe_name)(*args)
    assert res.get("ok") is False
    assert "invalid characters" in str(res.get("error"))


def test_clean_secret_strips_invisible_paste_artifacts():
    assert probes.clean_secret("\u200b sk-abc\u00a0") == "sk-abc"
    assert probes.clean_secret("sk\u200b-abc") == "sk-abc"
    assert probes.clean_secret("sk-abc") == "sk-abc"
    assert probes.clean_secret(MASKED) is None
    assert probes.clean_secret("   ") is None


def test_telegram_get_me_survives_non_json_200_body(monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("not json")

    monkeypatch.setattr(probes.httpx, "get", lambda *a, **k: _Resp())
    res = probes.telegram_get_me("123:real-token")
    assert res["ok"] is False
    assert res["error"] == "non-JSON response body"


# ----------------------------------------------------------------------
# setup service: raw-vs-masked and friendly pre-probe validation
# ----------------------------------------------------------------------
def test_provider_add_rejects_ellipsis_key_before_probing(tmp_path, monkeypatch):
    def _no_call(*a, **k):
        raise AssertionError("probe must not run for a truncated key")

    monkeypatch.setattr(probes, "openai_list_models", _no_call)
    svc = _svc(tmp_path)
    res = svc.validate(
        "provider_add",
        {
            "id": "p1",
            "protocol": "openai_compatible",
            "base_url": "https://x/v1",
            "api_key": MASKED,
        },
    )
    assert not res.ok
    assert any("truncated copy" in e for e in res.errors)


def test_provider_test_probes_raw_key_not_mask(tmp_path, monkeypatch):
    svc = _seed_provider_draft(tmp_path, with_raw=True)
    captured: dict = {}

    def fake_probe(base, key, model, **k):
        captured.update(base=base, key=key, model=model)
        return {"ok": True}

    monkeypatch.setattr(probes, "openai_completion_probe", fake_probe)
    res = svc.validate("provider_test", {"model": "m1"})
    assert res.ok
    assert captured["key"] == "sk-live-key-123"


def test_provider_test_on_old_masked_draft_fails_cleanly(tmp_path):
    # Draft written by the pre-fix version: masked key, no _raw. The old
    # code raised UnicodeEncodeError here; now it must produce a normal
    # StepResult error.
    svc = _seed_provider_draft(tmp_path, with_raw=False)
    res = svc.validate("provider_test", {"model": "m1"})
    assert not res.ok
    assert any("completion probe failed" in e for e in res.errors)


# ----------------------------------------------------------------------
# wizard e2e: replay the reported session (no crash, retry menu works)
# ----------------------------------------------------------------------
def _run_wizard_from_provider_test(monkeypatch, tmp_path, inputs, probe, *, with_raw=True):
    if with_raw:
        _seed_provider_draft(tmp_path, with_raw=True)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("zero.manage.services.setup.STEP_ORDER",
                        ["provider_add", "provider_test", "final_validation", "test_message"])
    monkeypatch.setattr(probes, "openai_completion_probe", probe)
    # no tty in test environments — never let getpass touch /dev/tty
    monkeypatch.setattr("zero.manage.cli.getpass.getpass", lambda _p="": "")
    it = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))

    from zero.manage.cli import main

    return main(["setup"])


def test_wizard_survives_masked_key_and_retry_menu(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky_probe(base, key, model, **k):
        calls["n"] += 1
        assert key == "sk-live-key-123"  # raw key, NEVER the mask
        if calls["n"] == 1:
            return {"ok": False, "error": "unreachable: ConnectError"}
        return {"ok": True}

    # inputs: model (Enter=default), retry menu (Enter=retry), then two
    # field-less steps (Enter)
    rc = _run_wizard_from_provider_test(
        monkeypatch, tmp_path, ["", "", "", ""], flaky_probe
    )
    assert rc == 0
    assert calls["n"] == 2  # failed once, retried with the SAME answers
    assert (tmp_path / "config.yaml").exists()


def test_wizard_masked_draft_no_crash_skip_works(tmp_path, monkeypatch):
    # THE reported crash: a draft whose provider_add carries only the
    # masked key. The wizard used to die with UnicodeEncodeError; now it
    # shows a readable probe error and 's' skips the optional step.
    probe_calls: list[tuple] = []

    def recording_probe(base, key, model, **k):
        probe_calls.append((base, key, model))
        return {"ok": False, "error": "unreachable: ConnectError"}

    # inputs: model (Enter), menu 's' (skip optional), final_validation (Enter),
    # test_message (Enter)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    _seed_provider_draft(tmp_path, with_raw=False)
    monkeypatch.setattr("zero.manage.services.setup.STEP_ORDER",
                        ["provider_add", "provider_test", "final_validation", "test_message"])
    monkeypatch.setattr(probes, "openai_completion_probe", recording_probe)
    monkeypatch.setattr("zero.manage.cli.getpass.getpass", lambda _p="": "")
    it = iter(["", "s", "", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(it))

    from zero.manage.cli import main

    rc = main(["setup"])
    assert rc == 0
    # the masked key reached the probe layer but was rejected cleanly
    assert probe_calls and probe_calls[0][1] == MASKED
    assert (tmp_path / "config.yaml").exists()
