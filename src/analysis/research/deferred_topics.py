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
import shutil
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
    # Wording and impact are metadata.  The immutable operation is identified
    # by the frozen research scope, group set and refresh intent so a changed
    # prompt cannot reset the same task's retry budget or create duplicate
    # evidence for one candidate run.
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
        "recovery_groups": [],
        "recovery_index": 0,
        "candidate_roots": [],
        "candidate_evidence_roots": [],
        "candidate_artifacts": [],
        "latest_candidate": None,
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
    phase, group_id = _next_group(dict(job))
    if group_id is None:
        _finish(jobs, dict(job), state, pack_path)
        return

    task = dict(job)
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
        run = _structured_round(
            jobs, task, current, group, state, payload, group_root,
            reconcile=phase == "recovery",
        )
        current.update(run)
        task["network_requests"] = sum(
            int(value.get("network_requests", 0))
            for key, value in results.items() if key != group_id
        ) + int(run.get("network_requests", 0))
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
    if phase == "forward":
        task["group_index"] = index + 1
        if _group_needs_recovery(current):
            _queue_recovery_group(task, group_id)
        if task["group_index"] < len(group_ids):
            task.update(state="checkpointed", reason="group_complete; next_group_ready")
            jobs.save(task)
            return
        _finish(jobs, task, state, pack_path)
        return
    else:
        _advance_recovery(task, group_id)
        if _recovery_group_still_open(current):
            task["failures"] = int(task.get("failures", 0)) + 1

    if _next_group(task)[1] is not None:
        task.update(state="checkpointed", reason="group_complete; next_group_ready")
        jobs.save(task)
        return
    _finish(jobs, task, state, pack_path)


def _structured_round(jobs, task, current, group, state, payload, group_root, *, reconcile: bool = False):
    from analysis.acquisition.runtime import AcquisitionRuntime
    from analysis.structured.runtime import StructuredDataRuntime

    cutoff = date.fromisoformat(state["as_of"])
    pack_path = Path(task["baseline"]["manifest"]["path"]).resolve().parent
    identity = jobs.w.resolve(state["canonical_ticker"], cutoff).identity
    if identity is None:
        raise ResearchError("deferred_topic_company_identity_unresolved")
    db = group_root / "analysis.db"
    data_root = group_root / "data"
    run_id = current.get("run_id")
    reconcile_topics = [
        topic for topic, result in (current.get("dataset_results") or {}).items()
        if result.get("state") in {"failed", "partial"}
    ]
    if reconcile and current.get("state") == "checkpointed":
        # The current run still has resumable pages.  Recovery must continue
        # that checkpoint; reconcile is only for a terminal failed/partial run.
        reconcile = False
    if reconcile and run_id and not reconcile_topics:
        # A candidate build or document fallback can fail after the structured
        # run succeeded.  Reuse its immutable run instead of creating a
        # needless reconcile run.
        reconcile = False
    with AcquisitionRuntime.create(db, data_root) as acquisition:
        runtime = StructuredDataRuntime(acquisition)
        if reconcile and run_id:
            parent_run_id = run_id
            plan = runtime.plan(
                [identity], mode="reconcile", company_scope="company-only",
                dataset_ids=reconcile_topics or list(group["topics"]),
                report_periods=list(task["baseline"].get("report_periods") or ()),
                # The nine deferred datasets are explicit scope routes.  Some
                # are intentionally absent from the lite profile's selection
                # list, so the acquisition plan must use the base scope while
                # materialization still keeps the frozen report profile.
                research_profile_id=None,
                parent_run_id=parent_run_id,
                as_of=datetime.combine(cutoff, time.max, ZoneInfo("Asia/Shanghai")),
            )
            run_id = plan.run_ids[0]
            current["inherited_jobs"] = list((current.get("structured_status") or {}).get("jobs", []))
            current["inherited_coverage"] = list(current.get("coverage") or [])
            current["reconcile_parent_run_id"] = parent_run_id
            current["reconcile_topics"] = reconcile_topics or list(group["topics"])
            current["run_id"] = run_id
            _persist_group_run_id(jobs, task, current)
        elif not run_id:
            plan = runtime.plan(
                [identity], mode="incremental", company_scope="company-only",
                dataset_ids=list(group["topics"]),
                report_periods=list(task["baseline"].get("report_periods") or ()),
                research_profile_id=None,
                as_of=datetime.combine(cutoff, time.max, ZoneInfo("Asia/Shanghai")),
            )
            run_id = plan.run_ids[0]
            current["run_id"] = run_id
            _persist_group_run_id(jobs, task, current)
        result = runtime.execute(run_id, max_jobs_per_round=1)
        status = result["status"]
        coverage = runtime.storage.list_acquisition_coverage(run_id=run_id, limit=None)
        jobs_status = status.get("jobs", [])
        replaced_jobs = {item.get("reconciles_job_id")
                         for item in runtime.storage.list_jobs(run_id, limit=None)}
        jobs_status = [item for item in current.get("inherited_jobs", [])
                       if item.get("job_id") not in replaced_jobs] + list(jobs_status)
        coverage = [item for item in current.get("inherited_coverage", [])
                    if item.get("job_id") not in replaced_jobs] + coverage
        status = {**status, "jobs": jobs_status}
        dataset_results = _dataset_results(group["topics"], jobs_status, coverage)
        snapshots = _snapshot_inventory(db, data_root, group["topics"])
        # Each persisted acquisition attempt is one page request. Counting the
        # group database once avoids adding cumulative run totals on resumes.
        with sqlite3.connect(db) as con:
            attempts = con.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0]
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
    fallback = _fallback_documents(
        jobs, task, current, group, state, pack_path, group_root, dataset_results,
    )
    # A previous candidate build or fallback attempt may have left transient
    # recovery fields on the group.  The terminal structured round below is a
    # fresh basis for publishing the next candidate, so remove those stale
    # fields before recording the new fallback result.
    _clear_retry_state(current)
    if _fallback_has_gaps(fallback):
        group_state = "partial"
        current.setdefault("candidate_gaps", []).append({
            "stage": "document_fallback",
            "reason": _fallback_gap_reason(fallback),
            "next_action": "resume_report_materials",
        })
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


