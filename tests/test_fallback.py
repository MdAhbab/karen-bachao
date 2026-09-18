"""Fallback parser tests.

The deterministic parser is the provider-outage net, so it is held to the same
ground truth as the LLM: every public sample and every synthetic scenario.
"""

import json
from pathlib import Path

import pytest

from app import fallback
from app.schemas import OptimizeRequest

HERE = Path(__file__).resolve().parent
PUBLIC = json.loads((HERE / "public_cases.json").read_text(encoding="utf-8"))["cases"]
SYNTHETIC = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
TOL = 0.01


def _matches(got, expected):
    if got["directive_type"] != expected["directive_type"]:
        return False
    adjust, want = got["structured_adjustment"], expected.get("structured_adjustment")
    if want is None:
        return adjust is None
    if not isinstance(adjust, dict):
        return False
    for key, value in want.items():
        if key not in adjust:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(float(adjust[key]) - float(value)) > TOL:
                return False
        elif adjust[key] != value:
            return False
    return True


@pytest.mark.parametrize("case", PUBLIC, ids=lambda c: c["id"])
def test_public_cases(case):
    request = OptimizeRequest(**case["input"])
    got = fallback.interpret(request.operator_notes, request.battery)
    expected = case["expected_output"]["directive_interpretation"]
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert _matches(g, e), (
            f"note {g['note_index']}: expected {e['directive_type']} "
            f"{e['structured_adjustment']}, got {g['directive_type']} "
            f"{g['structured_adjustment']}"
        )


@pytest.mark.parametrize("scenario", SYNTHETIC, ids=lambda s: s["id"])
def test_synthetic_scenarios(scenario):
    request = OptimizeRequest(**scenario["input"])
    got = fallback.interpret(request.operator_notes, request.battery)
    expected = scenario["expected_directives"]
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert _matches(g, e), (
            f"note {g['note_index']}: expected {e['directive_type']} "
            f"{e.get('structured_adjustment')}, got {g['directive_type']} "
            f"{g['structured_adjustment']}"
        )


@pytest.mark.parametrize("text,expected", [
    ("from 1 PM to 3 PM", [13, 14]),
    ("from noon until 2 PM", [12, 13]),
    ("between 13:00 and 15:00", [13, 14]),
    ("from 2 AM until 5 AM", [2, 3, 4]),
    ("from midnight until 3 AM", [0, 1, 2]),
    ("from 10 PM until midnight", [22, 23]),
    ("from one until three", [13, 14]),
    ("between 11 AM and 2 PM", [11, 12, 13]),
    ("from 6 PM until 9 PM", [18, 19, 20]),
])
def test_time_windows(text, expected):
    assert fallback.extract_hours(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("an 80% reduction in solar", 0.2),
    ("solar drops to about 25%", 0.25),
    ("will leave roughly one-fifth of normal output", 0.2),
    ("heavy cloud will halve rooftop output", 0.5),
    ("about half of the forecast solar", 0.5),
    ("expect no usable solar", 0.0),
])
def test_solar_factors(text, expected):
    assert fallback.extract_factor(text) == pytest.approx(expected, abs=TOL)


def test_unrelated_note_is_no_op():
    class B:
        capacity_kwh = 200
    got = fallback.interpret(["The cafeteria menu changes tomorrow."], B())
    assert got[0]["directive_type"] == "no_op"
    assert got[0]["applies"] is False
    assert got[0]["structured_adjustment"] is None
