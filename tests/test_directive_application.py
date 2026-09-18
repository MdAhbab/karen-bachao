"""Directive application tests (rubric category 2, the 25-point block).

Extracting a directive correctly is not enough: the judge replays the returned
schedule against its own ground truth. These tests assert each directive type
is actually ENFORCED in the plan, across several different energy profiles, and
that the base GridWise rules still hold at the same time.

All offline. No LLM calls.
"""

import math

import pytest

from app.optimizer import build_constraints, optimize
from app.replay import replay, totals
from app.schemas import OptimizeRequest

TOL = 0.01


def make_scenario(scenario_id="T", demand=None, solar=None, tariff=None, battery=None):
    """A 24-hour scenario with sensible defaults, overridable per field."""
    demand = demand or [150.0] * 24
    if solar is None:
        solar = [0.0] * 6 + [round(120 * math.exp(-((h - 12) ** 2) / 10), 1)
                             for h in range(6, 19)] + [0.0] * 5
    if tariff is None:
        tariff = [5.0] * 6 + [9.0] * 12 + [14.0] * 4 + [6.0] * 2
    battery = battery or {
        "capacity_kwh": 400, "initial_energy_kwh": 200, "minimum_energy_kwh": 40,
        "max_charge_kwh_per_hour": 80, "max_discharge_kwh_per_hour": 80,
    }
    return OptimizeRequest(
        scenario_id=scenario_id,
        operator_notes=["placeholder"],
        hours=[{"hour": h, "demand_kwh": demand[h], "solar_kwh": solar[h],
                "tariff_bdt_per_kwh": tariff[h]} for h in range(24)],
        battery=battery,
    )


def directive(kind, **adjustment):
    return {"note_index": 0, "applies": True, "directive_type": kind,
            "structured_adjustment": adjustment or None, "explanation": ""}


# Three different energy shapes, so a directive is proven under varied pressure.
PROFILES = {
    "baseline": {},
    "high_evening_demand": {"demand": [120.0] * 17 + [260.0] * 5 + [120.0] * 2},
    "no_solar": {"solar": [0.0] * 24},
}
PROFILE_IDS = list(PROFILES)


def _solve(profile_key, directives):
    request = make_scenario(**PROFILES[profile_key])
    plan, meta = optimize(request, directives)
    assert meta["feasible"], "scenario should be feasible"
    assert meta["dropped_directives"] == [], "no directive should be dropped"
    assert replay(request, directives, plan) == [], "plan must pass a full replay"
    return request, plan


# --------------------------------------------------------------------------
# no_charge_window
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILE_IDS)
@pytest.mark.parametrize("hours", [[2, 3, 4], [0], [23], list(range(0, 12))])
def test_no_charge_window_is_enforced(profile, hours):
    d = [directive("no_charge_window", hours=hours)]
    _, plan = _solve(profile, d)
    for h in hours:
        entry = plan[h]
        assert not (entry["battery_action"] == "charge" and entry["battery_kwh"] > TOL), \
            f"hour {h} charged inside a no_charge_window"


# --------------------------------------------------------------------------
# no_discharge_window
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILE_IDS)
@pytest.mark.parametrize("hours", [[18, 19], [0], [23], list(range(12, 24))])
def test_no_discharge_window_is_enforced(profile, hours):
    d = [directive("no_discharge_window", hours=hours)]
    _, plan = _solve(profile, d)
    for h in hours:
        entry = plan[h]
        assert not (entry["battery_action"] == "discharge" and entry["battery_kwh"] > TOL), \
            f"hour {h} discharged inside a no_discharge_window"


# --------------------------------------------------------------------------
# minimum_battery_reserve
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILE_IDS)
@pytest.mark.parametrize("hours,reserve", [
    ([18, 19, 20, 21], 150.0),
    ([0, 1, 2], 250.0),
    # Hour 23's state is the end-of-day value, which neutrality pins to
    # initial_energy_kwh, so a reserve above 200 here is impossible by design.
    ([23], 200.0),
    (list(range(24)), 100.0),
])
def test_minimum_battery_reserve_is_enforced(profile, hours, reserve):
    d = [directive("minimum_battery_reserve", hours=hours, minimum_energy_kwh=reserve)]
    _, plan = _solve(profile, d)
    for h in hours:
        assert plan[h]["battery_energy_after_kwh"] >= reserve - TOL, \
            f"hour {h} fell below the required reserve"