def _clear_retry_state(current: dict[str, Any]) -> None:
    """Remove transient candidate errors before a successful retry publishes."""

    for key in (
        "candidate_gaps",
        "candidate_state",
        "candidate_build_status",
        "candidate_audit",
        "candidate_pack_path",
        "candidate_snapshot_id",
        "reason",
    ):
        current.pop(key, None)


def _fallback_has_gaps(fallback: Mapping[str, Any]) -> bool:
    """Return whether filing fallback is incomplete for the requested slots."""

    if fallback.get("status") in {None, "not_needed"}:
        return False
    return bool(
        fallback.get("status") != "completed"
        or fallback.get("query_complete") is not True
        or fallback.get("materials_ready") is not True
        or fallback.get("failures")
        or fallback.get("missing_documents")
        or fallback.get("missing_slots")
        or fallback.get("remaining")
    )


def _fallback_gap_reason(fallback: Mapping[str, Any]) -> str:
    """Keep a compact, inspectable reason in the candidate audit."""

    failures = fallback.get("failures") or []
    if failures and isinstance(failures[0], Mapping):
        return str(failures[0].get("reason") or failures[0].get("stage") or "fallback_incomplete")
    return str(fallback.get("reason") or fallback.get("status") or "fallback_incomplete")


def _publish_group_candidate(jobs, task, current, state, pack_path, payload) -> None:
    """Build a candidate from all completed group projections and fallback roots."""

    from analysis.structured.research_lite import materialize_cache

    group_id = current["group_id"]
    group_root = jobs.w.state / "jobs" / task["task_id"] / "groups" / group_id
    current["publication_round"] = int(current.get("publication_round", 0)) + 1
    outputs = group_root / "rounds" / str(current["publication_round"])
    outputs.mkdir(parents=True, exist_ok=True)
    _persist_group_run_id(jobs, task, current)
    materialized = outputs / "materialized"
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
        candidate_roots = [item for item in task.get("candidate_roots", [])
                           if not Path(item).resolve().is_relative_to(group_root.resolve())]
        if materialized.joinpath(state["ticker"]).is_dir():
            candidate_roots.append(str(materialized.resolve()))
        candidate_roots = list(dict.fromkeys(candidate_roots))
        candidate_evidence = [item for item in task.get("candidate_evidence_roots", [])
                              if not Path(item).resolve().is_relative_to(group_root.resolve())]
        fallback_root = current.get("fallback", {}).get("output_root")
        if fallback_root:
            frozen = outputs / "documents"
            for name in ("document-evidence.jsonl", "live-documents.json"):
                source = Path(fallback_root) / name
                if source.is_file():
                    frozen.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, frozen / name)
            if frozen.is_dir():
                candidate_evidence.append(str(frozen.resolve()))
        candidate_evidence = list(dict.fromkeys(candidate_evidence))
        built = _build_candidate_pack(
            jobs, task, state, pack_path,
            candidate_roots=candidate_roots,
            candidate_evidence=candidate_evidence,
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
            "group_state": current.get("state"),
            "candidate_gaps": current.get("candidate_gaps", []),
            "candidate_roots": candidate_roots,
            "candidate_evidence_roots": candidate_evidence,
            "candidate_snapshot_id": built["pack_id"],
        }
        audit_path = outputs / "candidate-audit.json"
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
        task["latest_candidate"] = {
            "candidate_pack_path": built["pack_dir"],
            "candidate_snapshot_id": built["pack_id"],
            "candidate_build_status": built.get("status"),
            "group_id": group_id,
        }
        task["candidate_roots"] = candidate_roots
        task["candidate_evidence_roots"] = candidate_evidence
        if artifact["artifact_id"] not in task.setdefault("candidate_artifacts", []):
            task["candidate_artifacts"].append(artifact["artifact_id"])
    except Exception as exc:
        current.setdefault("candidate_gaps", []).append({
            "stage": "pack_build", "reason": str(exc), "next_action": "inspect_group_evidence",
        })
        current["candidate_state"] = "unavailable"


