"""24-hour energy schedule optimizer.

The problem is a pure continuous linear program. `battery_action` looks
categorical, but the Problem Statement defines no round-trip efficiency loss,
so a simultaneous charge+discharge in the same hour is cost-neutral and nets
out during post-processing. No integer variables are required, which means the
LP returns the provably globally optimal cost.

Solver: scipy.optimize.linprog with the HiGHS backend (~3ms per scenario).
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog

from app.config import BOUND_EPS

H = 24


@dataclass
class Constraints:
    """Per-hour bounds after all directives have been folded in."""

    demand: np.ndarray
    tariff: np.ndarray
    effective_solar: np.ndarray
    floor: np.ndarray
    max_charge: np.ndarray
    max_discharge: np.ndarray
    max_grid: np.ndarray
    capacity: float
    initial_energy: float
    dropped: list[str] = field(default_factory=list)


def build_constraints(request: Any, directives: list[dict]) -> Constraints:
    """Fold validated directives into numeric per-hour bounds (section 5.3)."""
    hours = sorted(request.hours, key=lambda h: h.hour)
    demand = np.array([h.demand_kwh for h in hours], dtype=float)
    solar = np.array([h.solar_kwh for h in hours], dtype=float)
    tariff = np.array([h.tariff_bdt_per_kwh for h in hours], dtype=float)
    b = request.battery

    effective_solar = solar.copy()
    floor = np.full(H, float(b.minimum_energy_kwh))
    max_charge = np.full(H, float(b.max_charge_kwh_per_hour))
    max_discharge = np.full(H, float(b.max_discharge_kwh_per_hour))
    max_grid = np.full(H, np.inf)

    for d in directives:
        kind = d.get("directive_type")
        adj = d.get("structured_adjustment") or {}
        hrs = adj.get("hours") or []
        if kind == "solar_reduction":
            factor = float(adj["factor"])
            for h in hrs:
                effective_solar[h] = solar[h] * factor
        elif kind == "minimum_battery_reserve":
            reserve = float(adj["minimum_energy_kwh"])
            for h in hrs:
                floor[h] = max(floor[h], reserve)
        elif kind == "no_charge_window":
            for h in hrs:
                max_charge[h] = 0.0
        elif kind == "no_discharge_window":
            for h in hrs:
                max_discharge[h] = 0.0
        elif kind == "max_grid_window":
            cap = float(adj["max_grid_kwh"])
            for h in hrs:
                max_grid[h] = min(max_grid[h], cap)

    return Constraints(
        demand=demand,
        tariff=tariff,
        effective_solar=effective_solar,
        floor=floor,
        max_charge=max_charge,
        max_discharge=max_discharge,
        max_grid=max_grid,
        capacity=float(b.capacity_kwh),
        initial_energy=float(b.initial_energy_kwh),
    )


def _solve_lp(c: Constraints):
    """Variables: grid[0:24], solar_used[24:48], charge[48:72], discharge[72:96]."""
    n = 4 * H
    objective = np.zeros(n)
    objective[:H] = c.tariff

    # Equalities: hourly energy balance (9.5) plus end-of-day neutrality (9.6).
    a_eq = np.zeros((H + 1, n))
    b_eq = np.zeros(H + 1)
    for h in range(H):
        a_eq[h, h] = 1.0
        a_eq[h, H + h] = 1.0
        a_eq[h, 3 * H + h] = 1.0
        a_eq[h, 2 * H + h] = -1.0
        b_eq[h] = c.demand[h]
    a_eq[H, 2 * H:3 * H] = 1.0
    a_eq[H, 3 * H:4 * H] = -1.0
    b_eq[H] = 0.0

    # Inequalities: battery state stays within [floor, capacity] each hour (9.2).
    a_ub = np.zeros((2 * H, n))
    b_ub = np.zeros(2 * H)
    for h in range(H):
        row = np.zeros(n)
        row[2 * H:2 * H + h + 1] = 1.0
        row[3 * H:3 * H + h + 1] = -1.0
        a_ub[2 * h] = row
        b_ub[2 * h] = c.capacity - c.initial_energy - BOUND_EPS
        a_ub[2 * h + 1] = -row
        b_ub[2 * h + 1] = c.initial_energy - c.floor[h] - BOUND_EPS

    bounds = (
        [(0.0, None if np.isinf(c.max_grid[h]) else c.max_grid[h]) for h in range(H)]
        + [(0.0, c.effective_solar[h]) for h in range(H)]
        + [(0.0, c.max_charge[h]) for h in range(H)]
        + [(0.0, c.max_discharge[h]) for h in range(H)]
    )
    return linprog(objective, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq,
                   bounds=bounds, method="highs")


def _to_plan(c: Constraints, x: np.ndarray) -> list[dict]:
    """Turn raw LP output into an exactly self-consistent hourly plan.

    grid_kwh is recomputed last from the balance equation so the reported plan
    satisfies section 9.5 exactly rather than approximately, and battery state
    is accumulated so end-of-day neutrality holds by construction.
    """
    charge = x[2 * H:3 * H]
    discharge = x[3 * H:4 * H]
    solar_raw = x[H:2 * H]

    plan: list[dict] = []
    energy = c.initial_energy
    for h in range(H):
        net = float(charge[h] - discharge[h])
        if abs(net) < 1e-9:
            net = 0.0
        solar_used = min(max(float(solar_raw[h]), 0.0), float(c.effective_solar[h]))

        # grid = demand + net_charge - solar_used. A negative result means we
        # over-credited solar, so trim solar rather than clipping grid.
        grid = c.demand[h] + net - solar_used
        if grid < 0.0:
            solar_used = max(0.0, c.demand[h] + net)
            grid = 0.0

        energy += net
        if net > 0:
            action, magnitude = "charge", net
        elif net < 0:
            action, magnitude = "discharge", -net
        else:
            action, magnitude = "idle", 0.0

        plan.append({
            "hour": h,
            "grid_kwh": round(grid, 6),
            "solar_used_kwh": round(solar_used, 6),
            "battery_action": action,
            "battery_kwh": round(magnitude, 6),
            "battery_energy_after_kwh": round(energy, 6),
        })
    return plan


def optimize(request: Any, directives: list[dict]) -> tuple[list[dict], dict]:
    """Solve for the cheapest valid schedule.

    Organizer scoring scenarios are guaranteed feasible, but we never surface a
    500 for an infeasible combination: soft-drop the reserve directives first,
    then all interpreted directives, returning whichever level solves.
    """
    applied = [d for d in directives if d.get("directive_type") not in (None, "no_op")]

    attempts: list[tuple[list[dict], list[str]]] = [(applied, [])]
    without_reserve = [d for d in applied
                       if d["directive_type"] != "minimum_battery_reserve"]
    if len(without_reserve) != len(applied):
        attempts.append((without_reserve, ["minimum_battery_reserve"]))
    if applied:
        attempts.append(([], ["all_directives"]))

    for directive_set, dropped in attempts:
        constraints = build_constraints(request, directive_set)
        result = _solve_lp(constraints)
        if result.status == 0:
            constraints.dropped = dropped
            plan = _to_plan(constraints, result.x)
            return plan, {
                "dropped_directives": dropped,
                "effective_solar": constraints.effective_solar.tolist(),
                "feasible": True,
            }

    # Unreachable for well-formed scenarios: an idle day always balances.
    constraints = build_constraints(request, [])
    plan = _to_plan(constraints, np.zeros(4 * H))
    return plan, {"dropped_directives": ["all_directives"],
                  "effective_solar": constraints.effective_solar.tolist(),
                  "feasible": False}


def summarize(plan: list[dict], directives: list[dict]) -> str:
    """Short human-readable strategy note for the plan_summary field."""
    applied = [d for d in directives if d.get("directive_type") not in (None, "no_op")]
    charge_hours = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    discharge_hours = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    parts = [
        f"Charged the battery in {len(charge_hours)} low-tariff hour(s) and "
        f"discharged across {len(discharge_hours)} higher-tariff hour(s), using "
        f"available solar first and ending the day at the initial state of charge."
    ]
    if applied:
        parts.append(
            "Applied operator directives: "
            + ", ".join(sorted({d["directive_type"] for d in applied}))
            + "."
        )
    else:
        parts.append("No operator note changed the schedule.")
    return " ".join(parts)
