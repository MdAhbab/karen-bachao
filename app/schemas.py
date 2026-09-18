"""Request and response models matching the Problem Statement contract exactly.

Sections referenced below are from the canonical Problem Statement:
 - 07  request schema
 - 10  response schema
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)

BATTERY_ACTIONS = ("charge", "discharge", "idle")


# --------------------------------------------------------------------------
# Request  (Problem Statement section 07)
# --------------------------------------------------------------------------
class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: Annotated[int, Field(ge=0, le=23)]
    demand_kwh: Annotated[float, Field(ge=0)]
    solar_kwh: Annotated[float, Field(ge=0)]
    tariff_bdt_per_kwh: float


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: Annotated[float, Field(gt=0)]
    initial_energy_kwh: Annotated[float, Field(ge=0)]
    minimum_energy_kwh: Annotated[float, Field(ge=0)]
    max_charge_kwh_per_hour: Annotated[float, Field(ge=0)]
    max_discharge_kwh_per_hour: Annotated[float, Field(ge=0)]


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str
    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourEntry], Field(min_length=24, max_length=24)]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, notes: list[str]) -> list[str]:
        if any(not str(n).strip() for n in notes):
            raise ValueError("operator_notes entries must be non-empty strings")
        return notes

    @field_validator("hours")
    @classmethod
    def _hours_cover_full_day(cls, hours: list[HourEntry]) -> list[HourEntry]:
        if {h.hour for h in hours} != set(range(24)):
            raise ValueError("hours must contain exactly one entry per hour 0..23")
        return hours


# --------------------------------------------------------------------------
# Response  (Problem Statement section 10)
# --------------------------------------------------------------------------
class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: Literal[DIRECTIVE_TYPES]  # type: ignore[valid-type]
    structured_adjustment: dict[str, Any] | None
    explanation: str


class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal[BATTERY_ACTIONS]  # type: ignore[valid-type]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
