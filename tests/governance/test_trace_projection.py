from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from analysis.governance.canonical import canonical_sha256
from analysis.governance.codex_tools import (
    canonical_input_pack_hash,
    canonical_session_manifest_hash,
)
from analysis.governance.models import (
    SnapshotAnchorLink,
    SnapshotDeltaLink,
    SnapshotRecordLink,
)
from analysis.governance.report_service import canonical_governance_report_hash
from analysis.governance.snapshot_service import (
    SnapshotAnchorInput,
    SnapshotDeltaInput,
    canonical_governance_snapshot_hash,
)
from analysis.governance.trace_renderer import (
    TRACE_FILENAMES,
    GovernanceTraceProjection,
    InMemoryGovernanceTraceProjectionLoader,
    TraceObjectView,
    TraceRenderError,
    TraceRenderer,
)

from .test_report_rendering import _make_bundle


def _trace_ready_bundle(bundle):
    old = bundle.snapshot
    anchors = tuple(
        SnapshotAnchorInput(
            question_id=link.question_id,
            state_kind=link.state_kind,
            anchor_record_id=link.anchor_record_id,
            reference_at=link.reference_at,
            available_at=link.available_at,
        )
        for link in old.anchor_links
    )
    deltas = tuple(
        SnapshotDeltaInput(
            question_id=link.question_id,
            delta_record_id=link.delta_record_id,
            disposition=link.disposition,
            effective_at=link.effective_at,
            available_at=link.available_at,
            announced_at=link.available_at,
            exclusion_reason=link.exclusion_reason,
        )
        for link in old.delta_links
    )
    record_ids = tuple(
        sorted(
            {
                *old.canonical_record_ids,
                *(item.anchor_record_id for item in anchors),
                *(item.delta_record_id for item in deltas),
            }
        )
    )
    record_hashes = {
        record_id: canonical_sha256(
            record_id,
            schema_name="trace-fixture-record",
            schema_version="1",
        )
        for record_id in record_ids
    }
    semantic_payload = {
        "namespace": "trace:render",
        "company_id": old.company_id,
        "state_at": old.state_at,
        "known_at": old.known_at,
        "state_time_precision": old.state_time_precision,
        "known_time_precision": old.known_time_precision,
        "original_timezone": old.original_timezone,
        "perspective": old.perspective,
        "acquisition_scope": old.acquisition_scope,
        "question_set_id": old.question_set_id,
        "question_set_version": old.question_set_version,
        "source_registry_version": old.source_registry_version,
        "query_pack_version": old.query_pack_version,
        "extractor_versions": old.extractor_versions,
        "reconstruction_version": old.reconstruction_version,
        "evidence_manifest_id": old.evidence_manifest_id,
        "evidence_manifest_hash": old.evidence_manifest_hash,
        "anchors": anchors,
        "deltas": deltas,
        "records": tuple((record_id, record_hashes[record_id]) for record_id in record_ids),
        "coverage": old.question_level_coverage,
        "active_gap_ids": old.active_gap_ids,
        "active_conflict_ids": old.active_conflict_ids,
        "pending_candidate_ids": old.pending_candidate_ids,
        "completeness_status": old.completeness_status,
        "future_knowledge_used": old.future_knowledge_used,
        "supersedes_snapshot_id": old.supersedes_snapshot_id,
    }
    snapshot_hash = canonical_governance_snapshot_hash(semantic_payload)
    snapshot_id = f"govsnapshot:{snapshot_hash}"

    anchor_links = []
    for item in anchors:
        identity = {
            "snapshot_id": snapshot_id,
            "question_id": item.question_id,
            "state_kind": item.state_kind,
            "anchor_record_id": item.anchor_record_id,
            "reference_at": item.reference_at,
            "available_at": item.available_at,
        }
        digest = canonical_sha256(
            identity,
            schema_name="governance-snapshot-anchor-link-id",
            schema_version="1",
        )
        provisional = SnapshotAnchorLink(
            anchor_link_id=f"govanchor:{digest}",
            governance_snapshot_id=snapshot_id,
            question_id=item.question_id,
            state_kind=item.state_kind,
            anchor_record_id=item.anchor_record_id,
            reference_at=item.reference_at,
            available_at=item.available_at,
            canonical_hash="0" * 64,
        )
        anchor_links.append(
            provisional.model_copy(
                update={"canonical_hash": provisional.calculate_canonical_hash()}
            )
        )

    delta_links = []
    for sequence, item in enumerate(deltas, start=1):
        identity = {
            "snapshot_id": snapshot_id,
            "question_id": item.question_id,
            "delta_record_id": item.delta_record_id,
            "disposition": item.disposition,
            "sequence": sequence,
            "effective_at": item.effective_at,
            "available_at": item.available_at,
            "exclusion_reason": item.exclusion_reason,
        }
        digest = canonical_sha256(
            identity,
            schema_name="governance-snapshot-delta-link-id",
            schema_version="1",
        )
        provisional = SnapshotDeltaLink(
            delta_link_id=f"govdelta:{digest}",
            governance_snapshot_id=snapshot_id,
            question_id=item.question_id,
            delta_record_id=item.delta_record_id,
            disposition=item.disposition,
            sequence=sequence,
            effective_at=item.effective_at,
            available_at=item.available_at,
            exclusion_reason=item.exclusion_reason,
            canonical_hash="0" * 64,
        )
        delta_links.append(
            provisional.model_copy(
                update={"canonical_hash": provisional.calculate_canonical_hash()}
            )
        )

    old_kind_by_id = {item.record_id: item.record_kind for item in old.record_links}
    record_links = []
    for record_id in record_ids:
        record_kind = old_kind_by_id.get(record_id, "governance_event")
        identity = {
            "snapshot_id": snapshot_id,
            "record_id": record_id,
            "record_kind": record_kind,
            "record_hash": record_hashes[record_id],
        }
        digest = canonical_sha256(
            identity,
            schema_name="governance-snapshot-record-link-id",
            schema_version="1",
        )
        provisional = SnapshotRecordLink(
            record_link_id=f"govrecordlink:{digest}",
            governance_snapshot_id=snapshot_id,
            record_id=record_id,
            record_kind=record_kind,
            canonical_hash="0" * 64,
        )
        record_links.append(
            provisional.model_copy(
                update={"canonical_hash": provisional.calculate_canonical_hash()}
            )
        )
    snapshot = old.model_copy(
        update={
            "governance_snapshot_id": snapshot_id,
            "anchor_links": tuple(sorted(anchor_links, key=lambda item: item.anchor_link_id)),
            "delta_links": tuple(delta_links),
            "record_links": tuple(
                sorted(record_links, key=lambda item: item.record_link_id)
            ),
            "canonical_record_ids": record_ids,
            "canonical_snapshot_hash": snapshot_hash,
        }
    )
    pack_seed = bundle.input_pack.model_copy(
        update={
            "governance_snapshot_id": snapshot_id,
            "governance_snapshot_hash": snapshot_hash,
        }
    )
    pack_hash = canonical_input_pack_hash(pack_seed)
    input_pack = pack_seed.model_copy(
        update={
            "input_pack_id": f"govinput:{pack_hash}",
            "canonical_hash": pack_hash,
        }
    )
    session_seed = bundle.session.model_copy(
        update={
            "input_pack_id": input_pack.input_pack_id,
            "input_pack_hash": input_pack.canonical_hash,
            "initial_snapshot_id": snapshot_id,
            "final_snapshot_id": snapshot_id,
        }
    )
    session_hash = canonical_session_manifest_hash(session_seed)
    session = session_seed.model_copy(update={"canonical_hash": session_hash})
    report_seed = bundle.report.model_copy(
        update={
            "governance_snapshot_id": snapshot_id,
            "governance_snapshot_hash": snapshot_hash,
            "session_manifest_hash": session_hash,
        }
    )
    report_hash = canonical_governance_report_hash(report_seed)
    report = report_seed.model_copy(update={"canonical_report_hash": report_hash})
    session = session.model_copy(update={"report_hash": report_hash})
    return (
        replace(
            bundle,
            report=report,
            session=session,
            snapshot=snapshot,
            input_pack=input_pack,
        ),
        semantic_payload,
    )


