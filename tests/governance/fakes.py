from __future__ import annotations

from collections.abc import Callable, Mapping


class FakeGovernanceAcquisitionPort:
    """In-memory protocol fake built from opaque shared-kernel objects."""

    def __init__(
        self,
        *,
        runs: Mapping[str, object] | None = None,
        coverage: Mapping[str, tuple[object, ...]] | None = None,
        manifests: Mapping[str, object] | None = None,
        artifacts: Mapping[tuple[str, str], object] | None = None,
        acquisition_handler: Callable[[object], object] | None = None,
    ) -> None:
        self.runs = dict(runs or {})
        self.coverage = dict(coverage or {})
        self.manifests = dict(manifests or {})
        self.artifacts = dict(artifacts or {})
        self.acquisition_handler = acquisition_handler
        self.artifact_reads: list[tuple[str, str, bool]] = []
        self.acquisition_requests: list[object] = []

    def get_run(self, run_id: str, /) -> object:
        return self.runs[run_id]

    def get_coverage(
        self,
        run_or_manifest_id: str,
        /,
        *,
        question_ids: tuple[str, ...] = (),
    ) -> tuple[object, ...]:
        values = self.coverage[run_or_manifest_id]
        if not question_ids:
            return values
        allowed = set(question_ids)
        return tuple(
            value
            for value in values
            if isinstance(value, Mapping) and value.get("question_id") in allowed
        )

    def get_manifest(self, manifest_id: str, /) -> object:
        return self.manifests[manifest_id]

    def load_artifact(
        self,
        manifest_id: str,
        artifact_id: str,
        /,
        *,
        for_llm: bool,
    ) -> object:
        self.artifact_reads.append((manifest_id, artifact_id, for_llm))
        return self.artifacts[(manifest_id, artifact_id)]

    def request_acquisition(self, request: object, /) -> object:
        self.acquisition_requests.append(request)
        if self.acquisition_handler is None:
            raise RuntimeError("no fake acquisition result configured")
        return self.acquisition_handler(request)


__all__ = ["FakeGovernanceAcquisitionPort"]
