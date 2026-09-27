from __future__ import annotations

import re

from analysis.models import ResearchRating
from .workspace import ResearchError, ResearchWorkspace


class Drafts:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def save_section(self, research_id: str, snapshot_id: str, number: int, markdown: str, judgment: str,
                     evidence_refs: list[str], invalidation: list[str] | None = None, unknowns: list[str] | None = None,
                     counter_evidence: list[str] | None = None, *, contract_version: str = "v1"):
        """Save one agent-authored section. Facts and evidence references must belong to the given snapshot."""
        state, _, pack = self.w.pack(research_id)
        if snapshot_id != state["snapshot_id"]:
            raise ResearchError("stale_draft_snapshot")
        if not 1 <= number <= 8 or not markdown.strip() or not judgment.strip() or not evidence_refs:
            raise ResearchError("section_requires_number_text_judgment_and_evidence")
        facts = {i["fact_ref"] for i in pack["metrics"] if i.get("fact_ref")}
        evidence = {i["evidence_id"] for i in pack["evidence"]}
        evidence.update(i["evidence_id"] for i in pack.get("supplemental_evidence", []))
        evidence.update(i["artifact_id"] for i in self.w.artifacts(research_id,"evidence_read"))
        evidence.update(i["artifact_id"] for i in self.w.business_profiles(research_id))
        calculations = {i["artifact_id"] for i in self.w.artifacts(research_id,"calculation")}
        exploration_refs = {ref for ref in evidence_refs if ref.startswith("exploration_")}
        for kind, ref in re.findall(r"\{\{(explore|cite):([^{}]+)\}\}", markdown):
            if kind == "explore" or ref.startswith("exploration_"):
                exploration_refs.add(ref.split(".", 1)[0])
        if exploration_refs:
            from .custom_python import get_validated_exploration
            for ref in exploration_refs:
                get_validated_exploration(self.w, research_id, ref)
        if set(evidence_refs) - facts - evidence - calculations - exploration_refs:
            raise ResearchError("unknown_section_evidence")
        if re.search(r"<\s*(?:script|iframe|img)|!\[.*?\]\(https?://", markdown, re.I):
            raise ResearchError("external_or_active_report_content_not_allowed")
        return self.w.artifact(research_id, "section", {"number": number, "markdown": markdown, "judgment": judgment,
            "evidence_refs": evidence_refs, "invalidation": invalidation or [], "unknowns": unknowns or [],
            "counter_evidence": counter_evidence or [], "author_role": "host_model", "contract_version": contract_version})

    def save_conclusion(self, research_id: str, snapshot_id: str, rating: str, summary: str,
                        theses: list[str], risks: list[str], invalidation: list[str],
                        calculation_id: str | None = None, valuation_unavailable_reason: str | None = None,
                        *, contract_version: str = "v1", risk_summary: str | None = None):
        """Persist the host's explicit rating and rationale; never asks for per-assumption human approval."""
        state, _, _ = self.w.pack(research_id)
        if snapshot_id != state["snapshot_id"]:
            raise ResearchError("stale_conclusion_snapshot")
        ResearchRating(rating)
        if not summary.strip() or not theses or not risks or (contract_version == "v1" and not invalidation):
            raise ResearchError("conclusion_reasoning_required")
        if calculation_id:
            calc = next((c for c in self.w.artifacts(research_id,"calculation") if c["artifact_id"] == calculation_id),None)
            if not calc or calc["method"] != "pe_scenarios":
                raise ResearchError("active_valuation_calculation_required")
        elif not valuation_unavailable_reason:
            raise ResearchError("valuation_or_specific_unavailability_reason_required")
        return self.w.artifact(research_id,"conclusion",{"rating":rating,"summary":summary,"theses":theses,"risks":risks,
            "invalidation":invalidation,"calculation_id":calculation_id,"valuation_unavailable_reason":valuation_unavailable_reason,
            "contract_version":contract_version,"risk_summary":risk_summary})

    def get_draft(self, research_id: str):
        sections = {s["number"]: s for s in self.w.artifacts(research_id,"section")}
        conclusions = self.w.artifacts(research_id,"conclusion")
        return {"sections": [sections[n] for n in sorted(sections)], "missing_sections": sorted(set(range(1,9))-sections.keys()),
                "conclusion": conclusions[-1] if conclusions else None}
