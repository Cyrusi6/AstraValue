from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from .canonical import canonical_datetime, canonical_decimal, canonical_json_text
from .codex_tools import canonical_input_pack_hash, canonical_session_manifest_hash
from .models import (
    CodexInputPack,
    CodexSessionManifest,
    GovernanceReport,
    GovernanceSnapshot,
)
from .redaction import redact_text, redact_value
from .report_service import canonical_governance_report_hash
from .snapshot_service import (
    SnapshotIntegrityError,
    validate_governance_snapshot_semantic_hash,
)


TRACE_RENDERER_VERSION = "1.0.0"
TRACE_FILENAMES = (
    "01-acquisition-coverage.md",
    "02-evidence-manifest.md",
    "03-extraction-results.md",
    "04-entity-resolution.md",
    "05-governance-reconstruction.md",
    "06-codex-input-pack.md",
    "07-codex-assessment.md",
    "08-report-validation.md",
)


class TraceRenderError(ValueError):
    """Raised when trace input cannot be resolved to authoritative objects."""


@dataclass(frozen=True, slots=True)
class TraceObjectView:
    """Small, structured projection for objects owned by another subsystem.

    Shared acquisition and the future repository adapter can expose their
    objects through this shape without handing the renderer raw Markdown or a
    private filesystem path.
    """

    object_id: str
    object_kind: str
    canonical_hash: str | None = None
    status: str | None = None
    summary: str | None = None
    reference_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GovernanceTraceProjection:
    """All authoritative objects needed to reproduce the eight trace files."""

    report: GovernanceReport
    session_manifest: CodexSessionManifest
    snapshot: GovernanceSnapshot
    input_pack: CodexInputPack
    snapshot_semantic_payload: Mapping[str, object]
    acquisition_objects: tuple[TraceObjectView, ...] = ()
    evidence_manifest_objects: tuple[TraceObjectView, ...] = ()
    extraction_objects: tuple[TraceObjectView, ...] = ()
    entity_resolution_objects: tuple[TraceObjectView, ...] = ()
    soft_state_objects: tuple[TraceObjectView, ...] = ()


class GovernanceTraceProjectionLoader(Protocol):
    """Loads a fresh structured projection for a fixed report identity."""

    def load_trace_projection(self, report_id: str) -> GovernanceTraceProjection:
        ...


class InMemoryGovernanceTraceProjectionLoader:
    """Deterministic adapter useful for offline tests and composition wiring."""

    def __init__(self, projections: Mapping[str, GovernanceTraceProjection]) -> None:
        self._projections = dict(projections)

    def load_trace_projection(self, report_id: str) -> GovernanceTraceProjection:
        try:
            return self._projections[report_id]
        except KeyError as exc:
            raise TraceRenderError(f"unknown governance report: {report_id}") from exc


@dataclass(frozen=True, slots=True)
class _TraceCheck:
    code: str
    passed: bool
    detail: str


def _stable_object_key(item: TraceObjectView) -> tuple[str, str, str]:
    return (item.object_kind, item.object_id, item.canonical_hash or "")


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return _display(value.value)
    if isinstance(value, datetime):
        return canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, BaseModel):
        return _json_text(value.model_dump(mode="python"))
    if isinstance(value, Mapping) or isinstance(value, (tuple, list, set, frozenset)):
        return _json_text(value)
    if isinstance(value, (str, int)):
        return redact_text(str(value))
    raise TraceRenderError(
        f"unsupported trace display value: {type(value).__name__}"
    )


def _json_text(value: Any) -> str:
    try:
        safe = redact_value(value)
        return canonical_json_text(safe)
    except (TypeError, ValueError) as exc:
        raise TraceRenderError("trace value is not safely serializable") from exc


def _markdown(value: Any) -> str:
    text = _display(value).replace("\r", " ").replace("\n", " ")
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, f"\\{character}")
    return text


