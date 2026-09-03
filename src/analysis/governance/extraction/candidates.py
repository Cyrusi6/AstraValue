from __future__ import annotations

from ..canonical import canonical_sha256
from ..models import ExtractorKind, GovernanceClaim, ReviewStatus
from .deterministic import DeterministicExtraction, DeterministicTableExtractor
from .loader import ManifestBoundArtifact


class CandidateBoundaryError(ValueError):
    pass


class CandidateExtractor:
    """Isolation boundary for LLM and explicitly ambiguous regex output.

    It deliberately exposes no review/approval operation.  Optional maintenance
    decisions live in the separate append-only ReviewDecision path.
    """

    def __init__(
        self,
        *,
        extractor_kind: ExtractorKind,
        name: str,
        version: str,
        ambiguous: bool = False,
        structured_draft_extractor: DeterministicTableExtractor | None = None,
    ) -> None:
        if not isinstance(extractor_kind, ExtractorKind):
            extractor_kind = ExtractorKind(extractor_kind)
        if extractor_kind == ExtractorKind.LLM:
            ambiguous = True
        elif extractor_kind != ExtractorKind.REGEX or not ambiguous:
            raise CandidateBoundaryError(
                "CandidateExtractor accepts only LLM or explicitly ambiguous regex output"
            )
        if not name.strip() or not version.strip():
            raise CandidateBoundaryError("candidate extractor name/version are required")
        self.extractor_kind = extractor_kind
        self.name = name
        self.version = version
        self.ambiguous = ambiguous
        self._structured = structured_draft_extractor or DeterministicTableExtractor(
            name=f"{name}.candidate-structure",
            version=version,
        )

    @property
    def for_llm(self) -> bool:
        return self.extractor_kind == ExtractorKind.LLM

    def extract(
        self,
        artifact: ManifestBoundArtifact,
        *,
        company_id: str,
    ) -> DeterministicExtraction:
        extracted = self._structured.extract(artifact, company_id=company_id)
        marker = (
            "candidate_llm"
            if self.extractor_kind == ExtractorKind.LLM
            else "candidate_ambiguous_regex"
        )
        return DeterministicExtraction(
            record_drafts=extracted.record_drafts,
            title_recalls=extracted.title_recalls,
            issue_codes=tuple(dict.fromkeys((*extracted.issue_codes, marker))),
        )

    def isolate_claim(self, claim: GovernanceClaim) -> GovernanceClaim:
        """Return a new pending candidate; never mutate or approve the input."""

        payload = claim.model_dump(mode="python")
        payload.update(
            extractor_kind=self.extractor_kind,
            extractor_version=self.version,
            review_status=ReviewStatus.PENDING,
        )
        payload.pop("canonical_hash", None)
        payload["canonical_hash"] = canonical_sha256(
            payload,
            schema_name=claim.schema_name,
            schema_version=claim.schema_version,
        )
        return GovernanceClaim.model_validate(payload, strict=True)


__all__ = ["CandidateBoundaryError", "CandidateExtractor"]