def _build_candidate_pack(jobs, task, state, pack_path, *, candidate_roots, candidate_evidence):
    """Build one candidate from every currently accumulated group input."""

    from analysis.structured.research_lite import build_lite_pack

    roots, evidence, peers = bound_roots(jobs.w, state, pack_path)
    projection_roots = [Path(item).resolve() for item in candidate_roots
                        if Path(item).resolve().is_dir()]
    all_roots = list(dict.fromkeys([*(str(item) for item in roots),
                                    *(str(item) for item in projection_roots)]))
    primary = next((Path(item) for item in all_roots
                    if (Path(item) / state["ticker"]).is_dir()), None)
    if primary is None:
        raise ResearchError("registered_projection_missing")
    evidence_roots = [Path(item).resolve() for item in [*evidence, *candidate_evidence]
                      if Path(item).resolve().is_dir()]
    built = build_lite_pack(
        input_root=primary, ticker=state["ticker"], as_of=date.fromisoformat(state["as_of"]),
        output_root=jobs.w.state / "deferred-topic-packs",
        supplements=tuple(Path(item) for item in all_roots if Path(item) != primary),
        # Candidate projections contain only the target company.  Keep them
        # out of the peer channel; peer inputs remain peer-only.
        peer_roots=tuple(dict.fromkeys(peers)),
        evidence_roots=tuple(dict.fromkeys(evidence_roots)),
        profile_id=task["baseline"].get("profile_id") or jobs.w.config["profile_id"],
    )
    return inherit_snapshot_materials(
        parent_pack=pack_path, built=built, output_root=jobs.w.state / "material-packs"
    )


