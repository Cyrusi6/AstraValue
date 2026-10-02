"""Bind business profiles to registered sources and a frozen research snapshot."""
from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

from analysis.business_evidence.corpus import FrozenCorpus
from analysis.business_evidence.models import digest
from analysis.business_evidence.profile import ProfileInput, build_profile, verify_citations
from analysis.business_evidence.store import FactStore, aware

from .workspace import ResearchError, digest as artifact_digest, sha


_ENVELOPE = {"artifact_id", "research_id", "snapshot_id", "source_binding"}


class BusinessProfiles:
    def __init__(self, workspace):
        self.workspace = workspace

    def _check_scope(self, state, profile):
        if profile.get("company_id") != state["ticker"]:
            raise ResearchError("business_profile_company_mismatch")
        try:
            cutoff = aware(profile["as_of"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ResearchError("business_profile_cutoff_required") from exc
        end = datetime.combine(date.fromisoformat(state["as_of"]), time.max,
                               timezone(timedelta(hours=8)))
        if cutoff > end:
            raise ResearchError("business_profile_future_cutoff")

    def _open_source(self, binding):
        required = {"facts_db", "acquisition_db", "data_root", "manifest_id"}
        if not isinstance(binding, dict) or not required <= binding.keys():
            raise ResearchError("business_profile_source_binding_invalid")
        root = self.workspace.path(binding["data_root"])
        corpus = FrozenCorpus(self.workspace.path(binding["acquisition_db"]), root, binding["manifest_id"])
        store = FactStore(self.workspace.path(binding["facts_db"]), corpus.manifest.storage_namespace_id)
        return store, corpus, root

    def _select_source(self, state):
        # Deployment configuration owns physical storage. Model tools only accept
        # the research ID and the analytical profile specification.
        sources = [s for s in self.workspace.config.get("business_evidence_sources", [])
                   if s.get("company_id") == state["ticker"]]
        if not sources:
            raise ResearchError("business_profile_source_unavailable:registered_reviewed_business_evidence_required")
        if len(sources) != 1:
            raise ResearchError("business_profile_source_ambiguous")
        source = sources[0]
        if not {"facts_db", "acquisition_db", "data_root", "manifest_id"} <= source.keys():
            raise ResearchError("business_profile_source_binding_invalid")
        return {key: str(self.workspace.path(source[key])) if key != "manifest_id" else source[key]
                for key in ("facts_db", "acquisition_db", "data_root", "manifest_id")}

    def _check_originals(self, profile, corpus, payload, data_root):
        evidence = payload.get("evidence", []) + payload.get("supplemental_evidence", [])
        by_hash = {}
        for item in evidence:
            if item.get("original_sha256"):
                by_hash.setdefault(item["original_sha256"], []).append(item)
        documents = {(c["manifest_id"], c["snapshot_id"], c["derived_artifact_id"])
                     for f in profile["facts"] for c in f["citations"]}
        documents.update((profile["manifest_id"], s["snapshot_id"], s["derived_artifact_id"])
                         for gap in profile["gaps"] for s in gap["searches"])
        corpora = {corpus.manifest.manifest_id: corpus}
        for manifest_id, snapshot_id, artifact_id in documents:
            if manifest_id not in corpora:
                corpora[manifest_id] = FrozenCorpus(corpus.db, data_root, manifest_id,
                                                    selection_policy=corpus.selection_policy)
            doc = corpora[manifest_id].document(snapshot_id, artifact_id)
            snap = doc["snapshot"]
            if doc["company_id"] != profile["company_id"] or snap.available_at > aware(profile["as_of"]):
                raise ResearchError("business_profile_original_scope_mismatch")
            items = by_hash.get(snap.sha256, [])
            if not items:
                raise ResearchError("business_profile_original_not_in_research_snapshot")
            for item in items:
                if str(item.get("company") or item.get("company_id") or profile["company_id"]) != profile["company_id"]:
                    raise ResearchError("business_profile_original_company_mismatch")
                path = Path(item.get("original_path") or "")
                if not path.is_file() or sha(path) != snap.sha256:
                    raise ResearchError("business_profile_original_integrity_failed")

    def build(self, research_id: str, profile: ProfileInput | dict) -> dict:
        """Build a cited profile using the task's registered, already acquired evidence."""
        state, _, _ = self.workspace.pack(research_id)
        spec = profile if isinstance(profile, ProfileInput) else ProfileInput.model_validate(profile)
        self._check_scope(state, spec.model_dump(mode="json"))
        binding = self._select_source(state)
        store, corpus, root = self._open_source(binding)
        result = build_profile(spec, store, corpus, root)
        return self.workspace.save_business_profile(research_id, result)

    def validate_for_save(self, research_id: str, profile: dict) -> dict:
        if not isinstance(profile, dict):
            raise ResearchError("business_profile_payload_invalid")
        state, _, payload = self.workspace.pack(research_id)
        self._check_scope(state, profile)
        binding = self._select_source(state)
        store, corpus, root = self._open_source(binding)
        try:
            spec = ProfileInput.model_validate(profile.get("profile_input"))
            expected = build_profile(spec, store, corpus, root)
        except (ValueError, TypeError) as exc:
            raise ResearchError("business_profile_replay_failed:" + str(exc)) from exc
        if expected != profile:
            raise ResearchError("business_profile_replay_mismatch")
        self._check_originals(expected, corpus, payload, root)
        return {**expected, "source_binding": binding}

    def validate_saved(self, research_id, artifact):
        state, _, payload = self.workspace.pack(research_id)
        if artifact.get("research_id") != research_id or artifact.get("snapshot_id") != state["snapshot_id"]:
            raise ResearchError("business_profile_snapshot_mismatch")
        value = {k: v for k, v in artifact.items() if k != "artifact_id"}
        if artifact.get("artifact_id") != "business_profile_" + artifact_digest(value)[:24]:
            raise ResearchError("business_profile_artifact_integrity_failed")
        profile = {k: v for k, v in artifact.items() if k not in _ENVELOPE}
        if profile.get("profile_id") != "profile-" + digest({k: v for k, v in profile.items() if k != "profile_id"}):
            raise ResearchError("business_profile_integrity_failed")
        self._check_scope(state, profile)
        _, corpus, root = self._open_source(artifact["source_binding"])
        if (profile["manifest_id"] != corpus.manifest.manifest_id
                or profile["manifest_hash"] != corpus.manifest.manifest_hash
                or profile["storage_namespace_id"] != corpus.manifest.storage_namespace_id):
            raise ResearchError("business_profile_manifest_mismatch")
        verify_citations(profile["facts"], corpus, root)
        self._check_originals(profile, corpus, payload, root)
        return artifact

    def list(self, research_id: str) -> list[dict]:
        return self.workspace.business_profiles(research_id)

    def get(self, research_id: str, profile_id: str | None = None) -> dict | None:
        profiles = self.list(research_id)
        if profile_id is None:
            return profiles[-1] if profiles else None
        return next((item for item in profiles if item.get("profile_id") == profile_id
                     or item.get("artifact_id") == profile_id), None)
