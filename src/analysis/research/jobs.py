"""Persistent bounded acquisition work; adapters own URLs, not the model."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

from .workspace import ResearchWorkspace, ResearchError, digest, encode, read_json, TOPICS


class MaterialJobs:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace
        with self.w.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS material_jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def save(self, job):
        with self.w.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM material_jobs WHERE id=?", (job["task_id"],)).fetchone()
            if row and json.loads(row["payload"])["attempts"] > job["attempts"]:
                raise ResearchError("stale_material_worker")
            con.execute("INSERT INTO material_jobs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (job["task_id"], encode(job)))

    def get(self, task_id: str):
        with self.w.connect() as con:
            row = con.execute("SELECT payload FROM material_jobs WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ResearchError("unknown_material_task")
        return json.loads(row["payload"])

    def request_materials(self, research_id: str, question: str, impact: str,
                          requirement_ids: list[str] | None = None, topic: str = "financials",
                          execute: bool | None = None, material_types: list[str] | None = None, refresh: bool = False):
        """Request materials. material_types supports sw_industry, audit_opinion, regulatory_records,
        customers_peer, guarantee, litigation, seo, allotment, bond_issuance, pledge, unlock_peer,
        market_quote (bounded current valuation), peer_facts (registered peers), report_documents
        (latest complete Chinese annual/interim reports and located passages).
        These complete fetch/verify/register automatically (execute defaults to true for this path).
        get_task reads progress; resume_task continues checkpoints. Empty API is not proof of no event.
        Legacy requirement_ids retains planned/execute behavior. refresh forces one bounded new attempt chain."""
        if not question.strip() or not impact.strip():
            raise ResearchError("research_question_and_material_impact_required")
        state, path, payload = self.w.pack(research_id)
        if material_types:
            if requirement_ids:raise ResearchError('choose_material_types_or_requirement_ids')
            from .supplements import MATERIAL_TYPES, existing
            from .core_materials import CORE_MATERIAL_TYPES
            kinds=sorted(set(material_types))
            if set(kinds)-set(MATERIAL_TYPES)-set(CORE_MATERIAL_TYPES):
                return {'status':'capability_gap','supported_material_types':list(MATERIAL_TYPES)+list(CORE_MATERIAL_TYPES)}
            if self.w.config.get('offline',False):
                # Offline reuse may register nothing new; it never starts a network worker.
                return {'status':'offline','materials':{k:existing(self.w,research_id,k) for k in kinds if k in MATERIAL_TYPES},
                        'read_entry':{'tool':'get_research_brief','research_id':research_id}}
            ident='j_'+digest({'research_id':research_id,'snapshot_id':state['snapshot_id'],
                              'material_types':kinds,'refresh':refresh})[:24]
            try:job=self.get(ident)
            except ResearchError:
                job={'task_id':ident,'research_id':research_id,'snapshot_id':state['snapshot_id'],
                     'topic':'core_materials' if set(kinds)&set(CORE_MATERIAL_TYPES) else 'supplements',
                     'question':question,'impact':impact,'material_types':kinds,
                     'refresh':refresh,'state':'planned','attempts':0,'run_ids':{},'result':None}
                self.save(job)
            return job if execute is False else self.resume_task(ident)
        if topic not in TOPICS and topic != "catalog":
            raise ResearchError("unsupported_material_topic")
        work = read_json(path / "next-work.json")["items"]
        if topic != "catalog" and not requirement_ids:
            existing = self.w.query_research(research_id, "evidence", question=question)
            return {"status": "selection_required", "reason": "read_existing_evidence_then_select_requirement_ids",
                    "evidence": existing, "requirements": [{k: x.get(k) for k in
                       ("requirement_id", "stage", "reason", "question_id", "period")} for x in work[:30]],
                    "total_requirements": len(work), "next_action": "query_material_requirements"}
        selected = [x for x in work if x.get("requirement_id") in (requirement_ids or [])]
        known = {x.get("requirement_id") for x in selected}
        if set(requirement_ids or []) - known:
            raise ResearchError("unknown_requirement_id")
        from .core_materials import CORE_MATERIAL_TYPES
        core_types = sorted({kind for x in selected for kind in x.get("material_types", [])
                             if kind in CORE_MATERIAL_TYPES})
        blocked = [x for x in selected if not x.get("acquire_allowed") or
                   not (x.get("dataset_id") or set(x.get("material_types", [])) & set(CORE_MATERIAL_TYPES))]
        if blocked:
            return {"status": "processing_or_research_gap", "items": blocked,
                    "reason": "not_a_routable_acquisition_gap; existing_data_not_downloaded_again"}
        request = {"research_id": research_id, "snapshot_id": state["snapshot_id"], "question": question,
                   "impact": impact, "topic": "core_materials" if core_types else topic,
                   "requirements": selected, "material_types": core_types}
        # Wording changes must not bypass a failed acquisition's retry budget.
        ident = "j_" + digest({"research_id": research_id, "snapshot_id": state["snapshot_id"],
            "topic": topic, "requirement_ids": sorted(set(requirement_ids or []))})[:24]
        try:
            job = self.get(ident)
        except ResearchError:
            job = {**request, "task_id": ident, "state": "planned", "attempts": 0, "run_ids": {}, "result": None}
            self.save(job)
        if execute:
            return self.resume_task(ident)
        return job

    def query_material_requirements(self, research_id: str, page: int = 1, page_size: int = 30):
        if page < 1 or not 1 <= page_size <= 40:
            raise ResearchError("invalid_pagination")
        state, path, _ = self.w.pack(research_id)
        work = read_json(path / "next-work.json")["items"]
        start = (page-1)*page_size
        return {"research_id": research_id, "snapshot_id": state["snapshot_id"], "items": work[start:start+page_size],
                "total": len(work), "next_page": page+1 if start+page_size < len(work) else None}

    def resume_task(self, task_id: str):
        """Execute one bounded round. Reuses the same provider run and checkpoint."""
        if self.w.config.get("offline", False):
            return {"task_id": task_id, "state": self.get(task_id)["state"], "status": "offline"}
        with self.w.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM material_jobs WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ResearchError("unknown_material_task")
            job = json.loads(row["payload"])
            now = datetime.now(timezone.utc)
            if job["state"] == "running" and job.get("lease_until") and datetime.fromisoformat(job["lease_until"]) <= now:
                job.update(state="interrupted", reason="worker_lease_expired", failures=job.get("failures", 0)+1)
            if job["state"] in {"running", "completed", "exhausted"}:
                return job
            if job.get("failures", 0) >= int(self.w.config.get("max_attempts",3)):
                job.update(state="exhausted", reason="same_request_attempt_limit; new_evidence_required")
                con.execute("UPDATE material_jobs SET payload=? WHERE id=?", (encode(job), task_id))
                return job
            job.update(state="running", attempts=job["attempts"]+1,
                lease_until=(now + timedelta(seconds=int(self.w.config.get("max_tool_seconds",120))+30)).isoformat())
            con.execute("UPDATE material_jobs SET payload=? WHERE id=?", (encode(job), task_id))
        folder = self.w.state / "jobs" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        from .supplement_transport import dump
        dump(folder/'workspace-config.json',self.w.config)
        env = dict(os.environ, PYTHONPATH=str(self.w.root / "src"), PYTHONIOENCODING="utf-8")
        try:
            with (folder / "worker.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-m", "analysis.research.jobs", "supervise", str(self.w.root), task_id, str(job["attempts"]),str(folder/'workspace-config.json')],
                    stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"task_id": task_id, "state": "running", "poll_after_seconds": 5, "pid": process.pid}
        except OSError as exc:
            job.update(state="failed", reason=str(exc), failures=job.get("failures", 0)+1); self.save(job)
            return job

    def execute_round(self, task_id):
        job=self.get(task_id)
        if self.w.config.get("offline", False):
            return
        if job['topic']=='core_materials':
            from .core_materials import execute
            execute(self,job)
            return
        if job['topic']=='supplements':
            from .supplements import execute
            execute(self,job)
            return
        from analysis.acquisition.runtime import AcquisitionRuntime
        from analysis.structured.runtime import StructuredDataRuntime
        from analysis.structured.research_lite import (
            materialize_cache,
            refresh_lite_catalog,
            build_lite_pack,
        )
        from analysis.structured.scope import load_research_profile
        job = self.get(task_id)
        state, pack_path, _ = self.w.pack(job["research_id"])
        profile_id = read_json(pack_path / "manifest.json").get("profile_id") or self.w.config["profile_id"]
        if state["snapshot_id"] != job["snapshot_id"]:
            raise ResearchError("material_task_snapshot_no_longer_active")
        output = self.w.state / "jobs" / task_id
        cutoff = date.fromisoformat(state["as_of"])
        from .core_materials import bound_roots
        roots, evidence_roots, peer_roots = bound_roots(self.w, state, pack_path)
        supplements = roots[1:]
        if job["topic"] == "catalog":
            refresh_lite_catalog(output_root=output, ticker=state["ticker"], as_of=cutoff)
            evidence_roots.append(output)
        else:
            identity = self.w.resolve(state["canonical_ticker"], cutoff).identity
            datasets = sorted({x["dataset_id"] for x in job["requirements"]})
            completed = True
            unfinished = []
            with AcquisitionRuntime.create(output / "analysis.db", output / "data") as acquisition:
                runtime = StructuredDataRuntime(acquisition)
                for dataset in datasets:
                    prior = job.get("dataset_status", {}).get(dataset, {})
                    if (dataset in job["run_ids"] and (prior.get("failed") or prior.get("blocked"))
                        and not any(prior.get(key) for key in ("pending", "retryable", "partial"))):
                        plan = runtime.plan([identity], mode="reconcile", company_scope="company-only",
                            dataset_ids=[dataset], parent_run_id=job["run_ids"][dataset],
                            research_profile_id=profile_id,
                            as_of=datetime.combine(cutoff, time.min, tzinfo=timezone.utc))
                        job["run_ids"][dataset] = plan.run_ids[0]
                        self.save(job)
                    if dataset not in job["run_ids"]:
                        periods = sorted({x["period"] for x in job["requirements"]
                                          if x["dataset_id"] == dataset and len(x.get("period", "")) == 10})
                        plan = runtime.plan([identity], mode="incremental", company_scope="company-only",
                            dataset_ids=[dataset], report_periods=periods, research_profile_id=profile_id,
                            as_of=datetime.combine(cutoff, time.min, tzinfo=timezone.utc))
                        job["run_ids"][dataset] = plan.run_ids[0]
                        self.save(job)
                    run = job["run_ids"][dataset]
                    round_result = runtime.execute(run, max_jobs_per_round=1)
                    status = round_result["status"]
                    job.setdefault("dataset_status", {})[dataset] = status
                    if status["failed"] or status.get("blocked", 0):
                        unfinished.append({"dataset_id": dataset, "run_id": run,
                                           "recovery": status.get("summary", {}).get("unfinished", []),
                                           "next_action": "incremental_or_reconcile"})
                    if status["pending"] or status["retryable"] or status["partial"]:
                        completed = False
            job["unfinished_datasets"] = unfinished
            if not completed:
                job.update(state="checkpointed", reason="bounded_round_completed; resume_same_run", failures=0)
                self.save(job)
                return
            job["publication_round"] = job.get("publication_round", 0) + 1
            self.save(job)
            materialized = output / "rounds" / str(job["publication_round"]) / "materialized"
            materialize_cache(output / "analysis.db", output / "data", materialized, cutoff,
                              tickers=[state["ticker"]], research_profile_id=profile_id, finalized_only=True)
            supplements.append(materialized)
        built = build_lite_pack(input_root=roots[0], ticker=state["ticker"], as_of=cutoff,
                               output_root=self.w.state / "packs", supplements=supplements,
                               profile_id=profile_id, peer_roots=peer_roots,
                               evidence_roots=evidence_roots)
        from .snapshot_materials import inherit_snapshot_materials
        built = inherit_snapshot_materials(parent_pack=pack_path, built=built, output_root=self.w.state / "material-packs")
        # A new snapshot is proposed, never silently adopted by the active research.
        artifact = self.w.artifact(job["research_id"], "snapshot_candidate", {
            "candidate_pack_path": built["pack_dir"], "candidate_snapshot_id": built["pack_id"],
            "reason": job["impact"], "build_status": built["status"], "source_task": task_id},
            expected_snapshot_id=job["snapshot_id"])
        if job.get("unfinished_datasets"):
            job.update(state="partial", result=artifact, reason="available_data_updated; unfinished_windows_preserved",
                       failures=job.get("failures", 0)+1)
        else:
            job.update(state="completed", result=artifact, reason=None)
        self.save(job)

    def adopt_snapshot(self, research_id: str, candidate_id: str):
        """Explicitly adopt a verified candidate and reopen analysis; old artifacts stay frozen."""
        state, revision = self.w.task(research_id)
        candidates = self.w.artifacts(research_id, "snapshot_candidate")
        candidate = next((x for x in candidates if x["artifact_id"] == candidate_id), None)
        if not candidate:
            raise ResearchError("candidate_not_in_active_snapshot")
        old = state["snapshot_id"]
        if candidate["snapshot_id"] != old:
            raise ResearchError("candidate_not_in_active_snapshot")
        path = Path(candidate["candidate_pack_path"])
        manifest = self.w._verify_pack(path)
        if manifest["ticker"] != state["ticker"] or manifest["as_of"] != state["as_of"]:
            raise ResearchError("candidate_identity_mismatch")
        if manifest["pack_id"] != candidate["candidate_snapshot_id"]:
            raise ResearchError("candidate_snapshot_mismatch")
        from .snapshot_artifacts import inherit_material_artifacts
        from .workspace import sha
        state.update(pack_path=str(path), snapshot_id=manifest["pack_id"], manifest_sha256=sha(path / "manifest.json"),
                     status="analysis_pending", adopted_from=old)
        state["stages"].update(analysis="needs_review", rendering="pending", processing=manifest["status"])
        result = {"research_id": research_id, "previous_snapshot": old, "snapshot_id": manifest["pack_id"],
                  "review_required": True, "reason": candidate["reason"]}
        with self.w.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            live = con.execute("SELECT state_json,revision FROM research_tasks WHERE id=?", (research_id,)).fetchone()
            if live is None or json.loads(live["state_json"])["snapshot_id"] != old:
                raise ResearchError("candidate_not_in_active_snapshot")
            if live["revision"] != revision:
                raise ResearchError("concurrent_task_update:reload_and_retry")
            inherit_material_artifacts(con, research_id, old, manifest["pack_id"])
            self.w._save(state, revision, connection=con)
        return result


def main():
    action, root, task_id, attempt_text, *config_paths = sys.argv[1:]
    w = ResearchWorkspace(Path(root),read_json(Path(config_paths[0])) if config_paths else None); jobs = MaterialJobs(w)
    attempt = int(attempt_text)
    if jobs.get(task_id)["attempts"] != attempt:
        raise ResearchError("stale_material_worker")
    if action == "operate":
        jobs.execute_round(task_id)
        return
    try:
        subprocess.run([sys.executable, "-m", "analysis.research.jobs", "operate", root, task_id, attempt_text, *config_paths],
                       check=True, timeout=int(w.config.get("max_tool_seconds",120)),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        job = jobs.get(task_id)
        if job["attempts"] != attempt:
            return
        job.update(state="interrupted" if isinstance(exc, subprocess.TimeoutExpired) else "failed",
                   failures=job.get("failures", 0)+1,
                   reason="bounded_execution_stopped; inspect_checkpoint_and_resume")
        jobs.save(job)


if __name__ == "__main__":
    main()