# --------------------------------------------------------------------------
# max_grid_window
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILE_IDS)
@pytest.mark.parametrize("hours,cap", [
    # Each cap must sit above demand - solar - max_discharge for those hours,
    # and leave the battery enough headroom to stay above its minimum.
    ([18, 19, 20], 200.0),
    ([0, 1, 2, 3], 120.0),
    ([23], 80.0),
])
def test_max_grid_window_is_enforced(profile, hours, cap):
    d = [directive("max_grid_window", hours=hours, max_grid_kwh=cap)]
    _, plan = _solve(profile, d)
    for h in hours:
        assert plan[h]["grid_kwh"] <= cap + TOL, \
            f"hour {h} imported {plan[h]['grid_kwh']} above the {cap} cap"


# --------------------------------------------------------------------------
# solar_reduction
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", ["baseline", "high_evening_demand"])
@pytest.mark.parametrize("hours,factor", [
    ([12, 13], 0.2),
    ([10, 11, 12], 0.5),
    ([9, 10, 11, 12, 13], 0.0),
    (list(range(6, 19)), 0.75),
])
def test_solar_reduction_caps_usable_solar(profile, hours, factor):
    request = make_scenario(**PROFILES[profile])
    base_solar = {h.hour: h.solar_kwh for h in request.hours}
    d = [directive("solar_reduction", hours=hours, factor=factor)]
    _, plan = _solve(profile, d)
    for h in hours:
        allowed = base_solar[h] * factor
        assert plan[h]["solar_used_kwh"] <= allowed + TOL, \
            f"hour {h} used more solar than the reduced availability"


def test_solar_reduction_only_touches_listed_hours():
    """A reduction must not silently shrink solar outside its window."""
    request = make_scenario()
    base = {h.hour: h.solar_kwh for h in request.hours}
    constraints = build_constraints(
        request, [directive("solar_reduction", hours=[12, 13], factor=0.25)])
    for h in range(24):
        expected = base[h] * 0.25 if h in (12, 13) else base[h]
        assert abs(constraints.effective_solar[h] - expected) < 1e-9


# --------------------------------------------------------------------------
# Combinations: directives must hold simultaneously
# --------------------------------------------------------------------------
def test_reserve_and_grid_cap_hold_together():
    hours_reserve, hours_cap = [18, 19, 20, 21], [19, 20, 21]
    d = [
        directive("minimum_battery_reserve", hours=hours_reserve,
                  minimum_energy_kwh=120.0),
        directive("max_grid_window", hours=hours_cap, max_grid_kwh=190.0),
    ]
    _, plan = _solve("high_evening_demand", d)
    for h in hours_reserve:
        assert plan[h]["battery_energy_after_kwh"] >= 120.0 - TOL
    for h in hours_cap:
        assert plan[h]["grid_kwh"] <= 190.0 + TOL


def test_all_five_directive_types_at_once():
    d = [
        directive("solar_reduction", hours=[11, 12], factor=0.3),
        directive("no_charge_window", hours=[2, 3]),
        directive("no_discharge_window", hours=[17, 18]),
        directive("minimum_battery_reserve", hours=[19, 20], minimum_energy_kwh=110.0),
        directive("max_grid_window", hours=[21, 22], max_grid_kwh=200.0),
    ]
    request, plan = _solve("baseline", d)
    base_solar = {h.hour: h.solar_kwh for h in request.hours}
    for h in (11, 12):
        assert plan[h]["solar_used_kwh"] <= base_solar[h] * 0.3 + TOL
    for h in (2, 3):
        assert plan[h]["battery_action"] != "charge" or plan[h]["battery_kwh"] <= TOL
    for h in (17, 18):
        assert plan[h]["battery_action"] != "discharge" or plan[h]["battery_kwh"] <= TOL
    for h in (19, 20):
        assert plan[h]["battery_energy_after_kwh"] >= 110.0 - TOL
    for h in (21, 22):
        assert plan[h]["grid_kwh"] <= 200.0 + TOL


def test_overlapping_grid_caps_take_the_tighter_one():
    d = [
        directive("max_grid_window", hours=[18, 19, 20], max_grid_kwh=240.0),
        directive("max_grid_window", hours=[19, 20, 21], max_grid_kwh=200.0),
    ]
    _, plan = _solve("high_evening_demand", d)
    assert plan[18]["grid_kwh"] <= 240.0 + TOL
    for h in (19, 20):
        assert plan[h]["grid_kwh"] <= 200.0 + TOL, "tighter cap must win on overlap"
    assert plan[21]["grid_kwh"] <= 200.0 + TOL


