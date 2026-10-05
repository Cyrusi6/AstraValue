from pathlib import Path

import pytest

from analysis.research.deferred_topics import (
    DEFERRED_TOPICS,
    group_definitions,
    validate_groups,
    _dataset_results,
)
from analysis.research import deferred_topics
from analysis.research.jobs import MaterialJobs
from analysis.research.workspace import ResearchError, sha
from test_research_workspace import workspace


def test_deferred_groups_are_frozen_and_ordered():
    groups = group_definitions()
    assert [item["group_id"] for item in groups] == [
        "governance", "capital_actions", "operations_assets"
    ]
    assert tuple(topic for item in groups for topic in item["topics"]) == DEFERRED_TOPICS
    assert validate_groups(["governance", "operations_assets"]) == (
        "governance", "operations_assets"
    )
    with pytest.raises(ResearchError, match="group_order_mismatch"):
        validate_groups(["operations_assets", "governance"])
    with pytest.raises(ResearchError, match="unknown_deferred_topic_group"):
        validate_groups(["unknown"])
    with pytest.raises(ResearchError, match="deferred_topic_group_required"):
        validate_groups([])


def test_deferred_request_freezes_baseline_and_is_idempotent(workspace):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    first = jobs.request_deferred_topics(
        rid, "核对延期主题", "可能改变治理和资本风险", execute=False
    )
    second = jobs.request_deferred_topics(
        rid, "换一种问法", "影响候选快照", execute=False
    )
    assert first["task_id"] == second["task_id"]
    assert first["groups"] == ["governance", "capital_actions", "operations_assets"]
    assert first["baseline"]["snapshot_id"] == workspace.task(rid)[0]["snapshot_id"]
    assert first["baseline"]["manifest"]["sha256"] == sha(
        Path(first["baseline"]["manifest"]["path"])
    )
    assert first["material_types"] == list(DEFERRED_TOPICS)
    assert not workspace.artifacts(rid, "catalog_api_materials")


def test_deferred_request_rejects_other_company(workspace):
    rid = workspace.prepare_research("000858", "2025-01-01")["research_id"]
    with pytest.raises(ResearchError, match="require_600519"):
        MaterialJobs(workspace).request_deferred_topics(
            rid, "核对延期主题", "影响候选", execute=False
        )


def test_deferred_subset_keeps_topic_scope_in_task(workspace):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    task = MaterialJobs(workspace).request_deferred_topics(
        rid, "只核对治理", "影响治理风险", groups=["governance"], execute=False
    )
    assert task["material_types"] == ["guarantee", "litigation", "pledge"]
    assert task["groups"] == ["governance"]


def test_dataset_result_keeps_empty_distinct_from_failed_and_pending():
    topics = ("guarantee", "litigation", "goodwill")
    jobs = [
        {"dataset_id": "guarantee", "state": "no_data"},
        {"dataset_id": "litigation", "state": "failed"},
        {"dataset_id": "goodwill", "state": "partial"},
    ]
    result = _dataset_results(topics, jobs, [{
        "dataset_id": "guarantee", "status": "no_data", "query_complete": True,
        "safe_through": "2025-01-01T00:00:00Z", "pagination_audit": {"complete": True},
    }])
    assert result["guarantee"]["state"] == "no_data"
    assert result["guarantee"]["query_complete"] is True
    assert result["litigation"]["state"] == "failed"
    assert result["litigation"]["query_complete"] is False
    assert result["goodwill"]["state"] == "pending"
    assert result["goodwill"]["query_complete"] is False


def test_empty_response_without_terminal_coverage_proof_stays_pending():
    result = _dataset_results(
        ("guarantee",),
        [{"dataset_id": "guarantee", "state": "no_data"}],
        [{"dataset_id": "guarantee", "status": "no_data", "query_complete": True}],
    )
    assert result["guarantee"]["state"] == "pending"
    assert result["guarantee"]["reason"] == "empty_response_completion_proof_missing"


def test_records_without_terminal_coverage_proof_stay_pending():
    result = _dataset_results(
        ("goodwill",),
        [{"dataset_id": "goodwill", "state": "succeeded"}],
        [{"dataset_id": "goodwill", "status": "complete", "query_complete": True}],
    )
    assert result["goodwill"]["state"] == "pending"
    assert result["goodwill"]["reason"] == "record_completion_proof_missing"


