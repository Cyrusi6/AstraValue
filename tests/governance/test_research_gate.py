from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock

import pytest

from analysis.governance.codex_runner import FakeResearchTaskBroker
from analysis.governance.codex_tools import (
    CodexInputPackBuilder,
    InMemoryCodexSessionRecorder,
)
from analysis.governance.models import (
    AuthoritativeSourceCandidate,
    BudgetUsage,
    ContextualEvidenceItem,
    CompletenessStatus,
    DeferredResearchItem,
    DiscoveryLead,
    GovernancePerspective,
    ReportGenerationStatus,
    ResearchBudget,
    ResearchResultBundle,
    ResearchTaskStatus,
    SourceRole,
    TimePrecision,
    UnresolvedResearchGap,
)
from analysis.governance.research_gate import (
    InMemoryResearchStaging,
    InMemorySnapshotCatalog,
    ReingestedEvidence,
    ResearchCoordinator,
    ResearchGate,
    ResearchIntegrityError,
    SnapshotAdoptionService,
    create_research_task,
    seal_research_result_bundle,
)

from .codex_test_support import H1, H2, NOW, make_input_pack, make_snapshot, make_tool_registry


def _task(*, known_at=NOW):
    return create_research_task(
        parent_session_id="govsession:one",
        company_id="cn-600519",
        question_ids=("GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",),
        gap_ids=("govgap:one",),
        question="find the formal roster anchor",
        state_at=NOW,
        known_at=known_at,
        perspective=(
            GovernancePerspective.STRICT
            if known_at == NOW
            else GovernancePerspective.RECONSTRUCTED
        ),
        known_evidence_ids=("govspan:known",),
        allowed_source_roles=(
            SourceRole.OFFICIAL_DISCLOSURE,
            SourceRole.CONTEXTUAL_EVIDENCE,
            SourceRole.DISCOVERY_ONLY,
        ),
        budget=make_input_pack(make_snapshot(), make_tool_registry()).research_budget,
        result_schema_hash=H1,
        created_at=NOW,
    )


def _bundle(task_id: str) -> ResearchResultBundle:
    provisional = ResearchResultBundle(
        research_result_bundle_id="govresearchbundle:gate",
        research_task_id=task_id,
        task_status=ResearchTaskStatus.COMPLETED,
        budget_used=BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=2,
            wall_clock_milliseconds=50,
            output_bytes=500,
        ),
        authoritative_source_candidates=(
            AuthoritativeSourceCandidate(
                item_id="govresearchitem:formal-eligible",
                source_role=SourceRole.OFFICIAL_DISCLOSURE,
                source_locator=(
                    "https://www.cninfo.com.cn/new/disclosure/eligible"
                ),
                title="eligible title",
                available_at=NOW,
                time_precision=TimePrecision.DATETIME,
                payload_hash=H1,
            ),
            AuthoritativeSourceCandidate(
                item_id="govresearchitem:formal-future",
                source_role=SourceRole.OFFICIAL_DISCLOSURE,
                source_locator=(
                    "https://www.cninfo.com.cn/new/disclosure/future-secret"
                ),
                title="future correction secret",
                available_at=NOW + timedelta(days=1),
                time_precision=TimePrecision.DATETIME,
                payload_hash=H2,
            ),
        ),
        contextual_evidence=(
            ContextualEvidenceItem(
                item_id="govresearchitem:context-eligible",
                source_locator="source:context:eligible",
                title="old news",
                summary="eligible contextual summary",
                available_at=NOW,
                time_precision=TimePrecision.DATETIME,
                payload_hash=H1,
            ),
            ContextualEvidenceItem(
                item_id="govresearchitem:context-unproven",
                source_locator="source:context:unproven-secret",
                title="unproven title secret",
                summary="unproven future secret",
                payload_hash=H2,
            ),
        ),
        discovery_leads=(
            DiscoveryLead(
                item_id="govresearchitem:lead",
                provider="akshare",
                locator="lead:opaque",
                lead_text="find the official original",
                payload_hash=H1,
            ),
        ),
        deferred_items=(
            DeferredResearchItem(
                item_id="govresearchitem:court",
                source_family="court",
                reason_code="deferred_v1",
                payload_hash=H1,
            ),
        ),
        unresolved_gaps=(
            UnresolvedResearchGap(
                item_id="govresearchitem:gap",
                gap_id="govgap:one",
                reason_code="not_found",
                detail="one source remained unavailable",
            ),
        ),
        created_at=NOW,
        canonical_hash="0" * 64,
    )
    return seal_research_result_bundle(provisional)