def _ids(values: Sequence[str]) -> str:
    return ", ".join(_display(value) for value in values) if values else "none"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    rendered = [
        "| " + " | ".join(_markdown(item) for item in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    rendered.extend(
        "| " + " | ".join(_markdown(item) for item in row) + " |" for row in rows
    )
    return rendered


def _projection_header(
    title: str,
    projection: GovernanceTraceProjection,
    renderer_version: str,
) -> list[str]:
    report = projection.report
    snapshot = projection.snapshot
    session = projection.session_manifest
    return [
        f"# {_markdown(title)}",
        "",
        f"> trace_renderer_version: {_markdown(renderer_version)}",
        "",
        *_table(
            ("字段", "值"),
            (
                ("report_id", report.governance_report_id),
                ("report_hash", report.canonical_report_hash),
                ("session_manifest_id", session.session_manifest_id),
                ("session_manifest_hash", session.canonical_hash),
                ("snapshot_id", snapshot.governance_snapshot_id),
                ("snapshot_hash", snapshot.canonical_snapshot_hash),
                ("evidence_manifest_id", snapshot.evidence_manifest_id),
                ("evidence_manifest_hash", snapshot.evidence_manifest_hash),
                ("state_at", snapshot.state_at),
                ("known_at", snapshot.known_at),
                ("perspective", snapshot.perspective),
            ),
        ),
        "",
    ]


def _object_table(objects: Sequence[TraceObjectView]) -> list[str]:
    rows = []
    for item in sorted(objects, key=_stable_object_key):
        rows.append(
            (
                item.object_id,
                item.object_kind,
                item.status or "",
                item.canonical_hash or "",
                item.summary or "",
                _ids(tuple(sorted(item.reference_ids))),
                _json_text(item.details) if item.details else "",
            )
        )
    return _table(
        ("object_id", "kind", "status", "hash", "summary", "references", "details"),
        rows,
    )


def _binding_checks(
    requested_report_id: str,
    projection: GovernanceTraceProjection,
) -> tuple[_TraceCheck, ...]:
    report = projection.report
    session = projection.session_manifest
    snapshot = projection.snapshot
    input_pack = projection.input_pack
    checks = (
        _TraceCheck(
            "requested_report_identity",
            report.governance_report_id == requested_report_id,
            "loaded report identity matches the requested authoritative ID",
        ),
        _TraceCheck(
            "report_session_identity",
            report.session_manifest_id == session.session_manifest_id
            and report.session_manifest_hash == session.canonical_hash,
            "report is bound to the loaded session manifest ID and hash",
        ),
        _TraceCheck(
            "report_snapshot_identity",
            report.governance_snapshot_id == snapshot.governance_snapshot_id
            and report.governance_snapshot_hash == snapshot.canonical_snapshot_hash
            and session.final_snapshot_id == snapshot.governance_snapshot_id,
            "report and session final binding resolve to the loaded snapshot",
        ),
        _TraceCheck(
            "report_manifest_identity",
            report.evidence_manifest_id == snapshot.evidence_manifest_id
            and report.evidence_manifest_hash == snapshot.evidence_manifest_hash,
            "report and snapshot use one evidence manifest",
        ),
        _TraceCheck(
            "input_pack_identity",
            session.input_pack_id == input_pack.input_pack_id
            and session.input_pack_hash == input_pack.canonical_hash
            and session.initial_snapshot_id == input_pack.governance_snapshot_id,
            "session input pack and initial snapshot identities match",
        ),
        _TraceCheck(
            "generation_status_identity",
            report.generation_status == session.generation_status,
            "report and session generation states match",
        ),
        _TraceCheck(
            "report_hash_identity",
            session.report_hash in {None, report.canonical_report_hash},
            "session report hash is absent only for an unfinished failure or matches",
        ),
        _TraceCheck(
            "citation_identity",
            tuple(report.citation_ids) == tuple(session.final_citation_ids),
            "report and session expose the same final citation index",
        ),
    )
    return checks


def _validate_authoritative_projection(
    requested_report_id: str,
    projection: GovernanceTraceProjection,
) -> None:
    """Fail before rendering if any authoritative object was modified."""

    try:
        GovernanceReport.model_validate(
            projection.report.model_dump(mode="python"), strict=True
        )
        CodexSessionManifest.model_validate(
            projection.session_manifest.model_dump(mode="python"), strict=True
        )
        CodexInputPack.model_validate(
            projection.input_pack.model_dump(mode="python"), strict=True
        )
        if (
            projection.report.canonical_report_hash
            != canonical_governance_report_hash(projection.report)
        ):
            raise TraceRenderError("governance report canonical hash mismatch")
        if (
            projection.session_manifest.canonical_hash
            != canonical_session_manifest_hash(projection.session_manifest)
        ):
            raise TraceRenderError("Codex session manifest canonical hash mismatch")
        input_hash = canonical_input_pack_hash(projection.input_pack)
        if (
            projection.input_pack.canonical_hash != input_hash
            or projection.input_pack.input_pack_id != f"govinput:{input_hash}"
        ):
            raise TraceRenderError("Codex input pack canonical hash mismatch")
        validate_governance_snapshot_semantic_hash(
            projection.snapshot,
            projection.snapshot_semantic_payload,
        )
    except SnapshotIntegrityError as exc:
        raise TraceRenderError(
            f"governance snapshot semantic integrity failed: {exc.code}"
        ) from exc
    except TraceRenderError:
        raise
    except (TypeError, ValueError) as exc:
        raise TraceRenderError("authoritative trace object schema validation failed") from exc

    failed_bindings = tuple(
        item.code
        for item in _binding_checks(requested_report_id, projection)
        if not item.passed
    )
    if failed_bindings:
        raise TraceRenderError(
            "authoritative trace bindings failed: " + ", ".join(failed_bindings)
        )


class TraceRenderer:
    """Deterministically render all human-readable traces from authoritative IDs."""

    def __init__(
        self,
        loader: GovernanceTraceProjectionLoader,
        *,
        renderer_version: str = TRACE_RENDERER_VERSION,
        output_root: str | Path | None = None,
    ) -> None:
        if not renderer_version.strip():
            raise TraceRenderError("renderer_version must be non-empty")
        self._loader = loader
        self.renderer_version = renderer_version
        self._output_root = (
            None if output_root is None else Path(output_root).resolve(strict=False)
        )

    def render(self, report_id: str) -> dict[str, bytes]:
        if not isinstance(report_id, str) or not report_id.strip():
            raise TraceRenderError("report_id must be non-empty")
        projection = self._loader.load_trace_projection(report_id)
        _validate_authoritative_projection(report_id, projection)
        renderers = (
            self._render_acquisition_coverage,
            self._render_evidence_manifest,
            self._render_extraction_results,
            self._render_entity_resolution,
            self._render_governance_reconstruction,
            self._render_codex_input_pack,
            self._render_codex_assessment,
            lambda item: self._render_report_validation(report_id, item),
        )
        rendered: dict[str, bytes] = {}
        for filename, renderer in zip(TRACE_FILENAMES, renderers, strict=True):
            content = renderer(projection)
            payload = "\n".join(content).rstrip() + "\n"
            encoded = payload.encode("utf-8", errors="strict")
            encoded.decode("utf-8", errors="strict")
            rendered[filename] = encoded
        return rendered

    def render_all(self, report_id: str) -> dict[str, bytes]:
        return self.render(report_id)

    def render_to_directory(self, report_id: str, directory: str | Path) -> tuple[Path, ...]:
        if self._output_root is None:
            raise TraceRenderError("render_to_directory requires a bound output_root")
        root = self._output_root
        root.mkdir(parents=True, exist_ok=True)
        requested = Path(directory)
        if not requested.is_absolute():
            requested = root / requested
        target = requested.resolve(strict=False)
        try:
            target.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise TraceRenderError("trace output escapes the configured root") from exc
        target.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for filename, payload in self.render(report_id).items():
            destination = target / filename
            if destination.is_symlink():
                raise TraceRenderError("trace destination cannot be a symbolic link")
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".governance-trace-",
                suffix=".tmp",
                dir=target,
                delete=False,
            )
            staging = Path(handle.name)
            try:
                with handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging, destination)
            finally:
                if staging.exists():
                    staging.unlink()
            written.append(destination)
        return tuple(written)

    def _render_acquisition_coverage(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("01 采集覆盖", projection, self.renderer_version)
        lines.extend(("## 问题级覆盖", ""))
        lines.extend(
            _table(
                ("question_id", "completeness_status", "coverage_entry_ids"),
                [
                    (
                        item.question_id,
                        item.completeness_status,
                        _ids(item.coverage_entry_ids),
                    )
                    for item in sorted(
                        projection.snapshot.question_level_coverage,
                        key=lambda value: value.question_id,
                    )
                ],
            )
        )
        lines.extend(("", "## 采集控制对象", ""))
        lines.extend(_object_table(projection.acquisition_objects))
        return lines

    def _render_evidence_manifest(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("02 证据清单", projection, self.renderer_version)
        lines.extend(("## Manifest 投影", ""))
        lines.extend(
            _table(
                ("字段", "值"),
                (
                    ("canonical_record_ids", _ids(projection.snapshot.canonical_record_ids)),
                    ("record_link_count", len(projection.snapshot.record_links)),
                    ("anchor_link_count", len(projection.snapshot.anchor_links)),
                    ("delta_link_count", len(projection.snapshot.delta_links)),
                ),
            )
        )
        lines.extend(("", "## 证据对象", ""))
        lines.extend(_object_table(projection.evidence_manifest_objects))
        return lines

    def _render_extraction_results(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("03 抽取结果", projection, self.renderer_version)
        lines.extend(("## Snapshot 记录链接", ""))
        lines.extend(
            _table(
                ("record_id", "record_kind", "role", "hash"),
                [
                    (link.record_id, link.record_kind, link.role, link.canonical_hash)
                    for link in sorted(
                        projection.snapshot.record_links,
                        key=lambda item: (item.record_kind, item.record_id),
                    )
                ],
            )
        )
        lines.extend(
            (
                "",
                "## 待定候选",
                "",
                f"- pending_candidate_ids: {_markdown(_ids(projection.snapshot.pending_candidate_ids))}",
                "",
                "## 抽取与校验对象",
                "",
            )
        )
        lines.extend(_object_table(projection.extraction_objects))
        return lines

    def _render_entity_resolution(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("04 实体解析", projection, self.renderer_version)
        lines.extend(
            (
                "## 身份、别名、任期与跨公司链接决定",
                "",
            )
        )
        lines.extend(_object_table(projection.entity_resolution_objects))
        lines.extend(
            (
                "",
                "## 活动身份相关不确定性",
                "",
                f"- conflict_ids: {_markdown(_ids(projection.snapshot.active_conflict_ids))}",
                f"- candidate_ids: {_markdown(_ids(projection.snapshot.pending_candidate_ids))}",
            )
        )
        return lines

    def _render_governance_reconstruction(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("05 治理状态重建", projection, self.renderer_version)
        lines.extend(("## 锚点", ""))
        lines.extend(
            _table(
                (
                    "question_id",
                    "state_kind",
                    "anchor_record_id",
                    "reference_at",
                    "available_at",
                    "hash",
                ),
                [
                    (
                        link.question_id,
                        link.state_kind,
                        link.anchor_record_id,
                        link.reference_at,
                        link.available_at,
                        link.canonical_hash,
                    )
                    for link in sorted(
                        projection.snapshot.anchor_links,
                        key=lambda item: (
                            item.question_id,
                            item.state_kind,
                            item.anchor_record_id,
                        ),
                    )
                ],
            )
        )
        lines.extend(("", "## 已应用与排除的增量", ""))
        lines.extend(
            _table(
                (
                    "sequence",
                    "question_id",
                    "delta_record_id",
                    "disposition",
                    "effective_at",
                    "available_at",
                    "exclusion_reason",
                    "hash",
                ),
                [
                    (
                        link.sequence,
                        link.question_id,
                        link.delta_record_id,
                        link.disposition,
                        link.effective_at,
                        link.available_at,
                        link.exclusion_reason or "",
                        link.canonical_hash,
                    )
                    for link in sorted(
                        projection.snapshot.delta_links,
                        key=lambda item: item.sequence,
                    )
                ],
            )
        )
        return lines

    def _render_codex_input_pack(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        pack = projection.input_pack
        lines = _projection_header("06 Codex 输入包", projection, self.renderer_version)
        lines.extend(
            _table(
                ("字段", "值"),
                (
                    ("input_pack_id", pack.input_pack_id),
                    ("input_pack_hash", pack.canonical_hash),
                    ("initial_snapshot_id", pack.governance_snapshot_id),
                    ("initial_snapshot_hash", pack.governance_snapshot_hash),
                    ("active_gap_ids", _ids(pack.active_gap_ids)),
                    ("active_conflict_ids", _ids(pack.active_conflict_ids)),
                    ("pending_candidate_ids", _ids(pack.pending_candidate_ids)),
                    ("important_event_ids", _ids(pack.important_event_ids)),
                    ("temporal_rules", _ids(pack.temporal_rules)),
                    ("report_output_schema_hash", pack.report_output_schema_hash),
                ),
            )
        )
        lines.extend(("", "## 问题摘要", ""))
        lines.extend(
            _table(
                (
                    "question_id",
                    "completeness_status",
                    "coverage_entry_ids",
                    "anchor_record_ids",
                    "summary",
                ),
                [
                    (
                        item.question_id,
                        item.completeness_status,
                        _ids(item.coverage_entry_ids),
                        _ids(item.anchor_record_ids),
                        item.summary,
                    )
                    for item in pack.question_summaries
                ],
            )
        )
        return lines

    def _render_codex_assessment(
        self, projection: GovernanceTraceProjection
    ) -> list[str]:
        lines = _projection_header("07 Codex 治理判断", projection, self.renderer_version)
        labels = (
            ("fact", "正式事实"),
            ("contextual", "外部背景"),
            ("judgment", "Codex 判断"),
        )
        for kind, label in labels:
            lines.extend((f"## {_markdown(label)}", ""))
            rows = []
            for section in projection.report.sections:
                for finding in section.findings:
                    if finding.kind != kind:
                        continue
                    rows.append(
                        (
                            section.section_id,
                            finding.finding_id,
                            finding.text,
                            _ids(finding.citation_ids),
                            finding.uncertainty or "",
                            getattr(getattr(finding, "source_role", None), "value", ""),
                        )
                    )
            lines.extend(
                _table(
                    (
                        "section_id",
                        "finding_id",
                        "text",
                        "citation_ids",
                        "uncertainty",
                        "source_role",
                    ),
                    rows,
                )
            )
            lines.append("")
        return lines

    def _render_report_validation(
        self,
        requested_report_id: str,
        projection: GovernanceTraceProjection,
    ) -> list[str]:
        report = projection.report
        snapshot = projection.snapshot
        binding_checks = _binding_checks(requested_report_id, projection)
        lines = _projection_header("08 报告校验", projection, self.renderer_version)
        lines.extend(("## Hard checks", ""))
        hard_rows = [
            (item.code, item.passed, item.detail) for item in binding_checks
        ]
        hard_rows.extend(
            (item.check_code, item.passed, item.detail or "")
            for item in report.technical_validation.checks
        )
        lines.extend(_table(("check_code", "passed", "detail"), hard_rows))
        lines.extend(
            (
                "",
                f"- technical_validation_passed: {_markdown(report.technical_validation.passed)}",
                f"- hard_failure_codes: {_markdown(_ids(report.technical_validation.hard_failure_codes))}",
                f"- trace_binding_failures: {_markdown(_ids(tuple(item.code for item in binding_checks if not item.passed)))}",
                f"- failure_code: {_markdown(report.failure_code or 'none')}",
                "",
                "## Soft states",
                "",
            )
        )
        soft_rows: list[tuple[Any, ...]] = [
            ("snapshot_completeness", snapshot.completeness_status, snapshot.governance_snapshot_id),
        ]
        soft_rows.extend(("active_gap", "open", item) for item in snapshot.active_gap_ids)
        soft_rows.extend(
            ("active_conflict", "open", item) for item in snapshot.active_conflict_ids
        )
        soft_rows.extend(
            ("pending_candidate", "pending", item)
            for item in snapshot.pending_candidate_ids
        )
        soft_rows.extend(
            ("data_limitation", "reported", item) for item in report.data_limitations
        )
        soft_rows.extend(
            (item.object_kind, item.status or "reported", item.object_id)
            for item in sorted(projection.soft_state_objects, key=_stable_object_key)
        )
        lines.extend(_table(("kind", "status", "object_or_detail"), soft_rows))
        return lines


__all__ = [
    "GovernanceTraceProjection",
    "GovernanceTraceProjectionLoader",
    "InMemoryGovernanceTraceProjectionLoader",
    "TRACE_FILENAMES",
    "TRACE_RENDERER_VERSION",
    "TraceObjectView",
    "TraceRenderError",
    "TraceRenderer",
]