def test_blocked_dataset_is_not_reported_as_records():
    result = _dataset_results(
        ("guarantee",),
        [{"dataset_id": "guarantee", "state": "blocked"}],
        [],
    )
    assert result["guarantee"]["state"] == "failed"
    assert result["guarantee"]["query_complete"] is False


def test_deferred_baseline_rejects_config_change(workspace):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance"], execute=False
    )
    workspace.config["max_attempts"] = int(workspace.config.get("max_attempts", 3)) + 1
    with pytest.raises(ResearchError, match="baseline_config_changed"):
        jobs.execute_round(task["task_id"])


def test_deferred_baseline_rejects_active_snapshot_change(workspace):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance"], execute=False
    )
    state, revision = workspace.task(rid)
    state["snapshot_id"] = "snapshot-changed"
    workspace._save(state, revision)
    with pytest.raises(ResearchError, match="snapshot_no_longer_active"):
        jobs.execute_round(task["task_id"])


def test_failed_group_does_not_block_later_group_but_final_stays_partial(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance", "capital_actions"], execute=False
    )

    def fake_round(_jobs, _task, _current, group, _state, _payload, _root):
        if group["group_id"] == "governance":
            return {"state": "failed", "dataset_results": {
                "guarantee": {"state": "failed"},
            }, "structured_status": {"failed": 1}}
        return {"state": "completed", "dataset_results": {
            "seo": {"state": "records"},
        }, "structured_status": {"succeeded": 1}}

    def fake_publish(_jobs, current_task, current, _state, _pack_path, _payload):
        if current["group_id"] != "capital_actions":
            return
        artifact = _jobs.w.artifact(rid, "snapshot_candidate", {
            "candidate_pack_path": "candidate-pack",
            "candidate_snapshot_id": "candidate-capital",
            "group_id": current["group_id"],
            "topics": current["topics"],
            "adoption_required": True,
        }, expected_snapshot_id=current_task["snapshot_id"])
        current["candidate_state"] = "ready"
        current["candidate_pack_path"] = "candidate-pack"
        current["candidate_snapshot_id"] = "candidate-capital"
        current.setdefault("candidate_artifacts", []).append(artifact["artifact_id"])
        current_task.setdefault("candidate_artifacts", []).append(artifact["artifact_id"])

    monkeypatch.setattr(deferred_topics, "_structured_round", fake_round)
    monkeypatch.setattr(deferred_topics, "_publish_group_candidate", fake_publish)
    jobs.execute_round(task["task_id"])
    checkpoint = jobs.get(task["task_id"])
    assert checkpoint["group_index"] == 1
    assert checkpoint["state"] == "checkpointed"
    assert checkpoint["group_results"]["governance"]["state"] == "failed"

    jobs.execute_round(task["task_id"])
    result = jobs.get(task["task_id"])
    assert result["state"] == "partial"
    assert result["result"]["build_status"] == "partial"
    assert result["result"]["adoption_required"] is True


def test_candidate_build_failure_cannot_report_completed(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance"], execute=False
    )

    monkeypatch.setattr(deferred_topics, "_structured_round", lambda *args: {
        "state": "completed", "dataset_results": {}, "structured_status": {"succeeded": 1},
    })

    def unavailable(*args):
        args[2]["candidate_state"] = "unavailable"
        args[2]["candidate_gaps"] = [{"stage": "pack_build", "reason": "test"}]

    monkeypatch.setattr(deferred_topics, "_publish_group_candidate", unavailable)
    jobs.execute_round(task["task_id"])
    result = jobs.get(task["task_id"])
    assert result["state"] == "partial"
    assert result["result"]["status"] == "partial"
    assert not workspace.artifacts(rid, "snapshot_candidate")