class _Reingestor:
    def __init__(self) -> None:
        self.items: list[str] = []

    def reingest(self, task, candidate):
        self.items.append(candidate.item_id)
        return ReingestedEvidence(
            source_item_id=candidate.item_id,
            acquisition_run_id="shared-run:reingest",
            evidence_manifest_id="shared-manifest:reingested",
            evidence_manifest_hash=H1,
            governance_snapshot_id="govsnapshot:reingested",
            governance_snapshot_hash=H2,
        )


class _SourcePolicy:
    policy_version = "governance-official-sources-v1"

    def __init__(self) -> None:
        self.requests: list[tuple[SourceRole, str, str, int | None, str]] = []

    def allows(self, *, source_role, scheme, hostname, port, path):
        self.requests.append((source_role, scheme, hostname, port, path))
        return (
            source_role == SourceRole.OFFICIAL_DISCLOSURE
            and scheme == "https"
            and hostname == "www.cninfo.com.cn"
            and port in {None, 443}
            and path.startswith("/new/disclosure/")
        )


class _SnapshotVerifier:
    def __init__(self, fail_snapshot_id: str | None = None) -> None:
        self.fail_snapshot_id = fail_snapshot_id
        self.checked: list[str] = []

    def verify(self, snapshot) -> None:
        self.checked.append(snapshot.governance_snapshot_id)
        if snapshot.governance_snapshot_id == self.fail_snapshot_id:
            raise ResearchIntegrityError(
                "snapshot_integrity_failed",
                "snapshot or manifest integrity verification failed",
            )


def test_sanitized_receipt_future_firewall_quarantine_no_title_or_summary_leak() -> None:
    task = _task()
    bundle = _bundle(task.research_task_id)
    staging = InMemoryResearchStaging()
    reingestor = _Reingestor()
    gate = ResearchGate(
        staging,
        authoritative_reingestor=reingestor,
        authoritative_source_policy=_SourcePolicy(),
    )
    receipt = gate.process(
        task=task,
        staged_bundle_artifact_id=staging.stage(bundle),
        gated_at=NOW,
    )
    assert reingestor.items == ["govresearchitem:formal-eligible"]
    assert receipt.quarantine_count == 3
    assert receipt.deferred_count == 1
    assert len(receipt.discovery_lead_ids) == 1
    assert receipt.discovery_lead_ids[0].startswith("research-discovery:")
    assert receipt.unresolved_gap_ids == ("govgap:one",)
    public = receipt.canonical_bytes().decode("utf-8")
    assert "future correction" not in public
    assert "unproven title" not in public
    assert "future-secret" not in public
    assert "summary" not in public
    for child_identity in (
        "govresearchbundle:gate",
        "govresearchitem:formal-eligible",
        "govresearchitem:formal-future",
        "govresearchitem:context-eligible",
        "govresearchitem:context-unproven",
        "govresearchitem:lead",
        "govresearchitem:court",
    ):
        assert child_identity not in public
    assert receipt.research_result_bundle_id.startswith("govresearchbundle:")
    assert receipt.research_result_bundle_id != bundle.research_result_bundle_id
    assert receipt.reingested_evidence[0].source_item_id.startswith(
        "govresearchitem:"
    )
    assert (
        receipt.reingested_evidence[0].source_item_id
        != "govresearchitem:formal-eligible"
    )
    for record in gate.quarantine_records:
        assert set(record.model_dump()).isdisjoint(
            {"title", "summary", "url", "value", "conclusion"}
        )
        record_bytes = record.canonical_bytes().decode("utf-8")
        assert "govresearchbundle:gate" not in record_bytes
        assert "formal-future" not in record_bytes
        assert "context-unproven" not in record_bytes
        assert "govresearchitem:court" not in record_bytes