def _make_projection(*, failed: bool = False, sensitive: bool = False):
    bundle, snapshot_semantic_payload = _trace_ready_bundle(
        _make_bundle(failed=failed, sensitive=sensitive)
    )
    secret_details = (
        {
            "token": "trace-token-must-not-appear",
            "client_secret": "client-secret-must-not-appear",
            "private_key": "private-key-must-not-appear",
            "browser_profile": "C:\\Users\\analyst\\Browser Profile",
            "source": "https://example.invalid/evidence?cookie=trace-cookie-must-not-appear",
            "note": "cache /home/analyst/private evidence.json",
            "tmp_note": "cache /tmp/private evidence.json",
            "var_note": "cache /var/private evidence.json",
            "mnt_note": "cache /mnt/c/Users/analyst/private evidence.json",
            "\\\\wsl$\\Ubuntu\\home\\analyst\\private-key": "mapping-key-secret",
            "raw": b"binary-secret-must-not-appear",
        }
        if sensitive
        else {"attempts": 1}
    )
    projection = GovernanceTraceProjection(
        report=bundle.report,
        session_manifest=bundle.session,
        snapshot=bundle.snapshot,
        input_pack=bundle.input_pack,
        snapshot_semantic_payload=snapshot_semantic_payload,
        acquisition_objects=(
            TraceObjectView(
                object_id="shared-coverage:two",
                object_kind="coverage_entry",
                canonical_hash="2" * 64,
                status="partial",
                reference_ids=("shared-attempt:two",),
                details=secret_details,
            ),
            TraceObjectView(
                object_id="shared-coverage:one",
                object_kind="coverage_entry",
                canonical_hash="1" * 64,
                status="success",
                reference_ids=("shared-attempt:one",),
            ),
        ),
        evidence_manifest_objects=(
            TraceObjectView(
                object_id="shared-manifest:render",
                object_kind="evidence_manifest",
                canonical_hash="2" * 64,
                status="frozen",
                reference_ids=("shared-raw:one",),
            ),
        ),
        extraction_objects=(
            TraceObjectView(
                object_id="govxrun:one",
                object_kind="governance_extraction_run",
                canonical_hash="3" * 64,
                status="complete",
                reference_ids=("govclaim:pending", "govrec:tenure"),
            ),
        ),
        entity_resolution_objects=(
            TraceObjectView(
                object_id="govp:cn-600519:person-one",
                object_kind="governance_person",
                canonical_hash="4" * 64,
                status="company_local",
                reference_ids=("govrec:tenure",),
            ),
        ),
        soft_state_objects=(
            TraceObjectView(
                object_id="research:budget-one",
                object_kind="budget_exhausted",
                status="soft",
                summary="补采预算耗尽，报告继续。",
            ),
        ),
    )
    return bundle, projection


