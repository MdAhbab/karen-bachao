"""Offline optimizer tests. No LLM calls, no network, no running service.

Feeds each public sample case the organizer's own expected directives, then
checks that our schedule is both fully valid under a replay and priced at the
organizer's reference optimum.
"""

import json
from pathlib import Path

import pytest

from app.optimizer import optimize, summarize
from app.replay import replay, totals
from app.schemas import OptimizeRequest

CASES = json.loads(
    (Path(__file__).parent / "public_cases.json").read_text(encoding="utf-8")
)["cases"]

TOL = 0.01


def _ids(case):
    return case["id"]


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_plan_is_valid_and_optimal(case):
    request = OptimizeRequest(**case["input"])
    directives = case["expected_output"]["directive_interpretation"]

    plan, meta = optimize(request, directives)

    assert meta["feasible"], "LP should be feasible for every public case"
    assert meta["dropped_directives"] == [], "no directive should need dropping"

    violations = replay(request, directives, plan)
    assert violations == [], f"{case['id']} produced an invalid plan: {violations}"

    recomputed = totals(request, plan)
    reference_cost = case["expected_output"]["total_cost_bdt"]
    assert recomputed["total_cost_bdt"] <= reference_cost + TOL, (
        f"{case['id']} cost {recomputed['total_cost_bdt']} is worse than the "
        f"organizer reference {reference_cost}"
    )


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_totals_match_the_plan(case):
    """Reported totals must be recomputable from hourly_plan (section 11.3)."""
    request = OptimizeRequest(**case["input"])
    plan, _ = optimize(request, case["expected_output"]["directive_interpretation"])
    t = totals(request, plan)

    assert abs(t["total_grid_kwh"] - sum(p["grid_kwh"] for p in plan)) < TOL
    assert abs(t["peak_grid_kwh"] - max(p["grid_kwh"] for p in plan)) < TOL


def test_summary_mentions_applied_directives():
    case = next(c for c in CASES if c["id"] == "SAMPLE-06")
    request = OptimizeRequest(**case["input"])
    directives = case["expected_output"]["directive_interpretation"]
    plan, _ = optimize(request, directives)
    text = summarize(plan, directives)
    assert "solar_reduction" in text and "no_charge_window" in text


def test_no_directives_still_produces_a_valid_plan():
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    plan, meta = optimize(request, [])
    assert meta["feasible"]
    assert replay(request, [], plan) == []


def test_contradictory_directives_degrade_instead_of_crashing():
    """An impossible reserve is soft-dropped rather than raising."""
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    impossible = [{
        "note_index": 0,
        "applies": True,
        "directive_type": "minimum_battery_reserve",
        # far above capacity, so no schedule can satisfy it
        "structured_adjustment": {"hours": list(range(24)),
                                  "minimum_energy_kwh": 10_000},
        "explanation": "impossible reserve",
    }]
    plan, meta = optimize(request, impossible)
    assert meta["feasible"]
    assert "minimum_battery_reserve" in meta["dropped_directives"]
    assert replay(request, [], plan) == []
