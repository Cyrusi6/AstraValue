from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analysis.acquisition.discovery import (
    BoundedDiscoveryEnvelope,
    DiscoveryCommitError,
    DiscoveryPageContext,
    DiscoveryPipeline,
    DiscoveryPolicySkipped,
    DiscoveryValidationError,
    NormalizedDiscoveryPage,
    NormalizedResource,
    no_data_is_proven,
    validate_discovery_proof_set,
)
from analysis.acquisition.models import ResourceRole
from analysis.acquisition.snapshots import ContentAddressedBlobStore, SnapshotService


NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


class Repository:
    def __init__(self, *, fail_discovery_commit=False):
        self.blobs = {}
        self.snapshots = {}
        self.observations = {}
        self.proofs = {}
        self.resources = {}
        self.fail_discovery_commit = fail_discovery_commit

    def find_raw_resource_snapshot(self, **filters):
        values = [
            item
            for item in self.snapshots.values()
            if all(getattr(item, key) == value for key, value in filters.items())
        ]
        return max(values, key=lambda item: item.version) if values else None

    def commit_discovery_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs[blob.content_blob_id] = blob
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.observations[observation.observation_id] = observation

    def save_discovery_observation(self, observation, **kwargs):
        self.observations[observation.observation_id] = observation

    def commit_discovery_bundle(self, observation, proof, resources, **kwargs):
        if self.fail_discovery_commit:
            raise RuntimeError("fixture transaction rollback")
        # Simulate one transaction: prepare copies and publish together.
        observations = {**self.observations, observation.observation_id: observation}
        proofs = {**self.proofs, proof.proof_id: proof}
        resource_rows = {
            **self.resources,
            **{item.discovered_resource_id: item for item in resources},
        }
        self.observations, self.proofs, self.resources = observations, proofs, resource_rows


class RetainedParser:
    def __init__(self, repository):
        self.repository = repository
        self.seen_snapshot_id = None

    def parse_retained_discovery(self, snapshot_id):
        assert isinstance(snapshot_id, str)
        assert snapshot_id in self.repository.snapshots
        assert self.repository.snapshots[snapshot_id].resource_role == ResourceRole.DISCOVERY_RESPONSE
        self.seen_snapshot_id = snapshot_id
        return _page(rows=1, total=1, terminal=True)


class Validator:
    def validate_and_normalize_without_retention(self, envelope):
        assert envelope.read_once() == b'{"rows":[]}'
        return _page(rows=0, total=0, terminal=True)


def _page(*, rows, total, terminal, page_count=1):
    resources = tuple(
        NormalizedResource(
            canonical_resource_id=f"announcement-{index}",
            resource_url=f"https://example.test/{index}.pdf",
            title=f"公告 {index}",
            source_timezone="Asia/Shanghai",
            required_fetch=True,
            row_locator=f"/rows/{index}",
            expected_mime_types=("application/pdf",),
        )
        for index in range(rows)
    )
    return NormalizedDiscoveryPage(
        resources=resources,
        parser_id="fixture-parser",
        parser_version="1.0.0",
        schema_id="fixture-schema",
        schema_version="1.0.0",
        schema_valid=True,
        declared_total=total,
        declared_page_count=page_count,
        terminal=terminal,
    )


def _context(page=1):
    return DiscoveryPageContext(
        attempt_id="discovery-attempt-1",
        physical_query_plan_item_id="plan-1",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        observed_at=NOW,
        retrieved_at=NOW,
        http_status=200,
        mime_type="application/json",
        page_number=page,
    )


def _pipeline(tmp_path, repository=None):
    repository = repository or Repository()
    snapshots = SnapshotService(
        ContentAddressedBlobStore(tmp_path / "data", "namespace-test"), repository
    )
    return repository, DiscoveryPipeline(snapshots, repository)


def test_freeze_before_parse_retained_discovery_keeps_row_locator_lineage(tmp_path):
    repository, pipeline = _pipeline(tmp_path)
    parser = RetainedParser(repository)
    result = pipeline.process_retained(
        body=b'{"rows":[{"id":1}]}',
        context=_context(),
        query_page_canonical="plan-1:page:1",
        parser=parser,
    )
    assert parser.seen_snapshot_id == result.discovery_snapshot_id
    assert result.proof.body_retained and result.proof.replayable
    assert result.proof.discovery_snapshot_id == result.discovery_snapshot_id
    assert result.resources[0].row_locator == "/rows/0"
    assert len(result.resources[0].row_hash) == 64


