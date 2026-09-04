from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable


# These type variables are deliberately supplied by the shared acquisition kernel.
# Governance owns no parallel run, coverage, raw-snapshot, manifest, or request model.
SharedRunT_co = TypeVar("SharedRunT_co", covariant=True)
SharedCoverageT_co = TypeVar("SharedCoverageT_co", covariant=True)
SharedArtifactT_co = TypeVar("SharedArtifactT_co", covariant=True)
SharedManifestT_co = TypeVar("SharedManifestT_co", covariant=True)
SharedReplenishmentRequestT_contra = TypeVar(
    "SharedReplenishmentRequestT_contra", contravariant=True
)
SharedReplenishmentResultT_co = TypeVar(
    "SharedReplenishmentResultT_co", covariant=True
)


@runtime_checkable
class GovernanceAcquisitionPort(
    Protocol[
        SharedRunT_co,
        SharedCoverageT_co,
        SharedArtifactT_co,
        SharedManifestT_co,
        SharedReplenishmentRequestT_contra,
        SharedReplenishmentResultT_co,
    ]
):
    """Minimal read/replenishment boundary over the one shared acquisition kernel."""

    def get_run(self, run_id: str, /) -> SharedRunT_co:
        ...

    def get_coverage(
        self,
        run_or_manifest_id: str,
        /,
        *,
        question_ids: tuple[str, ...] = (),
    ) -> tuple[SharedCoverageT_co, ...]:
        ...

    def load_artifact(
        self,
        manifest_id: str,
        artifact_id: str,
        /,
        *,
        for_llm: bool,
    ) -> SharedArtifactT_co:
        ...

    def get_manifest(self, manifest_id: str, /) -> SharedManifestT_co:
        ...

    def request_acquisition(
        self,
        request: SharedReplenishmentRequestT_contra,
        /,
    ) -> SharedReplenishmentResultT_co:
        ...


__all__ = ["GovernanceAcquisitionPort"]
