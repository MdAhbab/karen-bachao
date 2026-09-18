"""Unit tests for the interpretation layer itself.

Covers the parts of app/llm.py that decide whether a correct model answer is
kept or thrown away: response parsing, provider racing, tier escalation,
caching and the outage path. Every provider call is stubbed, so this file
makes NO network requests and costs no quota.
"""

import asyncio
import json

import httpx
import pytest

from app import llm
from app.schemas import Battery

BATTERY = Battery(
    capacity_kwh=400, initial_energy_kwh=200, minimum_energy_kwh=40,
    max_charge_kwh_per_hour=80, max_discharge_kwh_per_hour=80,
)
NOTES = ["Solar drops to 20% from 1 PM to 3 PM.", "The canteen menu changed."]

GOOD = [
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "hours": [13, 14], "factor": 0.2, "explanation": "reduced solar"},
    {"note_index": 1, "applies": False, "directive_type": "no_op",
     "explanation": "unrelated"},
]


@pytest.fixture(autouse=True)
def clear_cache():
    llm._CACHE.clear()
    yield
    llm._CACHE.clear()


# --------------------------------------------------------------------------
# Response parsing: a correct answer must survive its packaging
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,label", [
    ('{"directive_interpretation": [{"note_index": 0}]}', "plain object"),
    ('```json\n{"directive_interpretation": [{"note_index": 0}]}\n```', "fenced json"),
    ('```\n{"directive_interpretation": [{"note_index": 0}]}\n```', "bare fence"),
    ('  \n {"directive_interpretation": [{"note_index": 0}]}  \n ', "whitespace"),
    ('[{"note_index": 0}]', "bare array"),
    ('{"directives": [{"note_index": 0}]}', "alternate key"),
])
def test_parse_entries_accepts_common_packagings(raw, label):
    assert llm._parse_entries(raw) == [{"note_index": 0}], label


@pytest.mark.parametrize("raw", ["not json at all", "", "null", '{"unrelated": 1}', "42"])
def test_parse_entries_rejects_unusable_text(raw):
    with pytest.raises(Exception):
        llm._parse_entries(raw)


# --------------------------------------------------------------------------
# Provider racing and escalation
# --------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def _stub_entry(name, behaviour):
    """Build a (provider, model, callable) triple with scripted behaviour."""
    async def call(client, model, prompt):
        if behaviour == "ok":
            return GOOD
        if behaviour == "slow":
            await asyncio.sleep(5)
            return GOOD
        if behaviour == "http":
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(429))
        raise RuntimeError("boom")
    return ("stub", name, call)


def test_first_valid_answer_wins_the_race(monkeypatch):
    monkeypatch.setattr(llm, "_providers", lambda: [
        _stub_entry("slow-one", "slow"),
        _stub_entry("fast-one", "ok"),
    ])
    entries, label = _run(llm._race(None, "prompt", llm._providers()))
    assert entries == GOOD
    assert label == "stub:fast-one"


def test_race_survives_failures_around_a_good_model(monkeypatch):
    entries = [
        _stub_entry("broken", "boom"),
        _stub_entry("rate-limited", "http"),
        _stub_entry("working", "ok"),
    ]
    result, label = _run(llm._race(None, "prompt", entries))
    assert result == GOOD
    assert label == "stub:working"


def test_race_raises_when_every_model_fails():
    entries = [_stub_entry("a", "boom"), _stub_entry("b", "http")]
    with pytest.raises(Exception):
        _run(llm._race(None, "prompt", entries))


def test_tiers_are_sized_and_interleaved_across_providers(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "y")
    monkeypatch.setattr(llm, "GROQ_MODELS", ["g1", "g2"])
    monkeypatch.setattr(llm, "GEMINI_MODELS", ["m1", "m2"])
    monkeypatch.setattr(llm, "LLM_RACE_SIZE", 2)

    tiers = llm._tiers()
    assert [len(t) for t in tiers] == [2, 2]
    # Each tier must span both providers, so one provider being down cannot
    # take a whole tier with it.
    for tier in tiers:
        assert {p for p, _, _ in tier} == {"groq", "gemini"}


def test_one_provider_missing_still_yields_models(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "y")
    monkeypatch.setattr(llm, "GEMINI_MODELS", ["m1", "m2"])
    assert [p for p, _, _ in llm._providers()] == ["gemini", "gemini"]


def test_no_keys_means_no_providers(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "")
    assert llm._providers() == []


# --------------------------------------------------------------------------
# interpret(): guardrails, caching, and the outage path
# --------------------------------------------------------------------------
def test_interpret_sanitizes_and_caches(monkeypatch):
    calls = {"n": 0}

    async def fake_race(client, prompt, entries):
        calls["n"] += 1
        return GOOD, "stub:model"

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "ok")])
    monkeypatch.setattr(llm, "_race", fake_race)

    first, source = _run(llm.interpret(NOTES, BATTERY))
    assert source == "stub:model"
    assert calls["n"] == 1
    # Flattened model output must come back in the exact contract shape.
    assert first[0]["structured_adjustment"] == {"hours": [13, 14], "factor": 0.2}
    assert first[1]["structured_adjustment"] is None
    assert first[1]["applies"] is False

    second, source2 = _run(llm.interpret(NOTES, BATTERY))
    assert source2 == "cache"
    assert calls["n"] == 1, "a cached note must not call a provider again"
    assert second == first


