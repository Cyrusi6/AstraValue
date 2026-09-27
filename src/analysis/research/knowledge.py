"""Version-bound research entry to the accepted knowledge service."""

from __future__ import annotations

import re
from pathlib import Path

from analysis.knowledge import KnowledgeService
from analysis.structured.research_lite import _split_utf8

from .workspace import ResearchError, validate_integer_parameter


LEGACY_CARD_IDS = {"cashflow-definition": "knowledge.working_capital"}


class Knowledge:
    def __init__(self, workspace):
        self.w = workspace

    def _service(self) -> KnowledgeService:
        config = getattr(self.w, "config", {})
        root = Path(self.w.root)
        configured_catalog = root / config.get(
            "knowledge_catalog", "config/methods/knowledge/catalog.v1.json"
        )
        # ResearchWorkspace tests and isolated snapshots may use a temporary
        # root.  The versioned catalog is a code-owned input, so resolve it
        # from the project root when the snapshot does not carry a copy.
        if not configured_catalog.is_file():
            configured_catalog = Path(__file__).resolve().parents[3] / "config/methods/knowledge/catalog.v1.json"
        return KnowledgeService.from_catalog(
            configured_catalog,
            root / config.get("knowledge_store", "var/research/knowledge-store"),
        )

    def _resolved(self, bundle_id: str | None = None):
        service = self._service()
        configured_id = bundle_id or getattr(self.w, "config", {}).get("knowledge_bundle_id")
        coverage = service.coverage(configured_id)
        selected_id = coverage["bundle_id"]
        if not selected_id:
            return service, None, coverage, []
        bundle = service.get_bundle(selected_id)
        available = {row["method_id"] for row in coverage["methods"]}
        methods = [method for method in bundle["catalog"]["methods"] if method["method_id"] in available]
        return service, bundle, coverage, methods

    def available_methods(self, bundle_id: str | None = None) -> dict:
        """Expose versioned method metadata for the research material catalog."""
        _, bundle, coverage, methods = self._resolved(bundle_id)
        if bundle is None:
            return {"status": "no_default_release", "bundle_id": None, "items": []}
        return {
            "status": "ready", "bundle_id": bundle["bundle_id"],
            "bundle_kind": coverage["bundle_kind"],
            "items": [
                {"id": method["method_id"], "title": method["title"],
                 "version": method["version"], "question_ids": method["question_ids"],
                 "content_sha256": bundle["method_identities"][method["method_id"]]}
                for method in methods
            ],
        }

    def search_knowledge(self, question: str = "", card_id: str | None = None,
                         page: int = 1, page_size: int = 5,
                         bundle_id: str | None = None) -> dict:
        """Find published methods, or read a bounded page from one pinned version."""
        validate_integer_parameter(page, "page", "invalid_pagination")
        validate_integer_parameter(page_size, "page_size", "invalid_pagination", 10)
        service, bundle, coverage, methods = self._resolved(bundle_id)
        if bundle is None:
            return {"status": "no_default_release", "bundle_id": None,
                    "gap": "No complete default knowledge release has passed acceptance."}
        version = {"bundle_id": bundle["bundle_id"], "bundle_kind": coverage["bundle_kind"]}
        if card_id:
            method_id = LEGACY_CARD_IDS.get(card_id, card_id)
            method = next((m for m in methods if m["method_id"] == method_id), None)
            if method is None:
                return {"status": "knowledge_gap", "card_id": card_id, **version}
            detail = service.expand({"kind": "method", "bundle_id": bundle["bundle_id"],
                                     "method_id": method_id, "method_version": method["version"]})
            chunks = _split_utf8(detail.get("body") or "", 4000)
            if page > len(chunks):
                raise ResearchError("knowledge_page_out_of_range")
            return {
                "status": "ready", "card_id": method_id, **version,
                "metadata": {"id": method_id, "title": detail["title"],
                             "version": detail["version"], "question_ids": detail["question_ids"],
                             "content_status": detail["content_status"],
                             "content_sha256": detail["content_sha256"],
                             "source_refs": detail["source_refs"],
                             "sources": detail.get("sources", []),
                             "cases": detail.get("cases", []),
                             "applicability": detail.get("applicability"),
                             "required_inputs": detail.get("required_inputs", [])},
                "content": chunks[page - 1],
                "next_page": page + 1 if page < len(chunks) else None,
            }
        query = question.strip().casefold()
        terms = [part for part in re.split(r"\W+", query) if part]
        ranked = []
        for method in methods:
            searchable = " ".join([method["title"], method["method_id"],
                                   *method.get("question_ids", []),
                                   *method.get("steps", []), method.get("body", "")]).casefold()
            score = (2 if query and query in searchable else 0) + sum(term in searchable for term in terms)
            if not query or score:
                ranked.append((score, method))
        ranked.sort(key=lambda row: (-row[0], row[1]["method_id"]))
        start = (page - 1) * page_size
        return {
            "status": "ready" if ranked else "knowledge_gap", **version,
            "total": len(ranked),
            "items": [
                {"id": method["method_id"], "title": method["title"],
                 "version": method["version"], "question_ids": method["question_ids"],
                 "content_sha256": bundle["method_identities"][method["method_id"]]}
                for _, method in ranked[start:start + page_size]
            ],
            "next_page": page + 1 if start + page_size < len(ranked) else None,
        }
