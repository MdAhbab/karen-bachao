"""Final replay validator (Problem Statement section 08).

Independently replays a finished schedule hour by hour the same way the judge
does, so a plan that violates any rule is caught before it leaves the service.
Shared by the API (as a last gate) and by the test suite.
"""

from typing import Any

TOL = 0.01  # judge tolerance: 0.01 kWh / 0.01 BDT


def replay(request: Any, directives: list[dict], plan: list[dict]) -> list[str]:
    """Return a list of violation strings. Empty means the plan is valid."""
    errors: list[str] = []
    hours = {h.hour: h for h in request.hours}
    b = request.battery

    # --- fold directives into the expected per-hour limits -----------------
    effective_solar = {h: hours[h].solar_kwh for h in range(24)}
    floor = {h: float(b.minimum_energy_kwh) for h in range(24)}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}

    for d in directives:
        if not d.get("applies"):
            continue
        kind = d.get("directive_type")
        adj = d.get("structured_adjustment") or {}
        for h in adj.get("hours", []):
            if kind == "solar_reduction":
                effective_solar[h] = hours[h].solar_kwh * float(adj["factor"])
            elif kind == "minimum_battery_reserve":
                floor[h] = max(floor[h], float(adj["minimum_energy_kwh"]))
            elif kind == "no_charge_window":
                no_charge.add(h)
            elif kind == "no_discharge_window":
                no_discharge.add(h)
            elif kind == "max_grid_window":
                cap = float(adj["max_grid_kwh"])
                grid_cap[h] = min(grid_cap.get(h, cap), cap)

    # --- structural checks -------------------------------------------------
    if len(plan) != 24 or {p["hour"] for p in plan} != set(range(24)):
        errors.append("hourly_plan must contain exactly one entry per hour 0..23")
        return errors

    energy = float(b.initial_energy_kwh)
    for p in sorted(plan, key=lambda x: x["hour"]):
        h = p["hour"]
        grid = float(p["grid_kwh"])
        solar_used = float(p["solar_used_kwh"])
        action = p["battery_action"]
        magnitude = float(p["battery_kwh"])

        if grid < -TOL or solar_used < -TOL or magnitude < -TOL:
            errors.append(f"hour {h}: negative energy value")
        if action not in ("charge", "discharge", "idle"):
            errors.append(f"hour {h}: invalid battery_action {action!r}")
            continue
        if action == "idle" and abs(magnitude) > TOL:
            errors.append(f"hour {h}: idle hour must have battery_kwh = 0")

        # rate limits (9.3)
        if action == "charge" and magnitude > b.max_charge_kwh_per_hour + TOL:
            errors.append(f"hour {h}: charge exceeds max_charge_kwh_per_hour")
        if action == "discharge" and magnitude > b.max_discharge_kwh_per_hour + TOL:
            errors.append(f"hour {h}: discharge exceeds max_discharge_kwh_per_hour")

        # directive windows
        if action == "charge" and magnitude > TOL and h in no_charge:
            errors.append(f"hour {h}: charged during a no_charge_window")
        if action == "discharge" and magnitude > TOL and h in no_discharge:
            errors.append(f"hour {h}: discharged during a no_discharge_window")
        if h in grid_cap and grid > grid_cap[h] + TOL:
            errors.append(f"hour {h}: grid_kwh exceeds max_grid_window cap")

        # solar usage (9.4)
        if solar_used > effective_solar[h] + TOL:
            errors.append(f"hour {h}: solar_used_kwh exceeds effective solar")

        # energy balance (9.5)
        charge_amt = magnitude if action == "charge" else 0.0
        discharge_amt = magnitude if action == "discharge" else 0.0
        lhs = grid + solar_used + discharge_amt
        rhs = hours[h].demand_kwh + charge_amt
        if abs(lhs - rhs) > TOL:
            errors.append(f"hour {h}: energy balance off by {lhs - rhs:.4f}")

        # battery state transition (9.1) and bounds (9.2)
        energy += charge_amt - discharge_amt
        if abs(energy - float(p["battery_energy_after_kwh"])) > TOL:
            errors.append(f"hour {h}: battery_energy_after_kwh does not match transition")
        energy = float(p["battery_energy_after_kwh"])
        if energy < floor[h] - TOL:
            errors.append(f"hour {h}: battery below required minimum {floor[h]:.2f}")
        if energy > b.capacity_kwh + TOL:
            errors.append(f"hour {h}: battery above capacity")

    # end-of-day neutrality (9.6)
    final = float(sorted(plan, key=lambda x: x["hour"])[-1]["battery_energy_after_kwh"])
    if abs(final - b.initial_energy_kwh) > TOL:
        errors.append(
            f"end-of-day battery {final:.2f} != initial {b.initial_energy_kwh:.2f}"
        )
    return errors


def totals(request: Any, plan: list[dict]) -> dict:
    """Recompute reported totals directly from the plan (section 11.3)."""
    tariff = {h.hour: h.tariff_bdt_per_kwh for h in request.hours}
    total_grid = sum(float(p["grid_kwh"]) for p in plan)
    total_cost = sum(float(p["grid_kwh"]) * tariff[p["hour"]] for p in plan)
    peak = max((float(p["grid_kwh"]) for p in plan), default=0.0)
    return {
        "total_grid_kwh": round(total_grid, 4),
        "total_cost_bdt": round(total_cost, 4),
        "peak_grid_kwh": round(peak, 4),
    }
