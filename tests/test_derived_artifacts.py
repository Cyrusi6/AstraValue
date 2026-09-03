from __future__ import annotations

import hashlib

import pytest

from analysis.acquisition.snapshots import (
    ContentAddressedBlobStore,
    SnapshotCommitCoordinator,
    SnapshotPathEscape,
    canonical_json_sha256,
)


class _Repository:
    def __init__(self) -> None:
        self.artifacts = {}

    def save_derived_artifact(self, artifact):
        artifact_id = artifact["artifact_id"]
        previous = self.artifacts.get(artifact_id)
        if previous is not None and previous != artifact:
            raise ValueError("immutable")
        self.artifacts[artifact_id] = artifact


def _artifact(archived, *, extractor_version: str, parameters: dict):
    identity = canonical_json_sha256(
        {
            "parent_snapshot_id": archived.parent_snapshot_id,
            "extractor_id": archived.extractor_id,
            "extractor_version": extractor_version,
            "parameters": parameters,
            "output_sha256": archived.output_sha256,
        }
    )
    return {
        "artifact_id": f"derived-{identity}",
        "parent_snapshot_id": archived.parent_snapshot_id,
        "extractor_id": archived.extractor_id,
        "extractor_version": extractor_version,
        "parameters": parameters,
        "output_sha256": archived.output_sha256,
        "byte_length": archived.byte_length,
        "relative_path": archived.relative_path,
    }


def test_derived_output_is_immutable_and_versioned_by_extractor_and_hash(tmp_path):
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    repository = _Repository()
    coordinator = SnapshotCommitCoordinator(store, repository)

    first = coordinator.publish_derived_artifact(
        parent_snapshot_id="snapshot-1",
        extractor_id="pymupdf-text",
        output=b"version one",
        build_artifact=lambda archived: _artifact(
            archived,
            extractor_version="1.0.0",
            parameters={"sort": True},
        ),
    )
    repeated = coordinator.publish_derived_artifact(
        parent_snapshot_id="snapshot-1",
        extractor_id="pymupdf-text",
        output=b"version one",
        build_artifact=lambda archived: _artifact(
            archived,
            extractor_version="1.0.0",
            parameters={"sort": True},
        ),
    )
    changed = coordinator.publish_derived_artifact(
        parent_snapshot_id="snapshot-1",
        extractor_id="pymupdf-text",
        output=b"version two",
        build_artifact=lambda archived: _artifact(
            archived,
            extractor_version="2.0.0",
            parameters={"sort": True},
        ),
    )

    assert repeated == first
    assert changed["artifact_id"] != first["artifact_id"]
    assert changed["relative_path"] != first["relative_path"]
    assert len(repository.artifacts) == 2
    assert store.read_verified_derived(
        first["relative_path"],
        expected_sha256=first["output_sha256"],
        expected_length=first["byte_length"],
    ) == b"version one"
    assert hashlib.sha256(b"version one").hexdigest() == first["output_sha256"]


def test_derived_path_components_cannot_escape_data_root(tmp_path):
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    with pytest.raises(SnapshotPathEscape):
        store.archive_derived_bytes(
            parent_snapshot_id="../snapshot",
            extractor_id="extractor",
            output=b"text",
        )
