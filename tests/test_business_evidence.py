from copy import deepcopy
from types import SimpleNamespace
import sqlite3

import pytest

from analysis.business_evidence.corpus import FrozenCorpus, split_pages
from analysis.business_evidence.models import Citation, ReviewBatch
from analysis.business_evidence.routing import load_routes, route_pages, build_routes
from analysis.business_evidence.store import FactStore


class Corpus:
    manifest = SimpleNamespace(manifest_id="m1", manifest_hash="a" * 64)
    selection_policy = "test"
    def validate(self):
        pass
    def citation(self, cite, company):
        return {**cite.model_dump(), "title": "更正公告" if cite.snapshot_id == "new" else "年报",
                "available_at": "2024-04-02T00:00:00+00:00" if cite.snapshot_id == "new" else "2024-03-01T00:00:00+00:00"}


def record(name="original", value="100.00", snapshot="old", **changes):
    result = {"record_id": name, "company_id": "600519", "subject": "示例公司", "subject_role": "company",
        "period": "2023", "metric": "revenue", "dimensions": {"product": "甲"}, "basis": "合并营业收入",
        "event_stage": "reported", "value_type": "decimal", "value": value, "unit": "CNY", "question_ids": ["Q02"],
        "citations": [{"snapshot_id": snapshot, "derived_artifact_id": snapshot + "-text", "page_number": 2,
                       "section": "分产品收入", "quote": "营业收入为 100.00 元，更正为 200.00 元。"}]}
    result.update(changes)
    return result


def batch(*records, corrections=()):
    return ReviewBatch(schema_version="1.0.0", reviewer="AI fixture", records=records, corrections=corrections)


def test_same_fact_multiple_sources_idempotency_and_immutable_history(tmp_path):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    first = record()
    duplicate = record("duplicate", "100", "another")
    result = store.import_batch(batch(first, duplicate), Corpus())
    assert result["new_facts"] == 1 and result["new_citations"] == 2
    assert store.import_batch(batch(first, duplicate), Corpus())["new_facts"] == 0
    view = store.query(as_of="2025-01-01T00:00:00Z")
    assert len(view["facts"]) == 1 and len(view["facts"][0]["citations"]) == 2
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE facts SET payload='{}'")


@pytest.mark.parametrize("change", [{"subject": "拟投资公司", "subject_role": "investee"},
    {"period": "2022"}, {"unit": "CNY_10000"}, {"basis": "母公司营业收入"},
    {"dimensions": {"product": "乙"}}, {"event_stage": "planned"}])
def test_different_subject_period_scope_unit_and_stage_never_merge(tmp_path, change):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    result = store.import_batch(batch(record(), record("distinct", **change)), Corpus())
    assert result["new_facts"] == 2
    assert store.query(as_of="2025-01-01T00:00:00Z")["counts"]["conflict"] == 0


def test_conflict_is_not_resolved_by_newer_document_but_explicit_correction_is(tmp_path):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    old, new = record(), record("corrected", "200", "new", revision="corrected-2024-04-02")
    store.import_batch(batch(old, new), Corpus())
    assert store.query(as_of="2024-04-01T00:00:00Z")["counts"] == {"current": 1, "superseded": 0, "conflict": 0}
    assert store.query(as_of="2024-05-01T00:00:00Z")["counts"]["conflict"] == 2
    correction = {"previous_record_id": "original", "replacement_record_id": "corrected",
                  "reason": "原数值披露错误", "citation": new["citations"][0]}
    result = store.import_batch(batch(old, new, corrections=[correction]), Corpus())
    assert result["new_corrections"] == 1 and result["new_facts"] == 0
    assert store.query(as_of="2024-05-01T00:00:00Z")["counts"] == {"current": 1, "superseded": 1, "conflict": 0}
    assert store.query(as_of="2024-04-01T00:00:00Z")["counts"]["current"] == 1


@pytest.mark.parametrize("failure", ["unit", "same", "missing", "not_cited"])
def test_invalid_correction_rolls_back_entire_batch(tmp_path, failure):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    old, new = record(), record("corrected", "200", "new")
    if failure == "unit": new["unit"] = "CNY_10000"
    if failure == "same": new["value"] = "100"
    correction = {"previous_record_id": "missing" if failure == "missing" else "original",
        "replacement_record_id": "corrected", "reason": "更正", "citation": deepcopy(new["citations"][0])}
    if failure == "not_cited": correction["citation"]["section"] = "另一段"
    with pytest.raises(ValueError):
        store.import_batch(batch(old, new, corrections=[correction]), Corpus())
    assert store.query(as_of="2025-01-01T00:00:00Z")["facts"] == []