def _finish(jobs, task, state, pack_path) -> None:
    groups = task.get("group_results", {})
    # Reference group artifacts only; feeding previous final artifacts back
    # into the next final creates a new identity on every unchanged finish.
    candidate_ids = list(dict.fromkeys(
        ident for value in groups.values() for ident in value.get("candidate_artifacts", [])
    ))
    ready = [value for value in groups.values() if value.get("candidate_state") == "ready"]
    partial = any(
        value.get("state") not in {"completed"}
        or value.get("candidate_state") != "ready"
        or value.get("candidate_gaps")
        or value.get("candidate_build_status") not in {None, "ready"}
        for value in groups.values()
    )
    final = None
    aggregate_error = None
    if ready:
        latest = task.get("latest_candidate") or ready[-1]
        if task.get("candidate_roots"):
            try:
                aggregate = _build_candidate_pack(
                    jobs, task, state, pack_path,
                    candidate_roots=task.get("candidate_roots", ()),
                    candidate_evidence=task.get("candidate_evidence_roots", ()),
                )
                latest = {
                    "candidate_pack_path": aggregate["pack_dir"],
                    "candidate_snapshot_id": aggregate["pack_id"],
                    "candidate_build_status": aggregate.get("status"),
                    "group_id": "all",
                }
                task["latest_candidate"] = latest
            except Exception as exc:
                aggregate_error = f"{type(exc).__name__}:{str(exc)[:500]}"
                partial = True
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
            "aggregate_candidate_roots": list(task.get("candidate_roots", ())),
            "aggregate_candidate_evidence_roots": list(task.get("candidate_evidence_roots", ())),
            "aggregate_build_error": aggregate_error,
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
    recovery_open = bool(_open_recovery_groups(task))
    if recovery_open:
        recovery = list(task.get("recovery_groups") or ())
        open_groups = set(_open_recovery_groups(task))
        task["recovery_index"] = next(
            (index for index, group_id in enumerate(recovery) if group_id in open_groups),
            0,
        )
    task.update(
        state="partial" if partial or final is None or recovery_open else "completed",
        reason=(None if not partial and final and not recovery_open
                else "candidate_or_group_gaps_preserved"),
        next_action=("resume_failed_groups" if recovery_open
                     else None if not partial and final else task.get("next_action")),
    )
    jobs.save(task)


