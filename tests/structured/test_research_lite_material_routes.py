import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from analysis.structured.research_lite import build_lite_pack
from analysis.structured.reporting_bridge import build_report_request
from analysis.structured.storage import canonical_sha256
from test_research_lite import _fact, _standalone_projection, _write_jsonl


AS_OF = date(2026, 9, 14)


def _peer_fact(ticker, ident, metric="operating_income", value="80", period="2025-12-31", **extra):
    item = _fact(ident, metric, value, period,
                 kind="market_quote" if metric.startswith("eastmoney_") else "cumulative")
    item["ticker"] = ticker
    item.update(extra)
    if metric.startswith("eastmoney_"):
        item["unit"] = "ratio"
    return item


def _build(tmp_path, source, **kwargs):
    result = build_lite_pack(input_root=source, ticker="600519", as_of=AS_OF,
                             output_root=tmp_path / "packs", max_tokens=50000, **kwargs)
    pack = Path(result["pack_dir"])
    return result, {name: json.loads((pack / name).read_text(encoding="utf-8"))
                    for name in ("core-pack.json", "manifest.json", "next-work.json")}


def _primary(root):
    _standalone_projection(root / "600519", "main", [
        _fact("main-income", "operating_income", "100", "2025-12-31")])


def _requirement(payload, name):
    return next(item for item in payload["coverage_requirements"] if item["requirement_id"] == name)


def _reports(root, *, evidence=True):
    root.mkdir(parents=True, exist_ok=True)
    documents, passages = [], []
    for label, period, category, route, published in (
        ("annual", "2025-12-31", "D01", "RD01", "2026-04-01"),
        ("interim", "2026-06-30", "D02", "RD07", "2026-08-20"),
    ):
        original = root / (label + ".pdf")
        original.write_bytes(b"%PDF-test-" + label.encode())
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        documents.append({"ticker": "600519", "period": period, "document_class": category,
                          "resource_id": label, "parse_status": "parsed", "published_at": published,
                          "original_path": original.name, "original_sha256": digest})
        passages.append({"company": "600519", "period": period, "route_id": route,
                         "document_class": category, "evidence_id": "evidence-" + label,
                         "original_sha256": digest, "locator": "page:1",
                         "text": "公司披露的主营业务和治理说明。", "data_nature": "source_text"})
    (root / "live-documents.json").write_text(json.dumps({"documents": documents}), encoding="utf-8")
    if evidence:
        _write_jsonl(root / "document-evidence.jsonl", passages)
    return documents, passages