def _renderer(
    projection: GovernanceTraceProjection,
    *,
    output_root: Path | None = None,
) -> TraceRenderer:
    return TraceRenderer(
        InMemoryGovernanceTraceProjectionLoader(
            {projection.report.governance_report_id: projection}
        ),
        output_root=output_root,
    )


def test_eight_files_projection_are_exact_and_utf8() -> None:
    _bundle, projection = _make_projection()
    rendered = _renderer(projection).render(projection.report.governance_report_id)
    assert tuple(rendered) == TRACE_FILENAMES
    assert len(rendered) == 8
    for filename, payload in rendered.items():
        assert filename.endswith(".md")
        text = payload.decode("utf-8", errors="strict")
        assert projection.report.governance_report_id in text
        assert projection.snapshot.governance_snapshot_id in text
        assert projection.snapshot.evidence_manifest_id in text


def test_projection_contains_anchor_delta_and_all_active_ids() -> None:
    _bundle, projection = _make_projection()
    rendered = _renderer(projection).render_all(
        projection.report.governance_report_id
    )
    reconstruction = rendered["05-governance-reconstruction.md"].decode("utf-8")
    assert "govrec:roster" in reconstruction
    assert "govrec:appointment" in reconstruction
    assert "govrec:future" in reconstruction
    assert "applied" in reconstruction
    assert "excluded" in reconstruction
    assert r"superseded\_by\_visible\_correction" in reconstruction

    codex_input = rendered["06-codex-input-pack.md"].decode("utf-8")
    for object_id in (
        "govgap:missing-roster-history",
        "govconflict:overlap",
        "govclaim:pending",
    ):
        assert object_id in codex_input


