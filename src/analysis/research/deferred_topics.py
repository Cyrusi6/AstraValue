"""Candidate-only recovery for the nine deferred 600519 structured topics.

The normal research supplement path registers readable API material against the
active research snapshot.  That is useful for an ordinary on-demand reading
request, but it is too early for the deferred Moutai follow-up: the API runs,
their raw snapshots, and any filing fallback must be inspected before the
active catalog can change.  This module keeps the same structured planner,
executor, resume and materialization code while staging every new input under
the job directory and publishing only a snapshot candidate artifact.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .core_materials import bound_roots
from .snapshot_materials import inherit_snapshot_materials
from .workspace import ResearchError, digest, read_json, sha


VERSION = "deferred-topic-candidate-v1.0.0"
TICKER = "600519"

GROUPS: tuple[dict[str, Any], ...] = (
    {
        "group_id": "governance",
        "order": 1,
        "topics": ("guarantee", "litigation", "pledge"),
        "report_requirements": (
            "lite.evidence.D.governance",
            "lite.evidence.C.cash_debt_and_risk",
        ),
    },
    {
        "group_id": "capital_actions",
        "order": 2,
        "topics": ("seo", "allotment", "bond_issuance", "unlock_peer"),
        "report_requirements": ("lite.evidence.D.capital_actions",),
    },
    {
        "group_id": "operations_assets",
        "order": 3,
        "topics": ("customers_peer", "goodwill"),
        "report_requirements": (
            "lite.evidence.A.segments_and_channels",
            "lite.evidence.C.cash_debt_and_risk",
        ),
    },
)
GROUP_BY_ID = {item["group_id"]: item for item in GROUPS}
TOPIC_TO_GROUP = {
    topic: item["group_id"] for item in GROUPS for topic in item["topics"]
}
DEFERRED_TOPICS = tuple(topic for item in GROUPS for topic in item["topics"])


def group_definitions() -> list[dict[str, Any]]:
    """Return the frozen order without exposing mutable module state."""

    return [
        {
            "group_id": item["group_id"],
            "order": item["order"],
            "topics": list(item["topics"]),
            "report_requirements": list(item["report_requirements"]),
        }
        for item in GROUPS
    ]


def validate_groups(groups: Sequence[str] | None) -> tuple[str, ...]:
    """Validate a subset while retaining the project-defined order."""

    requested = tuple(item["group_id"] for item in GROUPS) if groups is None else tuple(groups)
    if not requested:
        raise ResearchError("deferred_topic_group_required")
    unknown = sorted(set(requested) - set(GROUP_BY_ID))
    if unknown:
        raise ResearchError("unknown_deferred_topic_group:" + ",".join(unknown))
    if len(set(requested)) != len(requested):
        raise ResearchError("duplicate_deferred_topic_group")
    positions = [next(item["order"] for item in GROUPS if item["group_id"] == value) for value in requested]
    if positions != sorted(positions):
        raise ResearchError("deferred_topic_group_order_mismatch")
    return requested


def request(jobs, research_id: str, question: str, impact: str,
            *, groups: Sequence[str] | None = None, execute: bool | None = None,
            refresh: bool = False) -> dict[str, Any]:
    """Create a durable candidate-only follow-up task for the 600519 scope.

    This is intentionally an internal ``MaterialJobs`` capability rather than
    a second public research API.  The caller still uses ``resume_task`` and
    receives the same job state model as other bounded material requests.
    """

    if not question.strip() or not impact.strip():
        raise ResearchError("research_question_and_material_impact_required")
    state, pack_path, payload = jobs.w.pack(research_id)
    if state.get("ticker") != TICKER or state.get("canonical_ticker") != TICKER + ".SH":
        raise ResearchError("deferred_topics_require_600519")
    selected_groups = validate_groups(groups)
    selected_topics = tuple(topic for group_id in selected_groups for topic in GROUP_BY_ID[group_id]["topics"])
    manifest_path = pack_path / "manifest.json"
    manifest = read_json(manifest_path)
    periods = payload.get("periods") or {}
    report_periods = sorted({
        str(value)[:10]
        for key in ("annual", "quarters")
        for value in periods.get(key, [])
        if len(str(value)[:10]) == 10
    })
    roots, evidence_roots = jobs.w.material_input_roots(date.fromisoformat(state["as_of"]))
    baseline = {
        "version": VERSION,
        "research_id": research_id,
        "ticker": state["ticker"],
        "canonical_ticker": state["canonical_ticker"],
        "snapshot_id": state["snapshot_id"],
        "as_of": state["as_of"],
        "scope": state.get("scope"),
        "profile_id": manifest.get("profile_id") or jobs.w.config.get("profile_id"),
        "profile_sha256": manifest.get("profile_sha256"),
        "manifest": {"path": str(manifest_path.resolve()), "sha256": sha(manifest_path)},
        "config_sha256": digest(jobs.w.config),
        "periods": periods,
        "report_periods": report_periods,
        "projection_roots": _root_descriptors(roots),
        "evidence_roots": _root_descriptors(evidence_roots),
    }
    task_identity = {
        "version": VERSION,
        "research_id": research_id,
        "snapshot_id": state["snapshot_id"],
        "groups": selected_groups,
        "refresh": bool(refresh),
    }
    task_id = "j_" + digest(task_identity)[:24]
    try:
        return jobs.get(task_id)
    except ResearchError:
        pass
    job = {
        "task_id": task_id,
        "research_id": research_id,
        "snapshot_id": state["snapshot_id"],
        "topic": "deferred_topics",
        "question": question,
        "impact": impact,
        "material_types": list(selected_topics),
        "groups": list(selected_groups),
        "group_definitions": [GROUP_BY_ID[item] for item in selected_groups],
        "group_index": 0,
        "group_results": {},
        "candidate_roots": [],
        "candidate_evidence_roots": [],
        "candidate_artifacts": [],
        "refresh": bool(refresh),
        "baseline": baseline,
        "state": "planned",
        "attempts": 0,
        "failures": 0,
        "result": None,
        "network_requests": 0,
        "cache_hits": 0,
    }
    jobs.save(job)
    return job if execute is False else jobs.resume_task(task_id)


def execute(jobs, job: Mapping[str, Any]) -> None:
    """Execute at most one structured round for the current group."""

    if job.get("state") == "completed":
        return
    w = jobs.w
    state, pack_path, payload = w.pack(job["research_id"])
    _assert_baseline(state, pack_path, job, w.config)
    group_ids = tuple(job.get("groups") or ())
    index = int(job.get("group_index", 0))
    if index >= len(group_ids):
        _finish(jobs, dict(job), state, pack_path)
        return

    task = dict(job)
    group_id = group_ids[index]
    group = dict(GROUP_BY_ID[group_id])
    results = dict(task.get("group_results") or {})
    current = dict(results.get(group_id) or {
        "group_id": group_id,
        "order": group["order"],
        "topics": list(group["topics"]),
        "state": "planned",
        "dataset_results": {},
        "fallback": {"status": "not_needed"},
        "candidate_artifacts": [],
    })
    group_root = w.state / "jobs" / task["task_id"] / "groups" / group_id
    group_root.mkdir(parents=True, exist_ok=True)
    try:
        run = _structured_round(jobs, task, current, group, state, payload, group_root)
        current.update(run)
        task["network_requests"] = int(task.get("network_requests", 0)) + int(run.get("network_requests", 0))
        task["cache_hits"] = int(task.get("cache_hits", 0)) + int(run.get("cache_hits", 0))
    except Exception as exc:
        current.update(
            state="failed",
            reason=f"{type(exc).__name__}:{str(exc)[:500]}",
            next_action="inspect_group_evidence_and_resume",
        )

    results[group_id] = current
    task["group_results"] = results
    # A group with unfinished pages remains the current group.  Complete,
    # no-data, failed, and retry-exhausted states can move independently to the
    # next group, so one bad theme cannot hide the other eight.
    if current.get("state") in {"checkpointed", "running", "pending", "retryable"}:
        task.update(state="checkpointed", reason="group_checkpoint_saved; resume_same_group")
        jobs.save(task)
        return

    _publish_group_candidate(jobs, task, current, state, pack_path, payload)
    results[group_id] = current
    task["group_results"] = results
    task["group_index"] = index + 1
    if task["group_index"] < len(group_ids):
        task.update(state="checkpointed", reason="group_complete; next_group_ready")
        jobs.save(task)
        return
    _finish(jobs, task, state, pack_path)


def _structured_round(jobs, task, current, group, state, payload, group_root):
    from analysis.acquisition.runtime import AcquisitionRuntime
    from analysis.structured.runtime import StructuredDataRuntime

    cutoff = date.fromisoformat(state["as_of"])
    profile_id = (task["baseline"].get("profile_id") or jobs.w.config["profile_id"])
    identity = jobs.w.resolve(state["canonical_ticker"], cutoff).identity
    if identity is None:
        raise ResearchError("deferred_topic_company_identity_unresolved")
    db = group_root / "analysis.db"
    data_root = group_root / "data"
    run_id = current.get("run_id")
    with AcquisitionRuntime.create(db, data_root) as acquisition:
        runtime = StructuredDataRuntime(acquisition)
        if not run_id:
            plan = runtime.plan(
                [identity], mode="incremental", company_scope="company-only",
                dataset_ids=list(group["topics"]),
                report_periods=list(task["baseline"].get("report_periods") or ()),
                research_profile_id=profile_id,
                as_of=datetime.combine(cutoff, time.max, ZoneInfo("Asia/Shanghai")),
            )
            run_id = plan.run_ids[0]
        result = runtime.execute(run_id, max_jobs_per_round=1)
        status = result["status"]
        coverage = runtime.storage.list_acquisition_coverage(run_id=run_id, limit=None)
        jobs_status = status.get("jobs", [])
        dataset_results = _dataset_results(group["topics"], jobs_status, coverage)
        snapshots = _snapshot_inventory(db, data_root, group["topics"])
        attempts = sum(int(item.get("attempt_count", 0)) for item in jobs_status)
    terminal = not any(status.get(key, 0) for key in ("pending", "retryable", "partial"))
    if not terminal:
        return {
            "run_id": run_id,
            "state": "checkpointed",
            "structured_status": status,
            "dataset_results": dataset_results,
            "coverage": coverage,
            "snapshots": snapshots,
            "network_requests": attempts,
            "next_action": "resume_same_group",
        }

    # A terminal scheduler status is not enough to publish a group.  The
    # per-dataset projection also requires a complete coverage record; a
    # missing completion proof remains checkpointed for inspection/resume.
    if any(value.get("state") == "pending" for value in dataset_results.values()):
        return {
            "run_id": run_id,
            "state": "checkpointed",
            "structured_status": status,
            "dataset_results": dataset_results,
            "coverage": coverage,
            "snapshots": snapshots,
            "network_requests": attempts,
            "next_action": "inspect_completion_proof_and_resume",
        }

    failed_topics = [value for value in dataset_results.values()
                     if value.get("state") in {"failed", "partial"}]
    group_state = "partial" if failed_topics else "completed"
    if status.get("failed") or status.get("blocked"):
        group_state = "partial" if status.get("succeeded") or status.get("no_data") else "failed"
    fallback = _fallback_documents(jobs, task, current, group, state, group_root, dataset_results)
    return {
        "run_id": run_id,
        "state": group_state,
        "structured_status": status,
        "dataset_results": dataset_results,
        "coverage": coverage,
        "snapshots": snapshots,
        "fallback": fallback,
        "network_requests": attempts,
        "next_action": "inspect_candidate" if group_state in {"completed", "partial"} else "resume_same_group",
    }


def _publish_group_candidate(jobs, task, current, state, pack_path, payload) -> None:
    """Build a candidate from all completed group projections and fallback roots."""

    from analysis.structured.research_lite import build_lite_pack, materialize_cache

    group_id = current["group_id"]
    group_root = jobs.w.state / "jobs" / task["task_id"] / "groups" / group_id
    materialized = group_root / "materialized"
    run_id = current.get("run_id")
    if run_id and current.get("structured_status", {}).get("summary", {}).get("runnable_finished", True):
        try:
            materialize_cache(
                group_root / "analysis.db", group_root / "data", materialized,
                date.fromisoformat(state["as_of"]), tickers=[state["ticker"]],
                research_profile_id=task["baseline"].get("profile_id"), finalized_only=True,
            )
        except Exception as exc:
            current.setdefault("candidate_gaps", []).append({
                "stage": "materialization", "reason": str(exc), "next_action": "inspect_group_run",
            })
    try:
        roots, evidence, peers = bound_roots(jobs.w, state, pack_path)
        candidate_roots = list(task.get("candidate_roots") or ())
        if materialized.joinpath(state["ticker"]).is_dir():
            candidate_roots.append(str(materialized.resolve()))
        candidate_roots = list(dict.fromkeys(candidate_roots))
        candidate_evidence = list(task.get("candidate_evidence_roots") or ())
        fallback_root = current.get("fallback", {}).get("output_root")
        if fallback_root:
            candidate_evidence.append(str(Path(fallback_root).resolve()))
        candidate_evidence = list(dict.fromkeys(candidate_evidence))
        all_roots = list(dict.fromkeys([*(str(item) for item in roots), *candidate_roots]))
        primary = next((Path(item) for item in all_roots if (Path(item) / state["ticker"]).is_dir()), None)
        if primary is None:
            raise ResearchError("registered_projection_missing")
        built = build_lite_pack(
            input_root=primary, ticker=state["ticker"], as_of=date.fromisoformat(state["as_of"]),
            output_root=jobs.w.state / "deferred-topic-packs",
            supplements=tuple(Path(item) for item in all_roots if Path(item) != primary),
            # Candidate projections contain only the target company.  Keep
            # them out of the peer channel; peer inputs remain peer-only.
            peer_roots=tuple(dict.fromkeys(peers)),
            evidence_roots=tuple(dict.fromkeys([*evidence, *candidate_evidence])),
            profile_id=task["baseline"].get("profile_id") or jobs.w.config["profile_id"],
        )
        built = inherit_snapshot_materials(
            parent_pack=pack_path, built=built, output_root=jobs.w.state / "material-packs"
        )
        audit = {
            "version": VERSION,
            "group_id": group_id,
            "topics": current["topics"],
            "baseline": task["baseline"],
            "dataset_results": current.get("dataset_results", {}),
            "structured_status": current.get("structured_status", {}),
            "coverage": current.get("coverage", []),
            "snapshots": current.get("snapshots", []),
            "fallback": current.get("fallback", {}),
            "candidate_roots": candidate_roots,
            "candidate_evidence_roots": candidate_evidence,
            "candidate_snapshot_id": built["pack_id"],
        }
        audit_path = group_root / "candidate-audit.json"
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        artifact = jobs.w.artifact(task["research_id"], "snapshot_candidate", {
            "candidate_pack_path": built["pack_dir"],
            "candidate_snapshot_id": built["pack_id"],
            "reason": task["impact"],
            "build_status": built["status"],
            "source_task": task["task_id"],
            "group_id": group_id,
            "topics": list(current["topics"]),
            "candidate_audit": {"path": str(audit_path.resolve()), "sha256": sha(audit_path)},
            "baseline_snapshot_id": task["baseline"]["snapshot_id"],
            "adoption_required": True,
        }, expected_snapshot_id=task["snapshot_id"])
        if artifact["artifact_id"] not in current.setdefault("candidate_artifacts", []):
            current["candidate_artifacts"].append(artifact["artifact_id"])
        current.update(candidate_state="ready", candidate_pack_path=built["pack_dir"],
                       candidate_snapshot_id=built["pack_id"], candidate_build_status=built.get("status"),
                       candidate_audit=audit)
        task["candidate_roots"] = candidate_roots
        task["candidate_evidence_roots"] = candidate_evidence
        if artifact["artifact_id"] not in task.setdefault("candidate_artifacts", []):
            task["candidate_artifacts"].append(artifact["artifact_id"])
    except Exception as exc:
        current.setdefault("candidate_gaps", []).append({
            "stage": "pack_build", "reason": str(exc), "next_action": "inspect_group_evidence",
        })
        current["candidate_state"] = "unavailable"


def _finish(jobs, task, state, pack_path) -> None:
    groups = task.get("group_results", {})
    candidate_ids = list(task.get("candidate_artifacts") or ())
    ready = [value for value in groups.values() if value.get("candidate_state") == "ready"]
    partial = any(
        value.get("state") not in {"completed"}
        or value.get("candidate_state") != "ready"
        or value.get("candidate_gaps")
        or value.get("candidate_build_status") not in {None, "ready"}
        for value in groups.values()
    )
    final = None
    if ready:
        latest = ready[-1]
        final = jobs.w.artifact(task["research_id"], "snapshot_candidate", {
            "candidate_pack_path": latest["candidate_pack_path"],
            "candidate_snapshot_id": latest["candidate_snapshot_id"],
            "reason": task["impact"],
            "build_status": "partial" if partial else "ready",
            "source_task": task["task_id"],
            "group_id": "all",
            "topics": [topic for group_id in task.get("groups", ())
                       for topic in GROUP_BY_ID[group_id]["topics"]],
            "group_candidate_artifacts": candidate_ids,
            "group_results": groups,
            "baseline": task["baseline"],
            "adoption_required": True,
            "is_final_candidate": True,
        }, expected_snapshot_id=task["snapshot_id"])
        if final["artifact_id"] not in candidate_ids:
            candidate_ids.append(final["artifact_id"])
    task["candidate_artifacts"] = candidate_ids
    task["result"] = final or {
        "status": "partial",
        "reason": "no_candidate_pack_available",
        "group_results": groups,
        "baseline": task["baseline"],
    }
    task.update(state="partial" if partial or final is None else "completed", reason=None if not partial and final else "candidate_or_group_gaps_preserved")
    jobs.save(task)


def _assert_baseline(state, pack_path, task, config: Mapping[str, Any] | None = None) -> None:
    baseline = task.get("baseline", {})
    if (state.get("snapshot_id") != task.get("snapshot_id") or
            state.get("snapshot_id") != baseline.get("snapshot_id")):
        raise ResearchError("material_task_snapshot_no_longer_active")
    for field in ("research_id", "ticker", "canonical_ticker", "as_of", "scope"):
        expected_value = baseline.get(field)
        if expected_value is not None and state.get(field) != expected_value:
            raise ResearchError("deferred_topic_baseline_changed:" + field)
    expected_config = baseline.get("config_sha256")
    if expected_config and config is not None and digest(config) != expected_config:
        raise ResearchError("deferred_topic_baseline_config_changed")
    expected = baseline.get("manifest", {}).get("sha256")
    manifest = pack_path / "manifest.json"
    if expected and sha(manifest) != expected:
        raise ResearchError("deferred_topic_baseline_manifest_changed")


def _dataset_results(topics: Sequence[str], jobs_status: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in jobs_status:
        grouped[str(item.get("dataset_id"))].append(item)
    result = {}
    for topic in topics:
        rows = grouped.get(topic, [])
        states = [str(item.get("state")) for item in rows]
        topic_coverage = [row for row in coverage if row.get("dataset_id") == topic]
        complete_coverage = [row for row in topic_coverage if _coverage_complete(row)]
        complete_empty = [row for row in complete_coverage if row.get("status") == "no_data"]
        complete_records = [row for row in complete_coverage if row.get("status") == "complete"]
        if not rows:
            state = "failed"
            reason = "structured_dataset_job_missing"
        elif any(value in {"pending", "retryable", "partial"} for value in states):
            state = "pending"
            reason = "query_not_complete"
        elif any(value in {"failed", "blocked"} for value in states):
            state = "failed" if not any(value in {"succeeded", "no_data"} for value in states) else "partial"
            reason = "structured_query_failed"
        elif all(value == "no_data" for value in states):
            if len(complete_empty) >= len(rows):
                state = "no_data"
                reason = "complete_empty_response"
            else:
                state = "pending"
                reason = "empty_response_completion_proof_missing"
        elif all(value == "succeeded" for value in states):
            if len(complete_records) >= len(rows):
                state = "records"
                reason = "structured_records_available"
            else:
                state = "pending"
                reason = "record_completion_proof_missing"
        else:
            state = "partial"
            reason = "structured_dataset_state_mixed"
        result[topic] = {
            "dataset_id": topic,
            "state": state,
            "reason": reason,
            "job_count": len(rows),
            "job_states": states,
            "coverage": topic_coverage,
            "query_complete": state in {"no_data", "records"},
        }
    return result


def _coverage_complete(row: Mapping[str, Any]) -> bool:
    """Require a terminal query and an explicit pagination end proof."""

    audit = row.get("pagination_audit")
    return (
        row.get("status") in {"complete", "no_data"}
        and row.get("query_complete") is True
        and bool(row.get("safe_through"))
        and isinstance(audit, Mapping)
        and audit.get("complete") is True
    )


def _fallback_documents(jobs, task, current, group, state, group_root, dataset_results):
    empty = [topic for topic, value in dataset_results.items() if value.get("state") == "no_data"]
    if not empty:
        return {"status": "not_needed", "reason": "api_not_complete_empty"}
    from .report_materials import refresh_report_materials

    roots, evidence = jobs.w.material_input_roots(date.fromisoformat(state["as_of"]))
    previous = list(task.get("candidate_evidence_roots") or ())
    output = group_root / "documents"
    requirements = [{"requirement_id": value} for value in group["report_requirements"]]
    try:
        result = refresh_report_materials(
            ticker=state["ticker"], cutoff=date.fromisoformat(state["as_of"]),
            output_root=output, db_path=group_root / "documents.db", data_root=group_root / "document-data",
            evidence_roots=tuple(dict.fromkeys([*roots, *evidence, *previous])),
            requirements=requirements, allow_network=True, refresh=bool(task.get("refresh")),
            max_seconds=max(5, min(60, int(jobs.w.config.get("max_tool_seconds", 120)) - 15)),
            profile_id=task["baseline"].get("profile_id") or jobs.w.config["profile_id"],
        )
        result["trigger_topics"] = empty
        result["scope"] = "report_period_and_registered_route_only"
        result["source_files"] = _root_descriptors([output])
        return result
    except Exception as exc:
        return {
            "status": "failed", "reason": str(exc), "trigger_topics": empty,
            "scope": "report_period_and_registered_route_only",
        }


def _snapshot_inventory(db: Path, data_root: Path, topics: Sequence[str]) -> list[dict[str, Any]]:
    if not db.is_file():
        return []
    rows = []
    with sqlite3.connect(db) as con:
        payloads = con.execute("SELECT payload FROM raw_resource_snapshots ORDER BY created_at,snapshot_id").fetchall()
    for (raw,) in payloads:
        item = json.loads(raw)
        relative = item.get("archive_relative_path")
        path = (data_root / relative).resolve() if relative else None
        entry = {
            "snapshot_id": item.get("snapshot_id"),
            "source_definition_id": item.get("source_definition_id"),
            "source_definition_version": item.get("source_definition_version"),
            "query_page_canonical": item.get("query_page_canonical"),
            "page_number": item.get("page_number"),
            "sha256": item.get("sha256"),
            "archive_relative_path": relative,
            "available_at": item.get("available_at"),
            "created_at": item.get("created_at"),
            "file_hash_verified": bool(path and path.is_file() and sha(path) == item.get("sha256")),
            "topic_scope": list(topics),
        }
        rows.append(entry)
    return rows


def _root_descriptors(roots: Sequence[Path]) -> list[dict[str, Any]]:
    result = []
    for root in dict.fromkeys(Path(item).resolve() for item in roots):
        files = {}
        if root.is_dir():
            for name in ("document-evidence.jsonl", "live-documents.json", "official-table-facts.jsonl"):
                path = root / name
                if path.is_file():
                    files[name] = {"path": str(path), "sha256": sha(path)}
        result.append({"root": str(root), "files": files})
    return result


__all__ = [
    "DEFERRED_TOPICS", "GROUPS", "group_definitions", "request", "execute", "validate_groups",
]
