"""Preserve verified research materials when acquisition rebuilds the core pack."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timezone, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from .pack_outputs import output_hashes, read_pack_outputs
from .workspace import ResearchError, digest, read_json, sha


VERSION = "snapshot-material-inheritance-v1.0.0"
CORE_OUTPUTS = {
    "core-pack.json", "core-pack.md", "core-coverage.json", "question-coverage.jsonl",
    "evidence-index.jsonl", "next-work.json",
}
MATERIAL_FIELDS = (
    "supplemental_evidence", "processing_attachments", "organized_disclosures",
    "statements", "financial_cell_resolutions",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _verify_file(path: Any, expected: Any, label: str) -> dict[str, str]:
    if not path or not expected:
        raise ResearchError("material_inheritance_source_descriptor_missing:" + label)
    source = Path(path).resolve()
    if not source.is_file():
        raise ResearchError("material_inheritance_source_missing:" + label + ":" + str(source))
    if sha(source) != expected:
        raise ResearchError("material_inheritance_source_changed:" + label + ":" + str(source))
    return {"path": str(source), "sha256": expected}


def _verify_inputs(manifest: Mapping[str, Any]) -> None:
    for group in ("source_inputs", "auxiliary_inputs", "peer_inputs"):
        for item in manifest.get(group, []):
            descriptors = list(item.get("files", {}).values()) + item.get("manifests", [])
            if item.get("path"):
                descriptors.append(item)
            for row in descriptors:
                _verify_file(row.get("path"), row.get("sha256"), group)


def _row_key(field: str, row: Mapping[str, Any]) -> tuple:
    if field in {"supplemental_evidence", "statements"}:
        return (row.get("evidence_id"),)
    if field == "processing_attachments":
        return (row.get("path"), row.get("sha256"))
    if field == "organized_disclosures":
        return tuple(row.get(key) for key in ("type", "evidence_id", "period", "counterparty", "rule_version"))
    return tuple(row.get(key) for key in ("metric_id", "period", "period_type", "evidence_id", "page"))


def _merge_rows(field: str, parent: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict]:
    rows = {}
    for item in [*parent.get(field, []), *current.get(field, [])]:
        key = _row_key(field, item)
        if not any(key):
            raise ResearchError("material_inheritance_identity_missing:" + field)
        if key in rows and rows[key] != item:
            raise ResearchError("material_inheritance_conflicting_material:" + field + ":" + str(key))
        rows[key] = item
    return list(rows.values())


def _verify_materials(core: Mapping[str, Any], cutoff_text: str) -> list[dict[str, str]]:
    originals = {}
    cutoff = datetime.combine(datetime.fromisoformat(cutoff_text).date(), time.max,
                              tzinfo=timezone(timedelta(hours=8)))
    evidence = {row["evidence_id"]: row for row in [*core.get("evidence", []), *core.get("supplemental_evidence", [])]}
    for field in ("supplemental_evidence", "processing_attachments", "statements"):
        for index, item in enumerate(core.get(field, [])):
            label = f"{field}[{index}]"
            source = item
            if field == "statements" and not item.get("path"):
                source = evidence.get(item.get("evidence_id"), {})
            descriptor = _verify_file(source.get("original_path") or source.get("path"),
                                      source.get("original_sha256") or source.get("sha256"), label)
            originals[(descriptor["path"], descriptor["sha256"])] = descriptor
            published = source.get("published_at")
            if published:
                try:
                    stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ResearchError("material_inheritance_publication_invalid:" + label) from exc
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=cutoff.tzinfo)
                if stamp > cutoff:
                    raise ResearchError("material_inheritance_after_cutoff:" + label)
            if field == "supplemental_evidence" and not item.get("locator"):
                raise ResearchError("material_inheritance_locator_missing:" + label)
    for field in ("organized_disclosures", "financial_cell_resolutions"):
        for item in core.get(field, []):
            if item.get("evidence_id") not in evidence:
                raise ResearchError("material_inheritance_evidence_missing:" + field)
    return list(originals.values())


def _apply_blank_resolutions(core: dict, coverage: dict, work: dict) -> list[dict]:
    """An original blank remains a disclosure observation; new numbers prevail."""
    by_key = defaultdict(list)
    for row in core["metrics"]:
        by_key[(row["metric_id"], row["period"], row["period_type"])].append(row)
    active, inactive = [], []
    resolved_requirements = set()
    for resolution in core.get("financial_cell_resolutions", []):
        key = tuple(resolution.get(name) for name in ("metric_id", "period", "period_type"))
        current = by_key.get(key, [])
        cells = resolution.get("source_cells") or []
        supported = (resolution.get("state") == "disclosed_blank" and len(cells) >= 2
                     and isinstance(cells[-2], str) and not cells[-2].strip())
        if not supported:
            raise ResearchError("material_inheritance_blank_resolution_unverified:" + str(key))
        if not current or any(row.get("state") not in {"pending", "disclosed_blank"} or row.get("fact") for row in current):
            inactive.append({"resolution": resolution,
                             "reason": "current_fact_or_state_takes_precedence" if current else "outside_current_metric_window"})
            continue
        for row in current:
            row.update(state="disclosed_blank", reason=resolution.get("reason"), resolution=resolution)
            resolved_requirements.add(row.get("requirement_id"))
        active.append(resolution)
        current_requirements = {row.get("requirement_id") for row in current}
        for rows in (core["coverage_requirements"], coverage["requirements"]):
            for row in rows:
                if row.get("requirement_id") in current_requirements:
                    row.update(state="disclosed_blank", reason=resolution.get("reason"))
    if "financial_cell_resolutions" in core:
        core["financial_cell_resolutions"] = active
        work["financial_cell_resolutions"] = active
        work["items"] = [row for row in work.get("items", []) if row.get("requirement_id") not in resolved_requirements]
    if any(item.get("type") == "counterparty_concentration" for item in core.get("organized_disclosures", [])):
        for rows in (core["coverage_requirements"], coverage["requirements"]):
            for row in rows:
                if row.get("requirement_id") == "lite.context.counterparty_disclosure" and row["state"] == "pending":
                    row.update(state="source_text_available", reason="reported_top_five_aggregates_available_names_not_inferred")
        for row in work.get("items", []):
            if row.get("requirement_id") == "lite.context.counterparty_disclosure":
                row.update(stage="semantic_processing", acquire_allowed=False,
                           reason="reported_top_five_aggregates_available_names_not_inferred")
    work["stage_counts"] = dict(Counter(row.get("stage") for row in work.get("items", [])))
    coverage["counts"] = dict(Counter(row["state"] for row in coverage["requirements"]))
    return inactive


def _material_markdown(markdown: str, core: Mapping[str, Any]) -> str:
    blanks = {(row["label"], row["period"]): row["resolution"] for row in core["metrics"]
              if row["state"] == "disclosed_blank"}
    requirements = {row.get("requirement_id"): row for row in core["coverage_requirements"]}
    periods, lines = [], []
    for line in markdown.splitlines():
        if line.startswith("| 指标 |"):
            periods = [part.strip() for part in line.split("|")[2:-1]]
        elif periods and line.startswith("| "):
            cells = [part.strip() for part in line.split("|")[1:-1]]
            if len(cells) == len(periods) + 1:
                for index, period in enumerate(periods, start=1):
                    resolution = blanks.get((cells[0], period))
                    if resolution and cells[index] == "待补":
                        cells[index] = "原表留空"
                line = "| " + " | ".join(cells) + " |"
        if line.startswith("- `") and "`：" in line:
            ident = line[3:].split("`：", 1)[0]
            row = requirements.get(ident)
            if row:
                line = f"- `{ident}`：{row['state']} / {row.get('reason') or '未说明'}"
        lines.append(line)
    return "\n".join(lines)


def inherit_snapshot_materials(*, parent_pack: Path, built: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
    """Return an immutable candidate retaining located materials from its parent."""
    parent_pack = Path(parent_pack).resolve()
    parent_manifest = read_json(parent_pack / "manifest.json")
    parent_outputs = read_pack_outputs(parent_pack, parent_manifest)
    parent = json.loads(parent_outputs["core-pack.json"])
    extras = set(parent_outputs) - CORE_OUTPUTS
    if not extras and not any(parent.get(key) for key in MATERIAL_FIELDS):
        return dict(built)

    base = Path(built["pack_dir"]).resolve()
    manifest = read_json(base / "manifest.json")
    base_outputs = read_pack_outputs(base, manifest)
    for key in ("ticker", "as_of"):
        if manifest[key] != parent_manifest[key]:
            raise ResearchError("material_inheritance_identity_mismatch:" + key)
    _verify_inputs(parent_manifest)
    _verify_inputs(manifest)
    core = json.loads(base_outputs["core-pack.json"])
    coverage = json.loads(base_outputs["core-coverage.json"])
    work = json.loads(base_outputs["next-work.json"])
    for field in MATERIAL_FIELDS:
        if field in parent or field in core:
            core[field] = _merge_rows(field, parent, core)
    originals = _verify_materials(core, manifest["as_of"])
    inactive = _apply_blank_resolutions(core, coverage, work)

    # Preserve the exact earlier audit bytes, with an explicit parent-only scope.
    outputs = parent_outputs | base_outputs
    parent_audit = {}
    if "processing-audit.json" in parent_outputs:
        name = "parent-audits/" + digest(parent_manifest["pack_id"])[:24] + "/processing-audit.json"
        outputs[name] = parent_outputs["processing-audit.json"]
        parent_audit = {"path": name, "snapshot_id": parent_manifest["pack_id"], "scope": "parent_snapshot_only"}
    from .processing import audit_metrics
    if len(core["periods"]["annual"]) == 5 and len(core["periods"]["quarters"]) == 12:
        audit = audit_metrics(core)
        if audit["errors"]:
            raise ResearchError("material_inheritance_current_audit_failed:" + ";".join(audit["errors"]))
    else:
        audit = {"status": "pending", "reason": "processing_audit_requires_standard_window", "errors": [],
                 "boundary": "parent audit applies only to its original snapshot; current metrics were not audited by the standard-window checker"}
    audit["base_snapshot_id"] = manifest["pack_id"]
    core["processing_audit"] = audit
    outputs["processing-audit.json"] = _json_bytes(audit)

    parent_metadata = {key: parent_manifest[key] for key in (
        "pack_version", "selection_version", "formula_version", "profile_id", "profile_sha256", "periods",
        "processing_identity", "statement_sources", "material_inheritance_identity",
    ) if key in parent_manifest}
    identity = {"version": VERSION, "parent_snapshot_id": parent_manifest["pack_id"],
                "parent_manifest": {"path": str(parent_pack / "manifest.json"), "sha256": sha(parent_pack / "manifest.json")},
                "base_snapshot_id": manifest["pack_id"],
                "base_manifest": {"path": str(base / "manifest.json"), "sha256": sha(base / "manifest.json")},
                "source_materials": originals, "parent_metadata": parent_metadata}
    pack_id = "lite-pack-" + digest(identity)[:24]
    core["pack_id"] = pack_id
    inactive = list({digest(item): item for item in [
        *parent.get("inherited_materials", {}).get("inactive_financial_cell_resolutions", []), *inactive,
    ]}.values())
    core["inherited_materials"] = {"version": VERSION, "parent_snapshot_id": parent_manifest["pack_id"],
        "counts": {key: len(core.get(key, [])) for key in MATERIAL_FIELDS},
        "inactive_financial_cell_resolutions": inactive, "parent_audit": parent_audit}
    markdown = _material_markdown(base_outputs["core-pack.md"].decode("utf-8"), core).replace(manifest["pack_id"], pack_id)
    markdown += "\n\n已继承并核验原件的补充资料：" + "、".join(
        f"{label}{len(core.get(key, []))}项" for key, label in (
            ("supplemental_evidence", "原文"), ("statements", "报表"),
            ("organized_disclosures", "披露整理"), ("processing_attachments", "附件"))) + "。按证据ID读取。\n"
    from analysis.structured.research_lite import _count_tokens
    count = _count_tokens(markdown)
    core["token_count"] = count
    if count["count"] > core["token_budget"]:
        core["status"] = "budget_exceeded"
    outputs.update({"core-pack.json": _json_bytes(core), "core-pack.md": markdown.encode("utf-8"),
                    "core-coverage.json": _json_bytes(coverage), "next-work.json": _json_bytes(work)})
    if parent_manifest.get("statement_sources"):
        sources = [*parent_manifest["statement_sources"], *manifest.get("statement_sources", [])]
        for source in sources:
            _verify_file(source.get("path"), source.get("sha256"), "statement_sources")
        manifest["statement_sources"] = list({digest(item): item for item in sources}.values())
    manifest.update(pack_id=pack_id, pack_identity_hash=digest(identity), parent_snapshot_id=parent_manifest["pack_id"],
                    material_inheritance_identity=identity, token_count=count, status=core["status"], output_hashes=output_hashes(outputs))
    manifest.setdefault("auxiliary_inputs", []).append({
        "role": "inherited_originals", "files": {"original-" + digest(row)[:24]: row for row in originals},
        "referenced_request_count": 0, "referenced_uncached_request_count": 0,
    })
    target = Path(output_root).resolve() / manifest["ticker"] / manifest["as_of"] / pack_id
    if target.is_relative_to(parent_pack) or target.is_relative_to(base):
        raise ResearchError("material_inheritance_output_inside_input")

    def verify_existing():
        existing = read_json(target / "manifest.json")
        if existing != manifest or read_pack_outputs(target, existing) != outputs:
            raise ResearchError("immutable_material_inheritance_conflict")

    reused = target.is_dir()
    if reused:
        verify_existing()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".material-candidate-", dir=target.parent) as temporary:
            stage = Path(temporary)
            for name, content in outputs.items():
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (stage / "manifest.json").write_bytes(_json_bytes(manifest))
            try:
                stage.rename(target)
            except FileExistsError:
                verify_existing()
    return {**built, "pack_dir": str(target), "pack_id": pack_id, "status": core["status"],
            "token_count": count, "cache_reused": reused, "materials_inherited": True}