def test_overlapping_reserves_take_the_higher_one():
    d = [
        directive("minimum_battery_reserve", hours=[18, 19], minimum_energy_kwh=100.0),
        directive("minimum_battery_reserve", hours=[19, 20], minimum_energy_kwh=180.0),
    ]
    _, plan = _solve("baseline", d)
    assert plan[18]["battery_energy_after_kwh"] >= 100.0 - TOL
    assert plan[19]["battery_energy_after_kwh"] >= 180.0 - TOL, "higher reserve must win"
    assert plan[20]["battery_energy_after_kwh"] >= 180.0 - TOL


# --------------------------------------------------------------------------
# no_op must change nothing
# --------------------------------------------------------------------------
def test_no_op_does_not_change_the_plan():
    request = make_scenario()
    plain, _ = optimize(request, [])
    with_noop, _ = optimize(request, [
        {"note_index": 0, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None, "explanation": ""}])
    assert totals(request, plain) == totals(request, with_noop)


# --------------------------------------------------------------------------
# A directive must cost money, never save it
# --------------------------------------------------------------------------
@pytest.mark.parametrize("d", [
    [directive("no_charge_window", hours=[0, 1, 2, 3, 4, 5])],
    [directive("no_discharge_window", hours=[18, 19, 20, 21])],
    [directive("minimum_battery_reserve", hours=[18, 19, 20], minimum_energy_kwh=300.0)],
    [directive("max_grid_window", hours=[18, 19, 20], max_grid_kwh=200.0)],
    [directive("solar_reduction", hours=[10, 11, 12, 13], factor=0.1)],
])
def test_constraints_never_lower_the_cost(d):
    """A hard constraint can only shrink the feasible set, so cost cannot drop.

    This catches a whole class of bug where a directive is silently ignored:
    an ignored constraint would give back exactly the unconstrained cost, and a
    mis-applied one could look cheaper than the true optimum.
    """
    request = make_scenario(**PROFILES["high_evening_demand"])
    free_plan, _ = optimize(request, [])
    free_cost = totals(request, free_plan)["total_cost_bdt"]

    plan, meta = optimize(request, d)
    assert meta["feasible"] and meta["dropped_directives"] == []
    constrained_cost = totals(request, plan)["total_cost_bdt"]

    assert constrained_cost >= free_cost - TOL, (
        f"constrained cost {constrained_cost} is below the unconstrained optimum "
        f"{free_cost}, so the constraint was not really applied"
    )


# --------------------------------------------------------------------------
# Impossible directives must degrade safely, never emit an invalid plan
# --------------------------------------------------------------------------
IMPOSSIBLE = {
    "reserve above capacity":
        [directive("minimum_battery_reserve", hours=[12], minimum_energy_kwh=999.0)],
    "reserve at hour 23 above the pinned end-of-day level":
        [directive("minimum_battery_reserve", hours=[23], minimum_energy_kwh=399.0)],
    "grid cap below what demand minus discharge allows":
        [directive("max_grid_window", hours=[18, 19, 20], max_grid_kwh=10.0)],
    "grid cap of zero all day":
        [directive("max_grid_window", hours=list(range(24)), max_grid_kwh=0.0)],
}


@pytest.mark.parametrize("label", list(IMPOSSIBLE), ids=lambda s: s[:38])
def test_impossible_directive_still_returns_a_valid_plan(label):
    """The service must never answer with a plan that breaks the base rules.

    An impossible directive is soft-dropped so a valid schedule still goes out,
    rather than raising or emitting something the judge would reject outright.
    """
    request = make_scenario(**PROFILES["high_evening_demand"])
    plan, meta = optimize(request, IMPOSSIBLE[label])

    assert meta["feasible"], "a fallback level must always solve"
    assert meta["dropped_directives"], "an impossible directive must be reported dropped"
    # The plan still has to satisfy every base GridWise rule.
    assert replay(request, [], plan) == []
    assert len(plan) == 24


def test_dropping_is_minimal_not_wholesale():
    """A feasible directive must survive alongside an impossible one."""
    request = make_scenario(**PROFILES["baseline"])
    directives = [
        directive("no_charge_window", hours=[2, 3]),
        {"note_index": 1, "applies": True,
         "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [23], "minimum_energy_kwh": 399.0},
         "explanation": ""},
    ]
    plan, meta = optimize(request, directives)
    assert meta["feasible"]
    assert "minimum_battery_reserve" in meta["dropped_directives"]
    # The achievable no_charge_window must still be honoured.
    for h in (2, 3):
        assert plan[h]["battery_action"] != "charge" or plan[h]["battery_kwh"] <= TOL
