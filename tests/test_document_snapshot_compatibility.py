from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import pytest

from analysis.acquisition.snapshots import ContentAddressedBlobStore, SnapshotService
from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionRun,
    AttemptKind,
    DiscoveredResource,
    DiscoveryObservation,
    DiscoveryProof,
    PhysicalQueryPlanItem,
    PublishedAtPrecision,
    SourceDefinitionRef,
    StorageNamespace,
)
from analysis.acquisition.repository import AcquisitionRepository
from analysis.acquisition.registry import (
    DEFAULT_REGISTRY_PATH,
    SourceRegistryError,
    SourceRegistryLoader,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.documents import ingest_document, ingest_downloaded_document
from analysis.documents import SourceReviewRequired, ingest_registered_document
from analysis.models import DocumentIngestRequest, DocumentRecord, SourceRecord


PUBLISHED_AT = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


class _DocumentRepository:
    def __init__(self) -> None:
        self.blobs = {}
        self.snapshots = {}
        self.observations = []
        self.derived_artifacts = {}
        self.events: list[str] = []

    def commit_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.events.append("snapshot_committed")
        self.blobs.setdefault(blob.content_blob_id, blob)
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.observations.append(observation)

    def save_resource_observation(self, observation, **kwargs):
        self.events.append("observation_appended")
        self.observations.append(observation)

    def find_raw_resource_snapshot(self, **filters):
        matches = [
            snapshot
            for snapshot in self.snapshots.values()
            if all(
                getattr(snapshot, field) == value
                for field, value in filters.items()
                if value is not None
            )
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    def save_derived_artifact(self, artifact):
        self.events.append("derived_committed")
        existing = self.derived_artifacts.get(artifact.derived_artifact_id)
        if existing is not None:
            assert existing == artifact
        self.derived_artifacts[artifact.derived_artifact_id] = artifact


def _pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    result = document.tobytes()
    document.close()
    return result


def _service(tmp_path: Path) -> tuple[SnapshotService, _DocumentRepository]:
    repository = _DocumentRepository()
    service = SnapshotService(
        ContentAddressedBlobStore(tmp_path / "data", "namespace-documents"),
        repository,
    )
    return service, repository


def _runtime_with_derived_text_allowed(tmp_path: Path) -> AcquisitionRuntime:
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["registry_version"] = "1.1.0"
    definition = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "cninfo.disclosures"
    )
    definition["version"] = "1.1.0"
    definition["license_policy"]["save_derived_text"] = "allowed"
    definition["license_policy"]["llm_processing"] = "allowed"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return AcquisitionRuntime.create(
        tmp_path / "runtime.db",
        tmp_path / "runtime-data",
        registry_path=registry_path,
        orchestrator_factory=lambda _: None,
    )


def _download_kwargs(service: SnapshotService, content: bytes) -> dict:
    return {
        "ticker": "600519",
        "content": content,
        "title": "贵州茅台年度报告",
        "source_name": "巨潮资讯",
        "source_url": "https://static.cninfo.com.cn/finalpage/report.pdf",
        "published_at": PUBLISHED_AT,
        "provider": "cninfo",
        "announcement_id": "announcement-600519-2025",
        "snapshot_service": service,
        "source_definition_id": "cninfo.disclosures",
        "source_definition_version": "1.0.0",
        "canonical_resource_id": "announcement-600519-2025",
        "upstream_material_id": "announcement-600519-2025",
    }


def test_download_freezes_snapshot_before_parse_and_derived_commit(monkeypatch, tmp_path):
    service, repository = _service(tmp_path)
    content = _pdf_bytes("AstraValue snapshot before parsing verification text")

    from analysis import documents

    real_extract = documents._extract_text

    def observed_extract(path, source_suffix=None):
        assert repository.events == ["snapshot_committed"]
        assert path.is_file()
        repository.events.append("parsed")
        return real_extract(path, source_suffix)

    monkeypatch.setattr(documents, "_extract_text", observed_extract)
    record = ingest_downloaded_document(**_download_kwargs(service, content))

    assert repository.events == ["snapshot_committed", "parsed", "derived_committed"]
    assert record.raw_resource_snapshot_id in repository.snapshots
    assert record.derived_artifact_id in repository.derived_artifacts
    assert Path(record.archived_path).read_bytes() == content


def test_repeated_bytes_are_stable_and_changed_url_versions_supersede(tmp_path):
    service, repository = _service(tmp_path)
    first_content = _pdf_bytes("AstraValue immutable first document version text")
    first = ingest_downloaded_document(
        **_download_kwargs(service, first_content)
    )
    first_json = first.model_dump_json()
    repeated = ingest_downloaded_document(
        **_download_kwargs(service, first_content)
    )

    assert repeated.model_dump_json() == first_json
    assert len(repository.snapshots) == 1
    assert len(repository.observations) == 2

    changed = ingest_downloaded_document(
        **_download_kwargs(
            service,
            _pdf_bytes("AstraValue changed second document version text"),
        )
    )

    assert changed.document_version == 2
    assert changed.supersedes_document_id == first.document_id
    assert changed.raw_resource_snapshot_id != first.raw_resource_snapshot_id
    assert changed.archived_path != first.archived_path
    assert first.model_dump_json() == first_json
    assert Path(first.archived_path).is_file()
    assert Path(first.text_path).is_file()


def test_old_document_json_loads_with_snapshot_fields_defaulted():
    legacy = DocumentRecord.model_validate(
        {
            "document_id": "doc-legacy",
            "ticker": "600519",
            "title": "legacy",
            "archived_path": "var/raw/legacy.pdf",
            "text_path": "var/raw/legacy.pdf.txt",
            "sha256": "a" * 64,
            "source": {
                "source_id": "source-legacy",
                "name": "legacy source",
            },
        }
    )

    assert legacy.raw_resource_snapshot_id is None
    assert legacy.derived_artifact_id is None
    assert legacy.supersedes_document_id is None
    assert legacy.document_version == 1
    assert legacy.source.source_definition_id is None
    assert legacy.source.raw_resource_snapshot_id is None
    assert SourceRecord.model_validate(legacy.source.model_dump()).canonical_resource_id is None


def test_snapshot_backed_manual_ingest_stays_under_injected_data_root(tmp_path):
    service, _ = _service(tmp_path)
    source_path = tmp_path / "manual.txt"
    source_path.write_text("manual official investor relations material", encoding="utf-8")

    record = ingest_document(
        DocumentIngestRequest(
            ticker="600519",
            path=str(source_path),
            title="投资者关系材料",
            source_name="贵州茅台投资者关系网站",
            source_url="https://www.moutaichina.com/investors/manual.txt",
            published_at=PUBLISHED_AT,
        ),
        snapshot_service=service,
        source_definition_id="moutai.ir",
        source_definition_version="1.0.0",
        canonical_resource_id="moutai-ir-manual-1",
    )

    data_root = service.blob_store.data_root
    Path(record.archived_path).resolve().relative_to(data_root)
    Path(record.text_path).resolve().relative_to(data_root)
    assert not (tmp_path / "raw" / "600519").exists()


def test_snapshot_backed_ingest_integrates_with_sqlite_repository(tmp_path):
    data_root = tmp_path / "data"
    repository = AcquisitionRepository(
        tmp_path / "analysis.db",
        initialize=True,
        data_root=data_root,
    )
    namespace = StorageNamespace(
        namespace_id="namespace-integration",
        binding_nonce="binding-integration",
        layout_version=1,
        database_identity_hash="a" * 64,
        data_root_identity_hash="b" * 64,
    )
    repository.save_storage_namespace(namespace)
    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    loaded_registry = loader.load_registry(question_set=questions)
    repository.save_source_registry_version(loaded_registry.registry)
    cninfo_definition = loaded_registry.definition("cninfo.disclosures")
    now = datetime.now(timezone.utc)
    run = AcquisitionRun(
        run_id="run-document-integration",
        ticker="600519",
        company_name="贵州茅台",
        mode="baseline",
        as_of=now,
        registry_id=loaded_registry.registry.registry_id,
        registry_version=loaded_registry.registry.registry_version,
        registry_content_hash=loaded_registry.content_hash,
        question_set_id=questions.question_set.question_set_id,
        question_set_version=questions.question_set.version,
        question_set_content_hash=questions.content_hash,
        source_definition_refs=(
            SourceDefinitionRef(
                source_definition_id="cninfo.disclosures",
                version="1.0.0",
                content_hash=loaded_registry.source_definition_hashes[
                    (cninfo_definition.source_definition_id, cninfo_definition.version)
                ],
            ),
        ),
        storage_namespace_id=namespace.namespace_id,
    )
    discovery_plan = PhysicalQueryPlanItem(
        plan_item_id="plan-discovery-integration",
        run_id=run.run_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        query_id="cninfo.periodic_report",
        query_family="periodic_report",
        execution_key="cninfo.periodic_report:600519",
        attempt_kind=AttemptKind.DISCOVERY,
        request_method="GET",
        endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        partition_key="periodic_report",
        pagination_fingerprint="page-number-v1",
        ordinal=0,
        time_start=now - timedelta(days=1),
        time_end=now,
    )
    repository.save_plan_bundle(run, (discovery_plan,), (), ())
    lease, owner_token = repository.claim_lease(run.run_id, ttl_seconds=600)
    discovery_attempt = AcquisitionAttempt(
        attempt_id="attempt-discovery-integration",
        run_id=run.run_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        physical_query_plan_item_id=discovery_plan.plan_item_id,
        execution_key=discovery_plan.execution_key,
        attempt_kind=AttemptKind.DISCOVERY,
        query_id=discovery_plan.query_id,
        work_position="page:1",
        retry_group_id="retry-discovery-integration",
        lease_epoch=lease.lease_epoch,
        started_at=now,
    )
    repository.save_attempt(discovery_attempt, owner_token=owner_token)

    discovery_body = b'{"items":[{"id":"announcement-600519-2025"}]}'
    discovery_hash = hashlib.sha256(discovery_body).hexdigest()
    discovery_observation = DiscoveryObservation(
        observation_id="observation-discovery-integration",
        attempt_id=discovery_attempt.attempt_id,
        physical_query_plan_item_id=discovery_plan.plan_item_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        page_number=1,
        observed_at=now,
        retrieved_at=now,
        http_status=200,
        mime_type="application/json",
        response_sha256=discovery_hash,
        response_byte_length=len(discovery_body),
    )
    proof = DiscoveryProof(
        proof_id="proof-discovery-integration",
        observation_id=discovery_observation.observation_id,
        attempt_id=discovery_attempt.attempt_id,
        physical_query_plan_item_id=discovery_plan.plan_item_id,
        response_sha256=discovery_hash,
        response_byte_length=len(discovery_body),
        http_status=200,
        mime_type="application/json",
        parser_id="fixture-parser",
        parser_version="1.0.0",
        schema_id="fixture-schema",
        schema_version="1.0.0",
        schema_valid=True,
        page_number=1,
        declared_total=1,
        declared_page_count=1,
        normalized_row_count=1,
        terminal=True,
        body_retained=False,
        replayable=False,
        created_at=now,
    )
    resource = DiscoveredResource(
        discovered_resource_id="resource-integration",
        proof_id=proof.proof_id,
        discovery_observation_id=discovery_observation.observation_id,
        discovery_attempt_id=discovery_attempt.attempt_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="announcement-600519-2025",
        upstream_material_id="announcement-600519-2025",
        resource_url="https://static.cninfo.com.cn/finalpage/report.pdf",
        title="贵州茅台年度报告",
        published_at_raw=PUBLISHED_AT.isoformat(),
        published_at=PUBLISHED_AT,
        published_at_precision=PublishedAtPrecision.INSTANT,
        source_timezone="Asia/Shanghai",
        page_number=1,
        row_locator="$.items[0]",
        row_hash="f" * 64,
        required_fetch=True,
        expected_mime_types=("application/pdf",),
    )
    repository.commit_discovery_bundle(
        discovery_observation,
        proof,
        (resource,),
        owner_token=owner_token,
        lease_epoch=lease.lease_epoch,
    )

    fetch_plan = PhysicalQueryPlanItem(
        plan_item_id="plan-fetch-integration",
        run_id=run.run_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        query_id="cninfo.fetch_document",
        query_family="document_fetch",
        execution_key="cninfo.fetch:announcement-600519-2025",
        attempt_kind=AttemptKind.FETCH,
        request_method="GET",
        endpoint=resource.resource_url,
        partition_key="periodic_report",
        pagination_fingerprint="single-resource-v1",
        ordinal=1,
        parent_plan_item_id=discovery_plan.plan_item_id,
        discovered_resource_id=resource.discovered_resource_id,
        time_start=now - timedelta(days=1),
        time_end=now,
    )
    repository.save_physical_query_plan_item(fetch_plan)
    fetch_attempt = AcquisitionAttempt(
        attempt_id="attempt-fetch-integration",
        run_id=run.run_id,
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        physical_query_plan_item_id=fetch_plan.plan_item_id,
        execution_key=fetch_plan.execution_key,
        attempt_kind=AttemptKind.FETCH,
        discovered_resource_id=resource.discovered_resource_id,
        parent_discovery_attempt_id=discovery_attempt.attempt_id,
        work_position="resource:announcement-600519-2025",
        retry_group_id="retry-fetch-integration",
        lease_epoch=lease.lease_epoch,
        started_at=now,
    )
    repository.save_attempt(fetch_attempt, owner_token=owner_token)

    service = SnapshotService(
        ContentAddressedBlobStore(data_root, namespace.namespace_id), repository
    )
    content = _pdf_bytes("AstraValue SQLite repository integration verification text")
    kwargs = _download_kwargs(service, content)
    kwargs.update(
        attempt_id=fetch_attempt.attempt_id,
        discovered_resource_id=resource.discovered_resource_id,
        parent_discovery_attempt_id=discovery_attempt.attempt_id,
    )
    record = ingest_downloaded_document(**kwargs)

    assert repository.get_raw_resource_snapshot(
        record.raw_resource_snapshot_id
    ).snapshot_id == record.raw_resource_snapshot_id
    assert repository.get_derived_artifact(
        record.derived_artifact_id
    ).parent_snapshot_id == record.raw_resource_snapshot_id
    assert len(
        repository.list_resource_observations(snapshot_id=record.raw_resource_snapshot_id)
    ) == 1


def test_registered_manual_ingest_uses_alias_audit_chain_and_is_unchanged(tmp_path):
    runtime = _runtime_with_derived_text_allowed(tmp_path)
    source_path = tmp_path / "registered.txt"
    source_path.write_text(
        "registered business model evidence with enough deterministic text",
        encoding="utf-8",
    )
    request = DocumentIngestRequest(
        ticker="600519",
        path=str(source_path),
        title="已注册来源手工材料",
        source_name="用户自由填写的来源名",
        source_url="https://static.cninfo.com.cn/manual/registered.txt",
    )

    first = ingest_registered_document(
        request,
        runtime,
        source_definition_id="official",
    )
    repeated = ingest_registered_document(
        request.model_copy(
            update={
                "source_definition_id": "cninfo.disclosures",
                "source_definition_version": "1.1.0",
            }
        ),
        runtime,
    )

    assert repeated.model_dump_json() == first.model_dump_json()
    assert first.source.name == runtime.source_definition(
        "cninfo.disclosures"
    ).display_name
    assert first.source.name != request.source_name
    Path(first.archived_path).resolve().relative_to(runtime.data_root)
    Path(first.text_path).resolve().relative_to(runtime.data_root)
    assert len(
        runtime.repository.list_resource_observations(
            snapshot_id=first.raw_resource_snapshot_id
        )
    ) == 2
    runs = runtime.repository.list_runs(run_kind="ad_hoc")
    assert len(runs) == 2
    outcomes = []
    for run in runs:
        assert any(
            event.event_type.value == "finalized"
            for event in runtime.repository.list_run_events(run.run_id)
        )
        for attempt in runtime.repository.list_attempts(run_id=run.run_id):
            outcomes.extend(
                event.outcome.value
                for event in runtime.repository.list_attempt_events(attempt.attempt_id)
                if event.outcome is not None
            )
    assert outcomes.count("success") == 3
    assert outcomes.count("unchanged") == 1
    manifest = runtime.manifest_service.build_evidence_manifest(
        run_id=runs[0].run_id,
        question_set_version=runtime.loaded_questions.question_set.version,
        registry_id=runtime.loaded_registry.registry.registry_id,
        registry_version=runtime.loaded_registry.registry.registry_version,
        as_of=datetime.now(timezone.utc),
        snapshot_ids=(first.raw_resource_snapshot_id,),
        derived_artifact_ids=(first.derived_artifact_id,),
        coverage_summary={"request_scope": "ad_hoc", "coverage_accounted": True},
    )
    assert runtime.repository.get_evidence_manifest(manifest.manifest_id) == manifest
    assert runtime.manifest_service.resolve_llm_materials(manifest) == (
        source_path.read_bytes(),
    )


def test_unregistered_manual_url_creates_one_redacted_candidate_and_no_evidence(tmp_path):
    runtime = AcquisitionRuntime.create(
        tmp_path / "runtime.db",
        tmp_path / "runtime-data",
        orchestrator_factory=lambda _: None,
    )
    source_path = tmp_path / "candidate.txt"
    source_path.write_text("must not become formal evidence", encoding="utf-8")
    request = DocumentIngestRequest(
        ticker="600519",
        path=str(source_path),
        title="未知来源",
        source_name="unknown",
        source_url="https://new-ir.example.test/report?access_token=do-not-store",
    )

    with pytest.raises(SourceReviewRequired) as first_error:
        ingest_registered_document(request, runtime)
    with pytest.raises(SourceReviewRequired) as repeated_error:
        ingest_registered_document(request, runtime)

    assert repeated_error.value.candidate_id == first_error.value.candidate_id
    candidate = runtime.repository.get_source_candidate(first_error.value.candidate_id)
    assert candidate.status.value == "pending_review"
    assert "do-not-store" not in candidate.model_dump_json()
    assert len(runtime.repository.list_source_candidates()) == 1
    assert runtime.repository.list_runs() == []
    assert runtime.blob_store.scan_orphans(()) == ()


def test_registered_manual_ingest_fails_closed_when_derived_text_policy_is_pending(
    tmp_path,
):
    runtime = AcquisitionRuntime.create(
        tmp_path / "runtime.db",
        tmp_path / "runtime-data",
        orchestrator_factory=lambda _: None,
    )
    source_path = tmp_path / "pending-policy.txt"
    source_path.write_text("must stay outside derived storage", encoding="utf-8")
    request = DocumentIngestRequest(
        ticker="600519",
        path=str(source_path),
        title="许可尚未批准",
        source_name="巨潮资讯",
        source_url="https://static.cninfo.com.cn/manual/pending-policy.txt",
    )

    with pytest.raises(SourceRegistryError, match="派生文本"):
        ingest_registered_document(request, runtime)

    assert runtime.repository.list_runs() == []
    assert runtime.blob_store.scan_orphans(()) == ()
