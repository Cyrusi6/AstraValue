"""Host-neutral writing contract; historical drafts remain readable."""
from pathlib import Path
import hashlib
import re

from .drafts import Drafts
from .workspace import ResearchError, ROOT

VERSION = "buy-side-v2"
# The prompt files in config/prompts are both v5.  Keep this separate from
# VERSION: the latter is the persisted writing-data contract used by drafts.
PROMPT_VERSION = "buy-side-v5"
FORBIDDEN = ("由于输入包未提供", "受限于输入包", "本报告不作断言", "读者应自行注意", "结构化无记录不证明无事项")


def prompt_document(stage="research"):
    names = {"research": "buy_side_research.md", "review": "buy_side_review.md"}
    if stage not in names:
        raise ResearchError("unknown_prompt_stage")
    text = (ROOT / "config/prompts" / names[stage]).read_text(encoding="utf-8")
    return {"version": PROMPT_VERSION, "stage": stage, "sha256": hashlib.sha256(text.encode()).hexdigest(), "prompt": text}


def check_prose(markdown, number=None):
    if any(x in markdown for x in FORBIDDEN):
        raise ResearchError("editorial_revision:remove_defensive_boilerplate")
    if re.search(r"(?m)^\s{0,3}#{1,6}\s*.*(?:反证|失效条件|未知项|缺失信息|下行风险)", markdown):
        raise ResearchError("editorial_revision:use_single_global_risk_summary")


class Authoring:
    def __init__(self, workspace):
        self.w = workspace
        self.d = Drafts(workspace)

    def get_research_prompt(self, stage: str = "research"):
        """Return the identical versioned prompt for every host; task parameters are separate."""
        return prompt_document(stage)

    def save_section(self, research_id: str, snapshot_id: str, number: int, markdown: str,
                     judgment: str, evidence_refs: list[str]):
        """Save analysis and evidence only. No per-chapter risk, rebuttal or missing-data fields."""
        check_prose(markdown, number)
        return self.d.save_section(research_id, snapshot_id, number, markdown, judgment, evidence_refs,
                                   contract_version=VERSION)

    def save_conclusion(self, research_id: str, snapshot_id: str, rating: str, summary: str,
                        theses: list[str], risk_summary: str, calculation_id: str | None = None,
                        valuation_unavailable_reason: str | None = None):
        """Save the investment view and the single risk/invalidating-triggers paragraph (max 300 nonspace characters)."""
        check_prose(summary)
        check_prose(risk_summary)
        if not risk_summary.strip() or len(re.sub(r"\s", "", risk_summary)) > 300:
            raise ResearchError("risk_summary_requires_1_to_300_nonspace_characters")
        return self.d.save_conclusion(research_id, snapshot_id, rating, summary, theses,
            [risk_summary], [], calculation_id, valuation_unavailable_reason,
            contract_version=VERSION, risk_summary=risk_summary)

    def get_draft(self, research_id: str, number: int = 0):
        """Read one chapter (1..8), or compact index and conclusion (0); full prose stays in storage."""
        if number not in range(9):
            raise ResearchError("chapter_number_out_of_range")
        draft = self.d.get_draft(research_id)
        if number:
            return {"section": next((s for s in draft["sections"] if s["number"] == number), None)}
        return {**draft, "sections": [{"number": s["number"], "artifact_id": s["artifact_id"],
                                       "judgment": s["judgment"]} for s in draft["sections"]]}
