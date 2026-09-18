"""Guardrail tests: malformed model output must never reach the optimizer.

Every case here feeds deliberately broken LLM output through sanitize() and
asserts it comes back as a legal interpretation (section 08).
"""

import pytest

from app.guardrails import sanitize, sanitize_entry
from app.schemas import Battery

BATTERY = Battery(
    capacity_kwh=200,
    initial_energy_kwh=100,
    minimum_energy_kwh=20,
    max_charge_kwh_per_hour=50,
    max_discharge_kwh_per_hour=50,
)

NOTES = ["note a", "note b"]


def _entry(**kwargs):
    base = {"note_index": 0, "applies": True, "explanation": "x"}
    base.update(kwargs)
    return base


def test_one_entry_per_note_in_order():
    out = sanitize([], NOTES, BATTERY)
    assert [e["note_index"] for e in out] == [0, 1]
    assert all(e["directive_type"] == "no_op" for e in out)


def test_missing_note_becomes_no_op():
    raw = [_entry(note_index=0, directive_type="no_charge_window", hours=[1, 2])]
    out = sanitize(raw, NOTES, BATTERY)
    assert out[0]["directive_type"] == "no_charge_window"
    assert out[1]["directive_type"] == "no_op"
    assert out[1]["applies"] is False


def test_duplicate_note_index_does_not_duplicate_output():
    raw = [
        _entry(note_index=0, directive_type="no_charge_window", hours=[1]),
        _entry(note_index=0, directive_type="no_discharge_window", hours=[5]),
    ]
    out = sanitize(raw, NOTES, BATTERY)
    assert len(out) == 2
    assert [e["note_index"] for e in out] == [0, 1]


def test_unknown_directive_type_becomes_no_op():
    out = sanitize_entry(_entry(directive_type="turn_off_campus", hours=[1]), 0, BATTERY)
    assert out["directive_type"] == "no_op"
    assert out["structured_adjustment"] is None
    assert out["applies"] is False


@pytest.mark.parametrize("raw_hours,expected", [
    ([5, 3, 4], [3, 4, 5]),          # unsorted
    ([3, 3, 4], [3, 4]),             # duplicates
    ([-2, 3, 99], [3]),              # out of range stripped
    (["7", 8.0], [7, 8]),            # string and float coercion
])
def test_hours_are_normalised(raw_hours, expected):
    out = sanitize_entry(
        _entry(directive_type="no_charge_window", hours=raw_hours), 0, BATTERY)
    assert out["structured_adjustment"]["hours"] == expected


def test_empty_hours_becomes_no_op():
    out = sanitize_entry(_entry(directive_type="no_charge_window", hours=[]), 0, BATTERY)
    assert out["directive_type"] == "no_op"


@pytest.mark.parametrize("given,expected", [
    (0.2, 0.2),
    (80, 0.8),      # percentage-style answer normalised once
    (1, 1.0),
    (0, 0.0),
])
def test_solar_factor_normalised(given, expected):
    out = sanitize_entry(
        _entry(directive_type="solar_reduction", hours=[1], factor=given), 0, BATTERY)
    assert out["structured_adjustment"]["factor"] == pytest.approx(expected)


@pytest.mark.parametrize("bad", [-0.5, 101, float("inf"), float("nan"), None, "abc"])
def test_invalid_solar_factor_becomes_no_op(bad):
    out = sanitize_entry(
        _entry(directive_type="solar_reduction", hours=[1], factor=bad), 0, BATTERY)
    assert out["directive_type"] == "no_op"


def test_reserve_above_capacity_is_clamped():
    out = sanitize_entry(
        _entry(directive_type="minimum_battery_reserve", hours=[1],
               minimum_energy_kwh=99999), 0, BATTERY)
    assert out["structured_adjustment"]["minimum_energy_kwh"] == BATTERY.capacity_kwh


def test_negative_grid_cap_becomes_no_op():
    out = sanitize_entry(
        _entry(directive_type="max_grid_window", hours=[1], max_grid_kwh=-5), 0, BATTERY)
    assert out["directive_type"] == "no_op"


def test_applies_is_forced_to_match_directive_type():
    """A model claiming applies=false on a real directive is corrected."""
    out = sanitize_entry(
        _entry(directive_type="no_charge_window", hours=[1], applies=False), 0, BATTERY)
    assert out["directive_type"] == "no_charge_window"
    assert out["applies"] is True

    out = sanitize_entry(_entry(directive_type="no_op", applies=True), 0, BATTERY)
    assert out["applies"] is False
    assert out["structured_adjustment"] is None


def test_nested_structured_adjustment_is_accepted():
    raw = _entry(directive_type="max_grid_window",
                 structured_adjustment={"hours": [18, 19], "max_grid_kwh": 155})
    out = sanitize_entry(raw, 0, BATTERY)
    assert out["structured_adjustment"] == {"hours": [18, 19], "max_grid_kwh": 155}


@pytest.mark.parametrize("junk", [None, "a string", 42, [], {"nothing": "useful"}])
def test_junk_entries_never_raise(junk):
    out = sanitize_entry(junk, 0, BATTERY)
    assert out["directive_type"] == "no_op"
    assert out["note_index"] == 0


def test_sanitize_survives_non_list_input():
    out = sanitize({"unexpected": "shape"}, NOTES, BATTERY)
    assert len(out) == 2
    assert all(e["directive_type"] == "no_op" for e in out)
