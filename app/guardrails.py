"""Deterministic guardrails over LLM output (Problem Statement section 08).

Model output is untrusted structured data. Nothing here trusts a field: every
entry is rebuilt into a legal shape, and anything that cannot be repaired
becomes no_op rather than an invented constraint.
"""

import math
from typing import Any

ALLOWED = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

NEEDS_HOURS = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
}


def _no_op(note_index: int, explanation: str = "") -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation or "This note does not affect today's energy schedule.",
    }


def _clean_hours(raw: Any) -> list[int]:
    """Unique integers 0..23 in ascending order. Anything else is discarded."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: set[int] = set()
    for value in raw:
        if isinstance(value, bool):
            continue
        try:
            hour = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            out.add(hour)
    return sorted(out)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_factor(value: Any) -> float | None:
    """Solar factor is the fraction remaining and must land in [0, 1].

    A model that answers with a percentage (80 meaning "80% remains") is
    normalised once; anything still out of range is rejected.
    """
    number = _finite(value)
    if number is None:
        return None
    if 1.0 < number <= 100.0:
        number = number / 100.0
    if 0.0 <= number <= 1.0:
        return round(number, 6)
    return None


def sanitize_entry(raw: Any, note_index: int, battery: Any) -> dict:
    """Coerce one model entry into a valid interpretation entry."""
    if not isinstance(raw, dict):
        return _no_op(note_index)

    explanation = str(raw.get("explanation") or "").strip()[:400]

    # Providers disagree on this field name: some answer with "type" or
    # "directive". Treat them all as the same field rather than discarding a
    # correct interpretation over its label.
    kind = None
    for key in ("directive_type", "type", "directive"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            kind = value.strip()
            break
    if not isinstance(kind, str) or kind not in ALLOWED:
        return _no_op(note_index, explanation)
    if kind == "no_op":
        return _no_op(note_index, explanation)

    # The adjustment may arrive nested or flattened onto the entry.
    nested = raw.get("structured_adjustment")
    source = nested if isinstance(nested, dict) else raw

    hours = _clean_hours(source.get("hours"))
    if kind in NEEDS_HOURS and not hours:
        return _no_op(note_index, explanation)

    if kind == "solar_reduction":
        factor = _clean_factor(source.get("factor"))
        if factor is None:
            return _no_op(note_index, explanation)
        adjustment: dict[str, Any] = {"hours": hours, "factor": factor}

    elif kind == "minimum_battery_reserve":
        reserve = _finite(source.get("minimum_energy_kwh"))
        if reserve is None or reserve < 0:
            return _no_op(note_index, explanation)
        # A reserve above capacity can never be satisfied; clamp it so the
        # directive still constrains the plan instead of being discarded.
        reserve = min(reserve, float(battery.capacity_kwh))
        adjustment = {"hours": hours, "minimum_energy_kwh": reserve}

    elif kind == "max_grid_window":
        cap = _finite(source.get("max_grid_kwh"))
        if cap is None or cap < 0:
            return _no_op(note_index, explanation)
        adjustment = {"hours": hours, "max_grid_kwh": cap}

    else:  # no_charge_window / no_discharge_window
        adjustment = {"hours": hours}

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": kind,
        "structured_adjustment": adjustment,
        "explanation": explanation or f"Interpreted as a {kind} directive.",
    }


def sanitize(raw_entries: Any, notes: list[str], battery: Any) -> list[dict]:
    """Produce exactly one entry per note, in note_index order.

    Missing notes become no_op; duplicate note_index values keep the first
    usable entry; unindexed entries fall back to positional order.
    """
    by_index: dict[int, Any] = {}
    positional: list[Any] = []

    if isinstance(raw_entries, list):
        for position, entry in enumerate(raw_entries):
            index = entry.get("note_index") if isinstance(entry, dict) else None
            if isinstance(index, bool):
                index = None
            try:
                index = int(index)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                index = position
            if 0 <= index < len(notes) and index not in by_index:
                by_index[index] = entry
            else:
                positional.append(entry)

    result: list[dict] = []
    for i in range(len(notes)):
        entry = by_index.get(i)
        if entry is None and positional:
            entry = positional.pop(0)
        result.append(sanitize_entry(entry, i, battery) if entry is not None
                      else _no_op(i))
    return result