def test_cache_key_separates_different_batteries(monkeypatch):
    async def fake_race(client, prompt, entries):
        return GOOD, "stub:model"

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "ok")])
    monkeypatch.setattr(llm, "_race", fake_race)

    _run(llm.interpret(NOTES, BATTERY))
    other = Battery(capacity_kwh=999, initial_energy_kwh=200, minimum_energy_kwh=40,
                    max_charge_kwh_per_hour=80, max_discharge_kwh_per_hour=80)
    _, source = _run(llm.interpret(NOTES, other))
    assert source != "cache", "a different capacity changes percentage reserves"


def test_total_outage_falls_back_without_raising(monkeypatch):
    async def always_fail(client, prompt, entries):
        raise RuntimeError("all models down")

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "boom")])
    monkeypatch.setattr(llm, "_race", always_fail)

    entries, source = _run(llm.interpret(NOTES, BATTERY))
    assert source == "fallback"
    assert len(entries) == len(NOTES)
    assert [e["note_index"] for e in entries] == [0, 1]
    assert entries[1]["directive_type"] == "no_op"


def test_fallback_results_are_not_cached(monkeypatch):
    async def always_fail(client, prompt, entries):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "boom")])
    monkeypatch.setattr(llm, "_race", always_fail)

    _run(llm.interpret(NOTES, BATTERY))
    assert llm._CACHE == {}, "a degraded answer must not be served to later requests"


def test_garbage_model_output_never_escapes_as_a_directive(monkeypatch):
    async def junk(client, prompt, entries):
        return [{"directive_type": "delete_everything", "hours": [99]},
                "not even an object"], "stub:model"

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "ok")])
    monkeypatch.setattr(llm, "_race", junk)

    entries, _ = _run(llm.interpret(NOTES, BATTERY))
    assert all(e["directive_type"] == "no_op" for e in entries)
    assert all(e["structured_adjustment"] is None for e in entries)


def test_interpret_always_returns_one_entry_per_note(monkeypatch):
    async def too_few(client, prompt, entries):
        return [GOOD[0]], "stub:model"

    monkeypatch.setattr(llm, "_providers", lambda: [_stub_entry("m", "ok")])
    monkeypatch.setattr(llm, "_race", too_few)

    notes = ["a", "b", "c"]
    entries, _ = _run(llm.interpret(notes, BATTERY))
    assert len(entries) == 3
    assert [e["note_index"] for e in entries] == [0, 1, 2]


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------
def test_503_is_retried_once_then_succeeds():
    attempts = {"n": 0}

    class FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                response = httpx.Response(503, request=httpx.Request("POST", url))
                raise httpx.HTTPStatusError("503", request=response.request,
                                            response=response)
            return httpx.Response(200, json={"ok": True},
                                  request=httpx.Request("POST", url))

    result = _run(llm._post_with_retry(FakeClient(), "http://x", {}, {}))
    assert result.status_code == 200
    assert attempts["n"] == 2


def test_429_is_not_retried():
    """A spent quota window will not clear in under a second, so escalate."""
    attempts = {"n": 0}

    class FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            attempts["n"] += 1
            response = httpx.Response(429, request=httpx.Request("POST", url))
            raise httpx.HTTPStatusError("429", request=response.request,
                                        response=response)

    with pytest.raises(httpx.HTTPStatusError):
        _run(llm._post_with_retry(FakeClient(), "http://x", {}, {}))
    assert attempts["n"] == 1, "a 429 must not be retried"


# --------------------------------------------------------------------------
# Prompt content: the rules the models depend on must actually be sent
# --------------------------------------------------------------------------
def test_prompt_carries_the_rules_models_need():
    from app.prompt import build_prompt
    prompt = build_prompt(NOTES, BATTERY)

    assert str(BATTERY.capacity_kwh) in prompt, "percentage reserves need capacity"
    assert "REMAINS" in prompt, "factor semantics must be stated"
    assert "END-EXCLUSIVE" in prompt, "window convention must be stated"
    for kind in ("solar_reduction", "minimum_battery_reserve", "no_charge_window",
                 "no_discharge_window", "max_grid_window", "no_op"):
        assert kind in prompt
    for i in range(len(NOTES)):
        assert f"note_index {i}" in prompt
    assert NOTES[0] in prompt and NOTES[1] in prompt


def test_prompt_never_contains_a_key():
    from app.prompt import build_prompt
    prompt = build_prompt(NOTES, BATTERY).lower()
    for token in ("gsk_", "aq.ab8", "api_key", "authorization", "bearer"):
        assert token not in prompt