def test_A19_classification_contextual_time_discovery_only_authoritative_reingest() -> None:
    task = _task()
    staging = InMemoryResearchStaging()
    gate = ResearchGate(
        staging,
        authoritative_reingestor=_Reingestor(),
        authoritative_source_policy=_SourcePolicy(),
    )
    receipt = gate.process(
        task=task,
        staged_bundle_artifact_id=staging.stage(_bundle(task.research_task_id)),
        gated_at=NOW,
    )
    assert receipt.reingested_evidence[0].acquisition_run_id.startswith("shared-run:")
    assert len(receipt.eligible_contextual_artifact_ids) == 1
    assert receipt.eligible_contextual_artifact_ids[0].startswith(
        "research-contextual:"
    )
    assert "context-eligible" not in receipt.eligible_contextual_artifact_ids[0]
    assert dict(receipt.quarantine_reason_counts) == {
        "available_at_unproven": 1,
        "deferred_source": 1,
        "future_information": 1,
    }


def _formal_only_bundle(task_id: str, locator: str) -> ResearchResultBundle:
    base = _bundle(task_id)
    candidate = base.authoritative_source_candidates[0].model_copy(
        update={"source_locator": locator}
    )
    return seal_research_result_bundle(
        base.model_copy(
            update={
                "authoritative_source_candidates": (candidate,),
                "contextual_evidence": (),
                "discovery_leads": (),
                "deferred_items": (),
                "unresolved_gaps": (),
                "canonical_hash": "0" * 64,
            }
        )
    )


@pytest.mark.parametrize(
    "locator",
    (
        "file:///etc/passwd",
        "https://reader:secret@www.cninfo.com.cn/new/disclosure/eligible",
        "http://localhost/new/disclosure/eligible",
        "http://sub.localhost/new/disclosure/eligible",
        "http://127.0.0.1/new/disclosure/eligible",
        "http://127.1/new/disclosure/eligible",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.2/new/disclosure/eligible",
        "http://240.0.0.1/new/disclosure/eligible",
        "https://evil.example/new/disclosure/eligible",
        "https://www.cninfo.com.cn/unregistered/path",
    ),
)
def test_authoritative_locator_must_be_public_http_and_registered(locator: str) -> None:
    task = _task()
    staging = InMemoryResearchStaging()
    reingestor = _Reingestor()
    gate = ResearchGate(
        staging,
        authoritative_reingestor=reingestor,
        authoritative_source_policy=_SourcePolicy(),
    )
    receipt = gate.process(
        task=task,
        staged_bundle_artifact_id=staging.stage(
            _formal_only_bundle(task.research_task_id, locator)
        ),
        gated_at=NOW,
    )
    assert reingestor.items == []
    assert receipt.reingested_evidence == ()
    assert dict(receipt.quarantine_reason_counts) == {"source_policy": 1}


def test_authoritative_reingestion_fails_closed_without_versioned_policy() -> None:
    task = _task()
    staging = InMemoryResearchStaging()
    reingestor = _Reingestor()
    gate = ResearchGate(staging, authoritative_reingestor=reingestor)
    receipt = gate.process(
        task=task,
        staged_bundle_artifact_id=staging.stage(
            _formal_only_bundle(
                task.research_task_id,
                "https://www.cninfo.com.cn/new/disclosure/eligible",
            )
        ),
        gated_at=NOW,
    )
    assert reingestor.items == []
    assert receipt.reingested_evidence == ()
    assert dict(receipt.quarantine_reason_counts) == {"source_policy": 1}


