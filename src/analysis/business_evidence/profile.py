"""Deterministic offline business profiles over frozen, cited assertions."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, model_validator

from .corpus import FrozenCorpus
from .models import Citation, EvidenceModel, digest
from .store import FactStore, aware


METHOD_VERSION = "business-profile-v1.1.0"
TOPICS = dict(zip((f"Q{i:02d}" for i in range(1, 11)), (
    "起源、业务构成与盈利模式", "产品与地区经济", "客户、供应商与渠道", "价格与单位经济",
    "产能与产销存", "资本开支", "研发投入与产出", "产业链", "战略变化", "竞争力声明与可观察证据")))
KINDS = {"disclosed_fact": "披露事实", "company_statement": "公司表述", "analysis": "分析判断"}
STATES = {"current": "可用事实", "missing": "未取得可用事实", "conflict": "口径内数值冲突",
          "superseded": "仅有已更正旧值", "zero_denominator": "分母为零"}
GAPS = {"not_found_in_reviewed_materials": "已审材料内未形成证据", "external_evidence_needed": "需外部证据",
        "comparability": "口径限制", "conflict": "来源数值冲突"}
UNITS = {"CNY": ("亿元", "0.00000001"), "CNY_10000": ("亿元", "0.0001"),
         "tonne": ("吨", "1"), "percent": ("%", "1"), "count": ("个", "1"),
         "person": ("人", "1"), "10k-tonne": ("吨", "10000"), "CNY_per_tonne": ("万元/吨", "0.0001")}
TOKEN = re.compile(r"\{\{(fact|period|cell):([^{}]+)\}\}")


class Selector(EvidenceModel):
    subject: str = Field(min_length=1)
    subject_role: Literal["company", "subsidiary", "investee", "third_party"]
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dimensions: dict[str, str]
    basis: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    event_stage: Literal["reported", "completed"] = "reported"


class Series(EvidenceModel):
    series_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    selector: Selector
    selector_overrides: dict[str, Selector] = Field(default_factory=dict)
    note: str = Field(min_length=1)


class Calculation(EvidenceModel):
    series_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    operation: Literal["share_pct", "blended_unit_revenue", "gross_margin_pct"]
    numerator: str
    denominator: str
    note: str = Field(min_length=1)


class Statement(EvidenceModel):
    statement_id: str = Field(min_length=1)
    question_id: str = Field(pattern=r"^Q(?:0[1-9]|10)$")
    kind: Literal["disclosed_fact", "company_statement", "analysis"]
    text: str = Field(min_length=1)
    fact_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded_text(self):
        plain = TOKEN.sub("", self.text)
        if "{{" in plain or "}}" in plain or re.search(r"\d", plain):
            raise ValueError("numeric_narrative_requires_fact_or_cell_token")
        if self.kind != "disclosed_fact" and not self.limitations:
            raise ValueError("interpretation_requires_limitations")
        return self


class SearchScope(EvidenceModel):
    snapshot_id: str
    derived_artifact_id: str
    pages: tuple[int, ...] = ()  # Empty means the complete archived document.
    keywords: tuple[str, ...] = ()


class Gap(EvidenceModel):
    gap_id: str = Field(min_length=1)
    question_ids: tuple[str, ...] = Field(min_length=1)
    kind: Literal["not_found_in_reviewed_materials", "external_evidence_needed", "comparability", "conflict"]
    description: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    fact_ids: tuple[str, ...] = ()
    searches: tuple[SearchScope, ...] = ()

    @model_validator(mode="after")
    def explicit_scope(self):
        if any(q not in TOPICS for q in self.question_ids):
            raise ValueError("unknown_business_question")
        if self.kind == "not_found_in_reviewed_materials" and not self.searches:
            raise ValueError("search_miss_requires_document_scope")
        if self.kind == "conflict" and not self.fact_ids:
            raise ValueError("conflict_gap_requires_fact_ids")
        return self


def period_kind(period: str) -> str:
    if not re.fullmatch(r"\d{4}(?:H1)?", period):
        raise ValueError("profile_period_must_be_year_or_yearH1")
    return "half_year" if period.endswith("H1") else "full_year"


class ProfileInput(EvidenceModel):
    schema_version: Literal["1.0.0"]
    title: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: str
    periods: tuple[str, ...] = Field(min_length=1)
    series: tuple[Series, ...] = Field(min_length=1)
    calculations: tuple[Calculation, ...] = ()
    statements: tuple[Statement, ...] = ()
    gaps: tuple[Gap, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        cutoff = aware(self.as_of).date()
        for period in self.periods:
            kind = period_kind(period)
            end = date(int(period[:4]), 6, 30) if kind == "half_year" else date(int(period), 12, 31)
            if end > cutoff:
                raise ValueError("profile_period_not_complete_at_cutoff")
        for series in self.series:
            if set(series.selector_overrides) - set(self.periods):
                raise ValueError("selector_override_period_not_requested")
            for override in series.selector_overrides.values():
                if any(getattr(override, k) != getattr(series.selector, k) for k in ("subject", "subject_role", "event_stage")):
                    raise ValueError("selector_override_changes_subject_or_stage")
                if UNITS.get(override.unit, (override.unit,))[0] != UNITS.get(series.selector.unit, (series.selector.unit,))[0]:
                    raise ValueError("selector_override_incompatible_display_unit")
        for values in (self.periods, [s.series_id for s in (*self.series, *self.calculations)],
                       [s.statement_id for s in self.statements], [g.gap_id for g in self.gaps]):
            if len(values) != len(set(values)):
                raise ValueError("duplicate_profile_identifier")
        covered = {s.question_id for s in self.statements} | {q for g in self.gaps for q in g.question_ids}
        if covered != set(TOPICS):
            raise ValueError("all_ten_topics_require_statement_or_gap")
        return self


def _number(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display(value: str, unit: str) -> dict:
    label, scale = UNITS.get(unit, (unit, "1"))
    with localcontext() as ctx:
        ctx.prec = 40
        shown = (Decimal(value) * Decimal(scale)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"value": _number(shown), "unit": label, "scale": scale, "rounding": "half_up_2dp"}


def _cell(facts: list[dict], selector: Selector, period: str) -> dict:
    expected = selector.model_dump()
    matches = [f for f in facts if f["period"] == period and f["value_type"] == "decimal"
               and all(f[key] == value for key, value in expected.items())]
    active = [f for f in matches if f["status"] != "superseded"]
    result = {"period": period, "fact_ids": sorted(f["fact_id"] for f in matches), "unit": selector.unit}
    if any(f["status"] == "conflict" for f in active) or len(active) > 1:
        return {**result, "status": "conflict"}
    if not active:
        return {**result, "status": "superseded" if matches else "missing"}
    fact = active[0]
    return {**result, "status": "current", "value": fact["value"], "selected_fact_ids": [fact["fact_id"]],
            "display": _display(fact["value"], selector.unit)}


def _calculation(calc: Calculation, rows: dict, periods: tuple[str, ...]) -> dict:
    if calc.numerator not in rows or calc.denominator not in rows:
        raise ValueError("calculation_operand_missing")
    a, b = rows[calc.numerator], rows[calc.denominator]
    # Derived-on-derived chains are intentionally outside this method version.
    if "selector" not in a or "selector" not in b:
        raise ValueError("calculation_requires_base_series")
    formulas = {"share_pct": ("percent", "numerator / denominator * 100"),
                "blended_unit_revenue": ("CNY_per_tonne", "numerator / denominator"),
                "gross_margin_pct": ("percent", "(1 - denominator / numerator) * 100")}
    unit, formula = formulas[calc.operation]
    currency_scales = {"CNY": Decimal(1), "CNY_10000": Decimal(10000)}
    cells = {}
    for period in periods:
        sa = a.get("selector_overrides", {}).get(period, a["selector"])
        sb = b.get("selector_overrides", {}).get(period, b["selector"])
        if any(sa[k] != sb[k] for k in ("subject", "subject_role", "event_stage")):
            raise ValueError("calculation_subject_or_stage_mismatch")
        if sa["event_stage"] != "reported":
            raise ValueError("calculation_requires_reported_facts")
        scale_a, scale_b = currency_scales.get(sa["unit"]), currency_scales.get(sb["unit"])
        if calc.operation == "share_pct":
            if (sa["metric"] != sb["metric"] or sa["basis"] != sb["basis"] or scale_a is None or scale_b is None
                    or any(sa["dimensions"].get(k) != v for k, v in sb["dimensions"].items())
                    or sa["dimensions"] == sb["dimensions"]):
                raise ValueError("share_scope_or_unit_mismatch")
        elif calc.operation == "gross_margin_pct":
            if (sa["metric"] != "operating_revenue" or sb["metric"] != "operating_cost"
                    or sa["basis"] != sb["basis"] or sa["dimensions"] != sb["dimensions"]
                    or scale_a is None or scale_b is None):
                raise ValueError("margin_scope_or_unit_mismatch")
        else:
            if (sa["metric"] != "operating_revenue" or sb["metric"] != "sales_volume"
                    or scale_a is None or sb["unit"] != "tonne" or sa["dimensions"] != sb["dimensions"]):
                raise ValueError("unit_revenue_scope_or_unit_mismatch")
            scale_b = Decimal(1)
        ac, bc = a["cells"][period], b["cells"][period]
        cell = {"period": period, "unit": unit, "formula": formula,
                "operand_cells": [f"{calc.numerator}:{period}", f"{calc.denominator}:{period}"],
                "operand_scales": [_number(scale_a), _number(scale_b)],
                "fact_ids": sorted(set(ac["fact_ids"] + bc["fact_ids"]))}
        failures = {c["status"] for c in (ac, bc)} - {"current"}
        if failures:
            cell["status"] = next(s for s in ("conflict", "superseded", "missing") if s in failures)
        elif Decimal(ac["value"] if calc.operation == "gross_margin_pct" else bc["value"]) == 0:
            cell["status"] = "zero_denominator"
        else:
            with localcontext() as ctx:
                ctx.prec = 40
                av, bv = Decimal(ac["value"]) * scale_a, Decimal(bc["value"]) * scale_b
                value = _number((1 - bv / av) * 100 if calc.operation == "gross_margin_pct"
                                else av / bv * (100 if calc.operation == "share_pct" else 1))
            cell.update(status="current", value=value, display=_display(value, unit),
                        input_values=[ac["value"], bc["value"]],
                        input_fact_ids=ac["selected_fact_ids"] + bc["selected_fact_ids"],
                        decimal_precision=40)
        cells[period] = cell
    return {**calc.model_dump(mode="json"), "unit": unit, "cells": cells}


def verify_citations(facts: list[dict], corpus: FrozenCorpus, data_root: Path):
    checked = {corpus.manifest.manifest_id: corpus}
    for fact in facts:
        for cite in fact["citations"]:
            mid = cite["manifest_id"]
            if mid not in checked:
                checked[mid] = FrozenCorpus(corpus.db, data_root, mid, selection_policy=corpus.selection_policy)
            original = Citation.model_validate({k: cite[k] for k in Citation.model_fields})
            actual = checked[mid].citation(original, fact["company_id"])
            if actual != {k: v for k, v in cite.items() if k != "citation_id"}:
                raise ValueError("stored_citation_provenance_changed")


def build_profile(spec: ProfileInput, store: FactStore, corpus: FrozenCorpus, data_root: Path) -> dict:
    corpus.validate()
    query = store.query(as_of=spec.as_of, company_id=spec.company_id)
    facts = {f["fact_id"]: f for f in query["facts"]}
    rows = {s.series_id: {**s.model_dump(mode="json"), "unit": s.selector.unit,
                         "cells": {p: _cell(query["facts"], s.selector_overrides.get(p, s.selector), p) for p in spec.periods}}
            for s in spec.series}
    for calc in spec.calculations:
        rows[calc.series_id] = _calculation(calc, rows, spec.periods)
    used = {fid for row in rows.values() for c in row["cells"].values() for fid in c["fact_ids"]}
    statements = []
    for statement in spec.statements:
        refs = set(statement.fact_ids)
        cell_refs = []
        for kind, identifier in TOKEN.findall(statement.text):
            if kind in ("fact", "period"):
                refs.add(identifier)
            else:
                sid, sep, period = identifier.partition(":")
                if not sep or sid not in rows or period not in rows[sid]["cells"]:
                    raise ValueError("unknown_narrative_cell")
                cell = rows[sid]["cells"][period]
                if cell["status"] != "current":
                    raise ValueError("narrative_cell_is_not_current")
                refs.update(cell.get("selected_fact_ids", cell.get("input_fact_ids", [])))
                cell_refs.append(identifier)
        if not refs:
            raise ValueError("statement_requires_evidence")
        for fid in refs:
            if fid not in facts or facts[fid]["status"] != "current":
                raise ValueError("narrative_fact_missing_future_or_not_current")
            if statement.kind == "disclosed_fact" and facts[fid]["event_stage"] not in ("reported", "completed"):
                raise ValueError("company_claim_cannot_be_disclosed_fact")
        used.update(refs)
        statements.append({**statement.model_dump(mode="json"), "fact_ids": sorted(refs),
                           "cell_refs": sorted(set(cell_refs))})
    gaps = []
    for gap in spec.gaps:
        if any(fid not in facts for fid in gap.fact_ids):
            raise ValueError("gap_fact_missing_at_cutoff")
        if gap.kind == "conflict" and not any(facts[f]["status"] == "conflict" for f in gap.fact_ids):
            raise ValueError("gap_does_not_reference_a_conflict")
        searches = []
        for scope in gap.searches:
            doc = corpus.document(scope.snapshot_id, scope.derived_artifact_id)
            if doc["company_id"] != spec.company_id or doc["exclusion"]:
                raise ValueError("gap_search_company_or_selection_mismatch")
            if doc["snapshot"].available_at > aware(spec.as_of):
                raise ValueError("gap_search_future_document")
            pages = list(scope.pages) if scope.pages else list(doc["pages"])
            if len(pages) != len(set(pages)) or any(p not in doc["pages"] for p in pages):
                raise ValueError("gap_search_page_missing_or_duplicate")
            searches.append({**scope.model_dump(mode="json"), "title": doc["title"], "reviewed_pages": pages,
                "text_sha256": doc["artifact"].output_sha256,
                "keyword_matches": {k: [p for p in pages if k in doc["pages"][p]] for k in scope.keywords},
                "keyword_hits_are_not_verified_facts": True})
        used.update(gap.fact_ids)
        gaps.append({**gap.model_dump(mode="json"), "searches": searches})
    selected = [facts[fid] for fid in sorted(used)]
    verify_citations(selected, corpus, data_root)
    result = {"schema_version": "1.0.0", "method_version": METHOD_VERSION, "title": spec.title,
        "company_id": spec.company_id, "as_of": query["as_of"], "input_sha256": digest(spec.model_dump(mode="json")),
        "profile_input": spec.model_dump(mode="json"),
        "manifest_id": corpus.manifest.manifest_id, "manifest_hash": corpus.manifest.manifest_hash,
        "storage_namespace_id": corpus.manifest.storage_namespace_id, "selection_policy": corpus.selection_policy,
        "periods": [{"period": p, "kind": period_kind(p)} for p in spec.periods],
        "statements": statements, "series": list(rows.values()), "gaps": gaps, "facts": selected,
        "topics": [{"question_id": q, "title": title,
            "statement_ids": [s["statement_id"] for s in statements if s["question_id"] == q],
            "gap_ids": [g["gap_id"] for g in gaps if q in g["question_ids"]]} for q, title in TOPICS.items()],
        "acceptance": {"citation_verification": "passed", "source_review": "ai_reviewed",
            "human_profile_acceptance": "pending", "network_calls": 0, "acquisition_writes": 0},
        "unavailable_cells": [{"series_id": s["series_id"], "period": p, "status": c["status"],
                               "fact_ids": c["fact_ids"]} for s in rows.values() for p, c in s["cells"].items()
                              if c["status"] != "current"]}
    return {"profile_id": "profile-" + digest(result), **result}
