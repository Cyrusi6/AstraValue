"""Manifest-bound governance extraction and deterministic admission."""

from .candidates import CandidateBoundaryError, CandidateExtractor
from .deterministic import (
    ClaimFieldDraft,
    DeterministicExtraction,
    DeterministicExtractor,
    DeterministicTableExtractor,
    EvidenceLocatorDraft,
    RecordDraft,
    TableContractError,
    TitleRecall,
)
from .loader import (
    ManifestBoundArtifact,
    ManifestBoundExtractionLoader,
    ManifestBoundLoadError,
)
from .orchestrator import (
    ExtractionOrchestrator,
    ExtractionSink,
    GovernanceExtractionOrchestrator,
    InMemoryExtractionSink,
    OrchestrationResult,
    RejectedRecord,
    conflicting_draft_fields,
    detect_claim_conflicts,
    independent_source_count,
)
from .validators import (
    GovernanceFieldValidator,
    ValidationAssessment,
    validate_period_order,
    validate_total_relationship,
)

__all__ = [
    "CandidateBoundaryError",
    "CandidateExtractor",
    "ClaimFieldDraft",
    "DeterministicExtraction",
    "DeterministicExtractor",
    "DeterministicTableExtractor",
    "EvidenceLocatorDraft",
    "ExtractionOrchestrator",
    "ExtractionSink",
    "GovernanceExtractionOrchestrator",
    "GovernanceFieldValidator",
    "InMemoryExtractionSink",
    "ManifestBoundArtifact",
    "ManifestBoundExtractionLoader",
    "ManifestBoundLoadError",
    "OrchestrationResult",
    "RecordDraft",
    "RejectedRecord",
    "TableContractError",
    "TitleRecall",
    "ValidationAssessment",
    "conflicting_draft_fields",
    "detect_claim_conflicts",
    "independent_source_count",
    "validate_period_order",
    "validate_total_relationship",
]