def test_unresolved_gap_must_be_bound_to_parent_task_before_side_effects() -> None:
    task = _task()
    base = _formal_only_bundle(
        task.research_task_id,
        "https://www.cninfo.com.cn/new/disclosure/eligible",
    )
    foreign_gap = UnresolvedResearchGap(
        item_id="govresearchitem:foreign-gap",
        gap_id="govgap:foreign",
        reason_code="not_found",
        detail="foreign task gap",
    )
    bundle = seal_research_result_bundle(
        base.model_copy(
            update={"unresolved_gaps": (foreign_gap,), "canonical_hash": "0" * 64}
        )
    )
    staging = InMemoryResearchStaging()
    reingestor = _Reingestor()
    gate = ResearchGate(
        staging,
        authoritative_reingestor=reingestor,
        authoritative_source_policy=_SourcePolicy(),
    )
    with pytest.raises(ResearchIntegrityError) as mismatch:
        gate.process(
            task=task,
            staged_bundle_artifact_id=staging.stage(bundle),
            gated_at=NOW,
        )
    assert mismatch.value.code == "gap_binding_mismatch"
    assert reingestor.items == []
    assert gate.quarantine_records == ()


def test_bundle_schema_hash_and_task_binding_fail_closed() -> None:
    task = _task()
    bundle = _bundle(task.research_task_id).model_copy(
        update={"canonical_hash": "f" * 64}
    )
    staging = InMemoryResearchStaging()
    gate = ResearchGate(staging)
    with pytest.raises(ResearchIntegrityError) as mismatch:
        gate.process(
            task=task,
            staged_bundle_artifact_id=staging.stage(bundle),
            gated_at=NOW,
        )
    assert mismatch.value.code == "bundle_hash_mismatch"


def test_A18_minimal_payload_task_contract_recursion_has_no_parent_transcript_or_urls() -> None:
    task = _task()
    payload = task.model_dump()
    assert task.recursion_depth == 1
    assert set(payload).isdisjoint(
        {"parent_transcript", "browser_profile", "database_path", "urls", "fulltext"}
    )
    assert payload["known_evidence_ids"] == ("govspan:known",)


def test_output_limit_contract_violation_fails_closed() -> None:
    task = _task()
    bundle = _bundle(task.research_task_id).model_copy(
        update={
            "budget_used": BudgetUsage(
                rounds=1,
                child_tasks=1,
                network_requests=1,
                wall_clock_milliseconds=10,
                output_bytes=task.budget.max_output_bytes + 1,
            ),
            "canonical_hash": "0" * 64,
        }
    )
    bundle = seal_research_result_bundle(bundle)
    staging = InMemoryResearchStaging()
    with pytest.raises(ResearchIntegrityError) as violation:
        ResearchGate(staging).process(
            task=task,
            staged_bundle_artifact_id=staging.stage(bundle),
            gated_at=NOW,
        )
    assert violation.value.code == "budget_contract_violation"


def _budget_exhausted_bundle(
    task_id: str,
    *,
    reason: str,
    usage: BudgetUsage,
) -> ResearchResultBundle:
    return seal_research_result_bundle(
        ResearchResultBundle(
            research_result_bundle_id="govresearchbundle:exhausted",
            research_task_id=task_id,
            task_status=ResearchTaskStatus.BUDGET_EXHAUSTED,
            budget_used=usage,
            failure_reason=reason,
            created_at=NOW,
            canonical_hash="0" * 64,
        )
    )


@pytest.mark.parametrize(
    ("reason", "usage"),
    (
        (
            "unknown_budget_reason",
            BudgetUsage(
                rounds=0,
                child_tasks=0,
                network_requests=0,
                wall_clock_milliseconds=0,
                output_bytes=0,
            ),
        ),
        (
            "budget_unavailable",
            BudgetUsage(
                rounds=0,
                child_tasks=0,
                network_requests=0,
                wall_clock_milliseconds=0,
                output_bytes=0,
            ),
        ),
        (
            "wall_clock_budget",
            BudgetUsage(
                rounds=1,
                child_tasks=1,
                network_requests=0,
                wall_clock_milliseconds=10,
                output_bytes=0,
            ),
        ),
        (
            "network_request_budget",
            BudgetUsage(
                rounds=1,
                child_tasks=1,
                network_requests=4,
                wall_clock_milliseconds=10,
                output_bytes=0,
            ),
        ),
    ),
)
def test_budget_exhausted_reason_must_match_observed_usage(
    reason: str,
    usage: BudgetUsage,
) -> None:
    task = _task()
    staging = InMemoryResearchStaging()
    with pytest.raises(ResearchIntegrityError) as violation:
        ResearchGate(staging).process(
            task=task,
            staged_bundle_artifact_id=staging.stage(
                _budget_exhausted_bundle(
                    task.research_task_id,
                    reason=reason,
                    usage=usage,
                )
            ),
            gated_at=NOW,
        )
    assert violation.value.code == "budget_contract_violation"


