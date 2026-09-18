"""Prompt and response schema for operator-note interpretation.

All notes in a scenario are interpreted in a single call: one round trip keeps
p95 latency down, and the model sees the notes together so it can tell a real
directive apart from a distractor.
"""

from typing import Any

SYSTEM_INSTRUCTION = """\
You convert campus energy operator notes into structured directives for a \
24-hour electricity scheduling optimizer. You are a precise extraction engine, \
not an advisor.

SUPPORTED DIRECTIVE TYPES (no others exist):

1. solar_reduction - usable solar is reduced during specific hours.
   structured_adjustment: {"hours": [int], "factor": number}
2. minimum_battery_reserve - battery energy must stay at or above a level.
   structured_adjustment: {"hours": [int], "minimum_energy_kwh": number}
3. no_charge_window - battery charging unavailable during specific hours.
   structured_adjustment: {"hours": [int]}
4. no_discharge_window - battery discharging unavailable during specific hours.
   structured_adjustment: {"hours": [int]}
5. max_grid_window - grid import capped during specific hours.
   structured_adjustment: {"hours": [int], "max_grid_kwh": number}
6. no_op - the note does not affect this 24-hour energy schedule.
   structured_adjustment: null

RULES:

- Return exactly one entry per operator note, in note_index order 0..N-1.
- Time windows are START-INCLUSIVE and END-EXCLUSIVE. "1 PM to 3 PM" is
  [13, 14]. "noon until 2 PM" is [12, 13]. "6 PM until 9 PM" is [18, 19, 20].
  "from 2 AM until 5 AM" is [2, 3, 4]. A window ending at or past midnight
  stops at hour 23.
- hours must be unique integers 0..23 in ascending order.
- For solar_reduction, `factor` is the FRACTION OF SOLAR THAT REMAINS, not the
  reduction. "an 80% reduction" means factor 0.2. "drops to about 25%" means
  factor 0.25. "roughly half" or "halved" means factor 0.5. "one-fifth of
  normal output" means factor 0.2. factor is always between 0 and 1.
- Reserve levels stated as a percentage are a percentage OF BATTERY CAPACITY.
  Convert to kWh using the capacity given below.
- Notes about scheduling, staffing, bookings, menus, deadlines, notices,
  academic events or anything unrelated to electricity, solar, battery or grid
  import are no_op. Do not invent an energy rule for them.
- Never invent demand, tariff, battery parameters or unsupported directive
  types. If a note is about energy but matches no supported type, use no_op.
- applies must be true for every non-no_op directive, and false only for no_op.
- explanation is one short sentence.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "directive_interpretation": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {"type": "integer"},
                    "applies": {"type": "boolean"},
                    "directive_type": {
                        "type": "string",
                        "enum": [
                            "solar_reduction",
                            "minimum_battery_reserve",
                            "no_charge_window",
                            "no_discharge_window",
                            "max_grid_window",
                            "no_op",
                        ],
                    },
                    "hours": {"type": "array", "items": {"type": "integer"}},
                    "factor": {"type": "number"},
                    "minimum_energy_kwh": {"type": "number"},
                    "max_grid_kwh": {"type": "number"},
                    "explanation": {"type": "string"},
                },
                "required": ["note_index", "applies", "directive_type", "explanation"],
            },
        }
    },
    "required": ["directive_interpretation"],
}


def build_prompt(notes: list[str], battery: Any) -> str:
    """Render the user-side prompt for one scenario."""
    lines = [
        SYSTEM_INSTRUCTION,
        "",
        "BATTERY CONTEXT (use for percentage-based reserve levels):",
        f"- capacity_kwh: {battery.capacity_kwh}",
        f"- initial_energy_kwh: {battery.initial_energy_kwh}",
        f"- minimum_energy_kwh: {battery.minimum_energy_kwh}",
        "",
        "OPERATOR NOTES:",
    ]
    for i, note in enumerate(notes):
        lines.append(f'  note_index {i}: "{note}"')
    lines += [
        "",
        f"Return JSON with a directive_interpretation array of exactly "
        f"{len(notes)} entries, one per note, in note_index order.",
        "Put the hours/factor/minimum_energy_kwh/max_grid_kwh fields directly on "
        "each entry; omit the ones that do not apply to the chosen type.",
    ]
    return "\n".join(lines)
