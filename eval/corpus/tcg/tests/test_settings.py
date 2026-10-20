"""Settings: registry, validation, .env persistence, and the Settings API."""

import json
import os

import pytest

from src import config
from src.decide import rule_based_judgment, system_prompt
from tests.test_web import fetch


# --- Reading & validating --------------------------------------------------- #

def test_defaults_when_unset():
    assert config.get("BUY_EDGE_PCT") == 15.0
    assert config.get("RUN_SOURCE") == "mock"
    assert config.get("LLM_BASE_URL") == "https://api.deepseek.com"


def test_bad_hand_edited_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BUY_EDGE_PCT", "lots")
    assert config.get("BUY_EDGE_PCT") == 15.0


@pytest.mark.parametrize("key, value", [
    ("LLM_API_KEY", "sk-abc\nEVIL=1"),        # newline: .env injection attempt
    ("LLM_API_KEY", "short"),
    ("LLM_API_KEY", "has space in it"),
    ("LLM_BASE_URL", "http://example.com"),   # http only allowed for localhost
    ("LLM_BASE_URL", "not a url"),
    ("LLM_MODEL", "two words"),
    ("RUN_SOURCE", "production"),
    ("BUY_EDGE_PCT", "500"),
    ("BUY_EDGE_PCT", "abc"),
    ("NOT_A_SETTING", "x"),
])
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(config.ConfigError):
        config.validate(key, value)


def test_valid_values_are_normalized():
    assert config.validate("LLM_BASE_URL", "https://api.example.com/v1/") == "https://api.example.com/v1"
    assert config.validate("LLM_BASE_URL", "http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert config.validate("BUY_EDGE_PCT", " 12.50 ") == "12.5"


# --- Saving ----------------------------------------------------------------- #

def test_save_writes_env_and_applies_immediately():
    config.ENV_PATH.write_text("# my comment\nUNRELATED=keep-me\n")
    config.save({"LLM_API_KEY": "sk-test-1234567890abcd", "BUY_EDGE_PCT": "12"})

    text = config.ENV_PATH.read_text()
    assert "# my comment" in text and "UNRELATED=keep-me" in text   # other lines untouched
    assert "LLM_API_KEY=sk-test-1234567890abcd" in text
    assert os.environ["LLM_API_KEY"] == "sk-test-1234567890abcd"   # live, no restart
    assert config.get("BUY_EDGE_PCT") == 12.0


def test_save_none_removes_and_restores_default():
    config.save({"BUY_EDGE_PCT": "12"})
    config.save({"BUY_EDGE_PCT": None})
    assert "BUY_EDGE_PCT" not in config.ENV_PATH.read_text()
    assert config.get("BUY_EDGE_PCT") == 15.0


def test_save_is_all_or_nothing():
    with pytest.raises(config.ConfigError) as info:
        config.save({"BUY_EDGE_PCT": "12", "LLM_MODEL": "bad model"})
    assert set(info.value.errors) == {"LLM_MODEL"}
    assert config.get("BUY_EDGE_PCT") == 15.0          # the valid one wasn't written either


def test_strong_edge_must_cover_buy_edge():
    with pytest.raises(config.ConfigError) as info:
        config.save({"BUY_EDGE_PCT": "30"})            # strong stays at its default of 20
    assert "STRONG_EDGE_PCT" in info.value.errors


def test_describe_never_includes_secret_values():
    config.save({"LLM_API_KEY": "sk-supersecret-value-9f2a"})
    items = {i["key"]: i for i in config.describe()}
    assert items["LLM_API_KEY"]["is_set"] is True
    assert items["LLM_API_KEY"]["hint"] == "ends in …9f2a"
    assert "supersecret" not in json.dumps(config.describe())
    assert items["TCGAPI_KEY"]["is_set"] is False


# --- Settings change behaviour -------------------------------------------------- #

FACTS = dict(status="watching", vs_market_pct=-18.0, trend_30d="flat", sold_avg=100.0,
             best_listing=82.0, target_buy=None, sold_count=5)


def test_buy_threshold_changes_rule_verdict():
    assert rule_based_judgment(FACTS).verdict == "buy"         # -18% beats the default 15%
    config.save({"STRONG_EDGE_PCT": "30", "BUY_EDGE_PCT": "25"})
    assert rule_based_judgment(FACTS).verdict == "hold"        # ...but not 25%


def test_prompt_uses_current_thresholds():
    assert "-15% or lower" in system_prompt()
    config.save({"STRONG_EDGE_PCT": "35", "BUY_EDGE_PCT": "25"})
    assert "-25% or lower" in system_prompt() and "-35%" in system_prompt()


def test_run_uses_saved_source(seeded_db, tmp_path, monkeypatch):
    from src import run as run_module
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.save({"RUN_SOURCE": "real"})
    with pytest.raises(config.ConfigError, match="TCGAPI_KEY"):   # real needs a key
        run_module.run(llm_mode="off")


# --- HTTP API --------------------------------------------------------------------- #

def post_json(url, payload, csrf=True):
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-Binder"] = "1"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    import urllib.error
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_settings_api_round_trip(server):
    status, body = post_json(server + "/api/settings", {"updates": {"LLM_API_KEY": "sk-route-test-0000aaaa"}})
    assert status == 200
    status, _, raw = fetch(server + "/api/settings")
    assert status == 200 and b"sk-route-test" not in raw            # never echoed back
    key = next(s for s in json.loads(raw)["settings"] if s["key"] == "LLM_API_KEY")
    assert key["is_set"] and key["hint"] == "ends in …aaaa"


def test_settings_api_requires_csrf_and_reports_field_errors(server):
    assert post_json(server + "/api/settings", {"updates": {}}, csrf=False)[0] == 403
    status, body = post_json(server + "/api/settings", {"updates": {"BUY_EDGE_PCT": "999"}})
    assert status == 400 and "BUY_EDGE_PCT" in body["errors"]


def test_connection_test_without_key(server):
    status, body = post_json(server + "/api/settings/test", {"target": "llm"})
    assert status == 200 and body["ok"] is False and "LLM API key" in body["message"]


def test_connection_test_uses_typed_values(server, monkeypatch):
    seen = {}

    def fake_check(settings):
        seen.update(key=settings.llm_api_key, model=settings.llm_model)
        return True, "Connected."

    monkeypatch.setattr("src.web._check_llm", fake_check)
    status, body = post_json(server + "/api/settings/test", {
        "target": "llm", "overrides": {"LLM_API_KEY": "sk-typed-not-saved-1", "LLM_MODEL": "my-model"}})
    assert body == {"ok": True, "message": "Connected."}
    assert seen == {"key": "sk-typed-not-saved-1", "model": "my-model"}
    assert "LLM_API_KEY" not in os.environ                        # testing doesn't save


def test_tcgapi_check_key_messages():
    from src.market.tcgapi import check_key

    class Session:
        def __init__(self, codes):
            self.codes = list(codes)

        def get(self, *a, **k):
            return type("R", (), {"status_code": self.codes.pop(0)})()

    assert check_key("k", Session([401]))[0] is False
    ok, msg = check_key("k", Session([200, 403]))
    assert ok and "aren't on this plan" in msg
    ok, msg = check_key("k", Session([200, 200]))
    assert ok and "including eBay sold comps" in msg
