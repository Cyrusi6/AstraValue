from __future__ import annotations

import re

from analysis.models import ResearchRating
from .workspace import ResearchError, ResearchWorkspace


class Drafts:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def save_section(self, research_id: str, snapshot_id: str, number: int, markdown: str, judgment: str,
                     evidence_refs: list[str], invalidation: list[str], unknowns: list[str] | None = None,
                     counter_evidence: list[str] | None = None):
        """Save one agent-authored section. Facts and evidence references must belong to the given snapshot."""
        state, _, pack = self.w.pack(research_id)
        if snapshot_id != state["snapshot_id"]:
            raise ResearchError("stale_draft_snapshot")
        if not 1 <= number <= 8 or not markdown.strip() or not judgment.strip() or not invalidation or not evidence_refs:
            raise ResearchError("section_requires_number_text_judgment_evidence_and_invalidation")
        facts = {i["fact_ref"] for i in pack["metrics"] if i.get("fact_ref")}
        evidence = {i["evidence_id"] for i in pack["evidence"]}
        evidence.update(i["artifact_id"] for i in self.w.artifacts(research_id,"evidence_read"))
        calculations = {i["artifact_id"] for i in self.w.artifacts(research_id,"calculation")}
        if set(evidence_refs) - facts - evidence - calculations:
            raise ResearchError("unknown_section_evidence")
        if re.search(r"<\s*(?:script|iframe|img)|!\[.*?\]\(https?://", markdown, re.I):
            raise ResearchError("external_or_active_report_content_not_allowed")
        return self.w.artifact(research_id, "section", {"number": number, "markdown": markdown, "judgment": judgment,
            "evidence_refs": evidence_refs, "invalidation": invalidation, "unknowns": unknowns or [],
            "counter_evidence": counter_evidence or [], "author_role": "host_model"})

    def save_conclusion(self, research_id: str, snapshot_id: str, rating: str, summary: str,
                        theses: list[str], risks: list[str], invalidation: list[str],
                        calculation_id: str | None = None, valuation_unavailable_reason: str | None = None):
        """Persist the host's explicit rating and rationale; never asks for per-assumption human approval."""
        state, _, _ = self.w.pack(research_id)
        if snapshot_id != state["snapshot_id"]:
            raise ResearchError("stale_conclusion_snapshot")
        ResearchRating(rating)
        if not summary.strip() or not theses or not risks or not invalidation:
            raise ResearchError("conclusion_reasoning_required")
        if calculation_id:
            calc = next((c for c in self.w.artifacts(research_id,"calculation") if c["artifact_id"] == calculation_id),None)
            if not calc or calc["method"] != "pe_scenarios":
                raise ResearchError("active_valuation_calculation_required")
        elif not valuation_unavailable_reason:
            raise ResearchError("valuation_or_specific_unavailability_reason_required")
        return self.w.artifact(research_id,"conclusion",{"rating":rating,"summary":summary,"theses":theses,"risks":risks,
            "invalidation":invalidation,"calculation_id":calculation_id,"valuation_unavailable_reason":valuation_unavailable_reason})

    def get_draft(self, research_id: str):
        sections = {s["number"]: s for s in self.w.artifacts(research_id,"section")}
        conclusions = self.w.artifacts(research_id,"conclusion")
        return {"sections": [sections[n] for n in sorted(sections)], "missing_sections": sorted(set(range(1,9))-sections.keys()),
                "conclusion": conclusions[-1] if conclusions else None}
