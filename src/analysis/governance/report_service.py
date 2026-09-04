from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from .canonical import canonical_sha256
from .codex_runner import CodexReportDraft, CodexRunner
from .codex_tools import (
    CodexInputPack,
    CodexToolError,
    CodexToolService,
    InMemoryCodexSessionRecorder,
    canonical_input_pack_hash,
    canonical_session_manifest_hash,
)
from .models import (
    CompletenessStatus,
    GovernanceReport,
    GovernanceReportSection,
    GovernanceSnapshot,
    ReportGenerationStatus,
    ReportTechnicalValidation,
    ToolReadStatus,
    ValidationCheck,
)
from .research_gate import ResearchCoordinator


class GovernanceReportIntegrityError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class GovernanceReportRunResult:
    report: GovernanceReport
    session_manifest: Any


def _hash(schema_name: str, payload: Any, version: str = "1.0.0") -> str:
    return canonical_sha256(
        payload,
        schema_name=schema_name,
        schema_version=version,
    )


def _check(code: str, passed: bool, detail: str) -> ValidationCheck:
    return ValidationCheck(check_code=code, passed=passed, detail=detail)


def canonical_governance_report_hash(
    value: GovernanceReport | dict[str, Any],
) -> str:
    """Recompute the independent content hash for a governance report."""

    if isinstance(value, GovernanceReport):
        data = dict(value.model_dump(mode="python"))
    else:
        data = dict(value)
    data.setdefault("kind", "governance_report")
    data.setdefault("schema_version", "1.0.0")
    data.setdefault("decision_author", "codex")
    payload: dict[str, Any] = {}
    for name, model_field in GovernanceReport.model_fields.items():
        if name == "canonical_report_hash":
            continue
        if name in data:
            payload[name] = data[name]
        elif not model_field.is_required():
            payload[name] = model_field.default
        else:
            raise ValueError(f"missing required report hash field: {name}")
    return _hash(
        "governance-report",
        payload,
        str(payload["schema_version"]),
    )


def validate_report_inputs(
    *,
    snapshot: GovernanceSnapshot,
    input_pack: CodexInputPack,
) -> tuple[ValidationCheck, ...]:
    binding_ok = (
        input_pack.governance_snapshot_id == snapshot.governance_snapshot_id
        and input_pack.governance_snapshot_hash == snapshot.canonical_snapshot_hash
        and input_pack.evidence_manifest_id == snapshot.evidence_manifest_id
        and input_pack.evidence_manifest_hash == snapshot.evidence_manifest_hash
    )
    time_ok = (
        input_pack.company_id == snapshot.company_id
        and input_pack.state_at == snapshot.state_at
        and input_pack.known_at == snapshot.known_at
        and input_pack.perspective == snapshot.perspective
    )
    future_ok = all(
        link.available_at <= snapshot.known_at
        for link in (*snapshot.anchor_links, *snapshot.delta_links)
    )
    lineage_ok = (
        tuple(
            sorted(
                link.record_id
                for link in snapshot.record_links
                if link.role.value == "canonical"
            )
        )
        == snapshot.canonical_record_ids
    )
    input_hash = canonical_input_pack_hash(input_pack)
    input_hash_ok = (
        input_pack.canonical_hash == input_hash
        and input_pack.input_pack_id == f"govinput:{input_hash}"
    )
    return (
        _check(
            "input_pack_hash",
            input_hash_ok,
            "input pack ID and canonical hash match its semantic content",
        ),
        _check(
            "snapshot_binding",
            binding_ok,
            "input pack is bound to the immutable snapshot and manifest",
        ),
        _check(
            "bitemporal_identity",
            time_ok,
            "company, state_at, known_at and perspective match",
        ),
        _check(
            "future_firewall",
            future_ok,
            "all linked substantive evidence is visible by known_at",
        ),
        _check(
            "snapshot_lineage",
            lineage_ok,
            "canonical record index matches immutable links",
        ),
    )


