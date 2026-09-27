from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis.research.knowledge import Knowledge
from analysis.research.workspace import ResearchError


ROOT = Path(__file__).resolve().parents[1]


class FakeKnowledgeService:
    method = {
        "method_id": "knowledge.working_capital",
        "version": "1.1.0",
        "title": "营运资本、再投资与自由现金流边界",
        "question_ids": ["ES02.Q08"],
        "steps": ["固定期初期末和合并范围"],
        "body": "营运资本需要区分经营占款、融资和暂时释放。" * 500,
        "content_status": "published",
    }

    def __init__(self):
        self.bundle = {
            "bundle_id": "kb-accepted",
            "catalog": {"methods": [self.method]},
            "method_identities": {self.method["method_id"]: "method-sha"},
        }

    def coverage(self, bundle_id=None):
        if bundle_id is None:
            return {"bundle_id": None, "bundle_kind": "none", "methods": []}
        return {"bundle_id": bundle_id, "bundle_kind": "candidate", "methods": [
            {"method_id": self.method["method_id"], "content_sha256": "method-sha"}
        ]}

    def get_bundle(self, bundle_id):
        assert bundle_id == "kb-accepted"
        return self.bundle

    def expand(self, reference):
        assert reference["bundle_id"] == "kb-accepted"
        assert reference["method_id"] == self.method["method_id"]
        return {
            **self.method,
            "content_sha256": "method-sha",
            "source_refs": [{"kind": "source", "source_id": "chapter10", "source_version": "1"}],
        }


def workspace(tmp_path):
    return SimpleNamespace(root=ROOT, config={
        "knowledge_catalog": "config/methods/knowledge/catalog.v1.json",
        "knowledge_store": str(tmp_path / "knowledge-store"),
        "knowledge_bundle_id": None,
    })


def test_no_default_release_is_explicit(tmp_path):
    result = Knowledge(workspace(tmp_path)).search_knowledge("现金流")
    assert result["status"] == "no_default_release"
    assert result["bundle_id"] is None


def test_candidate_is_used_only_when_bundle_is_explicit(tmp_path, monkeypatch):
    knowledge = Knowledge(workspace(tmp_path))
    fake = FakeKnowledgeService()
    monkeypatch.setattr(knowledge, "_service", lambda: fake)

    result = knowledge.search_knowledge("营运资本", bundle_id="kb-accepted")
    assert result["status"] == "ready"
    assert result["bundle_id"] == "kb-accepted"
    assert result["items"][0]["id"] == "knowledge.working_capital"

    legacy = knowledge.search_knowledge(card_id="cashflow-definition", bundle_id="kb-accepted")
    assert legacy["card_id"] == "knowledge.working_capital"
    assert legacy["metadata"]["content_sha256"] == "method-sha"
    assert "营运资本" in legacy["content"]


def test_versioned_read_is_paginated_and_bounded(tmp_path, monkeypatch):
    knowledge = Knowledge(workspace(tmp_path))
    monkeypatch.setattr(knowledge, "_service", lambda: FakeKnowledgeService())
    first = knowledge.search_knowledge(card_id="knowledge.working_capital", page=1,
                                       page_size=5, bundle_id="kb-accepted")
    assert first["next_page"] == 2
    second = knowledge.search_knowledge(card_id="knowledge.working_capital", page=2,
                                        page_size=5, bundle_id="kb-accepted")
    assert second["content"]
    with pytest.raises(ResearchError, match="knowledge_page_out_of_range"):
        knowledge.search_knowledge(card_id="knowledge.working_capital", page=999,
                                   bundle_id="kb-accepted")


def test_pagination_parameters_are_rejected(tmp_path):
    knowledge = Knowledge(workspace(tmp_path))
    with pytest.raises(ResearchError, match="invalid_pagination"):
        knowledge.search_knowledge(page_size=11)
