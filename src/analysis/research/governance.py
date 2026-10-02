"""Governance evidence artifacts in the existing research/reporting workflow."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from analysis.governance.acquisition import SharedGovernanceAcquisitionPort
from analysis.governance.canonical import canonicalize
from analysis.governance.extraction import GovernanceExtractionOrchestrator, ManifestBoundExtractionLoader
from analysis.governance.extraction.deterministic import TableContractError
from analysis.governance.extraction.orchestrator import detect_claim_conflicts
from analysis.governance.models import (
    CompletenessStatus, GovernancePerspective, GovernanceQuestionCoverageLink,
    GovernanceRecordBase,
)
from analysis.governance.reconstruction import (
    CoverageState, DeltaCandidate, GovernanceQuery, reconstruct_governance_state,
)
from analysis.governance.snapshot_service import (
    GovernanceSnapshotService, SnapshotBuildRequest, governance_snapshot_semantic_payload,
)

from .workspace import ResearchError, digest, sha


VERSION = "research-governance-v1.0.0"
_ENVELOPE = {"artifact_id", "research_id", "snapshot_id"}
_BALANCE_KINDS = {"roster_snapshot", "role_tenure", "ownership_snapshot", "pledge_position_snapshot", "control_relation"}


class Governance:
    def __init__(self, workspace):
        self.w = workspace

    def _binding(self, state):
        sources = [s for s in self.w.config.get("governance_sources", []) if s.get("company_id") == state["ticker"]]
        if not sources:
            return None
        if len(sources) != 1:
            raise ResearchError("governance_source_ambiguous")
        source = sources[0]
        if not {"acquisition_db", "data_root", "manifest_id", "source_roles"} <= source.keys():
            raise ResearchError("governance_source_binding_invalid")
        return dict(acquisition_db=str(self.w.path(source["acquisition_db"])),
                    data_root=str(self.w.path(source["data_root"])),
                    manifest_id=source["manifest_id"], source_roles=source["source_roles"])

    def _port(self, state, binding):
        cutoff = datetime.combine(date.fromisoformat(state["as_of"]), time.max, timezone(timedelta(hours=8)))
        return SharedGovernanceAcquisitionPort(acquisition_db=Path(binding["acquisition_db"]),
            data_root=Path(binding["data_root"]), manifest_id=binding["manifest_id"],
            company_id=state["ticker"], known_at=cutoff, source_roles=binding["source_roles"])

    def list_governance_materials(self, research_id: str):
        """List frozen shared-acquisition members; no download or bulk archive is started."""
        state, _, _ = self.w.pack(research_id)
        binding = self._binding(state)
        if binding is None:
            return {"status": "capability_gap", "reason": "registered_governance_manifest_required", "items": []}
        port = self._port(state, binding)
        return {"manifest_id": binding["manifest_id"], "items": port.get_manifest(binding["manifest_id"])["artifacts"]}

    def _originals(self, port, descriptors, payload):
        evidence = payload.get("evidence", []) + payload.get("supplemental_evidence", [])
        result = []
        for raw_id in sorted({d["raw_snapshot_id"] for d in descriptors}):
            snapshot = port.repository.get_raw_resource_snapshot(raw_id)
            matches = [e for e in evidence if e.get("original_sha256") == snapshot.sha256]
            if not matches:
                raise ResearchError("governance_original_not_in_research_snapshot")
            for item in matches:
                if str(item.get("company") or item.get("company_id") or port.company_id) != port.company_id:
                    raise ResearchError("governance_original_company_mismatch")
                path = Path(item.get("original_path") or "")
                if not path.is_file() or sha(path) != snapshot.sha256:
                    raise ResearchError("governance_original_integrity_failed")
            result.append(dict(raw_snapshot_id=raw_id, sha256=snapshot.sha256,
                               source_url=snapshot.canonical_url, available_at=snapshot.available_at.isoformat()))
        return result

    def _build(self, state, payload, binding, artifact_ids):
        port = self._port(state, binding)
        manifest = port.manifest
        descriptors = {d["artifact_id"]: d for d in port.get_manifest(manifest.manifest_id)["artifacts"]}
        selected = sorted(set(artifact_ids))
        if not selected or set(selected) - descriptors.keys():
            raise ResearchError("governance_manifest_member_ids_required")
        originals = self._originals(port, [descriptors[i] for i in selected], payload)
        results, gaps = [], []
        for artifact_id in selected:
            if not descriptors[artifact_id]["supported"]:
                gaps.append(dict(artifact_id=artifact_id, reason="normalized_governance_table_required"))
                continue
            stable_id = digest([VERSION, manifest.manifest_hash, artifact_id, state["ticker"]])
            extractor = GovernanceExtractionOrchestrator(ManifestBoundExtractionLoader(port),
                clock=lambda: manifest.created_at, id_factory=lambda value=stable_id: value)
            try:
                result = extractor.execute(manifest_id=manifest.manifest_id,
                                           artifact_id=artifact_id, company_id=state["ticker"])
            except TableContractError as exc:
                gaps.append(dict(artifact_id=artifact_id, reason=exc.reason_code))
                continue
            results.append(result)
            if not result.canonical_records:
                gaps.append(dict(artifact_id=artifact_id, reason="no_admitted_governance_records"))
            gaps.extend(dict(artifact_id=artifact_id, reason=reason, record_key=rejected.record_key)
                        for rejected in result.rejected_records for reason in rejected.reason_codes)
        claims = {c.claim_id: c for result in results for c in result.claims}
        spans = {s.evidence_span_id: s for result in results for s in result.evidence_spans}
        conflicts = detect_claim_conflicts(claims.values())
        records = {r.record_id: r for result in results for r in result.canonical_records
                   if isinstance(r, GovernanceRecordBase) and not (set(r.claim_ids) & conflicts)}
        coverage = port.get_coverage(manifest.manifest_id)
        coverage_by_question = {}
        for entry in coverage:
            if entry.question_id.startswith("GOV."):
                coverage_by_question.setdefault(entry.question_id, []).append(entry.coverage_entry_id)
        uncovered = {r.question_id for r in records.values()} - coverage_by_question.keys()
        if uncovered or not coverage_by_question:
            return dict(status="capability_gap", reason="shared_governance_coverage_required",
                        missing_questions=sorted(uncovered), gaps=gaps)
        if not records:
            return dict(status="capability_gap", reason="no_admitted_governance_records", gaps=gaps)
        # Source coverage describes collection work, not proof of a complete
        # governance state. Missing changes must never become a negative fact.
        gaps.append(dict(reason="governance_state_completeness_not_established"))
        query = GovernanceQuery.normalize(port.known_at, perspective="strict")
        states = []
        by_question = {}
        for record in records.values():
            if record.kind in _BALANCE_KINDS:
                gaps.append(dict(record_id=record.record_id, reason="typed_balance_event_mapping_required"))
                continue
            by_question.setdefault(record.question_id, []).append(record)
        for question, group in sorted(by_question.items()):
            # Without an explicit effective time, the wrapper uses disclosure
            # visibility only; typed reducers still validate the record's own
            # annual/interval dates. No effective date is written back.
            deltas = [DeltaCandidate(r.record_id, r,
                r.effective_at or min(claims[c].announced_at for c in r.claim_ids),
                min(claims[c].announced_at for c in r.claim_ids), r.available_at)
                for r in group]
            reconstructed = reconstruct_governance_state("interval", query, deltas=deltas,
                coverage=[CoverageState(question, CompletenessStatus.INCOMPLETE,
                                        tuple(coverage_by_question[question]))])
            states.append(dict(question_id=question, projection=canonicalize(reconstructed)))
        gap_ids = tuple(sorted({"govgap:" + digest(g)[:32] for g in gaps}))
        conflict_ids = tuple(sorted("govconflict:" + digest(c)[:32] for c in conflicts))
        request = SnapshotBuildRequest(namespace=manifest.storage_namespace_id, company_id=state["ticker"],
            state_at=port.known_at, known_at=port.known_at, perspective=GovernancePerspective.STRICT,
            question_set_version=manifest.question_set_version,
            source_registry_version=manifest.registry_version, query_pack_version=VERSION,
            extractor_versions=tuple(sorted({r.run.extractor_version for r in results})),
            reconstruction_version=VERSION, evidence_manifest_id=manifest.manifest_id,
            evidence_manifest_hash=manifest.manifest_hash, actual_manifest_hash=manifest.manifest_hash,
            canonical_records=tuple(records[k] for k in sorted(records)),
            claims=tuple(claims[k] for k in sorted(claims)), evidence_spans=tuple(spans[k] for k in sorted(spans)),
            question_level_coverage=tuple(GovernanceQuestionCoverageLink(question_id=q,
                coverage_entry_ids=tuple(sorted(ids)), completeness_status=CompletenessStatus.INCOMPLETE)
                for q, ids in sorted(coverage_by_question.items())),
            active_gap_ids=gap_ids, active_conflict_ids=conflict_ids,
            pending_candidate_ids=tuple(sorted({c for r in results for c in r.candidate_claim_ids})),
            completeness_status=CompletenessStatus.CONFLICTED if conflicts else CompletenessStatus.INCOMPLETE,
            created_at=manifest.created_at)
        snapshot = GovernanceSnapshotService(manifest.storage_namespace_id).create(request).snapshot
        return dict(title="治理取证与状态快照", version=VERSION, source_binding=binding,
            source_artifact_ids=selected, governance_snapshot_id=snapshot.governance_snapshot_id,
            governance_snapshot=snapshot.model_dump(mode="json"),
            snapshot_semantic_payload=canonicalize(governance_snapshot_semantic_payload(request)),
            extractions=[canonicalize(r) for r in results], reconstruction=states, originals=originals,
            gaps=gaps, status="evidence_available_with_gaps",
            acceptance={"human_research_acceptance": "pending", "negative_fact_inferred": False})

    def build_governance_snapshot(self, research_id: str, artifact_ids: list[str]):
        """Extract registered frozen members and persist a cited research artifact."""
        state, _, payload = self.w.pack(research_id)
        binding = self._binding(state)
        if binding is None:
            return {"status": "capability_gap", "reason": "registered_governance_manifest_required"}
        result = self._build(state, payload, binding, artifact_ids)
        if result["status"] == "capability_gap":
            return result
        return self.w.artifact(research_id, "governance_snapshot", result, expected_snapshot_id=state["snapshot_id"])

    def validate_saved(self, research_id, artifact):
        state, _, payload = self.w.pack(research_id)
        if artifact.get("research_id") != research_id or artifact.get("snapshot_id") != state["snapshot_id"]:
            raise ResearchError("governance_research_snapshot_mismatch")
        value = {k: v for k, v in artifact.items() if k != "artifact_id"}
        if artifact.get("artifact_id") != "governance_snapshot_" + digest(value)[:24]:
            raise ResearchError("governance_artifact_integrity_failed")
        expected = self._build(state, payload, artifact["source_binding"], artifact["source_artifact_ids"])
        if expected != {k: v for k, v in artifact.items() if k not in _ENVELOPE}:
            raise ResearchError("governance_replay_mismatch")
        return artifact

    def get_governance_snapshot(self, research_id: str, artifact_id: str):
        """Recheck stored facts, lineage and originals, including after restart."""
        artifact = next((a for a in self.w.artifacts(research_id, "governance_snapshot") if a["artifact_id"] == artifact_id), None)
        if artifact is None:
            raise ResearchError("governance_artifact_not_in_current_snapshot")
        return self.validate_saved(research_id, artifact)