def test_tampered_report_fails_before_trace_rendering() -> None:
    _bundle, projection = _make_projection()
    tampered = replace(
        projection,
        report=projection.report.model_copy(
            update={"data_limitations": ("tampered-after-hash",)}
        ),
    )

    with pytest.raises(TraceRenderError, match="report canonical hash mismatch"):
        _renderer(tampered).render(tampered.report.governance_report_id)


def test_tampered_session_manifest_fails_before_trace_rendering() -> None:
    _bundle, projection = _make_projection()
    tampered = replace(
        projection,
        session_manifest=projection.session_manifest.model_copy(
            update={"model_profile": "tampered-after-hash"}
        ),
    )

    with pytest.raises(TraceRenderError, match="session manifest canonical hash mismatch"):
        _renderer(tampered).render(tampered.report.governance_report_id)


def test_tampered_input_pack_fails_before_trace_rendering() -> None:
    _bundle, projection = _make_projection()
    tampered = replace(
        projection,
        input_pack=projection.input_pack.model_copy(
            update={"temporal_rules": ("tampered-after-hash",)}
        ),
    )

    with pytest.raises(TraceRenderError, match="input pack canonical hash mismatch"):
        _renderer(tampered).render(tampered.report.governance_report_id)


def test_tampered_snapshot_fails_before_trace_rendering() -> None:
    _bundle, projection = _make_projection()
    tampered = replace(
        projection,
        snapshot=projection.snapshot.model_copy(
            update={"original_timezone": "UTC"}
        ),
    )

    with pytest.raises(
        TraceRenderError,
        match="snapshot semantic integrity failed: snapshot_semantic_mismatch",
    ):
        _renderer(tampered).render(tampered.report.governance_report_id)


def test_tampered_snapshot_semantic_preimage_fails_before_trace_rendering() -> None:
    _bundle, projection = _make_projection()
    tampered_payload = dict(projection.snapshot_semantic_payload)
    tampered_payload["namespace"] = "trace:tampered"
    tampered = replace(projection, snapshot_semantic_payload=tampered_payload)

    with pytest.raises(
        TraceRenderError,
        match="snapshot semantic integrity failed: snapshot_hash_mismatch",
    ):
        _renderer(tampered).render(tampered.report.governance_report_id)


def test_assessment_projection_keeps_fact_context_and_judgment_separate() -> None:
    _bundle, projection = _make_projection()
    assessment = _renderer(projection).render(
        projection.report.governance_report_id
    )["07-codex-assessment.md"].decode("utf-8")
    fact_offset = assessment.index("## 正式事实")
    context_offset = assessment.index("## 外部背景")
    judgment_offset = assessment.index("## Codex 判断")
    assert fact_offset < context_offset < judgment_offset
    assert "govfinding:fact" in assessment[fact_offset:context_offset]
    assert "govfinding:context" in assessment[context_offset:judgment_offset]
    assert "govfinding:judgment" in assessment[judgment_offset:]


def test_redaction_removes_secrets_private_paths_and_hidden_material() -> None:
    _bundle, projection = _make_projection(sensitive=True)
    rendered = _renderer(projection).render(projection.report.governance_report_id)
    combined = b"\n".join(rendered.values())
    for forbidden in (
        b"trace-token-must-not-appear",
        b"trace-cookie-must-not-appear",
        b"client-secret-must-not-appear",
        b"private-key-must-not-appear",
        b"mapping-key-secret",
        b"should-not-appear",
        b"Browser Profile",
        b"Private Folder",
        b"/home/analyst/private",
        b"/tmp/private",
        b"/var/private",
        b"/mnt/c/Users/analyst/private",
        b"wsl$",
        b"binary-secret-must-not-appear",
    ):
        assert forbidden not in combined
    assert b"[REDACTED]" in combined or b"%5BREDACTED%5D" in combined
    assert b"REDACTED" in combined