def test_budget_exhausted_allows_only_the_reason_specific_observer_overrun() -> None:
    task = _task()
    bundle = _budget_exhausted_bundle(
        task.research_task_id,
        reason="network_request_budget",
        usage=BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=task.budget.max_network_requests + 1,
            wall_clock_milliseconds=10,
            output_bytes=100,
        ),
    )
    staging = InMemoryResearchStaging()
    receipt = ResearchGate(staging).process(
        task=task,
        staged_bundle_artifact_id=staging.stage(bundle),
        gated_at=NOW,
    )
    assert receipt.budget_exhausted
    assert receipt.unresolved_gap_ids == task.gap_ids


def test_A20_budget_exhaustion_is_soft_and_coordinator_records_result() -> None:
    snapshot = make_snapshot(
        completeness=CompletenessStatus.INCOMPLETE,
        gap_ids=("govgap:one",),
    )
    registry = make_tool_registry()
    pack = make_input_pack(snapshot, registry)
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=pack,
        started_at=NOW,
    )

    def result(task):
        return _bundle(task.research_task_id).model_copy(
            update={
                "authoritative_source_candidates": (),
                "contextual_evidence": (),
                "discovery_leads": (),
                "deferred_items": (),
            }
        ).model_copy(update={"canonical_hash": "0" * 64})

    # Reseal after the structural simplification performed above.
    def sealed_result(task):
        bundle = result(task).model_copy(
            update={
                "research_result_bundle_id": (
                    "govresearchbundle:" + task.research_task_id.split(":", 1)[1]
                ),
                "canonical_hash": "0" * 64,
            }
        )
        return seal_research_result_bundle(bundle)

    broker = FakeResearchTaskBroker(sealed_result)
    staging = InMemoryResearchStaging()
    coordinator = ResearchCoordinator(
        broker=broker,
        staging=staging,
        gate=ResearchGate(staging),
        recorder=recorder,
        result_schema_hash=H1,
    )
    for index in range(2):
        receipt = coordinator.request(
            session_id="govsession:one",
            question_ids=("GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",),
            gap_ids=("govgap:one",),
            question=f"attempt {index}",
            allowed_source_roles=(SourceRole.OFFICIAL_DISCLOSURE,),
            requested_at=NOW + timedelta(seconds=index),
        )
        assert not receipt.budget_exhausted
    exhausted = coordinator.request(
        session_id="govsession:one",
        question_ids=("GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",),
        gap_ids=("govgap:one",),
        question="third attempt",
        allowed_source_roles=(SourceRole.OFFICIAL_DISCLOSURE,),
        requested_at=NOW + timedelta(seconds=3),
    )
    assert exhausted.task_status == ResearchTaskStatus.BUDGET_EXHAUSTED
    assert exhausted.unresolved_gap_ids == ("govgap:one",)
    assert len(broker.submitted) == 2
    assert len(recorder.manifest("govsession:one").research_task_ids) == 3


def _input_pack_with_budget(snapshot, budget: ResearchBudget):
    registry = make_tool_registry()
    return CodexInputPackBuilder(registry).build(
        snapshot=snapshot,
        question_descriptions={
            item.question_id: f"{item.question_id} compact status"
            for item in snapshot.question_level_coverage
        },
        important_records=(),
        important_event_ids=(),
        research_budget=budget,
        report_output_schema_hash=H1,
        created_at=NOW,
    )