class GovernanceReportService:
    """Runs Codex over a fixed snapshot and publishes a schema-checked report.

    Business uncertainty is copied into the report and never turned into a
    technical failure.  Only explicit integrity checks can fail publication.
    """

    def __init__(
        self,
        *,
        runner: CodexRunner,
        tool_service: CodexToolService,
        recorder: InMemoryCodexSessionRecorder,
        snapshot_resolver: Callable[[str], GovernanceSnapshot],
        research_coordinator: ResearchCoordinator | None = None,
        additional_checks: Sequence[
            Callable[[GovernanceSnapshot, CodexInputPack], ValidationCheck]
        ] = (),
    ) -> None:
        self._runner = runner
        self._tool_service = tool_service
        self._recorder = recorder
        self._snapshot_resolver = snapshot_resolver
        self._research_coordinator = research_coordinator
        self._additional_checks = tuple(additional_checks)

    def generate(
        self,
        *,
        report_run_id: str,
        session_id: str,
        input_pack: CodexInputPack,
        model_profile: str,
        tool_protocol_version: str,
        started_at: datetime,
        completed_at: datetime,
        initial_citation_ids: Sequence[str] = (),
    ) -> GovernanceReportRunResult:
        initial = self._snapshot_resolver(input_pack.governance_snapshot_id)
        self._recorder.start(
            session_id=session_id,
            parent_report_run_id=report_run_id,
            model_profile=model_profile,
            runner_protocol_version=self._runner.protocol_version,
            tool_protocol_version=tool_protocol_version,
            input_pack=input_pack,
            started_at=started_at,
        )
        checks = list(validate_report_inputs(snapshot=initial, input_pack=input_pack))
        checks.extend(check(initial, input_pack) for check in self._additional_checks)
        if not all(item.passed for item in checks):
            return self._failed_result(
                report_run_id=report_run_id,
                session_id=session_id,
                input_pack=input_pack,
                snapshot=initial,
                checks=checks,
                failure_code="input_integrity_failure",
                completed_at=completed_at,
            )

        try:
            outcome = self._runner.run(
                input_pack=input_pack,
                tool_service=self._tool_service,
                session_id=session_id,
                research_coordinator=self._research_coordinator,
            )
            draft = outcome.draft
        except CodexToolError as exc:
            checks.append(
                _check(
                    exc.code,
                    False,
                    "a governance evidence tool failed its security or integrity contract",
                )
            )
            return self._failed_result(
                report_run_id=report_run_id,
                session_id=session_id,
                input_pack=input_pack,
                snapshot=initial,
                checks=checks,
                failure_code="tool_integrity_failure",
                completed_at=completed_at,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            checks.append(
                _check(
                    "runner_output_schema",
                    False,
                    f"Codex output failed structural validation: {type(exc).__name__}",
                )
            )
            return self._failed_result(
                report_run_id=report_run_id,
                session_id=session_id,
                input_pack=input_pack,
                snapshot=initial,
                checks=checks,
                failure_code="report_schema_invalid",
                completed_at=completed_at,
            )
        except Exception as exc:
            checks.append(
                _check(
                    "runner_execution",
                    False,
                    f"Codex runner failed without publishing a report: {type(exc).__name__}",
                )
            )
            return self._failed_result(
                report_run_id=report_run_id,
                session_id=session_id,
                input_pack=input_pack,
                snapshot=initial,
                checks=checks,
                failure_code="runner_failed",
                completed_at=completed_at,
            )

        final_snapshot_id, final_snapshot_hash, _revision = self._recorder.current_binding(
            session_id
        )
        final_snapshot = self._snapshot_resolver(final_snapshot_id)
        final_binding_ok = final_snapshot.canonical_snapshot_hash == final_snapshot_hash
        final_time_ok = (
            final_snapshot.company_id == initial.company_id
            and final_snapshot.state_at == initial.state_at
            and final_snapshot.known_at == initial.known_at
            and final_snapshot.perspective == initial.perspective
        )
        checks.extend(
            (
                _check(
                    "final_snapshot_binding",
                    final_binding_ok,
                    "session final snapshot hash resolves exactly",
                ),
                _check(
                    "final_snapshot_time",
                    final_time_ok,
                    "snapshot adoption preserved company and bitemporal query",
                ),
            )
        )
        final_checks = validate_report_inputs(snapshot=final_snapshot, input_pack=input_pack)
        final_future = next(item for item in final_checks if item.check_code == "future_firewall")
        final_lineage = next(item for item in final_checks if item.check_code == "snapshot_lineage")
        try:
            GovernanceSnapshot.model_validate(final_snapshot.model_dump(mode="python"), strict=True)
            final_schema_ok = True
        except ValidationError:
            final_schema_ok = False
        checks.extend(
            (
                _check(
                    "final_snapshot_schema",
                    final_schema_ok,
                    "the final snapshot still satisfies the closed domain schema",
                ),
                _check(
                    "final_snapshot_future_firewall",
                    final_future.passed,
                    "all final snapshot evidence is visible by known_at",
                ),
                _check(
                    "final_snapshot_lineage",
                    final_lineage.passed,
                    "the final snapshot canonical index matches its record links",
                ),
            )
        )

        reads = self._recorder.tool_reads(session_id)
        read_integrity_ok = True
        read_integrity_code: str | None = None
        for expected_sequence, read in enumerate(reads, start=1):
            try:
                if read.session_id != session_id or read.sequence != expected_sequence:
                    raise CodexToolError(
                        "tool_sequence_conflict",
                        "persisted reads are not a contiguous session sequence",
                    )
                if read.status == ToolReadStatus.FAILED:
                    raise CodexToolError(
                        read.error_code or "tool_read_failed",
                        "a tool failed after entering the governed read path",
                    )
                read_snapshot = self._snapshot_resolver(read.governance_snapshot_id)
                if (
                    read_snapshot.company_id != initial.company_id
                    or read_snapshot.state_at != initial.state_at
                    or read_snapshot.known_at != initial.known_at
                    or read_snapshot.perspective != initial.perspective
                ):
                    raise CodexToolError(
                        "snapshot_binding_mismatch",
                        "tool read snapshot changed the report's bitemporal identity",
                    )
                self._tool_service.validate_persisted_read(
                    read,
                    input_pack=input_pack,
                    expected_snapshot_hash=read_snapshot.canonical_snapshot_hash,
                )
            except (CodexToolError, KeyError, ValidationError, TypeError, ValueError) as exc:
                read_integrity_ok = False
                read_integrity_code = getattr(exc, "code", type(exc).__name__)
                break
        checks.append(
            _check(
                "tool_read_integrity",
                read_integrity_ok,
                (
                    "all persisted tool reads passed schema, hash, snapshot, time and lineage checks"
                    if read_integrity_ok
                    else f"persisted tool read failed: {read_integrity_code}"
                ),
            )
        )
        tool_citations = {
            citation_id
            for read in reads
            if read.status == ToolReadStatus.SUCCEEDED
            for citation_id in read.citation_ids
        }
        # CodexInputPack has indexes but no citation payloads. A citation becomes
        # report-visible only through a successful, revalidated tool read.
        citations_ok = set(draft.citation_ids).issubset(tool_citations)
        checks.append(
            _check(
                "citation_lineage",
                citations_ok,
                "every report citation came from a successful validated tool read",
            )
        )
        checks.append(
            _check(
                "runner_output_schema",
                True,
                "Codex returned a validated GovernanceReport draft",
            )
        )
        if not all(item.passed for item in checks):
            return self._failed_result(
                report_run_id=report_run_id,
                session_id=session_id,
                input_pack=input_pack,
                snapshot=final_snapshot,
                checks=checks,
                failure_code="output_integrity_failure",
                completed_at=completed_at,
                draft=draft,
            )
        return self._completed_result(
            report_run_id=report_run_id,
            session_id=session_id,
            snapshot=final_snapshot,
            draft=draft,
            checks=checks,
            completed_at=completed_at,
        )

    def _completed_result(
        self,
        *,
        report_run_id: str,
        session_id: str,
        snapshot: GovernanceSnapshot,
        draft: CodexReportDraft,
        checks: Sequence[ValidationCheck],
        completed_at: datetime,
    ) -> GovernanceReportRunResult:
        limitations = set(draft.data_limitations)
        if snapshot.completeness_status == CompletenessStatus.INCOMPLETE:
            limitations.add("snapshot_incomplete")
        if snapshot.completeness_status == CompletenessStatus.CONFLICTED:
            limitations.add("snapshot_conflicted")
        if snapshot.active_gap_ids:
            limitations.add("active_gaps_present")
        if snapshot.pending_candidate_ids:
            limitations.add("pending_candidates_present")
        technical = ReportTechnicalValidation(passed=True, checks=tuple(checks))
        return self._build_pair(
            report_run_id=report_run_id,
            session_id=session_id,
            snapshot=snapshot,
            sections=draft.sections,
            data_limitations=tuple(sorted(limitations)),
            citation_ids=draft.citation_ids,
            technical=technical,
            generation_status=ReportGenerationStatus.COMPLETED,
            failure_code=None,
            output_schema_validated=True,
            completed_at=completed_at,
        )

    def _failed_result(
        self,
        *,
        report_run_id: str,
        session_id: str,
        input_pack: CodexInputPack,
        snapshot: GovernanceSnapshot,
        checks: Sequence[ValidationCheck],
        failure_code: str,
        completed_at: datetime,
        draft: CodexReportDraft | None = None,
    ) -> GovernanceReportRunResult:
        failed_checks = [item.check_code for item in checks if not item.passed]
        if not failed_checks:
            failed_checks = [failure_code]
            checks = (*checks, _check(failure_code, False, "technical integrity failure"))
        technical = ReportTechnicalValidation(
            passed=False,
            hard_failure_codes=tuple(sorted(set(failed_checks))),
            checks=tuple(checks),
        )
        return self._build_pair(
            report_run_id=report_run_id,
            session_id=session_id,
            snapshot=snapshot,
            sections=(),
            data_limitations=("technical_failure",),
            citation_ids=(),
            technical=technical,
            generation_status=ReportGenerationStatus.FAILED,
            failure_code=failure_code,
            output_schema_validated=False,
            completed_at=completed_at,
        )

    def _build_pair(
        self,
        *,
        report_run_id: str,
        session_id: str,
        snapshot: GovernanceSnapshot,
        sections: Sequence[GovernanceReportSection],
        data_limitations: Sequence[str],
        citation_ids: Sequence[str],
        technical: ReportTechnicalValidation,
        generation_status: ReportGenerationStatus,
        failure_code: str | None,
        output_schema_validated: bool,
        completed_at: datetime,
    ) -> GovernanceReportRunResult:
        report_seed = _hash(
            "governance-report-identity",
            {
                "report_run_id": report_run_id,
                "session_id": session_id,
                "snapshot_id": snapshot.governance_snapshot_id,
            },
        )
        report_id = f"govreport:{report_seed}"
        normalized_citations = tuple(sorted(set(citation_ids)))
        session_hash = self._recorder.preview_manifest_hash(
            session_id,
            generation_status=generation_status,
            final_citation_ids=normalized_citations,
            output_schema_validated=output_schema_validated,
            failure_code=failure_code,
            completed_at=completed_at,
        )
        report_payload = {
            "kind": "governance_report",
            "schema_version": "1.0.0",
            "governance_report_id": report_id,
            "company_id": snapshot.company_id,
            "state_at": snapshot.state_at,
            "known_at": snapshot.known_at,
            "perspective": snapshot.perspective,
            "governance_snapshot_id": snapshot.governance_snapshot_id,
            "governance_snapshot_hash": snapshot.canonical_snapshot_hash,
            "evidence_manifest_id": snapshot.evidence_manifest_id,
            "evidence_manifest_hash": snapshot.evidence_manifest_hash,
            "session_manifest_id": session_id,
            "session_manifest_hash": session_hash,
            "generation_status": generation_status,
            "decision_author": "codex",
            "sections": tuple(sections),
            "active_gap_ids": snapshot.active_gap_ids,
            "active_conflict_ids": snapshot.active_conflict_ids,
            "pending_candidate_ids": snapshot.pending_candidate_ids,
            "data_limitations": tuple(sorted(set(data_limitations))),
            "citation_ids": normalized_citations,
            "future_knowledge_used": snapshot.future_knowledge_used,
            "technical_validation": technical,
            "failure_code": failure_code,
            "created_at": completed_at,
        }
        report_hash = canonical_governance_report_hash(report_payload)
        report = GovernanceReport(
            **report_payload,
            canonical_report_hash=report_hash,
        )
        session = self._recorder.finalize_session(
            session_id,
            generation_status=generation_status,
            final_citation_ids=report.citation_ids,
            report_hash=report_hash,
            output_schema_validated=output_schema_validated,
            failure_code=failure_code,
            completed_at=completed_at,
        )
        if (
            session.canonical_hash != session_hash
            or session.canonical_hash != canonical_session_manifest_hash(session)
            or report.canonical_report_hash != canonical_governance_report_hash(report)
        ):
            raise GovernanceReportIntegrityError(
                "report_session_hash_mismatch",
                "report/session hashes changed while finalizing the immutable pair",
            )
        return GovernanceReportRunResult(report=report, session_manifest=session)


__all__ = [
    "GovernanceReportIntegrityError",
    "GovernanceReportRunResult",
    "GovernanceReportService",
    "canonical_governance_report_hash",
    "validate_report_inputs",
]
