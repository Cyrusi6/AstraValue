"""Text-only requirements and peer observations retain real frozen sources."""
import json

import pytest

from analysis.models import SourceRecord
from analysis.structured.reporting_bridge import build_report_request
from test_reporting_bridge import NOW, _hash, _pack, _write_json


def _with_text_coverage(tmp_path, records=None):
    pack = _pack(tmp_path)
    source = SourceRecord(source_id="text-only-source", name="Official company description",
        source_type="supplier-structured", retrieved_at=NOW, available_at=NOW,
        document_hash="d" * 64, raw_resource_snapshot_id="text-snapshot", source_definition_id="structured-test",
        source_definition_version="1.0.0", url="https://example.invalid/description")
    path = tmp_path / "text-run" / "sources.jsonl"
    path.parent.mkdir()
    values = [source.model_dump(mode="json")] if records is None else records
    path.write_text("".join(json.dumps(row) + "\n" for row in values), encoding="utf8")
    coverage = {"company": "600519", "question_id": "ES02.Q01", "requirement_id": "REQ.ES02.Q01.001",
        "period": "2025-12-31", "state": "source_text_available", "source_ids": [source.source_id],
        "record_ids": ["text-record"], "reason": "text_requires_review"}
    (pack / "question-coverage.jsonl").write_text(json.dumps(coverage) + "\n", encoding="utf8")
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf8"))
    manifest["source_inputs"].append({"files": {"runs/text-only/sources.jsonl": {"path": str(path), "sha256": _hash(path)}}})
    manifest["output_hashes"]["question-coverage.jsonl"] = _hash(pack / "question-coverage.jsonl")
    _write_json(pack / "manifest.json", manifest)
    return pack, source, path


def test_coverage_only_source_is_copied_from_frozen_descriptor_without_a_numeric_fact(tmp_path):
    pack, source, _ = _with_text_coverage(tmp_path)
    request = build_report_request(pack)
    assert source.source_id not in {sid for fact in request.facts for sid in fact.source_ids}
    assert next(item for item in request.sources if item.source_id == source.source_id) == source
    assert request.research_coverage["requirements"][0]["source_ids"] == [source.source_id]
    assert request.research_coverage["requirements"][0]["state"] == "source_text_available"


def test_coverage_only_missing_source_fails_instead_of_creating_a_placeholder(tmp_path):
    pack, _, _ = _with_text_coverage(tmp_path, records=[])
    with pytest.raises(ValueError, match="report_pack_source_missing:text-only-source"):
        build_report_request(pack)


def test_coverage_only_source_bytes_remain_hash_bound(tmp_path):
    pack, _, path = _with_text_coverage(tmp_path)
    path.write_text("", encoding="utf8")
    with pytest.raises(ValueError, match="report_pack_input_hash_mismatch"):
        build_report_request(pack)


def test_coverage_only_source_conflict_is_not_silently_resolved(tmp_path):
    pack, source, path = _with_text_coverage(tmp_path)
    conflicting = source.model_copy(update={"document_hash": "e" * 64})
    with path.open("a", encoding="utf8") as stream:
        stream.write(conflicting.model_dump_json() + "\n")
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf8"))
    manifest["source_inputs"][-1]["files"]["runs/text-only/sources.jsonl"]["sha256"] = _hash(path)
    _write_json(pack / "manifest.json", manifest)
    with pytest.raises(ValueError, match="report_pack_conflicting_source:text-only-source"):
        build_report_request(pack)


def _with_peer_observation(tmp_path, *, include_source=True):
    pack = _pack(tmp_path)
    source = SourceRecord(
        source_id="peer-source", name="Peer annual filing", source_type="supplier-structured",
        retrieved_at=NOW, available_at=NOW, document_hash="f" * 64,
        raw_resource_snapshot_id="peer-snapshot",
    )
    path = tmp_path / "peer" / "sources.jsonl"
    path.parent.mkdir()
    path.write_text(source.model_dump_json() + "\n" if include_source else "", encoding="utf8")
    core_path = pack / "core-pack.json"
    core = json.loads(core_path.read_text(encoding="utf8"))
    core["peers"] = [{
        "ticker": "000858", "name": "五粮液", "comparability_basis": "same industry",
        "metrics": [{"metric_id": "operating_income", "period": "2025-12-31",
                     "value": "1000", "unit": "CNY", "source_ids": [source.source_id]}],
    }]
    _write_json(core_path, core)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf8"))
    manifest["peer_inputs"] = [{"ticker": "000858", "sha256": "b" * 64,
        "files": {"sources.jsonl": {"path": str(path), "sha256": _hash(path)}}}]
    manifest["output_hashes"]["core-pack.json"] = _hash(core_path)
    _write_json(pack / "manifest.json", manifest)
    return pack, source, path


def test_peer_observation_source_is_copied_from_frozen_descriptor(tmp_path):
    pack, source, _ = _with_peer_observation(tmp_path)
    request = build_report_request(pack)
    assert source.source_id not in {sid for fact in request.facts for sid in fact.source_ids}
    assert next(item for item in request.sources if item.source_id == source.source_id) == source
    assert request.peer_sets[0].metadata["peers"][0]["metrics"][0]["source_ids"] == [source.source_id]


def test_peer_observation_missing_source_fails_instead_of_creating_a_placeholder(tmp_path):
    pack, _, _ = _with_peer_observation(tmp_path, include_source=False)
    with pytest.raises(ValueError, match="report_pack_source_missing:peer-source"):
        build_report_request(pack)


def test_peer_observation_source_bytes_remain_hash_bound(tmp_path):
    pack, _, path = _with_peer_observation(tmp_path)
    path.write_text("", encoding="utf8")
    with pytest.raises(ValueError, match="report_pack_input_hash_mismatch"):
        build_report_request(pack)
