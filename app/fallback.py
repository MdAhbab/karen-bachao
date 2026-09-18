"""Deterministic operator-note parser.

This is an AVAILABILITY NET, not the primary interpretation path. It runs only
when every configured Gemini model has failed, so that a provider outage
degrades the answer instead of taking the service down. Normal operation
always goes through the LLM.
"""

import re
from typing import Any

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

FRACTIONS = {
    "one-half": 0.5, "halved": 0.5, "halve": 0.5, "half": 0.5,
    "one-third": 1 / 3, "a third": 1 / 3,
    "one-quarter": 0.25, "a quarter": 0.25, "quarter": 0.25,
    "one-fifth": 0.2, "a fifth": 0.2, "fifth": 0.2,
}

# Phrases that mean solar output goes to zero.
ZERO_SOLAR = (
    "no usable solar", "no solar", "zero solar", "no pv", "zero output",
    "no output", "no generation", "no usable output", "completely offline",
    "fully offline", "total outage", "no rooftop", "nothing from the panels",
)

# Phrases that block a battery operation.
BLOCKED = (
    "not", "no ", "cannot", "can not", "unavailable", "disabled", "isolated",
    "locked out", "lockout", "lock-out", "out of service", "prohibited",
    "forbidden", "inhibited", "blocked", "suspended", "offline", "off-line",
    "must not", "may not", "should not", "unable", "restricted", "halted",
    "taken out", "will not",
)

# Wording that refers to the battery supplying load, i.e. discharging.
DISCHARGE_WORDS = (
    "discharg", "supply load", "supply the load", "support load",
    "support the load", "provide power", "supplying load", "draw from the battery",
    "battery output", "feed the load",
)

CHARGE_WORDS = ("charg",)


def _hour_from(token: str, meridiem: str | None, is_end: bool = False) -> int | None:
    token = token.strip().lower()
    if token in ("noon", "midday"):
        return 12
    if token == "midnight":
        # As the end of a window, midnight closes the day at 24, not 0.
        return 24 if is_end else 0
    if token in WORD_NUMBERS:
        value = WORD_NUMBERS[token]
    else:
        match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", token)
        if not match:
            return None
        value = int(match.group(1))
    if meridiem == "pm" and value != 12:
        value += 12
    elif meridiem == "am" and value == 12:
        value = 0
    return value if 0 <= value <= 24 else None


def extract_hours(text: str) -> list[int]:
    """Parse a time window into start-inclusive, end-exclusive hours."""
    lowered = text.lower()
    words = "|".join(WORD_NUMBERS)
    pattern = (
        r"(?:from|between|starting)?\s*"
        r"(\d{1,2}(?::\d{2})?|noon|midnight|midday|" + words + r")"
        r"\s*(am|pm)?\s*"
        r"(?:to|until|till|through|-|–|and)\s*"
        r"(\d{1,2}(?::\d{2})?|noon|midnight|midday|" + words + r")"
        r"\s*(am|pm)?"
    )
    match = re.search(pattern, lowered)
    if not match:
        return []

    start_token, start_mer, end_token, end_mer = match.groups()
    # "11 to 2 PM" leaves the start unmarked; inherit the end's meridiem.
    if start_mer is None and end_mer is not None:
        start_mer = end_mer
    start = _hour_from(start_token, start_mer)
    end = _hour_from(end_token, end_mer, is_end=True)
    if start is None or end is None:
        return []

    named = {"noon", "midday", "midnight"}
    no_meridiem = (start_mer is None and end_mer is None
                   and start_token not in named and end_token not in named)

    # Campus notes written without AM/PM ("from one until three") describe
    # working hours, so an early-morning reading is almost always wrong.
    if no_meridiem and start < 7:
        start += 12
        end += 12

    if end <= start:
        end = min(start + 1, 24)
    return [h for h in range(start, min(end, 24))]


