from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .models import canonical, digest


DEFAULT_ROUTES = Path(__file__).resolve().parents[3] / "config/business_evidence/topic_routes.v1.json"


def load_routes(path: Path = DEFAULT_ROUTES) -> dict:
    config = json.loads(Path(path).read_text("utf-8"))
    if config.get("schema_version") != "1.0.0" or config.get("version") != "1.0.0":
        raise ValueError("unsupported_business_route_version")
    if [t["id"] for t in config["topics"]] != [f"Q{i:02d}" for i in range(1, 11)]:
        raise ValueError("routes_must_cover_all_ten_questions")
    questions = json.loads((DEFAULT_ROUTES.parents[1] / "data_sources/business_model_questions.v1.json").read_text("utf-8"))
    if [t["question_id"] for t in config["topics"]] != [t["question_id"] for t in questions["topics"]]:
        raise ValueError("route_question_set_mismatch")
    if config["neighbor_pages"] != 1:
        raise ValueError("route_v1_requires_one_neighbor_page")
    for topic in config["topics"]:
        for key in ("sections", "terms", "preferred_titles", "checks"):
            if not topic.get(key) or any(not isinstance(s, str) or not s.strip() for s in topic[key]):
                raise ValueError("incomplete_business_topic_route")
        for pattern in (*topic["sections"], *topic["terms"], *topic["preferred_titles"]):
            re.compile(pattern)
    return config


def route_pages(pages: dict, config: dict) -> dict:
    routes = {}
    for topic in config["topics"]:
        matches = []
        selected = set()
        for number, text in pages.items():
            headings = [pattern for pattern in topic["sections"] if re.search(pattern, text, re.I)]
            if headings or all(re.search(pattern, text, re.I) for pattern in topic["terms"]):
                matches.append({"page_number": number, "section_signals": headings,
                                "section_offsets": [{"pattern": pattern, "start": m.start(), "end": m.end()}
                                    for pattern in headings for m in re.finditer(pattern, text, re.I)],
                                "locator_type": "pdf_page" if number is not None else "html_document",
                                "status": "candidate_requires_review"})
                if number is None:
                    selected.add(None)
                else:
                    selected.update(p for p in (number - 1, number, number + 1) if p in pages)
        routes[topic["id"]] = {"matches": matches, "pages": sorted(selected, key=lambda x: x or 0),
            "status": "candidate_requires_review" if matches else "no_route_requires_fulltext_search",
            "full_document_reviewed": False, "checks": topic["checks"]}
    return routes


def build_routes(corpus, config: dict, snapshot_ids: list[str] | None = None, artifact_selection: dict | None = None) -> dict:
    corpus.validate()
    selected = set(snapshot_ids or corpus.items)
    if not selected <= corpus.items.keys():
        raise ValueError("route_snapshot_not_in_manifest")
    groups, documents, exclusions, pages_by_hash = {}, [], [], {}
    for snapshot_id in sorted(selected):
        item = corpus.items[snapshot_id]
        candidates = [corpus.repository.get_derived_artifact(a) for a in item.derived_artifact_ids]
        candidates = [a for a in candidates if a.artifact_type == "text"]
        if artifact_selection and snapshot_id in artifact_selection:
            candidates = [a for a in candidates if a.derived_artifact_id == artifact_selection[snapshot_id]]
        if len(candidates) != 1:
            raise ValueError("route_requires_one_explicit_text_artifact_per_snapshot")
        artifact = candidates[0]
        doc = corpus.document(snapshot_id, artifact.derived_artifact_id)
        if doc["exclusion"]:
            exclusions.append({"snapshot_id": snapshot_id, "title": doc["title"], "reason_code": doc["exclusion"]})
            continue
        signature = digest({"raw_sha256": doc["snapshot"].sha256, "text_sha256": artifact.output_sha256,
            "extractor_id": artifact.extractor_id, "extractor_version": artifact.extractor_version,
            "config": artifact.parameters.get("config"), "input_mode": artifact.parameters.get("input_mode")})
        if signature not in groups:
            routes = route_pages(doc["pages"], config)
            relevant = {n for route in routes.values() for n in route["pages"]}
            page_refs = {}
            for number in sorted(relevant, key=lambda x: x or 0):
                text = doc["pages"][number]
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                pages_by_hash[sha] = text
                page_refs[str(number)] = sha
            groups[signature] = {"routes": routes, "page_text_refs": page_refs,
                                 "total_pages": len(doc["pages"]), "selected_pages": len(relevant)}
        documents.append({"snapshot_id": snapshot_id, "derived_artifact_id": artifact.derived_artifact_id,
            "company_id": doc["company_id"], "title": doc["title"], "content_group": signature,
            "raw_sha256": doc["snapshot"].sha256, "text_sha256": artifact.output_sha256,
            "source_url": doc["snapshot"].canonical_url,
            "topic_priority": {t["id"]: "preferred" if any(re.search(p, doc["title"], re.I)
                                  for p in t["preferred_titles"]) else "supplement" for t in config["topics"]}})
    return {"schema_version": "1.0.0", "manifest_id": corpus.manifest.manifest_id,
        "manifest_hash": corpus.manifest.manifest_hash, "route_config": config, "route_config_sha256": digest(config),
        "consumer_selection_policy": corpus.selection_policy, "status": "candidate_routes_are_not_verified_facts",
        "documents": documents, "content_groups": groups, "page_texts": pages_by_hash, "exclusions": exclusions,
        "counts": {"selected_documents": len(documents), "excluded_documents": len(exclusions),
                   "unique_content_groups": len(groups), "reused_document_routes": len(documents) - len(groups),
                   "unique_page_texts": len(pages_by_hash)}, "network_calls": 0}


def publish_json(path: Path, payload) -> None:
    """Immutable generated artifacts; identical reruns are no-ops."""
    path = Path(path)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("output_exists_with_different_content")
        return
    with path.open("xb") as stream:
        stream.write(data)
