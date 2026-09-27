"""Typed public calculation contract; delegates arithmetic to existing Calculations."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["bear", "base", "bull"]
    growth: float = Field(gt=-1, description="Growth as a fraction relative to bound earnings period, e.g. 0.05 means 5%.")
    multiple: float = Field(gt=0, description="Model-selected forward PE multiple.")
    reason: str = Field(min_length=1, description="Investment rationale for this assumption, not boilerplate.")
    valid_until: date
    invalidation: list[str] = Field(min_length=1, description="Internal assumption validity record; not a per-chapter writing requirement.")


class ScenarioAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: list[Scenario] = Field(min_length=3, max_length=3, description="Array of three objects; not a dictionary keyed by scenario names.")
    reason: str = Field(min_length=1)


class NumericGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: list[float] = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1)


class PEAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: ScenarioAssumption
    sensitivity_growth: NumericGrid
    sensitivity_multiples: NumericGrid


class DividendGrowth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float = Field(ge=-1, allow_inf_nan=False)
    reason: str = Field(min_length=1)


class DividendHorizon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["next_12_months"]
    reason: str = Field(min_length=1)


class DividendAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dividend_growth: DividendGrowth
    horizon: DividendHorizon


def public_calculator(workspace):
    from .calculations import Calculations
    def calculate(research_id: str, method: Literal["financial_summary", "ratio", "cagr", "pe_scenarios", "price_change", "payout_ratio", "dividend_yield", "forecast_dividend_yield", "inventory_composition", "inventory_allowance_ratio"],
                  bindings: dict[str, str], assumptions: PEAssumptions | DividendAssumptions | None = None):
        """Deterministic arithmetic. inventory_composition and inventory_allowance_ratio bind only period (YYYY-MM-DD), with no assumptions or raw amounts; code verifies frozen note cells. price_change bindings: start_evidence/end_evidence (issuer contract-price announcements). payout_ratio: dividend_evidence/earnings (same annual year). dividend_yield and forecast_dividend_yield: dividend_evidence/shares/price. Forecast requires dividend_growth and next_12_months horizon with reasons. Announcement values are extracted by code; never pass raw amounts. PE uses earnings/shares/price facts and scenario array. financial_summary takes empty bindings."""
        # Note methods bind only period (YYYY-MM-DD); never model-entered amounts.
        schema = DividendAssumptions if method == "forecast_dividend_yield" else PEAssumptions
        parsed = schema.model_validate(assumptions) if assumptions is not None else None
        return Calculations(workspace).calculate(research_id,method,bindings,
            parsed.model_dump(mode="json") if parsed else None)
    return calculate
