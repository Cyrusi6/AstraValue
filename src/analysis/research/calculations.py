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
        """Compute financial_summary, ratio, CAGR or PE scenarios; explicit assumptions need reasons."""
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

        if method == "financial_summary":
            if bindings or assumptions:
                raise ResearchError("financial_summary_uses_frozen_history_only")
            indexed = {(x["metric_id"], x["period"], x["period_type"]): x for x in facts.values() if x["state"] == "ready"}
            rows, gaps = [], []
            annual = payload["periods"]["annual"]
            for metric in ("operating_income", "parent_net_profit"):
                a, b = indexed.get((metric,annual[0],"cumulative")), indexed.get((metric,annual[-1],"cumulative"))
                if a and b:
                    if any(a["fact"].get(k)!=b["fact"].get(k) for k in ("unit","scope","currency")):
                        raise ResearchError("cagr_unit_or_scope_mismatch")
                    years = int(annual[-1][:4])-int(annual[0][:4])
                    start, end = number(a["fact"]["value"]), number(b["fact"]["value"])
                    if years > 0 and min(start,end)>0:
                        rows.append({"metric":metric+"_cagr", "start":annual[0],"end":annual[-1],"years":years,
                                     "value":str((end/start)**(Decimal(1)/years)-1),"unit":"ratio",
                                     "formula":"(end/start)^(1/actual_years)-1", "input_fact_ids":[a["fact"]["fact_id"],b["fact"]["fact_id"]]})
            for period in annual:
                prior = str(int(period[:4])-1)+"-12-31"
                keys=[("net_profit",period,"cumulative"),("operating_income",period,"cumulative"),
                      ("total_assets",period,"instant"),("total_assets",prior,"instant"),
                      ("total_liabilities",period,"instant"),("total_liabilities",prior,"instant")]
                items=[indexed.get(k) for k in keys]
                if any(x is None for x in items):
                    gaps.append({"method":"consolidated_dupont","period":period,"missing":[list(k) for k,x in zip(keys,items) if x is None]});continue
                if len({(x["fact"]["scope"],x["fact"]["currency"],x["fact"]["unit"]) for x in items})!=1 or items[0]["fact"]["unit"]!="CNY" or items[0]["fact"]["scope"]!="consolidated":
                    raise ResearchError("dupont_scope_or_unit_mismatch")
                profit,revenue,a1,a0,l1,l0=[number(x["fact"]["value"]) for x in items]
                assets=(a1+a0)/2; equity=(a1-l1+a0-l0)/2
                if min(revenue,assets,equity)<=0:
                    gaps.append({"method":"consolidated_dupont","period":period,"reason":"nonpositive_denominator"});continue
                rows.append({"metric":"consolidated_dupont", "period":period,"unit":"ratio",
                             "net_margin":str(profit/revenue),"asset_turnover":str(revenue/assets),
                             "equity_multiplier":str(assets/equity),"roe":str(profit/equity),
                             "average_assets":str(assets),"average_equity":str(equity),
                             "formula":"net_profit/revenue * revenue/average_assets * average_assets/average_total_equity",
                             "input_fact_ids":[x["fact"]["fact_id"] for x in items],
                             "boundary":"合并净利润/期初期末平均总权益，含少数股东；非披露的加权归母ROE，非ROIC"})
            return self.w.artifact(research_id,"calculation",{"method":method,"formula_version":"research-financial-summary-v1",
                 "bindings":{},"assumptions":{},"result":{"rows":rows,"gaps":gaps},
                 "input_fact_ids":{ref:x["fact"]["fact_id"] for ref,x in facts.items()},
                 "validation":"deterministic_calculation","status":"ready_with_gaps" if gaps else "ready"})
        elif method == "ratio":
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
            return {"status": "capability_gap", "method": method, "supported": ["ratio", "cagr", "pe_scenarios", "financial_summary"]}
        return self.w.artifact(research_id, "calculation", {"method": method, "formula_version": "research-calculations-v1",
            "bindings": bindings, "input_fact_ids": {k: v["fact"]["fact_id"] for k, v in selected.items()},
            "input_definitions": {k: {"period": v["period"], "period_type": v["period_type"], "unit": v["fact"]["unit"]} for k,v in selected.items()},
            "assumptions": assumptions, "result": result, "validation": "deterministic_calculation", "status": "ready"})