def test_record_alias_collision_and_unknown_store_namespace_are_rejected(tmp_path):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    store.import_batch(batch(record()), Corpus())
    with pytest.raises(ValueError, match="immutable_aliases"):
        store.import_batch(batch(record(value="200")), Corpus())
    with pytest.raises(ValueError, match="namespace"):
        FactStore(store.path, "other")


@pytest.mark.parametrize("value,value_type", [("999", "decimal"), ("公司已建立强大护城河", "text")])
def test_unsupported_value_cannot_hide_behind_an_unrelated_quote(tmp_path, value, value_type):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    with pytest.raises(ValueError, match="missing_from_quote"):
        store.import_batch(batch(record(value=value, value_type=value_type)), Corpus())


def test_adjacent_numeric_table_columns_keep_their_boundaries(tmp_path):
    row = record(value="1018000.00")
    row["citations"][0]["quote"] = "计划投资 当期投入 累计投入\n1,018,000.00 121,954.68 672,070.68"
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    assert store.import_batch(batch(row), Corpus())["new_facts"] == 1


def test_routes_preserve_all_candidate_pages_neighbors_and_explicit_missing_state():
    config = load_routes()
    pages = {1: "封面", 2: "产能情况，实际产量 100 吨", 3: "表下注释，单位为吨", 4: "附表",
             5: "产能情况，实际产量 120 吨", 6: "续表", 7: "尾页"}
    routes = route_pages(pages, config)
    assert routes["Q05"]["pages"] == [1, 2, 3, 4, 5, 6]
    assert routes["Q07"]["status"] == "no_route_requires_fulltext_search"
    assert not routes["Q05"]["full_document_reviewed"]
    assert routes["Q05"]["matches"][0]["section_offsets"][0]["start"] == 0


def test_duplicate_snapshot_provenance_does_not_defeat_page_routing_reuse():
    corpus = Corpus()
    corpus.items = {sid: SimpleNamespace(derived_artifact_ids=(sid + "-text",)) for sid in ("a", "b")}
    corpus.repository = SimpleNamespace(get_derived_artifact=lambda aid: SimpleNamespace(
        derived_artifact_id=aid, artifact_type="text", extractor_id="mineru-precision-text", extractor_version="1.0.0",
        output_sha256="b" * 64, parameters={"config": {"model": "vlm"}, "input_mode": "upload",
        "bundle_artifact_id": aid + "-bundle", "layout_artifact_id": aid + "-layout", "parse_reuse": aid}))
    corpus.document = lambda *_: {"exclusion": None, "snapshot": SimpleNamespace(sha256="a" * 64, canonical_url="https://example.test/a.pdf"),
        "company_id": "600519", "title": "年报", "pages": {1: "产能情况，实际产量100吨"}}
    result = build_routes(corpus, load_routes())
    assert result["counts"]["selected_documents"] == 2
    assert result["counts"]["reused_document_routes"] == 1
    assert len(result["content_groups"]) == len(result["page_texts"]) == 1
    assert {d["snapshot_id"] for d in result["documents"]} == {"a", "b"}


def test_pdf_pages_cannot_be_missing_reordered_or_invented_and_html_has_null_page():
    assert split_pages("--- PDF page 1 ---\nA\n--- page 2 ---\nB", "application/pdf") == {1: "A", 2: "B"}
    assert split_pages("HTML 正文", "text/html") == {None: "HTML 正文"}
    for text in ("no markers", "--- page 2 ---\nB", "--- page 1 ---\nA\n--- page 1 ---\nB"):
        with pytest.raises(ValueError):
            split_pages(text, "application/pdf")


def test_citation_must_match_the_selected_page_and_company():
    corpus = object.__new__(FrozenCorpus)
    corpus.document = lambda *_: {"company_id": "600519", "exclusion": None, "pages": {1: "这是一条真实原文的公告记录。"}}
    citation = Citation(snapshot_id="s", derived_artifact_id="a", page_number=2, section="正文", quote="这是一条真实原文的公告记录。")
    with pytest.raises(ValueError, match="quote_not_found"):
        corpus.citation(citation, "600519")
    with pytest.raises(ValueError, match="company_mismatch"):
        corpus.citation(citation, "000001")
