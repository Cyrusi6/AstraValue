"""Archive a retained CNINFO directory without inventing a new remote discovery.

The input carries original response bytes and row lineage across namespaces.
Only new local-input observations and new HTTP fetch observations are persisted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .adapters.official import CninfoAcquisitionAdapter
from .discovery import (DiscoveryPageContext, NormalizedDiscoveryPage, NormalizedResource,
                        _build_discovered_resource)
from .materials import classify_material
from .models import (AcquisitionPlan, AcquisitionRun, CoverageEntry, DiscoveryObservation,
                     DiscoveryProof, DiscoveredResource, PhysicalQueryCoverageLink,
                     PhysicalQueryPlanItem, RawResourceSnapshot, canonical_json_bytes,
                     stable_acquisition_id)


FORMAT = "cninfo-retained-inventory-v1"
MIME = "application/vnd.astravalue.retained-inventory+json"
MAX_INPUT_BYTES = 64 * 1024 * 1024


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"retained_inventory_invalid: {message}")


def _verified_bytes(root: Path, relative: str, sha256: str, length: int) -> bytes:
    path = (root / relative).resolve()
    _check(path.is_relative_to(root.resolve()), "原始证据路径越界")
    _check(path.stat().st_size == length, "原始证据长度不符")
    body = path.read_bytes()
    _check(hashlib.sha256(body).hexdigest() == sha256, "原始证据哈希不符")
    return body


def export_cninfo_inventory(origin_db: Path, origin_data_root: Path,
                            run_ids: list[str], *, ticker: str) -> dict[str, Any]:
    """Read an existing finalized audit DB strictly read-only; copy proven rows."""
    origin_db, origin_data_root = origin_db.resolve(), origin_data_root.resolve()
    digest_before = hashlib.sha256(origin_db.read_bytes()).hexdigest()
    _check(bool(run_ids) and len(set(run_ids)) == len(run_ids), "需明确且不重复的来源运行")
    proofs, resources, runs = {}, {}, {}
    with sqlite3.connect(origin_db.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        def payload(table, key, value):
            row = db.execute(f"SELECT payload FROM {table} WHERE {key}=?", (value,)).fetchone()
            _check(row is not None, f"缺少{table}记录")
            return json.loads(row[0])
        namespace = db.execute("SELECT namespace_id FROM storage_namespaces").fetchall()
        _check(len(namespace) == 1, "来源namespace不唯一")
        namespace_id = namespace[0][0]
        for run_id in run_ids:
            run = payload("acquisition_runs", "run_id", run_id)
            _check(run["ticker"] == ticker and run["storage_namespace_id"] == namespace_id,
                   "来源运行公司或namespace不匹配")
            events = db.execute("SELECT payload FROM acquisition_run_events WHERE run_id=?", (run_id,)).fetchall()
            _check(any(json.loads(e[0])["event_type"] == "finalized" for e in events), "来源运行未终结")
            runs[run_id] = run
            rows = db.execute("""SELECT d.payload FROM discovered_resources d
                JOIN discovery_observations o ON o.observation_id=d.discovery_observation_id
                JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id
                WHERE a.run_id=? ORDER BY d.discovered_resource_id""", (run_id,)).fetchall()
            for (value,) in rows:
                resource = json.loads(value)
                if resource["source_definition_id"] != "cninfo.disclosures":
                    continue
                # Bootstrap records are not announcement rows.
                if not re.fullmatch(r"page:\d+/announcements:\d+", resource.get("row_locator") or ""):
                    continue
                proof_id = resource["proof_id"]
                if proof_id not in proofs:
                    proof = payload("discovery_proofs", "proof_id", proof_id)
                    observation = payload("discovery_observations", "observation_id", proof["observation_id"])
                    snapshot = payload("raw_resource_snapshots", "snapshot_id", proof["discovery_snapshot_id"])
                    statuses = db.execute("SELECT payload FROM snapshot_integrity_events WHERE snapshot_id=?",
                                          (snapshot["snapshot_id"],)).fetchall()
                    _check(not any(json.loads(e[0])["status"] == "quarantined" for e in statuses),
                           "来源discovery证据已隔离")
                    body = _verified_bytes(origin_data_root, snapshot["archive_relative_path"],
                                           snapshot["sha256"], snapshot["byte_length"])
                    proofs[proof_id] = dict(proof=proof, observation=observation, snapshot=snapshot,
                        origin_run_id=run_id, body_base64=base64.b64encode(body).decode("ascii"))
                canonical = resource["canonical_resource_id"]
                if canonical in resources:
                    _check(resources[canonical]["resource_url"] == resource["resource_url"],
                           "同canonical具有不同URL，必须先审查版本")
                    continue
                resources[canonical] = resource
    _check(digest_before == hashlib.sha256(origin_db.read_bytes()).hexdigest(), "来源数据库在读取期间变化")
    selected_proofs = {r["proof_id"] for r in resources.values()}
    bundle = dict(format=FORMAT, ticker=ticker, origin_namespace_id=namespace_id,
        origin_database_sha256=digest_before, origin_runs=runs,
        created_at=datetime.now(timezone.utc).isoformat(),
        entries=sorted(resources.values(), key=lambda r: (
            classify_material(r["title"]).archive_priority, r.get("published_at") or "", r["canonical_resource_id"])),
        proofs={p: proofs[p] for p in sorted(selected_proofs)})
    validate_inventory(bundle, ticker=ticker)
    return bundle


def validate_inventory(bundle: dict[str, Any], *, ticker: str) -> tuple[NormalizedResource, ...]:
    """Validate origin models, namespaces, hashes and exact raw announcement rows."""
    _check(bundle["format"] == FORMAT and bundle["ticker"] == ticker, "目录格式或公司不匹配")
    _check(bool(bundle["entries"]), "本地空目录不能证明来源no_data")
    _check(len(bundle["entries"]) <= 100_000, "目录超出有界大小")
    adapter = CninfoAcquisitionAdapter(None, snapshot_reader=lambda _: b"")
    parsed_proofs = {}
    for key, support in bundle["proofs"].items():
        proof = DiscoveryProof.model_validate(support["proof"])
        obs = DiscoveryObservation.model_validate(support["observation"])
        snap = RawResourceSnapshot.model_validate(support["snapshot"])
        run = AcquisitionRun.model_validate(bundle["origin_runs"][support["origin_run_id"]])
        body = base64.b64decode(support["body_base64"], validate=True)
        _check(proof.proof_id == key and proof.proof_kind == "http_response"
               and proof.http_status == 200 and proof.schema_valid and proof.body_retained,
               "来源proof不是有效保留HTTP响应")
        _check(proof.observation_id == obs.observation_id and proof.attempt_id == obs.attempt_id
               and proof.physical_query_plan_item_id == obs.physical_query_plan_item_id,
               "来源proof-observation谱系不符")
        _check(run.ticker == ticker and run.storage_namespace_id == bundle["origin_namespace_id"]
               and snap.storage_namespace_id == bundle["origin_namespace_id"], "来源范围不符")
        _check(snap.source_definition_id == obs.source_definition_id == "cninfo.disclosures"
               and snap.source_definition_version == obs.source_definition_version,
               "来源定义身份不符")
        _check(any(r.source_definition_id == obs.source_definition_id and r.version == obs.source_definition_version
                   for r in run.source_definition_refs), "来源run未冻结该定义")
        _check(proof.discovery_snapshot_id == obs.snapshot_id == snap.snapshot_id
               and snap.creating_observation_id == obs.observation_id
               and snap.physical_query_plan_item_id == proof.physical_query_plan_item_id,
               "来源snapshot谱系不符")
        _check(snap.resource_role.value == "discovery_response"
               and hashlib.sha256(body).hexdigest() == snap.sha256 == proof.response_sha256 == obs.response_sha256
               and len(body) == snap.byte_length == proof.response_byte_length == obs.response_byte_length,
               "来源响应哈希或长度不符")
        rows = json.loads(body.decode("utf-8"))["announcements"]
        _check(isinstance(rows, list) and len(rows) == proof.normalized_row_count, "来源行数不符")
        parsed_proofs[key] = (proof, obs, rows)
    normalized, seen = [], set()
    for data in bundle["entries"]:
        resource = DiscoveredResource.model_validate(data)
        _check(resource.canonical_resource_id not in seen, "目录含重复canonical")
        seen.add(resource.canonical_resource_id)
        proof, obs, raw_rows = parsed_proofs[resource.proof_id]
        match = re.fullmatch(r"page:(\d+)/announcements:(\d+)", resource.row_locator or "")
        _check(match is not None, "来源行定位格式不支持")
        page, index = map(int, match.groups())
        _check(page == proof.page_number == resource.page_number and index < len(raw_rows), "来源行定位越界")
        _check(resource.discovery_observation_id == obs.observation_id
               and resource.discovery_attempt_id == obs.attempt_id
               and resource.source_definition_version == obs.source_definition_version
               and resource.source_definition_id == obs.source_definition_id, "资源来源谱系不符")
        raw_row = raw_rows[index]
        _check(str(raw_row.get("secCode")) == ticker, "原始行证券代码不符")
        current = adapter._normalize_row(raw_row, index, SimpleNamespace(
            page=page, parser_schema_version="3", query_family="business_announcement",
            context={"schema_id": "cninfo.announcements", "fetch_policy": "required_attachment"}))
        for field, expected in {
            "canonical_resource_id": current.canonical_resource_id, "resource_url": current.url,
            "title": current.title, "published_at": current.published_at,
            "published_at_raw": current.published_raw, "upstream_material_id": current.upstream_material_id,
        }.items():
            _check(getattr(resource, field) == expected, f"来源原始行{field}不符")
        original = NormalizedResource(canonical_resource_id=resource.canonical_resource_id,
            resource_url=resource.resource_url, title=resource.title, source_timezone=resource.source_timezone,
            required_fetch=resource.required_fetch, upstream_material_id=resource.upstream_material_id,
            published_at_raw=resource.published_at_raw, published_at=resource.published_at,
            published_at_precision=resource.published_at_precision, row_locator=resource.row_locator,
            expected_mime_types=resource.expected_mime_types, metadata={**resource.metadata,
                "expected_mime_types": tuple(resource.metadata["expected_mime_types"])})
        context = DiscoveryPageContext(attempt_id=obs.attempt_id,
            physical_query_plan_item_id=obs.physical_query_plan_item_id,
            source_definition_id=obs.source_definition_id, source_definition_version=obs.source_definition_version,
            observed_at=obs.observed_at, retrieved_at=obs.retrieved_at, http_status=obs.http_status,
            mime_type=obs.mime_type, page_number=page)
        # Historical row hashes used repr(tuple); JSON persistence represents it as a list.
        _check(_build_discovered_resource(context, proof, original, index).model_dump(mode="json")
               == resource.model_dump(mode="json"),
               "来源规范化行哈希不符")
        normalized.append(NormalizedResource(
            canonical_resource_id=current.canonical_resource_id, resource_url=current.url,
            title=current.title, source_timezone=resource.source_timezone, required_fetch=True,
            upstream_material_id=current.upstream_material_id, published_at_raw=current.published_raw,
            published_at=current.published_at, published_at_precision=resource.published_at_precision,
            row_locator=f"retained-entry:{len(normalized)}",
            expected_mime_types=tuple(current.metadata["expected_mime_types"]),
            metadata={"origin_namespace_id": bundle["origin_namespace_id"],
                "origin_run_id": bundle["proofs"][resource.proof_id]["origin_run_id"],
                "origin_proof_id": resource.proof_id, "origin_snapshot_id": proof.discovery_snapshot_id,
                "origin_discovered_resource_id": resource.discovered_resource_id,
                "origin_row_locator": resource.row_locator, "origin_row_hash": resource.row_hash,
                "origin_observed_at": obs.observed_at.isoformat(), "mime_contract": "cninfo.announcements:3"}))
    return tuple(normalized)


def inventory_batch(bundle: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {**bundle, "entries": entries,
            "proofs": {key: bundle["proofs"][key] for key in sorted({r["proof_id"] for r in entries})}}


def plan_retained_inventory(runtime, bundle: dict[str, Any], *, persist=True) -> AcquisitionPlan:
    resources = validate_inventory(bundle, ticker=bundle["ticker"])
    definition = runtime.loaded_registry.definition("cninfo.disclosures")
    query = next(q for q in definition.queries if q.query_id == "cninfo.business_announcement")
    _check(query.discovery_schema.schema_version == "3" and definition.collection_role == "primary",
           "当前来源必须是支持历史HTML的正式主采合同")
    profile = runtime.build_profile(bundle["ticker"])
    start = min(r.published_at for r in resources)
    end = max(r.published_at for r in resources) + timedelta(milliseconds=1)
    now = runtime.clock()
    _check(end <= now, "目录不得包含未来公告")
    base = runtime.create_plan(profile, mode="incremental", run_kind="ad_hoc",
                               as_of=now, start_at=start, persist=False)
    body = canonical_json_bytes(bundle)
    _check(len(body) <= MAX_INPUT_BYTES, "单批目录输入超出限制")
    blob = runtime.blob_store.archive_bytes(body)
    ref = dict(sha256=blob.sha256, byte_length=blob.byte_length, archive_relative_path=blob.relative_path)
    key = stable_acquisition_id("retained-inventory", {"run_id": base.run.run_id, "sha256": blob.sha256})
    item = PhysicalQueryPlanItem(plan_item_id=key, run_id=base.run.run_id,
        source_definition_id=definition.source_definition_id, source_definition_version=definition.version,
        query_id=query.query_id, query_family="retained_inventory", execution_key=key,
        request_method="GET", endpoint=query.endpoint, fixed_headers=query.fixed_headers,
        normalized_parameters={}, retained_inventory_ref=ref,
        partition_key=key, pagination_fingerprint=FORMAT, ordinal=0, time_start=start, time_end=end)
    topic = next(t for t in runtime.loaded_questions.question_set.topics if t.question_id in query.question_ids)
    coverage = CoverageEntry(coverage_entry_id=stable_acquisition_id("coverage", key), run_id=base.run.run_id,
        source_definition_id=definition.source_definition_id, source_definition_version=definition.version,
        question_id=topic.question_id, query_id=query.query_id, plan_disposition="required",
        time_start=start, time_end=end)
    run = base.run.model_copy(update={"source_definition_refs": tuple(
        r for r in base.run.source_definition_refs if r.source_definition_id == definition.source_definition_id),
        "company_anchor_quality": "ad_hoc_retained_inventory_only"})
    plan = AcquisitionPlan(run=run, coverage_entries=(coverage,), physical_query_plan_items=(item,),
        coverage_links=(PhysicalQueryCoverageLink(plan_item_id=key, coverage_entry_id=coverage.coverage_entry_id),))
    if persist:
        runtime.orchestrator.persist_plan(plan)
    return plan


class _InventoryParser:
    def __init__(self, runtime, ticker):
        self.runtime, self.ticker = runtime, ticker

    def parse_retained_discovery(self, snapshot_id):
        bundle = json.loads(self.runtime.snapshot_bytes(snapshot_id))
        resources = validate_inventory(bundle, ticker=self.ticker)
        return NormalizedDiscoveryPage(resources=resources, parser_id=FORMAT, parser_version="1.0.0",
            schema_id=FORMAT, schema_version="1", schema_valid=True, declared_total=len(resources),
            declared_page_count=1, terminal=True)


def execute_retained_inventory(engine, run, definition, query, execution, *, lease_epoch, owner_token, heartbeat):
    from .status_classifier import AttemptClassification
    _check(run.run_kind.value == "ad_hoc" and run.request_scope == "ad_hoc", "本地目录禁止production")
    item = execution.plan_item
    if not execution.discovery_complete:
        attempt = engine._start_attempt(run, item, attempt_kind=item.attempt_kind,
            lease_epoch=lease_epoch, owner_token=owner_token, page=1,
            work_position="retained-inventory", retry_group_id=item.execution_key, retry_ordinal=0)
        execution.discovery_attempt_ids.append(attempt.attempt_id)
        reason = engine._runtime_policy_reason(definition)
        if reason is not None:
            engine._terminal(attempt, AttemptClassification("policy_skipped", reason),
                             owner_token=owner_token, protocol_summary={"io_performed": False})
            execution.reasons.append(reason)
            return execution
        try:
            ref = item.retained_inventory_ref
            _check(ref["byte_length"] <= MAX_INPUT_BYTES, "单批目录输入超出限制")
            body = engine.runtime.blob_store.read_verified(ref["archive_relative_path"],
                expected_sha256=ref["sha256"], expected_length=ref["byte_length"])
            validate_inventory(json.loads(body), ticker=run.ticker)
            now = engine._now()
            page = engine.discovery_pipeline.process_retained(body=body,
                context=DiscoveryPageContext(attempt_id=attempt.attempt_id,
                    physical_query_plan_item_id=item.plan_item_id, source_definition_id=definition.source_definition_id,
                    source_definition_version=definition.version, observed_at=now, retrieved_at=now,
                    http_status=None, mime_type=MIME, page_number=1, proof_kind="retained_inventory",
                    request_summary={"input_kind": "retained_inventory", "io_performed": False},
                    response_summary={"count_basis": "local_selection", "inventory_sha256": ref["sha256"]}),
                query_page_canonical=item.execution_key, parser=_InventoryParser(engine.runtime, run.ticker),
                owner_token=owner_token, lease_epoch=lease_epoch)
            engine._terminal(attempt, AttemptClassification("success", "retained_inventory_loaded"),
                owner_token=owner_token, proof_ids=(page.proof.proof_id,),
                snapshot_ids=(page.discovery_snapshot_id,), protocol_summary={"io_performed": False})
            execution.discovery_complete = True
            from .models import AcquisitionOutcome
            execution.discovery_outcome = AcquisitionOutcome.SUCCESS
            execution.proof_ids.append(page.proof.proof_id)
            execution.discovery_snapshot_ids.append(page.discovery_snapshot_id)
            execution.resources.update((r.canonical_resource_id, r) for r in page.resources)
        except Exception as exc:
            from .repository import StaleLeaseError
            if isinstance(exc, StaleLeaseError):
                raise
            engine._terminal(attempt, AttemptClassification("parse_failed", "retained_inventory_invalid"),
                owner_token=owner_token, protocol_summary={"io_performed": False, "error": str(exc)})
            execution.reasons.append("retained_inventory_invalid")
            return execution
    halted = engine._source_halts.get(engine._halt_key(run.run_id, item))
    adapter = None if halted else engine._adapter_for(definition)
    engine._execute_required_fetches(run, definition, query, adapter, execution,
        lease_epoch=lease_epoch, owner_token=owner_token, heartbeat=heartbeat)
    return execution
