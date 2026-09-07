"""Read an existing bound acquisition namespace without bootstrapping or I/O."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from analysis.acquisition.bootstrap import ROOT_MARKER_NAME, _identity
from analysis.acquisition.content_selection import NO_AUDIT_ENGLISH_ANNUAL_V1, content_exclusion_reason
from analysis.acquisition.manifests import EvidenceManifestService
from analysis.acquisition.repository import AcquisitionRepository
from analysis.acquisition.snapshots import ContentAddressedBlobStore

from .models import Citation, compact


PAGE_MARKER = re.compile(r"^--- (?:PDF )?page (\d+)(?: \|[^\n]*)? ---\s*$", re.M)


def split_pages(text: str, mime_type: str) -> dict[int | None, str]:
    markers = list(PAGE_MARKER.finditer(text))
    if not markers:
        if mime_type != "text/html":
            raise ValueError("missing_pdf_page_markers")
        return {None: text}
    numbers = [int(m.group(1)) for m in markers]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("non_contiguous_pdf_pages")
    return {int(m.group(1)): text[m.end():markers[i + 1].start() if i + 1 < len(markers) else len(text)].strip()
            for i, m in enumerate(markers)}


class FrozenCorpus:
    def __init__(self, db: Path, data_root: Path, manifest_id: str, *, selection_policy=NO_AUDIT_ENGLISH_ANNUAL_V1):
        self.selection_policy = selection_policy
        self.db = Path(db).resolve()
        self.repository = AcquisitionRepository(self.db, initialize=False)
        namespace = self.repository.get_storage_namespace()
        root = Path(data_root).resolve(strict=True)
        marker = json.loads((root / ROOT_MARKER_NAME).read_text("utf-8"))
        for field in ("namespace_id", "binding_nonce", "database_identity_hash", "data_root_identity_hash"):
            if marker.get(field) != getattr(namespace, field):
                raise ValueError("evidence_namespace_binding_mismatch")
        if (marker.get("state") != "bound" or namespace.database_identity_hash != _identity(self.db)
                or namespace.data_root_identity_hash != _identity(root)):
            raise ValueError("evidence_namespace_binding_mismatch")
        self.blob_store = ContentAddressedBlobStore(root, namespace)
        self.gate = EvidenceManifestService(self.blob_store, self.repository,
                                           self.repository.get_source_definition_version)
        self.manifest = self.repository.get_evidence_manifest(manifest_id)
        self.validate()
        self.items = {i.snapshot_id: i for i in self.manifest.items}
        self._documents = {}

    def validate(self):
        self.gate.validate_evidence_manifest(self.manifest)

    def document(self, snapshot_id: str, artifact_id: str):
        key = (snapshot_id, artifact_id)
        if key in self._documents:
            return self._documents[key]
        item = self.items.get(snapshot_id)
        if item is None or artifact_id not in item.derived_artifact_ids:
            raise ValueError("citation_not_in_frozen_manifest")
        snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
        artifact = self.repository.get_derived_artifact(artifact_id)
        if (artifact.parent_snapshot_id != snapshot_id or artifact.storage_namespace_id != self.manifest.storage_namespace_id
                or artifact.artifact_type != "text"):
            raise ValueError("citation_text_parent_mismatch")
        observation = self.repository.get_resource_observation(snapshot.creating_observation_id)
        resource = self.repository.get_discovered_resource(observation.discovered_resource_id)
        attempt = self.repository.get_attempt(observation.attempt_id)
        run = self.repository.get_run(attempt.run_id)
        if run.storage_namespace_id != self.manifest.storage_namespace_id:
            raise ValueError("citation_source_scope_mismatch")
        text = self.blob_store.read_verified_derived(artifact.archive_relative_path,
            expected_sha256=artifact.output_sha256, expected_length=artifact.output_byte_length).decode("utf-8")
        exclusion = content_exclusion_reason(resource.title, resource.resource_url,
            resource.expected_mime_types, self.selection_policy)
        result = {"snapshot": snapshot, "artifact": artifact, "title": resource.title,
                  "company_id": run.ticker, "pages": split_pages(text, snapshot.mime_type), "exclusion": exclusion}
        self._documents[key] = result
        return result

    def citation(self, citation: Citation, company_id: str) -> dict:
        doc = self.document(citation.snapshot_id, citation.derived_artifact_id)
        if doc["company_id"] != company_id:
            raise ValueError("citation_company_mismatch")
        if doc["exclusion"]:
            raise ValueError("citation_excluded_by_current_selection")
        page = doc["pages"].get(citation.page_number)
        if page is None or compact(citation.quote) not in compact(page):
            raise ValueError("citation_quote_not_found_on_page")
        snap, art = doc["snapshot"], doc["artifact"]
        return {**citation.model_dump(), "manifest_id": self.manifest.manifest_id,
            "manifest_hash": self.manifest.manifest_hash, "storage_namespace_id": self.manifest.storage_namespace_id,
            "snapshot_sha256": snap.sha256, "text_sha256": art.output_sha256,
            "page_sha256": hashlib.sha256(page.encode("utf-8")).hexdigest(),
            "source_definition_id": snap.source_definition_id,
            "source_definition_version": snap.source_definition_version,
            "canonical_resource_id": snap.canonical_resource_id, "source_url": snap.canonical_url,
            "available_at": snap.available_at.isoformat(), "published_at": snap.published_at.isoformat() if snap.published_at else None,
            "title": doc["title"], "raw_relative_path": snap.archive_relative_path,
            "text_relative_path": art.archive_relative_path}