def test_candidate_input_integrity_failure_does_not_block_group_completion(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance", "capital_actions"], execute=False
    )
    monkeypatch.setattr(deferred_topics, "_structured_round", lambda *args: {
        "state": "completed", "dataset_results": {}, "structured_status": {"succeeded": 1},
    })

    def broken_roots(*args):
        raise ResearchError("material_parent_input_changed")

    monkeypatch.setattr(deferred_topics, "bound_roots", broken_roots)
    jobs.execute_round(task["task_id"])
    result = jobs.get(task["task_id"])
    assert result["group_index"] == 1
    assert result["group_results"]["governance"]["candidate_state"] == "unavailable"
    assert result["state"] == "checkpointed"


def test_failed_group_can_reconcile_after_later_groups_finish(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance", "capital_actions"], execute=False
    )
    calls = []
    aggregate_calls = []

    def fake_round(_jobs, _task, current, group, _state, _payload, _root, *, reconcile=False):
        calls.append((group["group_id"], reconcile))
        if group["group_id"] == "governance" and not reconcile:
            return {"state": "failed", "dataset_results": {
                "guarantee": {"state": "failed"},
            }, "structured_status": {"failed": 1}}
        return {"state": "completed", "dataset_results": {
            group["topics"][0]: {"state": "records"},
        }, "structured_status": {"succeeded": 1}}

    def fake_publish(_jobs, current_task, current, _state, _pack_path, _payload):
        artifact = _jobs.w.artifact(rid, "snapshot_candidate", {
            "candidate_pack_path": f"candidate-{current['group_id']}",
            "candidate_snapshot_id": f"candidate-{current['group_id']}",
            "group_id": current["group_id"],
            "topics": current["topics"],
            "adoption_required": True,
        }, expected_snapshot_id=current_task["snapshot_id"])
        current["candidate_state"] = "ready"
        current["candidate_pack_path"] = f"candidate-{current['group_id']}"
        current["candidate_snapshot_id"] = f"candidate-{current['group_id']}"
        current["candidate_build_status"] = "ready"
        current.setdefault("candidate_artifacts", []).append(artifact["artifact_id"])
        current_task.setdefault("candidate_artifacts", []).append(artifact["artifact_id"])
        current_task["latest_candidate"] = {
            "candidate_pack_path": current["candidate_pack_path"],
            "candidate_snapshot_id": current["candidate_snapshot_id"],
        }
        root = f"root-{current['group_id']}"
        if root not in current_task.setdefault("candidate_roots", []):
            current_task["candidate_roots"].append(root)

    def fake_aggregate(_jobs, _task, _state, _pack_path, *, candidate_roots, candidate_evidence):
        aggregate_calls.append((list(candidate_roots), list(candidate_evidence)))
        return {"pack_dir": "aggregate-pack", "pack_id": "aggregate-id", "status": "ready"}

    monkeypatch.setattr(deferred_topics, "_structured_round", fake_round)
    monkeypatch.setattr(deferred_topics, "_publish_group_candidate", fake_publish)
    monkeypatch.setattr(deferred_topics, "_build_candidate_pack", fake_aggregate)

    jobs.execute_round(task["task_id"])
    jobs.execute_round(task["task_id"])
    first = jobs.get(task["task_id"])
    assert first["state"] == "partial"
    assert first["recovery_groups"] == ["governance"]
    assert first["next_action"] == "resume_failed_groups"

    jobs.execute_round(task["task_id"])
    recovered = jobs.get(task["task_id"])
    assert calls == [("governance", False), ("capital_actions", False), ("governance", True)]
    assert recovered["state"] == "completed"
    assert recovered["group_results"]["governance"]["state"] == "completed"
    assert aggregate_calls[-1][0] == ["root-governance", "root-capital_actions"]
    assert recovered["result"]["candidate_pack_path"] == "aggregate-pack"
    assert recovered["result"]["candidate_snapshot_id"] == "aggregate-id"


def test_planned_structured_run_id_is_saved_before_execution(workspace):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    task = jobs.request_deferred_topics(
        rid, "核对延期主题", "影响候选", groups=["governance"], execute=False
    )
    current = {
        "group_id": "governance",
        "run_id": "structured-run-planned",
        "state": "planned",
    }
    deferred_topics._persist_group_run_id(jobs, task, current)
    saved = jobs.get(task["task_id"])
    assert saved["group_results"]["governance"]["run_id"] == "structured-run-planned"