def test_atomic_without_retention_commits_proof_then_discards_body(tmp_path):
    repository, pipeline = _pipeline(tmp_path)
    envelope = BoundedDiscoveryEnvelope(
        b'{"rows":[]}',
        max_bytes=100,
        status_code=200,
        mime_type="application/json",
    )
    result = pipeline.process_without_retention(
        envelope=envelope,
        context=_context(),
        validator=Validator(),
        independent_replay_required=False,
    )
    assert envelope.discarded
    assert not result.proof.body_retained
    assert not result.proof.replayable
    assert result.proof.discovery_snapshot_id is None
    assert result.proof.proof_id in repository.proofs
    assert no_data_is_proven([result.proof])
    with pytest.raises(DiscoveryValidationError):
        envelope.read_once()


def test_proof_not_replayable_and_atomic_failure_leaves_no_partial_rows(tmp_path):
    repository = Repository(fail_discovery_commit=True)
    _, pipeline = _pipeline(tmp_path, repository)
    envelope = BoundedDiscoveryEnvelope(
        b'{"rows":[]}', max_bytes=100, status_code=200, mime_type="application/json"
    )
    with pytest.raises(DiscoveryCommitError):
        pipeline.process_without_retention(
            envelope=envelope,
            context=_context(),
            validator=Validator(),
            independent_replay_required=False,
        )
    assert envelope.discarded
    assert repository.observations == {}
    assert repository.proofs == {}
    assert repository.resources == {}


def test_policy_skip_when_replay_is_required_but_retention_forbidden(tmp_path):
    _, pipeline = _pipeline(tmp_path)
    envelope = BoundedDiscoveryEnvelope(
        b'{"rows":[]}', max_bytes=100, status_code=200, mime_type="application/json"
    )
    with pytest.raises(DiscoveryPolicySkipped) as error:
        pipeline.process_without_retention(
            envelope=envelope,
            context=_context(),
            validator=Validator(),
            independent_replay_required=True,
        )
    assert error.value.outcome == "policy_skipped"
    assert envelope.discarded


def test_missing_page_and_total_mismatch_prevent_no_data_or_coverage_finalize(tmp_path):
    _, pipeline = _pipeline(tmp_path)
    first = pipeline.process_without_retention(
        envelope=BoundedDiscoveryEnvelope(
            b'{"rows":[]}', max_bytes=100, status_code=200, mime_type="application/json"
        ),
        context=_context(page=1),
        validator=Validator(),
        independent_replay_required=False,
    ).proof
    second_data = first.model_dump(mode="python")
    second_data.update(
        proof_id="proof-page-3",
        observation_id="observation-page-3",
        page_number=3,
        terminal=True,
        declared_page_count=3,
    )
    first_data = first.model_dump(mode="python")
    first_data.update(terminal=False, declared_page_count=3)
    proof_one = type(first).model_validate(first_data)
    proof_three = type(first).model_validate(second_data)
    with pytest.raises(DiscoveryValidationError) as missing:
        validate_discovery_proof_set([proof_one, proof_three])
    assert missing.value.reason_code == "missing_page_proof"

    mismatch_data = first.model_dump(mode="python")
    mismatch_data.update(declared_total=1)
    mismatch = type(first).model_validate(mismatch_data)
    with pytest.raises(DiscoveryValidationError) as total:
        no_data_is_proven([mismatch])
    assert total.value.reason_code == "total_mismatch"


def test_no_data_requires_terminal_proof(tmp_path):
    _, pipeline = _pipeline(tmp_path)

    class NonTerminalValidator(Validator):
        def validate_and_normalize_without_retention(self, envelope):
            envelope.read_once()
            return _page(rows=0, total=0, terminal=False)

    proof = pipeline.process_without_retention(
        envelope=BoundedDiscoveryEnvelope(
            b'{"rows":[]}', max_bytes=100, status_code=200, mime_type="application/json"
        ),
        context=_context(),
        validator=NonTerminalValidator(),
        independent_replay_required=False,
    ).proof
    with pytest.raises(DiscoveryValidationError) as error:
        no_data_is_proven([proof])
    assert error.value.reason_code == "terminal_proof_missing"