def test_peer_inputs_merge_all_roots_and_keep_dedicated_root_out_of_company_facts(tmp_path):
    primary, first, second, peers = [tmp_path / name for name in ("primary", "first", "second", "peers")]
    _primary(primary)
    _standalone_projection(primary / "000858", "peer-primary", [
        _peer_fact("000858", "peer-income"),
        _peer_fact("000858", "unselected-income", value="999"),
    ], selected=["peer-income"])
    _standalone_projection(first / "000858", "peer-cost", [
        _peer_fact("000858", "peer-cost", metric="operating_cost", value="20")])
    _standalone_projection(first / "000568", "peer-second", [_peer_fact("000568", "second-income")])
    _standalone_projection(second / "000858", "peer-pe", [
        _peer_fact("000858", "peer-pe", metric="eastmoney_pe_ttm", value="16", period="2026-09-12")])
    _standalone_projection(peers / "000858", "peer-pb", [
        _peer_fact("000858", "peer-pb", metric="eastmoney_pb_mrq", value="5", period="2026-09-11")])
    _standalone_projection(peers / "600809", "peer-third", [_peer_fact("600809", "third-income")])
    _standalone_projection(peers / "600519", "old-company", [
        _fact("excluded-company-income", "operating_income", "999", "2025-12-31")])
    _reports(peers)

    result, outputs = _build(tmp_path, primary, supplements=(first, second), peer_roots=(peers, first))
    payload, manifest = outputs["core-pack.json"], outputs["manifest.json"]
    peer = next(item for item in payload["peers"] if item["ticker"] == "000858")
    metrics = {item["metric_id"]: item for item in peer["metrics"]}
    assert {key: item["value"] for key, item in metrics.items()} == {
        "operating_income": "80", "gross_margin": "0.75", "eastmoney_pe_ttm": "16", "eastmoney_pb_mrq": "5"}
    assert metrics["eastmoney_pe_ttm"]["period"] == "2026-09-12"
    assert metrics["eastmoney_pb_mrq"]["period"] == "2026-09-11"
    assert metrics["operating_income"]["currency"] == "CNY"
    assert metrics["operating_income"]["scope"] == "consolidated"
    assert metrics["eastmoney_pe_ttm"]["available_at"] == "2026-09-13T00:00:00Z"
    assert _requirement(payload, "lite.context.peer_comparison")["state"] == "ready"
    income = next(item for item in payload["metrics"]
                  if item["metric_id"] == "operating_income" and item["period"] == "2025-12-31"
                  and item["period_type"] == "cumulative")
    assert income["fact"]["fact_id"] == "main-income"
    assert [item["root"] for item in manifest["source_inputs"]] == [str(primary.resolve())]
    assert payload["evidence"] == []
    descriptors = [item for item in manifest["peer_inputs"] if item["ticker"] == "000858"]
    assert {item["root"] for item in descriptors} == {str(root.resolve()) for root in (primary, first, second, peers)}
    assert len(descriptors) == 4
    assert all(item["sha256"] == canonical_sha256({k: v for k, v in item.items() if k != "sha256"})
               for item in descriptors)
    request = build_report_request(Path(result["pack_dir"]))
    assert set(request.peer_sets[0].included_tickers) == {"000858", "000568", "600809"}

    with (second / "000858" / "facts.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="report_pack_input_hash_mismatch"):
        build_report_request(Path(result["pack_dir"]))
    with pytest.raises(ValueError, match="lite_(materialization|projection)"):
        _build(tmp_path, primary, supplements=(first, second), peer_roots=(peers,))


@pytest.mark.parametrize("missing", ["facts.jsonl", "manifest.json"])
def test_missing_formal_peer_input_fails_instead_of_becoming_empty(tmp_path, missing):
    primary, supplement = tmp_path / "primary", tmp_path / "supplement"
    _primary(primary)
    _standalone_projection(supplement / "000858", "peer", [_peer_fact("000858", "peer-income")])
    (supplement / "000858" / missing).unlink()
    with pytest.raises(ValueError, match="lite_materialization_"):
        _build(tmp_path, primary, supplements=(supplement,))


def test_empty_and_cutoff_inapplicable_peers_remain_pending_with_bound_inputs(tmp_path):
    primary, supplement = tmp_path / "primary", tmp_path / "supplement"
    _primary(primary)
    _standalone_projection(primary / "000858", "empty-peer", [])
    later = _peer_fact("000568", "published-after-cutoff")
    later["metadata"]["available_at"] = "2026-09-14T16:00:00Z"
    future_period = _peer_fact("000568", "future-market", metric="eastmoney_pe_ttm", period="2026-09-15")
    _standalone_projection(supplement / "000568", "future-peer", [later, future_period])
    _standalone_projection(supplement / "600809", "obsolete-peer", [
        _peer_fact("600809", "old-annual", period="2024-12-31")])
    _, outputs = _build(tmp_path, primary, supplements=(supplement,))
    payload = outputs["core-pack.json"]
    assert payload["peers"] == []
    assert _requirement(payload, "lite.context.peer_comparison")["state"] == "pending"
    assert len(outputs["manifest.json"]["peer_inputs"]) == 3
    work = next(item for item in outputs["next-work.json"]["items"]
                if item["requirement_id"] == "lite.context.peer_comparison")
    assert work["material_types"] == ["peer_facts"] and work["acquire_allowed"]
    assert work["task_hint"]["peer_tickers"] == ["000858", "000568", "600809"]
    assert work["task_hint"]["report_periods"] == ["2025-12-31"]
    assert work["task_hint"]["dependency_metric_ids"] == ["operating_cost"]


def test_peer_without_known_availability_and_conflicting_peer_values_do_not_count(tmp_path):
    primary, supplement = tmp_path / "primary", tmp_path / "supplement"
    _primary(primary)
    _standalone_projection(primary / "000858", "peer-one", [_peer_fact("000858", "value-one")])
    _standalone_projection(supplement / "000858", "peer-two", [_peer_fact("000858", "value-two", value="81")])
    unknown = _peer_fact("000568", "unknown-availability")
    unknown.pop("as_of")
    unknown["metadata"].pop("available_at")
    _write_jsonl(supplement / "000568" / "coverage-facts.jsonl", [unknown])
    _, outputs = _build(tmp_path, primary, supplements=(supplement,))
    assert outputs["core-pack.json"]["peers"] == []
    assert any(item.get("ticker") == "000858" and not item["resolved"]
               for item in outputs["core-pack.json"]["conflicts"])


def test_core_material_requests_are_scoped_and_existing_numeric_routes_survive(tmp_path):
    source = tmp_path / "source"
    _primary(source)
    _, outputs = _build(tmp_path, source)
    work = outputs["next-work.json"]["items"]
    for metric in ("market_price", "market_cap", "total_shares"):
        item = next(row for row in work if row.get("metric_id") == metric)
        assert item["dataset_id"] == "market_cap"
        assert item["material_types"] == ["market_quote"]
        assert item["task_hint"]["as_of"] == AS_OF.isoformat()
        assert item["acquire_allowed"]
    for label, category, period in (("annual", "D01", "2025-12-31"), ("interim", "D02", "2026-06-30")):
        item = next(row for row in work if row["requirement_id"] == "lite.context.latest_" + label + "_original")
        assert item["material_types"] == ["report_documents"]
        assert item["task_hint"] == {"action": "acquire_and_parse", "document_classes": [category],
                                     "report_periods": [period]}
        assert item["period"] == period and item["acquire_allowed"]
    slot = next(row for row in work if row["requirement_id"] == "lite.evidence.A.business_model")
    assert slot["material_types"] == ["report_documents"]
    assert slot["task_hint"]["route_ids"] == ["RD01"]
    assert slot["task_hint"]["report_periods"] == ["2025-12-31", "2026-06-30"]
    numeric = next(row for row in work if row.get("dataset_id") == "income_fields"
                   and row.get("raw_name") == "PARENT_NETPROFIT" and row["acquire_allowed"])
    assert numeric["stage"] == "acquisition" and "material_types" not in numeric
    calculated = next(row for row in work if row.get("metric_id") == "gross_margin")
    assert calculated["stage"] == "deterministic_calculation" and not calculated["acquire_allowed"]


@pytest.mark.parametrize("invalid", ["missing", "wrong_hash", "no_original_path", "future_publication", "future_evidence"])
def test_unverified_or_future_original_cannot_supply_readable_evidence(tmp_path, invalid):
    source = tmp_path / "source"
    _primary(source)
    documents, passages = _reports(source)
    if invalid == "missing":
        (source / "annual.pdf").unlink()
    elif invalid == "wrong_hash":
        (source / "annual.pdf").write_bytes(b"different original")
    elif invalid == "no_original_path":
        documents[0].pop("original_path")
    elif invalid == "future_publication":
        documents[0]["published_at"] = "2026-09-14T16:00:00Z"
    else:
        passages[0]["published_at"] = "2026-09-15"
        _write_jsonl(source / "document-evidence.jsonl", passages)
    (source / "live-documents.json").write_text(json.dumps({"documents": documents}), encoding="utf-8")
    _, outputs = _build(tmp_path, source)
    payload = outputs["core-pack.json"]
    assert {item["evidence_id"] for item in payload["evidence"]} == {"evidence-interim"}
    assert _requirement(payload, "lite.evidence.A.business_model")["state"] == "pending"
    assert payload["catalog"]["required_reports"]["latest_annual"]["state"] == (
        "parsed" if invalid == "future_evidence" else "pending")
    assert payload["catalog"]["required_reports"]["latest_interim"]["state"] == "parsed"


def test_evidence_only_roots_supply_originals_without_company_or_peer_projection(tmp_path):
    primary, evidence_root = tmp_path / "primary", tmp_path / "evidence"
    _primary(primary)
    _, passages = _reports(evidence_root, evidence=False)
    _write_jsonl(primary / "document-evidence.jsonl", passages)
    # Even broken formal exports in an evidence root are outside projection discovery.
    (evidence_root / "600519").mkdir()
    (evidence_root / "600519" / "facts.jsonl").write_text("invalid projection", encoding="utf-8")
    (evidence_root / "000858").mkdir()
    (evidence_root / "000858" / "facts.jsonl").write_text("invalid peer", encoding="utf-8")
    _, outputs = _build(tmp_path, primary, evidence_roots=(evidence_root,))
    payload, manifest = outputs["core-pack.json"], outputs["manifest.json"]
    assert {item["evidence_id"] for item in payload["evidence"]} == {"evidence-annual", "evidence-interim"}
    assert all(item["original_hash_verified"] and Path(item["original_path"]).is_absolute()
               for item in payload["evidence"])
    assert payload["peers"] == [] and manifest["peer_inputs"] == []
    assert [item["root"] for item in manifest["source_inputs"]] == [str(primary.resolve())]
    work = outputs["next-work.json"]["items"]
    documents = [item for item in work if item.get("material_types") == ["report_documents"]]
    assert documents and all(not item["acquire_allowed"] for item in documents)
    assert {item["task_hint"]["action"] for item in documents} == {"review_report", "read_evidence", "extract_evidence"}


def test_original_loss_invalidates_cached_parsed_report_without_evidence_entries(tmp_path):
    source = tmp_path / "source"
    _primary(source)
    _reports(source, evidence=False)
    first, outputs = _build(tmp_path, source)
    assert outputs["core-pack.json"]["catalog"]["required_reports"]["latest_annual"]["state"] == "parsed"
    assert any(name.startswith("original:") for item in outputs["manifest.json"]["auxiliary_inputs"]
               for name in item["files"])
    (source / "annual.pdf").unlink()
    with pytest.raises(FileNotFoundError, match="report_pack_input_missing"):
        build_report_request(Path(first["pack_dir"]))
    second, outputs = _build(tmp_path, source)
    assert second["pack_id"] != first["pack_id"] and not second["cache_reused"]
    assert outputs["core-pack.json"]["catalog"]["required_reports"]["latest_annual"]["state"] == "pending"
