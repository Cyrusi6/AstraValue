"""Bounded core inputs through the existing acquisition and snapshot pipeline."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .workspace import ResearchError, read_json, sha

CORE_MATERIAL_TYPES = ("market_quote", "peer_facts", "report_documents")


def bound_roots(workspace, state, pack_path):
    """Keep adopted inputs when a later material request builds another candidate."""
    roots, evidence = workspace.material_input_roots(date.fromisoformat(state["as_of"]))
    peers = workspace.peer_input_roots()
    manifest = read_json(pack_path / "manifest.json")
    for group, destinations in (("source_inputs", roots), ("auxiliary_inputs", evidence), ("peer_inputs", peers)):
        for item in manifest.get(group, []):
            descriptors = list(item.get("files", {}).values()) + item.get("manifests", [])
            if item.get("path"):
                descriptors.append(item)
            for descriptor in descriptors:
                path = Path(descriptor["path"])
                if not path.is_file() or sha(path) != descriptor["sha256"]:
                    raise ResearchError("material_parent_input_changed:" + str(path))
            if item.get("root"):
                destinations.append(Path(item["root"]))
            elif group == "peer_inputs" and item.get("path"):
                destinations.append(Path(item["path"]).parent.parent)
    return tuple(list(dict.fromkeys(values)) for values in (roots, evidence, peers))


def _targets(workspace, state, payload, job):
    """Select only requested metrics and the registered peer comparison window."""
    from analysis.structured.scope import load_research_profile

    ticker = state["ticker"]
    profile = load_research_profile(payload.get("profile_id") or workspace.config["profile_id"])
    kinds = set(job.get("material_types", []))
    targets = {}
    if "market_quote" in kinds:
        targets[(ticker, "market_cap")] = {"material_type": "market_quote", "periods": []}
    if "peer_facts" in kinds:
        # Only the latest annual comparison and current valuation are needed.
        annual = payload.get("periods", {}).get("annual", [])
        if not annual:
            raise ResearchError("peer_comparison_period_missing")
        for peer in profile["peer_sets"].get(ticker, []):
            row = next((p for p in payload.get("peers", []) if p["ticker"] == peer), {})
            metrics = {m["metric_id"] for m in row.get("metrics", [])}
            if job.get("refresh") or not {"operating_income", "gross_margin"} <= metrics:
                targets[(peer, "income_fields")] = {"material_type": "peer_facts", "periods": [annual[-1]]}
            if job.get("refresh") or not {"eastmoney_pe_ttm", "eastmoney_pb_mrq"} <= metrics:
                targets[(peer, "market_cap")] = {"material_type": "peer_facts", "periods": []}
    for item in job.get("requirements", []):
        if item.get("material_types") or not item.get("dataset_id"):
            continue
        value = targets.setdefault((ticker, item["dataset_id"]), {"material_type": "structured", "periods": []})
        if len(item.get("period", "")) == 10:
            value["periods"].append(item["period"])
    return [{"ticker": key[0], "dataset_id": key[1], **value} for key, value in sorted(targets.items())]


def _collect_structured(jobs, job, state, payload, folder, cutoff):
    from analysis.acquisition.runtime import AcquisitionRuntime
    from analysis.structured.runtime import StructuredDataRuntime

    targets = _targets(jobs.w, state, payload, job)
    pending, failures = [], []
    if not targets:
        return targets, pending, failures
    with AcquisitionRuntime.create(folder / "analysis.db", folder / "data") as acquisition:
        runtime = StructuredDataRuntime(acquisition)
        for target in targets:
            key = target["ticker"] + ":" + target["dataset_id"]
            try:
                identity = jobs.w.resolve(target["ticker"], cutoff).identity
                if identity is None:
                    raise ResearchError("material_company_identity_unresolved:" + target["ticker"])
                prior = job.get("dataset_status", {}).get(key, {})
                plan_args = dict(company_scope="company-only", dataset_ids=[target["dataset_id"]],
                    research_profile_id=payload.get("profile_id") or jobs.w.config["profile_id"],
                    as_of=datetime.combine(cutoff, time.max, ZoneInfo("Asia/Shanghai")))
                if (key in job["run_ids"] and (prior.get("failed") or prior.get("blocked"))
                    and not any(prior.get(k) for k in ("pending", "retryable", "partial"))):
                    plan = runtime.plan([identity], mode="reconcile", parent_run_id=job["run_ids"][key], **plan_args)
                    job["run_ids"][key] = plan.run_ids[0]
                    jobs.save(job)
                if key not in job["run_ids"]:
                    plan = runtime.plan([identity], mode="incremental",
                        report_periods=sorted(set(target["periods"])),
                        valuation_start=cutoff - timedelta(days=14) if target["dataset_id"] == "market_cap" else None,
                        **plan_args)
                    job["run_ids"][key] = plan.run_ids[0]
                    jobs.save(job)
                result = runtime.execute(job["run_ids"][key], max_jobs_per_round=1)
                status = result["status"]
                job.setdefault("dataset_status", {})[key] = status
                job.setdefault("attempted_job_ids", []).extend(result.get("attempted_job_ids", []))
                entry = {**target, "run_id": job["run_ids"][key], "summary": status.get("summary", {})}
                if any(status.get(k) for k in ("pending", "retryable", "partial")):
                    pending.append(entry)
                if status.get("failed") or status.get("blocked"):
                    failures.append(entry)
                jobs.save(job)
            except Exception as exc:
                # Independent datasets/companies still get their bounded attempt.
                failures.append({**target, "run_id": job["run_ids"].get(key),
                    "reason": str(exc), "next_action": "resume_task"})
    return targets, pending, failures


def execute(jobs, job):
    from analysis.structured.research_lite import build_lite_pack, materialize_cache

    if job["state"] == "completed":
        return
    w = jobs.w
    state, pack_path, payload = w.pack(job["research_id"])
    profile_id = read_json(pack_path / "manifest.json").get("profile_id") or w.config["profile_id"]
    if state["snapshot_id"] != job["snapshot_id"]:
        raise ResearchError("material_task_snapshot_no_longer_active")
    if w.config.get("offline", False):
        return
    cutoff = date.fromisoformat(state["as_of"])
    roots, evidence, peers = bound_roots(w, state, pack_path)
    folder = w.state / "jobs" / job["task_id"]
    folder.mkdir(parents=True, exist_ok=True)
    job["round"] = job.get("round", 0) + 1
    jobs.save(job)
    # Each published round remains immutable after a candidate binds its files.
    outputs = folder / "rounds" / str(job["round"])
    outputs.mkdir(parents=True, exist_ok=True)
    targets, pending, failures = _collect_structured(jobs, job, state, payload, folder, cutoff)
    material_results = {}
    if targets and job["run_ids"]:
        try:
            materialize_cache(folder / "analysis.db", folder / "data", outputs / "materialized", cutoff,
                              tickers=sorted({t["ticker"] for t in targets}), research_profile_id=profile_id,
                              finalized_only=True)
            if (outputs / "materialized" / state["ticker"]).is_dir():
                roots.append(outputs / "materialized")
            peers.append(outputs / "materialized")
        except Exception as exc:
            failures.append({"stage": "materialization", "reason": str(exc), "next_action": "resume_task"})
    if "report_documents" in job.get("material_types", []):
        from .report_materials import refresh_report_materials
        try:
            result = refresh_report_materials(ticker=state["ticker"], cutoff=cutoff,
                output_root=folder / "documents", db_path=folder / "documents.db", data_root=folder / "document-data",
                evidence_roots=[*roots, *evidence], requirements=job.get("requirements", []),
                profile_id=profile_id, refresh=job.get("refresh", False), allow_network=True,
                max_seconds=max(5, min(60, int(w.config.get("max_tool_seconds", 120)) - 15)))
            material_results["report_documents"] = result
            # Freeze small indexes; original PDFs/snapshots remain content-addressed.
            frozen = outputs / "documents"
            for name in ("document-evidence.jsonl", "live-documents.json"):
                source = folder / "documents" / name
                if source.is_file():
                    frozen.mkdir(parents=True, exist_ok=True)
                    (frozen / name).write_bytes(source.read_bytes())
            if frozen.is_dir():
                evidence.insert(0, frozen)
            if result.get("status") == "checkpointed":
                pending.append({"material_type": "report_documents", **result})
            elif result.get("status") != "completed":
                failures.append({"material_type": "report_documents", **result})
        except Exception as exc:
            failures.append({"material_type": "report_documents", "reason": str(exc), "next_action": "resume_task"})
    # A caller can combine the new core types with the existing API supplements.
    for kind in sorted(set(job.get("material_types", [])) - set(CORE_MATERIAL_TYPES)):
        try:
            child = jobs.request_materials(job["research_id"], job["question"], job["impact"],
                                          material_types=[kind], execute=False, refresh=job.get("refresh", False))
            # The public resume path owns leases and the retry budget. A child
            # running in another worker must never be executed inline again.
            jobs.resume_task(child["task_id"])
            child = jobs.get(child["task_id"])
            entry = {"material_type": kind, "task_id": child["task_id"], "state": child["state"],
                     "result": child.get("result"), "reason": child.get("reason")}
            material_results[kind] = entry
            if child["state"] in {"planned", "running", "checkpointed", "interrupted"}:
                pending.append(entry)
            elif child["state"] != "completed":
                failures.append(entry)
        except Exception as exc:
            entry = {"material_type": kind, "reason": str(exc), "next_action": "resume_task"}
            material_results[kind] = entry
            failures.append(entry)
    job["material_results"] = material_results
    job["unfinished_datasets"] = failures
    job["remaining"] = pending
    try:
        roots = list(dict.fromkeys(roots))
        primary = next((root for root in roots if (root / state["ticker"]).is_dir()), None)
        if primary is None:
            raise ResearchError("registered_projection_missing")
        built = build_lite_pack(input_root=primary, ticker=state["ticker"], as_of=cutoff,
            output_root=w.state / "packs", supplements=[p for p in roots if p != primary],
            evidence_roots=evidence, peer_roots=list(dict.fromkeys(peers)), profile_id=profile_id)
        from .snapshot_materials import inherit_snapshot_materials
        built = inherit_snapshot_materials(parent_pack=pack_path, built=built, output_root=w.state / "material-packs")
        job["result"] = w.artifact(job["research_id"], "snapshot_candidate", {
            "candidate_pack_path": built["pack_dir"], "candidate_snapshot_id": built["pack_id"],
            "reason": job["impact"], "build_status": built["status"], "source_task": job["task_id"]},
            expected_snapshot_id=job["snapshot_id"])
    except Exception as exc:
        failures.append({"stage": "pack_build", "reason": str(exc), "next_action": "resume_task"})
    if pending:
        job.update(state="checkpointed", reason="bounded_round_completed; resume_same_run", failures=0)
    elif failures:
        job.update(state="partial", reason="available_data_updated; unfinished_windows_preserved",
                   failures=job.get("failures", 0) + 1)
    else:
        job.update(state="completed", reason=None, failures=0)
    jobs.save(job)
