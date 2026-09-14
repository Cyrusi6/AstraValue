"""Auditable calculations over the active snapshot; the host supplies assumptions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date
from itertools import product
from typing import Any

from analysis.valuation import relative_valuation

from .workspace import ResearchError, ResearchWorkspace


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchError("invalid_numeric_input") from exc
    if not result.is_finite():
        raise ResearchError("nonfinite_input")
    return result


class Calculations:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def calculate(self, research_id: str, method: str, bindings: dict[str, str], assumptions: dict[str, Any] | None = None):
        """Compute ratio, CAGR or PE scenarios from bound fact references; explicit assumptions need reasons."""
        state, _, payload = self.w.pack(research_id)
        facts = {i["fact_ref"]: i for i in payload["metrics"] if i.get("fact_ref") and i.get("fact")}
        if any(ref not in facts for ref in bindings.values()):
            raise ResearchError("unknown_fact_reference")
        selected = {name: facts[ref] for name, ref in bindings.items()}
        assumptions = assumptions or {}
        for name, item in assumptions.items():
            if not isinstance(item, dict) or "value" not in item or not str(item.get("reason", "")).strip():
                raise ResearchError("assumption_requires_value_and_reason:" + name)
        inputs = {name: number(i["fact"]["value"]) for name, i in selected.items()}

        def require(names):
            if set(inputs) != set(names):
                raise ResearchError("required_fact_bindings:" + ",".join(names))

        if method == "ratio":
            require(["numerator", "denominator"])
            a, b = selected["numerator"], selected["denominator"]
            if any(a.get(k) != b.get(k) for k in ["period", "period_type"]):
                raise ResearchError("ratio_period_mismatch")
            if any(a["fact"].get(k) != b["fact"].get(k) for k in ["unit", "scope", "currency"]):
                raise ResearchError("ratio_unit_or_scope_mismatch")
            if inputs["denominator"] == 0:
                raise ResearchError("zero_denominator")
            result = {"value": str(inputs["numerator"] / inputs["denominator"]), "unit": "ratio"}
        elif method == "cagr":
            require(["start", "end"])
            a, b = selected["start"], selected["end"]
            for key in ["metric_id", "period_type"]:
                if a.get(key) != b.get(key):
                    raise ResearchError("cagr_definition_mismatch")
            if any(a["fact"].get(k) != b["fact"].get(k) for k in ("unit", "scope", "currency")):
                raise ResearchError("cagr_unit_or_scope_mismatch")
            if a["period"][4:] != b["period"][4:]:
                raise ResearchError("cagr_requires_matching_calendar_endpoints")
            years = int(b["period"][:4]) - int(a["period"][:4])
            if years <= 0 or min(inputs.values()) <= 0:
                raise ResearchError("cagr_requires_positive_values_and_span")
            result = {"value": str((inputs["end"] / inputs["start"]) ** (Decimal(1) / years) - 1),
                      "unit": "ratio", "years": years}
        elif method == "pe_scenarios":
            require(["earnings", "shares", "price"])
            if selected["earnings"]["metric_id"] != "parent_net_profit" or selected["earnings"]["period_type"] not in {"cumulative", "ttm"}:
                raise ResearchError("parent_earnings_annual_or_ttm_required")
            if selected["earnings"]["period_type"] == "cumulative" and not selected["earnings"]["period"].endswith("12-31"):
                raise ResearchError("partial_year_earnings_not_annualized_implicitly")
            if selected["shares"]["metric_id"] != "total_shares" or selected["price"]["metric_id"] != "market_price":
                raise ResearchError("shares_and_market_price_required")
            if selected["earnings"]["fact"]["unit"] != "CNY" or selected["shares"]["fact"]["unit"] != "shares" or selected["price"]["fact"]["unit"] != "CNY_per_share":
                raise ResearchError("pe_units_mismatch")
            if min(inputs.values()) <= 0:
                raise ResearchError("positive_pe_inputs_required")
            if set(assumptions) != {"scenarios", "sensitivity_growth", "sensitivity_multiples"}:
                raise ResearchError("explicit_scenarios_and_sensitivity_required")
            scenarios = assumptions["scenarios"]["value"]
            if len(scenarios) != 3 or {x.get("name") for x in scenarios} != {"bear", "base", "bull"}:
                raise ResearchError("bear_base_bull_required")
            base_eps = inputs["earnings"] / inputs["shares"]
            rows = []
            for scenario in scenarios:
                if not scenario.get("reason"):
                    raise ResearchError("scenario_reason_required")
                if not scenario.get("valid_until") or not scenario.get("invalidation"):
                    raise ResearchError("scenario_validity_and_invalidation_required")
                date.fromisoformat(scenario["valid_until"])
                growth = number(scenario["growth"])
                multiple = number(scenario["multiple"])
                if growth <= -1 or multiple <= 0:
                    raise ResearchError("invalid_growth_or_multiple")
                eps = base_eps * (1 + growth)
                verified = relative_valuation({"metric_per_share": float(eps), "target_multiple": float(multiple)}).model_dump(mode="json")
                value = eps * multiple
                rows.append({**scenario, "eps": str(eps), "fair_value": str(value),
                             "upside": str(value / inputs["price"] - 1), "existing_engine": verified})
            growths = assumptions["sensitivity_growth"]["value"]
            multiples = assumptions["sensitivity_multiples"]["value"]
            if not 1 <= len(growths) <= 10 or not 1 <= len(multiples) <= 10:
                raise ResearchError("sensitivity_dimensions_out_of_range")
            sensitivity = []
            for g, m in product(growths, multiples):
                g, m = number(g), number(m)
                if g <= -1 or m <= 0:
                    raise ResearchError("invalid_sensitivity_input")
                sensitivity.append({"growth": str(g), "multiple": str(m), "fair_value": str(base_eps * (1+g) * m)})
            result = {"base_eps_current_share_count": str(base_eps), "base_earnings_period": selected["earnings"]["period"],
                      "price": str(inputs["price"]), "price_fact_period": selected["price"]["fact"].get("period_end"),
                      "scenarios": rows, "sensitivity": sensitivity,
                      "limitation": "one-year forward earnings scenario; current shares assumed unchanged; not reported historical EPS"}
        else:
            return {"status": "capability_gap", "method": method, "supported": ["ratio", "cagr", "pe_scenarios"]}
        return self.w.artifact(research_id, "calculation", {"method": method, "formula_version": "research-calculations-v1",
            "bindings": bindings, "input_fact_ids": {k: v["fact"]["fact_id"] for k, v in selected.items()},
            "input_definitions": {k: {"period": v["period"], "period_type": v["period_type"], "unit": v["fact"]["unit"]} for k,v in selected.items()},
            "assumptions": assumptions, "result": result, "validation": "deterministic_calculation", "status": "ready"})
