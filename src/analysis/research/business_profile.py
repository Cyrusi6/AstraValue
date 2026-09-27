"""Research-workspace adapter for the evidence-bound business profile.

The profile builder still consumes the append-only business-evidence store and a
frozen acquisition corpus.  This adapter only binds its verified result to the
active research snapshot; it does not publish a separate report or acquire data.
"""
from __future__ import annotations

from pathlib import Path

from analysis.business_evidence.corpus import FrozenCorpus
from analysis.business_evidence.profile import ProfileInput, build_profile
from analysis.business_evidence.store import FactStore


class BusinessProfiles:
    def __init__(self, workspace):
        self.workspace = workspace

    def build(
        self,
        research_id: str,
        profile: ProfileInput | dict,
        facts_db: Path,
        acquisition_db: Path,
        data_root: Path,
        manifest_id: str,
    ) -> dict:
        """Build and bind one profile to the workspace snapshot.

        ``facts_db`` and ``acquisition_db`` must belong to the same storage
        namespace.  The corpus constructor re-checks the namespace marker and
        every citation is revalidated by ``build_profile`` before persistence.
        """
        spec = profile if isinstance(profile, ProfileInput) else ProfileInput.model_validate(profile)
        corpus = FrozenCorpus(acquisition_db, data_root, manifest_id)
        store = FactStore(facts_db, corpus.manifest.storage_namespace_id)
        result = build_profile(spec, store, corpus, Path(data_root))
        return self.workspace.save_business_profile(research_id, result)

    def list(self, research_id: str) -> list[dict]:
        return self.workspace.business_profiles(research_id)

    def get(self, research_id: str, profile_id: str | None = None) -> dict | None:
        profiles = self.list(research_id)
        if profile_id is None:
            return profiles[-1] if profiles else None
        return next((item for item in profiles if item.get("profile_id") == profile_id
                     or item.get("artifact_id") == profile_id), None)
