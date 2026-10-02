import json
from datetime import date
from pathlib import Path

import pytest

from analysis.research.snapshot_materials import inherit_snapshot_materials
from analysis.research.workspace import ResearchError, read_json, sha
from analysis.structured.research_lite import build_lite_pack
from analysis.structured.scope import STANDARD_PROFILE_ID


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _seal(path, core, extras=None, metadata=None):
    manifest = read_json(path / "manifest.json")
    _write(path / "core-pack.json", core)
    for name, content in (extras or {}).items():
        output = path / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    manifest.update(metadata or {})
    manifest["output_hashes"] = {name: sha(path / name) for name in set(manifest["output_hashes"]) | set(extras or {})}
    _write(path / "manifest.json", manifest)


def _fact(metric="market_price", value="100", period="2026-09-14", kind="market_quote"):
    return {"fact_id": "new-" + metric, "ticker": "600519", "metric_id": metric, "value": value,
            "unit": "CNY_per_share" if metric == "market_price" else "CNY", "currency": "CNY",
            "period_end": period, "period_type": kind, "scope": "consolidated", "source_ids": ["source-current"],
            "available_at": "2026-09-14T00:00:00Z"}


def _inputs(tmp_path, *, new_blank_fact=False):
    primary = tmp_path / "primary"
    source = primary / "600519/coverage-facts.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"")
    common = dict(input_root=primary, ticker="600519", as_of=date(2026, 9, 14),
                  output_root=tmp_path / "base-packs", profile_id=STANDARD_PROFILE_ID, max_tokens=100000)
    parent_built = build_lite_pack(**common)
    parent = Path(parent_built["pack_dir"])
    original = tmp_path / "report-original.txt"
    original.write_text("Located statement and disclosure text", encoding="utf-8")
    evidence = {"evidence_id": "original-statement", "original_path": str(original), "original_sha256": sha(original),
                "locator": "pages:1", "content": original.read_text(encoding="utf-8"), "period": "2025-12-31",
                "published_at": "2026-04-01", "title": "Annual statement", "group": "C"}
    core = read_json(parent / "core-pack.json")
    core.update(supplemental_evidence=[evidence], processing_attachments=[{
        "path": str(original), "sha256": sha(original), "title": "Source attachment", "boundary": "Source text"}],
        statements=[{"evidence_id": evidence["evidence_id"], "path": str(original), "sha256": sha(original),
                     "period": "2025-12-31", "statement": "balance_sheet", "scope": "consolidated", "pages": [1]}],
        organized_disclosures=[{"evidence_id": evidence["evidence_id"], "type": "counterparty_concentration",
                               "period": "2025-12-31", "share": "0.1", "rule_version": "test-1"}],
        financial_cell_resolutions=[{"metric_id": "accounts_receivable", "period": "2025-12-31", "period_type": "instant",
            "state": "disclosed_blank", "evidence_id": evidence["evidence_id"], "page": 1,
            "source_cells": ["应收账款", "", ""], "reason": "Issuer left this cell blank"}],
        processing_audit={"status": "passed", "parent_marker": "parent-only"})
    audit_bytes = b'{"status":"passed","parent_marker":"parent-only"}\r\n'
    _seal(parent, core, {"audit/future-proof.bin": b"\xff\x00 original bytes\r\n", "processing-audit.json": audit_bytes},
          {"processing_identity": {"version": "previous-processing"},
           "statement_sources": [{"path": str(original), "sha256": sha(original), "period": "2025-12-31"}]})
    increment = tmp_path / "increment/600519/coverage-facts.jsonl"
    increment.parent.mkdir(parents=True)
    facts = [_fact()]
    if new_blank_fact:
        facts.append(_fact("accounts_receivable", "123", "2025-12-31", "instant"))
    increment.write_text("".join(json.dumps(row) + "\n" for row in facts), encoding="utf-8")
    built = build_lite_pack(**common, supplements=(increment.parent.parent,))
    return parent, built, original, audit_bytes