def test_hard_failure_trace_separates_hard_checks_and_soft_states() -> None:
    _bundle, projection = _make_projection(failed=True)
    rendered = _renderer(projection).render(projection.report.governance_report_id)
    validation = rendered["08-report-validation.md"].decode("utf-8")
    assert "## Hard checks" in validation
    assert r"future\_firewall" in validation
    assert r"future\_leakage" in validation
    assert "## Soft states" in validation
    assert "govgap:missing-roster-history" in validation
    assert "govconflict:overlap" in validation
    assert "govclaim:pending" in validation
    assert r"budget\_exhausted" in validation
    assert all(payload for payload in rendered.values())


def test_deterministic_stable_render_ignores_set_like_input_order(tmp_path) -> None:
    _bundle, projection = _make_projection()
    first = _renderer(projection).render(projection.report.governance_report_id)
    second = _renderer(projection).render(projection.report.governance_report_id)
    reordered = replace(
        projection,
        acquisition_objects=tuple(reversed(projection.acquisition_objects)),
    )
    third = _renderer(reordered).render(reordered.report.governance_report_id)
    assert first == second == third

    output_dir = tmp_path / "trace"
    written = _renderer(projection, output_root=tmp_path).render_to_directory(
        projection.report.governance_report_id, output_dir
    )
    assert tuple(path.name for path in written) == TRACE_FILENAMES
    assert {path.name: path.read_bytes() for path in written} == first


def test_renderer_reloads_by_authoritative_report_id_each_time() -> None:
    _bundle, projection = _make_projection()

    class CountingLoader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def load_trace_projection(self, report_id: str) -> GovernanceTraceProjection:
            self.calls.append(report_id)
            return projection

    loader = CountingLoader()
    renderer = TraceRenderer(loader)
    renderer.render(projection.report.governance_report_id)
    renderer.render(projection.report.governance_report_id)
    assert loader.calls == [
        projection.report.governance_report_id,
        projection.report.governance_report_id,
    ]


def test_unknown_report_id_fails_without_accepting_handwritten_markdown() -> None:
    loader = InMemoryGovernanceTraceProjectionLoader({})
    with pytest.raises(TraceRenderError, match="unknown governance report"):
        TraceRenderer(loader).render("govreport:missing")


def test_trace_output_requires_bound_root_and_rejects_escape(tmp_path: Path) -> None:
    _bundle, projection = _make_projection()
    with pytest.raises(TraceRenderError, match="bound output_root"):
        _renderer(projection).render_to_directory(
            projection.report.governance_report_id,
            tmp_path / "unbound",
        )

    allowed = tmp_path / "allowed"
    renderer = _renderer(projection, output_root=allowed)
    with pytest.raises(TraceRenderError, match="escapes"):
        renderer.render_to_directory(
            projection.report.governance_report_id,
            tmp_path / "outside",
        )


def test_trace_output_rejects_symlink_escape(tmp_path: Path) -> None:
    _bundle, projection = _make_projection()
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")
    renderer = _renderer(projection, output_root=allowed)
    with pytest.raises(TraceRenderError, match="escapes"):
        renderer.render_to_directory(
            projection.report.governance_report_id,
            link,
        )


def test_unknown_trace_value_fails_closed_instead_of_stringifying() -> None:
    _bundle, projection = _make_projection()
    unsafe = replace(
        projection,
        acquisition_objects=(
            TraceObjectView(
                object_id="shared-coverage:unsafe",
                object_kind="coverage_entry",
                details={"opaque": object()},
            ),
        ),
    )
    with pytest.raises(TraceRenderError, match="safely serializable"):
        _renderer(unsafe).render(unsafe.report.governance_report_id)
