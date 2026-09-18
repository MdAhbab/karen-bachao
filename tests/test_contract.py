"""API contract and reliability tests (rubric categories 4 and 5).

Runs against the real FastAPI app through TestClient with the interpretation
step stubbed, so the full HTTP path, schema and error contract are exercised
with ZERO model-provider calls. Live model behaviour is covered separately by
tests/test_api.py.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app

HERE = Path(__file__).resolve().parent
PUBLIC = json.loads((HERE / "public_cases.json").read_text(encoding="utf-8"))["cases"]
TOL = 0.01

ALLOWED_TYPES = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
                 "no_discharge_window", "max_grid_window", "no_op"}


@pytest.fixture
def client(monkeypatch):
    """A client whose interpretation step never touches a provider."""
    async def stub(notes, battery):
        from app.guardrails import sanitize
        from app import fallback
        return sanitize(fallback.interpret(notes, battery), notes, battery), "stub"

    monkeypatch.setattr(llm, "interpret", stub)
    with TestClient(app) as c:
        yield c


def sample(index=0):
    return PUBLIC[index]["input"]


# --------------------------------------------------------------------------
# Endpoints and status behaviour
# --------------------------------------------------------------------------
def test_health_returns_exactly_the_required_body(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_provider(monkeypatch):
    """Readiness must not depend on any model key being present."""
    monkeypatch.setattr("app.config.GEMINI_API_KEY", "")
    monkeypatch.setattr("app.config.GROQ_API_KEY", "")
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_optimize_returns_200_for_every_public_case(client):
    for case in PUBLIC:
        response = client.post("/optimize-energy", json=case["input"])
        assert response.status_code == 200, case["id"]


# --------------------------------------------------------------------------
# Response schema (section 10)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("index", range(len(PUBLIC)))
def test_response_schema_is_exact(client, index):
    payload = sample(index)
    body = client.post("/optimize-energy", json=payload).json()

    required = {"scenario_id", "directive_interpretation", "hourly_plan",
                "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"}
    assert required <= set(body), f"missing: {required - set(body)}"

    assert body["scenario_id"] == payload["scenario_id"], "scenario_id must be echoed"
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]
    for field in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"):
        assert isinstance(body[field], (int, float))

    entries = body["directive_interpretation"]
    assert len(entries) == len(payload["operator_notes"]), "one entry per note"
    assert [e["note_index"] for e in entries] == list(range(len(entries))), \
        "entries must be in note_index order"

    for e in entries:
        assert set(e) == {"note_index", "applies", "directive_type",
                          "structured_adjustment", "explanation"}
        assert e["directive_type"] in ALLOWED_TYPES
        assert isinstance(e["applies"], bool)
        assert isinstance(e["explanation"], str)
        if e["directive_type"] == "no_op":
            assert e["applies"] is False
            assert e["structured_adjustment"] is None
        else:
            assert e["applies"] is True
            adj = e["structured_adjustment"]
            assert isinstance(adj, dict)
            hours = adj["hours"]
            assert hours == sorted(set(hours)), "hours unique and ascending"
            assert all(isinstance(h, int) and 0 <= h <= 23 for h in hours)

    plan = body["hourly_plan"]
    assert len(plan) == 24
    assert [p["hour"] for p in plan] == list(range(24))
    for p in plan:
        assert set(p) == {"hour", "grid_kwh", "solar_used_kwh", "battery_action",
                          "battery_kwh", "battery_energy_after_kwh"}
        assert p["battery_action"] in {"charge", "discharge", "idle"}
        assert p["grid_kwh"] >= -TOL and p["solar_used_kwh"] >= -TOL
        assert p["battery_kwh"] >= -TOL
        if p["battery_action"] == "idle":
            assert abs(p["battery_kwh"]) <= TOL


@pytest.mark.parametrize("index", range(len(PUBLIC)))
def test_reported_totals_match_the_plan(client, index):
    """Section 11.3: totals must be recomputable from hourly_plan."""
    payload = sample(index)
    body = client.post("/optimize-energy", json=payload).json()
    plan = body["hourly_plan"]
    tariff = {h["hour"]: h["tariff_bdt_per_kwh"] for h in payload["hours"]}

    assert abs(body["total_grid_kwh"] - sum(p["grid_kwh"] for p in plan)) <= TOL
    assert abs(body["peak_grid_kwh"] - max(p["grid_kwh"] for p in plan)) <= TOL
    assert abs(body["total_cost_bdt"]
               - sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan)) <= TOL


# --------------------------------------------------------------------------
# Request validation (section 6.1)
# --------------------------------------------------------------------------
def test_malformed_json_is_400(client):
    response = client.post("/optimize-energy", content=b"{ not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "error" in response.json()


INVALID_REQUESTS = {
    "empty body": {},
    "no operator_notes": {k: v for k, v in sample().items() if k != "operator_notes"},
    "empty operator_notes": dict(sample(), operator_notes=[]),
    "four operator_notes": dict(sample(), operator_notes=["a", "b", "c", "d"]),
    "blank note": dict(sample(), operator_notes=["   "]),
    "no hours": {k: v for k, v in sample().items() if k != "hours"},
    "23 hours": dict(sample(), hours=sample()["hours"][:23]),
    "25 hours": dict(sample(), hours=sample()["hours"] + [sample()["hours"][0]]),
    "duplicate hour": dict(sample(), hours=[sample()["hours"][0]] * 24),
    "hour out of range": dict(sample(), hours=[dict(sample()["hours"][0], hour=24)]
                              + sample()["hours"][1:]),
    "negative demand": dict(sample(),
                            hours=[dict(sample()["hours"][0], demand_kwh=-5)]
                            + sample()["hours"][1:]),
    "no battery": {k: v for k, v in sample().items() if k != "battery"},
    "zero capacity": dict(sample(), battery=dict(sample()["battery"], capacity_kwh=0)),
    "negative reserve": dict(sample(),
                             battery=dict(sample()["battery"], minimum_energy_kwh=-1)),
    "notes not a list": dict(sample(), operator_notes="just a string"),
    "hours not a list": dict(sample(), hours={"hour": 0}),
}


@pytest.mark.parametrize("label", list(INVALID_REQUESTS), ids=lambda s: s[:30])
def test_invalid_requests_are_rejected_cleanly(client, label):
    """Never a 5xx, and never a crash, for structurally bad input."""
    response = client.post("/optimize-energy", json=INVALID_REQUESTS[label])
    assert response.status_code in (400, 422), \
        f"{label}: got {response.status_code}"
    assert response.status_code < 500
    body = response.json()
    assert "error" in body


@pytest.mark.parametrize("label", list(INVALID_REQUESTS), ids=lambda s: s[:30])
def test_errors_leak_no_secret_or_stack_trace(client, label):
    response = client.post("/optimize-energy", json=INVALID_REQUESTS[label])
    text = response.text.lower()
    for token in ("traceback", "gemini_api_key", "groq_api_key", "gsk_", "aq.ab8",
                  "file \"", "line 1,"):
        assert token not in text, f"{label} leaked {token!r}"


# --------------------------------------------------------------------------
# Reliability
# --------------------------------------------------------------------------
def test_repeated_requests_are_stable_and_identical(client):
    """Valid requests must not drift or fail across repeats (rubric 5)."""
    payload = sample(5)
    first = client.post("/optimize-energy", json=payload).json()
    for _ in range(9):
        body = client.post("/optimize-energy", json=payload)
        assert body.status_code == 200
        assert body.json()["hourly_plan"] == first["hourly_plan"], \
            "identical input must give an identical plan"


def test_provider_total_outage_still_returns_a_valid_plan(monkeypatch):
    """Every model failing must degrade, not 500 (rubric 5, 2 points)."""
    import httpx

    async def always_fail(*args, **kwargs):
        raise httpx.ConnectError("provider unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", always_fail)
    llm._CACHE.clear()

    with TestClient(app) as c:
        response = c.post("/optimize-energy", json=sample(0))
    assert response.status_code == 200
    body = response.json()
    assert len(body["hourly_plan"]) == 24
    assert len(body["directive_interpretation"]) == len(sample(0)["operator_notes"])


def test_unknown_route_is_404_not_500(client):
    assert client.get("/does-not-exist").status_code == 404


def test_wrong_method_is_405(client):
    assert client.get("/optimize-energy").status_code == 405


def test_large_but_valid_numbers_are_handled(client):
    """Unusual yet legal magnitudes must not break the solver."""
    payload = dict(
        sample(),
        scenario_id="BIG",
        hours=[{"hour": h, "demand_kwh": 9000.0, "solar_kwh": 4000.0,
                "tariff_bdt_per_kwh": 250.0} for h in range(24)],
        battery={"capacity_kwh": 50000, "initial_energy_kwh": 25000,
                 "minimum_energy_kwh": 1000, "max_charge_kwh_per_hour": 5000,
                 "max_discharge_kwh_per_hour": 5000},
    )
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    assert len(response.json()["hourly_plan"]) == 24


def test_zero_demand_day_is_handled(client):
    payload = dict(
        sample(),
        scenario_id="ZERO",
        hours=[{"hour": h, "demand_kwh": 0.0, "solar_kwh": 0.0,
                "tariff_bdt_per_kwh": 5.0} for h in range(24)],
    )
    body = client.post("/optimize-energy", json=payload)
    assert body.status_code == 200
    assert body.json()["total_grid_kwh"] == pytest.approx(0.0, abs=TOL)