def _next_group(task: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return the next forward or recovery group without changing the job."""

    groups = tuple(task.get("groups") or ())
    index = int(task.get("group_index", 0))
    if index < len(groups):
        return "forward", groups[index]
    recovery = list(task.get("recovery_groups") or ())
    cursor = int(task.get("recovery_index", 0))
    results = task.get("group_results") or {}
    while cursor < len(recovery):
        group_id = recovery[cursor]
        current = results.get(group_id) or {}
        if _group_recovered(current):
            cursor += 1
            continue
        return "recovery", group_id
    return "", None


def _group_needs_recovery(current: Mapping[str, Any]) -> bool:
    return (
        current.get("state") in {"failed", "partial"}
        or current.get("candidate_state") != "ready"
        or bool(current.get("candidate_gaps"))
    )


def _group_recovered(current: Mapping[str, Any]) -> bool:
    return (
        current.get("state") == "completed"
        and current.get("candidate_state") == "ready"
        and not current.get("candidate_gaps")
        and current.get("candidate_build_status") in {None, "ready"}
    )


def _recovery_group_still_open(current: Mapping[str, Any]) -> bool:
    return not _group_recovered(current)


def _queue_recovery_group(task: dict[str, Any], group_id: str) -> None:
    queue = task.setdefault("recovery_groups", [])
    if group_id not in queue:
        queue.append(group_id)


def _open_recovery_groups(task: Mapping[str, Any]) -> list[str]:
    results = task.get("group_results") or {}
    return [
        group_id for group_id in task.get("recovery_groups", ())
        if not _group_recovered(results.get(group_id) or {})
    ]


def _advance_recovery(task: dict[str, Any], group_id: str) -> None:
    recovery = list(task.get("recovery_groups") or ())
    try:
        index = recovery.index(group_id)
    except ValueError:
        return
    task["recovery_index"] = index + 1


def _persist_group_run_id(jobs, task: dict[str, Any], current: Mapping[str, Any]) -> None:
    """Persist a newly planned run before its first execute call can fail."""

    results = dict(task.get("group_results") or {})
    results[current["group_id"]] = dict(current)
    task["group_results"] = results
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


def _fallback_documents(jobs, task, current, group, state, pack_path, group_root, dataset_results):
    empty = [topic for topic, value in dataset_results.items() if value.get("state") == "no_data"]
    if not empty:
        return {"status": "not_needed", "reason": "api_not_complete_empty"}
    from .report_materials import refresh_report_materials

    # Reuse the parent snapshot's bound roots.  This checks every declared
    # source/auxiliary input hash before a filing fallback can read it, so a
    # changed local directory cannot silently become a new baseline.
    roots, evidence, _ = bound_roots(jobs.w, state, pack_path)
    previous = list(task.get("candidate_evidence_roots") or ())
    # Keep pre-scope candidate indexes intact while continuing the same job.
    output = group_root / "documents-scoped"
    legacy = group_root / "documents"
    if legacy.is_dir():
        previous.append(legacy)
    requirements = [{"requirement_id": value} for value in group["report_requirements"]]
    frozen_periods = tuple(task["baseline"].get("report_periods") or ())
    document_periods = {
        "D01": max((value for value in frozen_periods if value.endswith("-12-31")), default=None),
        "D02": max((value for value in frozen_periods if value.endswith("-06-30")), default=None),
    }
    try:
        result = refresh_report_materials(
            ticker=state["ticker"], cutoff=date.fromisoformat(state["as_of"]),
            output_root=output, db_path=group_root / "documents.db", data_root=group_root / "document-data",
            evidence_roots=tuple(dict.fromkeys([*roots, *evidence, *previous])),
            requirements=requirements, allow_network=True, refresh=bool(task.get("refresh")),
            max_seconds=max(5, min(60, int(jobs.w.config.get("max_tool_seconds", 120)) - 15)),
            profile_id=task["baseline"].get("profile_id") or jobs.w.config["profile_id"],
            allowed_report_periods=tuple(value for value in document_periods.values() if value),
        )
        result["trigger_topics"] = empty
        result["scope"] = "report_period_and_registered_route_only"
        result["allowed_report_periods"] = [value for value in document_periods.values() if value]
        if (output / "live-documents.json").is_file() and (output / "document-evidence.jsonl").is_file():
            from .deferred_evidence import organize_topic_evidence
            result["topic_results"] = organize_topic_evidence(output, empty, result["allowed_report_periods"])
        result["source_files"] = _root_descriptors([output])
        return result
    except Exception as exc:
        return {
            "status": "failed", "reason": str(exc), "trigger_topics": empty,
            "scope": "report_period_and_registered_route_only",
            "allowed_report_periods": [value for value in document_periods.values() if value],
        }


def _snapshot_inventory(db: Path, data_root: Path, topics: Sequence[str]) -> list[dict[str, Any]]:
    if not db.is_file():
        return []
    rows = []
    with sqlite3.connect(db) as con:
        payloads = con.execute("SELECT payload FROM raw_resource_snapshots ORDER BY created_at,snapshot_id").fetchall()
    for (raw,) in payloads:
        item = json.loads(raw)
        query = item.get("query_page_canonical")
        try:
            query = json.loads(query) if isinstance(query, str) else query
        except json.JSONDecodeError:
            query = None
        dataset_id = query.get("dataset_id") if isinstance(query, Mapping) else None
        topic_scope = [dataset_id] if dataset_id in topics else []
        relative = item.get("archive_relative_path")
        path = (data_root / relative).resolve() if relative else None
        entry = {
            "snapshot_id": item.get("snapshot_id"),
            "dataset_id": dataset_id,
            "source_definition_id": item.get("source_definition_id"),
            "source_definition_version": item.get("source_definition_version"),
            "query_page_canonical": item.get("query_page_canonical"),
            "page_number": item.get("page_number"),
            "sha256": item.get("sha256"),
            "archive_relative_path": relative,
            "available_at": item.get("available_at"),
            "created_at": item.get("created_at"),
            "file_hash_verified": bool(path and path.is_file() and sha(path) == item.get("sha256")),
            "topic_scope": topic_scope,
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
