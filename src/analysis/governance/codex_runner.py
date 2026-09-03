from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import model_validator

from .codex_tools import CodexToolService
from .models import (
    CodexInputPack,
    GovernanceModel,
    GovernanceReportSection,
    ResearchResultBundle,
    ResearchTask,
    SourceRole,
)
from .research_gate import (
    ResearchCoordinator,
    SanitizedResearchReceipt,
    research_result_bundle_hash,
)


class CodexReportDraft(GovernanceModel):
    schema_name = "governance-codex-report-draft"
    kind: Literal["governance_report_draft"] = "governance_report_draft"
    schema_version: str = "1.0.0"
    sections: tuple[GovernanceReportSection, ...]
    data_limitations: tuple[str, ...] = ()
    citation_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_draft(self) -> "CodexReportDraft":
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("draft section IDs must be unique")
        if tuple(sorted(set(self.citation_ids))) != self.citation_ids:
            raise ValueError("draft citation IDs must be unique and sorted")
        used = {
            citation_id
            for section in self.sections
            for finding in section.findings
            for citation_id in finding.citation_ids
        }
        if not used.issubset(set(self.citation_ids)):
            raise ValueError("draft citation index must include every finding citation")
        return self


@dataclass(frozen=True, slots=True)
class CodexRunnerResult:
    draft: CodexReportDraft
    protocol_status: Literal["protocol_passed"] = "protocol_passed"
    real_codex_verified: Literal[False] = False
    network_verified: Literal[False] = False


class CodexRunner(Protocol):
    protocol_version: str

    def run(
        self,
        *,
        input_pack: CodexInputPack,
        tool_service: CodexToolService,
        session_id: str,
        research_coordinator: ResearchCoordinator | None = None,
    ) -> CodexRunnerResult: ...


class ResearchTaskBroker(Protocol):
    def submit(self, task: ResearchTask) -> ResearchResultBundle: ...


@dataclass(frozen=True, slots=True)
class CodexRuntimeTools:
    query_callback: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
    research_callback: Callable[..., SanitizedResearchReceipt] | None = None

    def query(self, tool_name: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.query_callback(tool_name, parameters)

    def request_research(
        self,
        *,
        question_ids: Sequence[str],
        gap_ids: Sequence[str],
        question: str,
        allowed_source_roles: Sequence[SourceRole],
        known_evidence_ids: Sequence[str] = (),
    ) -> SanitizedResearchReceipt:
        if self.research_callback is None:
            raise RuntimeError("research delegation is not configured")
        return self.research_callback(
            question_ids=question_ids,
            gap_ids=gap_ids,
            question=question,
            allowed_source_roles=allowed_source_roles,
            known_evidence_ids=known_evidence_ids,
        )


DraftFactory = Callable[[CodexInputPack, CodexRuntimeTools], CodexReportDraft]


class DeterministicCodexRunner:
    """Offline protocol fake; it never claims to be a real Codex execution."""

    protocol_version = "governance-codex-runner.v1"

    def __init__(self, draft_factory: DraftFactory) -> None:
        self._draft_factory = draft_factory
        self.calls = 0

    def run(
        self,
        *,
        input_pack: CodexInputPack,
        tool_service: CodexToolService,
        session_id: str,
        research_coordinator: ResearchCoordinator | None = None,
    ) -> CodexRunnerResult:
        self.calls += 1

        def query(tool_name: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
            # A deterministic timestamp is provided by the caller in offline tools.
            occurred_at = input_pack.created_at
            return tool_service.invoke(
                session_id=session_id,
                tool_name=tool_name,
                parameters=parameters,
                occurred_at=occurred_at,
            )

        research = None
        if research_coordinator is not None:
            research = lambda **kwargs: research_coordinator.request(  # noqa: E731
                session_id=session_id,
                requested_at=input_pack.created_at,
                **kwargs,
            )
        draft = self._draft_factory(
            input_pack,
            CodexRuntimeTools(query_callback=query, research_callback=research),
        )
        if not isinstance(draft, CodexReportDraft):
            raise TypeError("CodexRunner must return a validated CodexReportDraft")
        return CodexRunnerResult(draft=draft)


class FakeResearchTaskBroker:
    """Deterministic broker for contract tests; it performs no network access."""

    def __init__(
        self,
        results: Mapping[str, ResearchResultBundle]
        | Callable[[ResearchTask], ResearchResultBundle],
    ) -> None:
        self._results = results
        self.submitted: list[ResearchTask] = []
        self.protocol_status: Literal["protocol_passed"] = "protocol_passed"
        self.real_codex_verified: Literal[False] = False
        self.network_verified: Literal[False] = False

    def submit(self, task: ResearchTask) -> ResearchResultBundle:
        if task.recursion_depth != 1:
            raise ValueError("v1 broker rejects recursive research delegation")
        self.submitted.append(task)
        if callable(self._results):
            bundle = self._results(task)
        else:
            try:
                bundle = self._results[task.research_task_id]
            except KeyError as exc:
                raise KeyError("no fake result registered for research task") from exc
        if bundle.research_task_id != task.research_task_id:
            raise ValueError("fake result is bound to a different task")
        if bundle.canonical_hash != research_result_bundle_hash(bundle):
            raise ValueError("fake result bundle must carry its real canonical hash")
        return bundle


__all__ = [
    "CodexReportDraft",
    "CodexRunner",
    "CodexRunnerResult",
    "CodexRuntimeTools",
    "DeterministicCodexRunner",
    "FakeResearchTaskBroker",
    "ResearchTaskBroker",
]