def test_inheritance_keeps_bytes_lineage_blank_observation_and_rechecks_new_metrics(tmp_path):
    parent, built, _, audit_bytes = _inputs(tmp_path)
    before = {path: sha(path) for root in (parent, Path(built["pack_dir"])) for path in root.rglob("*") if path.is_file()}
    result = inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")
    candidate = Path(result["pack_dir"])
    core, manifest = read_json(candidate / "core-pack.json"), read_json(candidate / "manifest.json")
    assert (candidate / "audit/future-proof.bin").read_bytes() == b"\xff\x00 original bytes\r\n"
    old_audit = core["inherited_materials"]["parent_audit"]
    assert old_audit["scope"] == "parent_snapshot_only"
    assert (candidate / old_audit["path"]).read_bytes() == audit_bytes
    assert core["processing_audit"]["status"] == "checked_with_explicit_gaps"
    assert core["processing_audit"]["base_snapshot_id"] == built["pack_id"]
    assert "parent_marker" not in core["processing_audit"]
    price = next(row for row in core["metrics"] if row["metric_id"] == "market_price")
    assert price["fact"]["value"] == "100"
    cell = next(row for row in core["metrics"] if row["metric_id"] == "accounts_receivable" and row["period"] == "2025-12-31")
    assert cell["state"] == "disclosed_blank" and cell["fact"] is None
    assert len(core["financial_cell_resolutions"]) == 1
    original_work = read_json(Path(built["pack_dir"]) / "next-work.json")["items"]
    assert any(row.get("requirement_id") == cell["requirement_id"] and row["acquire_allowed"] for row in original_work)
    new_work = read_json(candidate / "next-work.json")
    assert not any(row.get("requirement_id") == cell["requirement_id"] for row in new_work["items"])
    markdown = (candidate / "core-pack.md").read_text(encoding="utf-8")
    line = next(line for line in markdown.splitlines() if line.startswith("| 应收账款 |"))
    assert line.endswith("| 原表留空 |")
    assert f"`{cell['requirement_id']}`：disclosed_blank" in markdown
    assert manifest["material_inheritance_identity"]["parent_metadata"]["processing_identity"]["version"] == "previous-processing"
    assert manifest["statement_sources"] == read_json(parent / "manifest.json")["statement_sources"]
    assert before == {path: sha(path) for path in before}
    again = inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")
    assert again["pack_id"] == result["pack_id"] and again["cache_reused"]


def test_formal_new_fact_is_not_overwritten_by_parent_blank_resolution(tmp_path):
    parent, built, _, _ = _inputs(tmp_path, new_blank_fact=True)
    result = inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")
    core = read_json(Path(result["pack_dir"]) / "core-pack.json")
    cell = next(row for row in core["metrics"] if row["metric_id"] == "accounts_receivable" and row["period"] == "2025-12-31")
    assert cell["state"] == "ready" and cell["fact"]["value"] == "123"
    assert core["financial_cell_resolutions"] == []
    retained = core["inherited_materials"]["inactive_financial_cell_resolutions"]
    assert len(retained) == 1 and retained[0]["reason"] == "current_fact_or_state_takes_precedence"


@pytest.mark.parametrize("missing", [False, True])
def test_inheritance_rejects_changed_or_lost_original(tmp_path, missing):
    parent, built, original, _ = _inputs(tmp_path)
    if missing:
        original.unlink()
    else:
        original.write_bytes(b"changed")
    with pytest.raises(ResearchError, match="material_inheritance_source_(missing|changed)"):
        inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")
    assert not (tmp_path / "candidates").exists()


def test_inheritance_rejects_parent_output_tamper_and_bad_current_arithmetic(tmp_path):
    parent, built, _, _ = _inputs(tmp_path)
    proof = parent / "audit/future-proof.bin"
    original = proof.read_bytes()
    proof.write_bytes(b"tampered")
    with pytest.raises(ResearchError, match="candidate_parent_output_changed"):
        inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")
    proof.write_bytes(original)
    base = Path(built["pack_dir"])
    core = read_json(base / "core-pack.json")
    price = next(row for row in core["metrics"] if row["metric_id"] == "market_price")
    price["yoy"] = {"prior_value": "100", "value": "2"}
    _seal(base, core)
    with pytest.raises(ResearchError, match="material_inheritance_current_audit_failed:yoy_mismatch"):
        inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates")


def test_unmodified_core_without_extra_materials_returns_builder_result(tmp_path):
    parent = tmp_path / "parent"
    _write(parent / "core-pack.json", {"metrics": []})
    _write(parent / "manifest.json", {"output_hashes": {"core-pack.json": sha(parent / "core-pack.json")}})
    built = {"pack_dir": "not-created-by-the-test", "pack_id": "base-id", "status": "ready"}
    assert inherit_snapshot_materials(parent_pack=parent, built=built, output_root=tmp_path / "candidates") == built
    assert not (tmp_path / "candidates").exists()
