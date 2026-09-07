"""Resumable, low-rate archive of a verified retained CNINFO inventory.

No discovery HTTP is performed. Finalized batches are never silently retried.
An access halt stops this entire archive job, including all later batches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from analysis.acquisition.content import EXTRACTOR_VERSION, extract_announcement_text
from analysis.acquisition.materials import CLASSIFIER_VERSION, classify_material, classify_snapshot_material
from analysis.acquisition.models import AcquisitionPlan, canonical_json_bytes
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.repository import AcquisitionNotFoundError
from analysis.acquisition.retained_inventory import inventory_batch, plan_retained_inventory, validate_inventory
from analysis.acquisition.runtime import AcquisitionRuntime


ACCESS_HALT_REASONS = {"upstream_bot_challenge", "challenge_page_detected", "http_403"}


def write_json(path, value):
    # Atomic replacement preserves the last complete report on interruption.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def emit(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def prepare(runtime, bundle, output, batch_size):
    validate_inventory(bundle, ticker=bundle["ticker"])
    digest = hashlib.sha256(canonical_json_bytes(bundle)).hexdigest()
    path = output / "archive-state.json"
    identity = dict(inventory_sha256=digest, namespace_id=runtime.namespace_id,
                    registry_content_hash=runtime.loaded_registry.content_hash)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if any(state[k] != v for k, v in identity.items()):
            raise ValueError("归档任务输入、namespace或注册表已变化，禁止静默续跑")
        return state
    entries = sorted(bundle["entries"], key=lambda r: (
        classify_material(r["title"]).archive_priority, r.get("published_at") or "", r["canonical_resource_id"]))
    batches = []
    for priority in (0, 1):
        group = [r for r in entries if classify_material(r["title"]).archive_priority == priority]
        for start in range(0, len(group), batch_size):
            rows = group[start:start+batch_size]
            plan = plan_retained_inventory(runtime, inventory_batch(bundle, rows), persist=False)
            filename = f"batch-{len(batches)+1:03d}-plan.json"
            write_json(output/filename, plan.model_dump(mode="json"))
            batches.append(dict(plan_file=filename, run_id=plan.run.run_id, priority=priority,
                                canonical_ids=[r["canonical_resource_id"] for r in rows]))
    state = {**identity, "format": "cninfo-archive-job-v1", "created_at": datetime.now(timezone.utc).isoformat(),
             "batch_size": batch_size, "batches": batches, "entry_count": len(entries)}
    write_json(path, state)
    return state


def report(runtime, bundle, state, output, *, derive=False):
    repo = runtime.repository
    rows = {r["canonical_resource_id"]: dict(canonical_resource_id=r["canonical_resource_id"],
        title=r["title"], resource_url=r["resource_url"], fetch_status="not_requested", text_status="not_available",
        origin_discovered_resource_id=r["discovered_resource_id"], origin_proof_id=r["proof_id"])
        for r in bundle["entries"]}
    finalized, halted, attempts_count = 0, [], Counter()
    for batch in state["batches"]:
        try:
            run = repo.get_run(batch["run_id"])
        except AcquisitionNotFoundError:
            continue
        finalized += any(e.event_type.value == "finalized" for e in repo.list_run_events(run.run_id))
        for attempt in repo.list_attempts(run_id=run.run_id, limit=None):
            terminal = [e for e in repo.list_attempt_events(attempt.attempt_id)
                        if e.event_type.value == "outcome_terminal"]
            if not terminal:
                continue
            event = terminal[-1]
            attempts_count[event.outcome.value] += 1
            if event.reason_code in ACCESS_HALT_REASONS:
                halted.append(dict(run_id=run.run_id, attempt_id=attempt.attempt_id, reason=event.reason_code))
            if attempt.attempt_kind.value != "fetch":
                for observation in repo.list_discovery_observations(attempt.attempt_id):
                    for resource in repo.list_discovered_resources(observation.observation_id):
                        decision = resource.metadata.get("content_selection", {})
                        if not resource.required_fetch and decision.get("action") == "metadata_only":
                            rows[resource.canonical_resource_id].update(
                                run_id=run.run_id, discovery_attempt_id=attempt.attempt_id,
                                fetch_status="metadata_only", fetch_reason=decision["reason_code"],
                                content_selection=decision, text_status="not_requested")
                continue
            resource = repo.get_discovered_resource(attempt.discovered_resource_id)
            row = rows[resource.canonical_resource_id]
            row.update(run_id=run.run_id, attempt_id=attempt.attempt_id,
                       fetch_status=event.outcome.value, fetch_reason=event.reason_code)
            observations = repo.list_resource_observations(attempt_id=attempt.attempt_id, limit=None)
            for obs in observations:
                row.update(http_status=obs.http_status, observed_at=obs.observed_at.isoformat(),
                           resource_observation_id=obs.observation_id)
                if not obs.snapshot_id:
                    continue
                snapshot = repo.get_raw_resource_snapshot(obs.snapshot_id)
                runtime.snapshot_bytes(snapshot.snapshot_id)
                row.update(snapshot_id=snapshot.snapshot_id, sha256=snapshot.sha256,
                    byte_length=snapshot.byte_length, mime_type=snapshot.mime_type,
                    archive_relative_path=snapshot.archive_relative_path)
                cache = output / f"text-{snapshot.snapshot_id}.json"
                cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
                versions = {"extractor_version": EXTRACTOR_VERSION, "classifier_version": CLASSIFIER_VERSION}
                if derive and any(cached.get(k) != v for k, v in versions.items()):
                    if cached:
                        old_version = cached.get("classifier_version") or cached.get("material", {}).get("classifier_version", "unknown")
                        history = output / "derivation-history"
                        history.mkdir(exist_ok=True)
                        prior = history / f"{cache.stem}-{old_version}.json"
                        if not prior.exists():
                            write_json(prior, cached)
                    try:
                        extraction = extract_announcement_text(runtime, snapshot.snapshot_id)
                        material = classify_snapshot_material(runtime, snapshot.snapshot_id, title=resource.title)
                        write_json(cache, dict(**versions, text_status="parsed", extraction=asdict(extraction), material=asdict(material)))
                    except Exception as exc:
                        write_json(cache, dict(**versions, text_status="requires_review", text_error=str(exc)))
                if cache.exists():
                    row.update(json.loads(cache.read_text(encoding="utf-8")))
                elif not derive:
                    row["text_status"] = "pending"
    result = dict(namespace_id=runtime.namespace_id, inventory_sha256=state["inventory_sha256"],
        origin_namespace_id=bundle["origin_namespace_id"], origin_database_sha256=bundle["origin_database_sha256"],
        generated_at=datetime.now(timezone.utc).isoformat(), entry_count=len(rows),
        finalized_batches=finalized, total_batches=len(state["batches"]), access_halts=halted,
        fetch_counts=dict(Counter(r["fetch_status"] for r in rows.values())),
        text_counts=dict(Counter(r["text_status"] for r in rows.values())),
        material_counts=dict(Counter(r.get("material", {}).get("material_type", "unknown") for r in rows.values())),
        attempt_outcome_counts=dict(attempts_count), rows=list(rows.values()),
        production_checkpoint_advanced=False, scope="ad_hoc_retained_inventory")
    write_json(output/"archive-report.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--batch-size", type=int, default=75)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--derive-only", action="store_true", help="只刷新版本化文本/分类派生，不请求来源")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100:
        parser.error("batch-size必须在1到100之间")
    if args.inventory.stat().st_size > 64 * 1024 * 1024:
        parser.error("目录输入超出64MiB")
    bundle = json.loads(args.inventory.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with AcquisitionRuntime.create(args.db, args.data_root, registry_path=args.registry,
                                   workspace_root=Path(__file__).resolve().parents[1]) as runtime:
        state = prepare(runtime, bundle, args.output_dir, args.batch_size)
        current = report(runtime, bundle, state, args.output_dir, derive=args.derive_only)
        if args.prepare_only or args.report_only or args.derive_only:
            emit(event="archive_prepared" if args.prepare_only else "archive_report",
                 **{k:v for k,v in current.items() if k != "rows"})
            return 0
        for index, batch in enumerate(state["batches"], 1):
            if current["access_halts"]:
                emit(event="archive_access_halted", reasons=current["access_halts"], counts=current["fetch_counts"])
                return 2
            plan = AcquisitionPlan.model_validate_json((args.output_dir/batch["plan_file"]).read_text(encoding="utf-8"))
            emit(event="batch_started", batch=index, total=len(state["batches"]), run_id=plan.run.run_id,
                 priority=batch["priority"], entry_count=len(batch["canonical_ids"]))
            result = runtime.orchestrator.execute_plan(plan)
            write_json(args.output_dir/f"batch-{index:03d}-result.json", result.as_dict())
            current = report(runtime, bundle, state, args.output_dir, derive=True)
            emit(event="batch_finished", batch=index, fetch_counts=current["fetch_counts"],
                 text_counts=current["text_counts"], access_halts=current["access_halts"])
        current = report(runtime, bundle, state, args.output_dir, derive=True)
        emit(event="archive_finished", **{k:v for k,v in current.items() if k != "rows"})
        return 0 if not current["access_halts"] and set(current["fetch_counts"]) <= {"success", "unchanged", "metadata_only"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
