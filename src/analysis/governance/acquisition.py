"""Read governance evidence through the shared acquisition repository and gate."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from analysis.acquisition.models import PolicyDecision
from analysis.business_evidence.corpus import FrozenCorpus

from .models import SourceRole


class SharedGovernanceAcquisitionPort:
    """A checked projection, never a second acquisition store or downloader.

    Physical bindings and source roles come from application configuration. A
    caller can select manifest member IDs, but cannot supply bytes, hashes or
    verification flags. Replenishment is delegated to the research workspace.
    """

    def __init__(self, *, acquisition_db: Path, data_root: Path, manifest_id: str,
                 company_id: str, known_at: datetime, source_roles: dict[str, str],
                 request_materials=None):
        self.corpus = FrozenCorpus(acquisition_db, data_root, manifest_id)
        self.repository = self.corpus.repository
        self.manifest = self.corpus.manifest
        self.company_id = company_id
        self.known_at = known_at
        self.source_roles = source_roles
        self._request_materials = request_materials
        self.get_run(self.manifest.run_id)

    def get_run(self, run_id: str, /):
        run = self.repository.get_run(run_id)
        if run.ticker != self.company_id or run.storage_namespace_id != self.manifest.storage_namespace_id:
            raise ValueError("governance_source_company_or_namespace_mismatch")
        return run

    def get_coverage(self, run_or_manifest_id: str, /, *, question_ids: tuple[str, ...] = ()):
        run_id = self.manifest.run_id if run_or_manifest_id == self.manifest.manifest_id else run_or_manifest_id
        self.get_run(run_id)
        return tuple(entry for entry in self.repository.list_coverage_entries(run_id)
                     if not question_ids or entry.question_id in question_ids)

    def _entries(self):
        # The shared gate rechecks both immutable metadata and original bytes.
        self.corpus.validate()
        entries = {}
        for item in self.manifest.items:
            snapshot = self.repository.get_raw_resource_snapshot(item.snapshot_id)
            observation = self.repository.get_resource_observation(snapshot.creating_observation_id)
            attempt = self.repository.get_attempt(observation.attempt_id)
            self.get_run(attempt.run_id)
            if snapshot.available_at > self.known_at:
                raise ValueError("governance_source_after_research_cutoff")
            source = self.repository.get_source_definition_version(
                snapshot.source_definition_id, snapshot.source_definition_version)
            role = self.source_roles.get(f"{source.source_definition_id}@{source.version}")
            if role is None:
                raise ValueError("governance_source_role_not_registered")
            role = SourceRole(role)
            if not snapshot.upstream_material_id or snapshot.published_at is None:
                raise ValueError("governance_source_lineage_incomplete")
            common = dict(raw_snapshot_id=snapshot.snapshot_id, content_hash=snapshot.sha256,
                source_role=role.value, upstream_material_id=snapshot.upstream_material_id,
                independence_group=f"{source.upstream_identity}:{snapshot.upstream_material_id}",
                integrity_verified=True, lineage_complete=True,
                llm_allowed=source.license_policy.llm_processing == PolicyDecision.ALLOWED,
                announced_at=snapshot.published_at, available_at=snapshot.available_at,
                retrieved_at=observation.retrieved_at, source_url=snapshot.canonical_url,
                source_definition_id=source.source_definition_id,
                source_definition_version=source.version)
            entries[snapshot.snapshot_id] = dict(common, artifact_id=snapshot.snapshot_id,
                artifact_kind="raw_snapshot", supported=snapshot.mime_type == "application/json")
            for artifact_id in item.derived_artifact_ids:
                artifact = self.repository.get_derived_artifact(artifact_id)
                if artifact.parent_snapshot_id != snapshot.snapshot_id:
                    raise ValueError("governance_derived_parent_mismatch")
                entries[artifact_id] = dict(common, artifact_id=artifact_id,
                    artifact_kind="derived_" + artifact.artifact_type,
                    derived_artifact_id=artifact_id, derived_artifact_hash=artifact.output_sha256,
                    supported=artifact.artifact_type == "table")
        return entries

    def get_manifest(self, manifest_id: str, /):
        if manifest_id != self.manifest.manifest_id:
            raise ValueError("governance_manifest_mismatch")
        return dict(manifest_id=manifest_id, manifest_hash=self.manifest.manifest_hash,
                    artifacts=list(self._entries().values()))

    def load_artifact(self, manifest_id: str, artifact_id: str, /, *, for_llm: bool):
        if manifest_id != self.manifest.manifest_id:
            raise ValueError("governance_manifest_mismatch")
        descriptor = self._entries().get(artifact_id)
        if descriptor is None:
            raise ValueError("governance_artifact_not_in_manifest")
        if for_llm and not descriptor["llm_allowed"]:
            raise ValueError("governance_llm_policy_denied")
        if "derived_artifact_id" in descriptor:
            artifact = self.repository.get_derived_artifact(artifact_id)
            content = self.corpus.blob_store.read_verified_derived(artifact.archive_relative_path,
                expected_sha256=artifact.output_sha256, expected_length=artifact.output_byte_length)
        else:
            snapshot = self.repository.get_raw_resource_snapshot(artifact_id)
            content = self.corpus.blob_store.read_verified(snapshot.archive_relative_path,
                expected_sha256=snapshot.sha256, expected_length=snapshot.byte_length)
        return dict(descriptor, content_bytes=content)

    def request_acquisition(self, request, /):
        if self._request_materials is None:
            raise ValueError("governance_replenishment_use_research_request_materials")
        return self._request_materials(request)