class _BlockingBroker:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()
        self.submitted = []

    def submit(self, task):
        with self._lock:
            self.submitted.append(task)
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test broker was not released")
        base = _bundle(task.research_task_id)
        return seal_research_result_bundle(
            base.model_copy(
                update={
                    "research_result_bundle_id": (
                        "govresearchbundle:"
                        + task.research_task_id.split(":", 1)[1]
                    ),
                    "budget_used": BudgetUsage(
                        rounds=1,
                        child_tasks=1,
                        network_requests=0,
                        wall_clock_milliseconds=10,
                        output_bytes=100,
                    ),
                    "authoritative_source_candidates": (),
                    "contextual_evidence": (),
                    "discovery_leads": (),
                    "deferred_items": (),
                    "canonical_hash": "0" * 64,
                }
            )
        )


def test_coordinator_atomically_reserves_session_budget_before_broker_submit() -> None:
    snapshot = make_snapshot(
        completeness=CompletenessStatus.INCOMPLETE,
        gap_ids=("govgap:one",),
    )
    pack = _input_pack_with_budget(
        snapshot,
        ResearchBudget(
            max_rounds=1,
            max_child_tasks=1,
            max_network_requests=4,
            max_parallelism=1,
            wall_clock_seconds=30,
            max_output_bytes=100_000,
        ),
    )
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=pack,
        started_at=NOW,
    )
    broker = _BlockingBroker()
    staging = InMemoryResearchStaging()
    coordinator = ResearchCoordinator(
        broker=broker,
        staging=staging,
        gate=ResearchGate(staging),
        recorder=recorder,
        result_schema_hash=H1,
    )

    def request(index: int):
        return coordinator.request(
            session_id="govsession:one",
            question_ids=("GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",),
            gap_ids=("govgap:one",),
            question=f"parallel attempt {index}",
            allowed_source_roles=(SourceRole.OFFICIAL_DISCLOSURE,),
            requested_at=NOW + timedelta(seconds=index),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(request, 1)
        assert broker.entered.wait(timeout=2)
        second = executor.submit(request, 2)
        exhausted = second.result(timeout=2)
        broker.release.set()
        completed = first.result(timeout=2)

    assert completed.task_status == ResearchTaskStatus.COMPLETED
    assert exhausted.task_status == ResearchTaskStatus.BUDGET_EXHAUSTED
    assert len(broker.submitted) == 1
    assert len(recorder.manifest("govsession:one").research_task_ids) == 2


def test_coordinator_rejects_research_after_session_finalization() -> None:
    snapshot = make_snapshot(
        completeness=CompletenessStatus.INCOMPLETE,
        gap_ids=("govgap:one",),
    )
    pack = make_input_pack(snapshot, make_tool_registry())
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=pack,
        started_at=NOW,
    )
    recorder.finalize_session(
        "govsession:one",
        generation_status=ReportGenerationStatus.COMPLETED,
        final_citation_ids=(),
        report_hash=H2,
        output_schema_validated=True,
        failure_code=None,
        completed_at=NOW,
    )
    broker = FakeResearchTaskBroker(lambda task: _bundle(task.research_task_id))
    staging = InMemoryResearchStaging()
    coordinator = ResearchCoordinator(
        broker=broker,
        staging=staging,
        gate=ResearchGate(staging),
        recorder=recorder,
        result_schema_hash=H1,
    )
    with pytest.raises(ResearchIntegrityError) as finalized:
        coordinator.request(
            session_id="govsession:one",
            question_ids=("GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",),
            gap_ids=("govgap:one",),
            question="too late",
            allowed_source_roles=(SourceRole.OFFICIAL_DISCLOSURE,),
            requested_at=NOW + timedelta(seconds=1),
        )
    assert finalized.value.code == "session_finalized"
    assert broker.submitted == []