def extract_factor(text: str) -> float | None:
    """Return the fraction of solar that REMAINS."""
    lowered = text.lower()

    if any(phrase in lowered for phrase in ZERO_SOLAR):
        return 0.0

    reduction = re.search(r"(\d{1,3})\s*%\s*(?:reduction|drop|decrease|less)", lowered)
    if reduction:
        return max(0.0, min(1.0, 1 - int(reduction.group(1)) / 100))

    if re.search(r"(?:reduc|drop|decreas|down|cut|fall|lower)\w*\s+(?:by\s+)?(\d{1,3})\s*%",
                 lowered):
        value = int(re.search(r"(\d{1,3})\s*%", lowered).group(1))
        return max(0.0, min(1.0, 1 - value / 100))

    remaining = re.search(r"(\d{1,3})\s*%", lowered)
    if remaining:
        return max(0.0, min(1.0, int(remaining.group(1)) / 100))

    # Longest phrase first so "one-half" wins over "half".
    for phrase in sorted(FRACTIONS, key=len, reverse=True):
        if phrase in lowered:
            return FRACTIONS[phrase]
    return None


def _first_number(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw)?", text.lower())
    return float(match.group(1)) if match else None


def _strip_clock(text: str) -> str:
    """Remove clock times so a numeric value is not confused with an hour."""
    cleaned = re.sub(r"\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)", " ", text.lower())
    return re.sub(r"\d{1,2}:\d{2}", " ", cleaned)


def interpret(notes: list[str], battery: Any) -> list[dict]:
    """Best-effort structured interpretation of every note."""
    out: list[dict] = []
    for i, note in enumerate(notes):
        lowered = note.lower()
        entry: dict[str, Any] | None = None
        hours = extract_hours(note)

        mentions_solar = any(w in lowered for w in
                             ("solar", "pv", "panel", "rooftop", "inverter"))
        mentions_battery = any(w in lowered for w in
                               ("battery", "charger", "charging", "discharg",
                                "state of charge", "storage"))
        mentions_grid = any(w in lowered for w in
                            ("grid", "import", "feeder", "transformer",
                             "substation", "intake"))
        blocked = any(word in lowered for word in BLOCKED)

        if mentions_solar and hours:
            factor = extract_factor(note)
            if factor is not None:
                entry = {"directive_type": "solar_reduction",
                         "hours": hours, "factor": factor}

        elif mentions_grid and hours and re.search(
                r"(?:not exceed|no more than|at or below|cap|limit|max|"
                r"must not go above|stay below|under)", lowered):
            cap = _first_number(_strip_clock(note))
            if cap is not None:
                entry = {"directive_type": "max_grid_window",
                         "hours": hours, "max_grid_kwh": cap}

        elif mentions_battery and hours:
            wants_reserve = re.search(
                r"(?:at least|no less than|minimum|reserve|keep|remain|hold|"
                r"maintain|retain|stay at or above)", lowered)
            if wants_reserve:
                percent = re.search(r"(\d{1,3})\s*%", lowered)
                if percent:
                    reserve = float(battery.capacity_kwh) * int(percent.group(1)) / 100
                else:
                    reserve = _first_number(_strip_clock(note))
                if reserve is not None:
                    entry = {"directive_type": "minimum_battery_reserve",
                             "hours": hours,
                             "minimum_energy_kwh": min(reserve,
                                                       float(battery.capacity_kwh))}
            elif blocked:
                # Check discharge first: "cannot supply load" has no "charg".
                if any(word in lowered for word in DISCHARGE_WORDS):
                    entry = {"directive_type": "no_discharge_window", "hours": hours}
                elif any(word in lowered for word in CHARGE_WORDS):
                    entry = {"directive_type": "no_charge_window", "hours": hours}

        if entry is None:
            out.append({
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "This note does not affect today's energy schedule.",
            })
        else:
            kind = entry.pop("directive_type")
            out.append({
                "note_index": i,
                "applies": True,
                "directive_type": kind,
                "structured_adjustment": entry,
                "explanation": f"Parsed as a {kind} directive.",
            })
    return out