def test_adopt_snapshot_is_explicit_revision_checked_and_non_mutating() -> None:
    old = make_snapshot(snapshot_id="govsnapshot:old", snapshot_hash=H1)
    new = make_snapshot(
        snapshot_id="govsnapshot:new",
        snapshot_hash=H2,
        supersedes_snapshot_id=old.governance_snapshot_id,
    )
    registry = make_tool_registry()
    pack = make_input_pack(old, registry)
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=pack,
        started_at=NOW,
    )
    before_hash = recorder.input_pack("govsession:one").canonical_hash
    verifier = _SnapshotVerifier()
    service = SnapshotAdoptionService(
        InMemorySnapshotCatalog((old, new)),
        recorder,
        verifier,
    )
    adoption = service.adopt_snapshot(
        session_id="govsession:one",
        new_snapshot_id=new.governance_snapshot_id,
        expected_revision=0,
        adopted_at_sequence=1,
        adopted_at=NOW,
    )
    assert recorder.current_binding("govsession:one") == (
        new.governance_snapshot_id,
        H2,
        1,
    )
    assert recorder.input_pack("govsession:one").canonical_hash == before_hash
    assert adoption.old_snapshot_id == old.governance_snapshot_id
    assert verifier.checked == [
        old.governance_snapshot_id,
        new.governance_snapshot_id,
    ]
    with pytest.raises(Exception) as conflict:
        service.adopt_snapshot(
            session_id="govsession:one",
            new_snapshot_id=old.governance_snapshot_id,
            expected_revision=0,
            adopted_at_sequence=2,
            adopted_at=NOW,
        )
    assert getattr(conflict.value, "code", None) == "revision_conflict"


def test_snapshot_adoption_requires_exact_lineage_and_full_integrity_check() -> None:
    old = make_snapshot(snapshot_id="govsnapshot:old", snapshot_hash=H1)
    no_lineage = make_snapshot(snapshot_id="govsnapshot:no-lineage", snapshot_hash=H2)
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=make_input_pack(old, make_tool_registry()),
        started_at=NOW,
    )
    verifier = _SnapshotVerifier()
    service = SnapshotAdoptionService(
        InMemorySnapshotCatalog((old, no_lineage)),
        recorder,
        verifier,
    )
    with pytest.raises(ResearchIntegrityError) as lineage:
        service.adopt_snapshot(
            session_id="govsession:one",
            new_snapshot_id=no_lineage.governance_snapshot_id,
            expected_revision=0,
            adopted_at_sequence=1,
            adopted_at=NOW,
        )
    assert lineage.value.code == "snapshot_lineage_mismatch"
    assert verifier.checked == [
        old.governance_snapshot_id,
        no_lineage.governance_snapshot_id,
    ]
    assert recorder.current_binding("govsession:one") == (
        old.governance_snapshot_id,
        H1,
        0,
    )


def test_snapshot_adoption_fails_before_mutation_when_integrity_verifier_rejects() -> None:
    old = make_snapshot(snapshot_id="govsnapshot:old", snapshot_hash=H1)
    new = make_snapshot(
        snapshot_id="govsnapshot:new",
        snapshot_hash=H2,
        supersedes_snapshot_id=old.governance_snapshot_id,
    )
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=make_input_pack(old, make_tool_registry()),
        started_at=NOW,
    )
    verifier = _SnapshotVerifier(fail_snapshot_id=new.governance_snapshot_id)
    service = SnapshotAdoptionService(
        InMemorySnapshotCatalog((old, new)),
        recorder,
        verifier,
    )
    with pytest.raises(ResearchIntegrityError) as integrity:
        service.adopt_snapshot(
            session_id="govsession:one",
            new_snapshot_id=new.governance_snapshot_id,
            expected_revision=0,
            adopted_at_sequence=1,
            adopted_at=NOW,
        )
    assert integrity.value.code == "snapshot_integrity_failed"
    assert verifier.checked == [
        old.governance_snapshot_id,
        new.governance_snapshot_id,
    ]
    assert recorder.current_binding("govsession:one") == (
        old.governance_snapshot_id,
        H1,
        0,
    )
