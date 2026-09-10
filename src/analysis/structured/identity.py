from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping, Sequence


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    PREREQUISITE_REQUIRED = "prerequisite_required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SecurityAlias:
    name: str
    valid_from: date | None = None
    valid_to: date | None = None

    def valid_at(self, value: date) -> bool:
        return (self.valid_from is None or self.valid_from <= value) and (
            self.valid_to is None or value <= self.valid_to
        )


@dataclass(frozen=True, slots=True)
class SecurityIdentity:
    company_id: str
    security_id: str
    canonical_ticker: str
    security_code: str
    market: str
    current_name: str
    security_type: str
    listing_date: date | None = None
    delisting_date: date | None = None
    aliases: tuple[SecurityAlias, ...] = ()
    source_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        market = self.market.upper()
        if market not in {"SSE", "SZSE", "BSE"}:
            raise ValueError(f"unsupported market identity: {self.market}")
        if not self.security_code.isdigit() or len(self.security_code) != 6:
            raise ValueError("security identity requires a six-digit code")
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[market]
        if self.canonical_ticker.upper() != f"{self.security_code}.{suffix}":
            raise ValueError("canonical ticker and market identity disagree")
        if not self.company_id or not self.security_id or not self.current_name:
            raise ValueError("company, security and current name are required")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "canonical_ticker", self.canonical_ticker.upper())

    @property
    def is_target_a_share(self) -> bool:
        return self.security_type.upper() in {"A", "A_SHARE", "A-SHARE"}


@dataclass(frozen=True, slots=True)
class CompanyResolution:
    query: str
    status: ResolutionStatus
    identity: SecurityIdentity | None
    candidates: tuple[SecurityIdentity, ...]
    prerequisite_dataset_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    performed_io: bool = False


class CompanyResolver:
    """Resolve only from a supplied, versioned local security master.

    The resolver deliberately has no transport dependency.  A cache miss is a
    B08 identity prerequisite (and, only when B08 is incomplete, C01), not an
    excuse to guess by ticker prefix or silently use the first candidate.
    """

    def __init__(self, identities: Iterable[SecurityIdentity]) -> None:
        values = tuple(identities)
        keys = [(item.security_id, item.canonical_ticker) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("security master contains duplicate identities")
        self._identities = values

    def resolve(
        self,
        query: str,
        *,
        as_of: date,
        market: str | None = None,
    ) -> CompanyResolution:
        raw = query.strip()
        if not raw:
            raise ValueError("company query is required")
        requested_code, requested_market = _parse_security_query(raw)
        explicit_market = _normalize_market(market) if market else requested_market
        if market and requested_market and explicit_market != requested_market:
            matches = tuple(
                item for item in self._identities if item.security_code == requested_code
            )
            return CompanyResolution(
                raw,
                ResolutionStatus.AMBIGUOUS,
                None,
                matches,
                reason_codes=("market_parameter_conflict",),
            )

        if requested_code is not None:
            matches = tuple(
                item
                for item in self._identities
                if item.security_code == requested_code
                and (explicit_market is None or item.market == explicit_market)
            )
            if not matches:
                same_code = tuple(
                    item for item in self._identities if item.security_code == requested_code
                )
                if same_code:
                    return CompanyResolution(
                        raw,
                        ResolutionStatus.AMBIGUOUS,
                        None,
                        same_code,
                        reason_codes=("market_identity_conflict",),
                    )
                return CompanyResolution(
                    raw,
                    ResolutionStatus.PREREQUISITE_REQUIRED,
                    None,
                    (),
                    ("B08", "C01"),
                    ("security_master_cache_miss",),
                )
        else:
            matches = tuple(
                item
                for item in self._identities
                if (explicit_market is None or item.market == explicit_market)
                and _name_matches(item, raw, as_of)
            )
            if not matches:
                return CompanyResolution(
                    raw,
                    ResolutionStatus.PREREQUISITE_REQUIRED,
                    None,
                    (),
                    ("B08", "C01"),
                    ("name_master_cache_miss",),
                )

        if len(matches) != 1:
            return CompanyResolution(
                raw,
                ResolutionStatus.AMBIGUOUS,
                None,
                tuple(sorted(matches, key=lambda item: item.security_id)),
                reason_codes=("multiple_security_candidates",),
            )
        identity = matches[0]
        if not identity.is_target_a_share:
            return CompanyResolution(
                raw,
                ResolutionStatus.UNSUPPORTED,
                None,
                (identity,),
                reason_codes=("non_a_share_security",),
            )
        if identity.market == "BSE":
            return CompanyResolution(
                raw,
                ResolutionStatus.UNSUPPORTED,
                None,
                (identity,),
                reason_codes=("bse_protocol_support_unverified",),
            )
        reasons: list[str] = []
        if identity.delisting_date is not None and identity.delisting_date <= as_of:
            reasons.append("delisted_identity_resolved_history_may_be_limited")
        return CompanyResolution(
            raw,
            ResolutionStatus.RESOLVED,
            identity,
            (identity,),
            reason_codes=tuple(reasons),
        )


def _name_matches(identity: SecurityIdentity, query: str, as_of: date) -> bool:
    if identity.current_name.casefold() == query.casefold():
        return True
    return any(
        item.name.casefold() == query.casefold() and item.valid_at(as_of)
        for item in identity.aliases
    )


def _normalize_market(value: str) -> str:
    normalized = value.strip().upper().replace(".", "")
    aliases = {
        "SH": "SSE",
        "SSE": "SSE",
        "XSHG": "SSE",
        "SZ": "SZSE",
        "SZSE": "SZSE",
        "XSHE": "SZSE",
        "BJ": "BSE",
        "BSE": "BSE",
    }
    if normalized not in aliases:
        raise ValueError(f"unknown market: {value}")
    return aliases[normalized]


def _parse_security_query(value: str) -> tuple[str | None, str | None]:
    normalized = value.strip().upper().replace(" ", "")
    if normalized.isdigit():
        return (normalized, None) if len(normalized) == 6 else (None, None)
    for separator in (".", "-"):
        if separator in normalized:
            left, right = normalized.split(separator, 1)
            if left.isdigit() and len(left) == 6:
                return left, _normalize_market(right)
    for prefix in ("SH", "SZ", "BJ"):
        if normalized.startswith(prefix) and normalized[2:].isdigit() and len(normalized[2:]) == 6:
            return normalized[2:], _normalize_market(prefix)
    return None, None


class ProfileDecisionStatus(str, Enum):
    RESOLVED = "resolved"
    MIXED = "mixed"
    PENDING = "pending"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SegmentProfileEvidence:
    period_key: str
    profile_id: str
    revenue_share: Decimal
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.period_key or not self.profile_id or not self.evidence_ids:
            raise ValueError("segment classification requires period, profile and evidence")
        if self.revenue_share < 0 or self.revenue_share > 1:
            raise ValueError("segment revenue share must be between zero and one")


@dataclass(frozen=True, slots=True)
class IndustrySignals:
    company_id: str
    source_profile_ids: tuple[str, ...] = ()
    source_evidence_ids: tuple[str, ...] = ()
    company_type_profile_id: str | None = None
    company_type_evidence_ids: tuple[str, ...] = ()
    segments: tuple[SegmentProfileEvidence, ...] = ()
    preprofit: bool = False
    preprofit_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndustryProfileDecision:
    status: ProfileDecisionStatus
    profile_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    common_acquisition_allowed: bool = True
    general_fallback_used: bool = False


class IndustryProfileRouter:
    def __init__(self, known_profile_ids: Iterable[str]) -> None:
        self._known = frozenset(known_profile_ids)
        if "general" not in self._known:
            raise ValueError("industry registry must explicitly contain general")

    def select(self, signals: IndustrySignals) -> IndustryProfileDecision:
        candidates: set[str] = set(signals.source_profile_ids)
        evidence: set[str] = set(signals.source_evidence_ids)
        if signals.source_profile_ids and not signals.source_evidence_ids:
            raise ValueError("source classifications require evidence")
        if signals.company_type_profile_id:
            if not signals.company_type_evidence_ids:
                raise ValueError("report companyType requires independent evidence")
            candidates.add(signals.company_type_profile_id)
            evidence.update(signals.company_type_evidence_ids)

        by_profile: dict[str, dict[str, SegmentProfileEvidence]] = {}
        for item in signals.segments:
            by_profile.setdefault(item.profile_id, {})[item.period_key] = item
        stable_segment_profiles: set[str] = set()
        for profile_id, periods in by_profile.items():
            material = [item for item in periods.values() if item.revenue_share > Decimal("0.5")]
            if len(material) >= 2:
                stable_segment_profiles.add(profile_id)
                for item in material:
                    evidence.update(item.evidence_ids)
        candidates.update(stable_segment_profiles)

        unknown = candidates.difference(self._known)
        if unknown:
            raise ValueError("industry signals reference unknown profiles: " + ", ".join(sorted(unknown)))
        base_candidates = candidates.difference({"preprofit"})
        overlays: set[str] = set()
        if signals.preprofit:
            if not signals.preprofit_evidence_ids:
                raise ValueError("preprofit overlay requires evidence")
            overlays.add("preprofit")
            evidence.update(signals.preprofit_evidence_ids)

        if not base_candidates:
            return IndustryProfileDecision(
                ProfileDecisionStatus.PENDING,
                tuple(sorted(overlays)),
                tuple(sorted(evidence)),
                ("industry_profile_evidence_missing",),
            )
        if len(base_candidates) > 1 and len(stable_segment_profiles) <= 1:
            return IndustryProfileDecision(
                ProfileDecisionStatus.CONFLICT,
                tuple(sorted(overlays)),
                tuple(sorted(evidence)),
                ("classification_sources_conflict",),
            )
        status = (
            ProfileDecisionStatus.MIXED
            if len(base_candidates) > 1
            else ProfileDecisionStatus.RESOLVED
        )
        return IndustryProfileDecision(
            status,
            tuple(sorted(base_candidates | overlays)),
            tuple(sorted(evidence)),
            ("mixed_business_profiles" if status is ProfileDecisionStatus.MIXED else "profile_evidenced",),
        )


class PeerDecisionKind(str, Enum):
    RULE_SELECTED = "rule_selected"
    USER_SELECTED = "user_selected"


@dataclass(frozen=True, slots=True)
class PeerCandidate:
    identity: SecurityIdentity
    profile_ids: tuple[str, ...]
    common_segment_ids: tuple[str, ...]
    principal_segment_share: Decimal | None
    business_model_match: bool | None
    region_match: bool | None
    comparison_period: str | None
    revenue: Decimal | None
    net_profit: Decimal | None
    evidence_ids: tuple[str, ...]
    user_selected: bool = False


@dataclass(frozen=True, slots=True)
class PeerDecision:
    security_id: str
    canonical_ticker: str
    selected: bool
    decision_kind: PeerDecisionKind | None
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PeerSelection:
    target_security_id: str
    decisions: tuple[PeerDecision, ...]
    selected_security_ids: tuple[str, ...]
    metric_subsets: Mapping[str, tuple[str, ...]]
    recursive_expansion: bool = False


def select_peers(
    *,
    target: PeerCandidate,
    candidates: Iterable[PeerCandidate],
    company_scope: str,
    max_peers: int = 6,
) -> PeerSelection:
    if company_scope not in {"company-only", "company-with-peers"}:
        raise ValueError("peer selection scope must be company-only or company-with-peers")
    if max_peers < 0 or max_peers > 6:
        raise ValueError("default peer selection is bounded to at most six")
    values = tuple(candidates)
    if company_scope == "company-only":
        decisions = tuple(
            PeerDecision(
                item.identity.security_id,
                item.identity.canonical_ticker,
                False,
                None,
                ("company_only_scope",),
                item.evidence_ids,
            )
            for item in values
        )
        return PeerSelection(target.identity.security_id, decisions, (), {"operating": (), "pe": ()})

    scored: list[tuple[Decimal, str, PeerCandidate]] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for item in values:
        reasons: list[str] = []
        if item.identity.security_id == target.identity.security_id:
            reasons.append("target_is_not_its_own_peer")
        if not item.identity.is_target_a_share or item.identity.market == "BSE":
            reasons.append("unsupported_security_identity")
        if not set(item.profile_ids).intersection(target.profile_ids):
            reasons.append("industry_profile_mismatch")
        if not item.common_segment_ids:
            reasons.append("common_principal_business_missing")
        if item.principal_segment_share is None or item.principal_segment_share <= Decimal("0.5"):
            reasons.append("principal_business_share_insufficient")
        if item.business_model_match is not True:
            reasons.append("business_model_unconfirmed")
        if item.region_match is not True:
            reasons.append("region_comparability_unconfirmed")
        if not item.comparison_period or item.comparison_period != target.comparison_period:
            reasons.append("comparison_period_mismatch")
        if not item.evidence_ids:
            reasons.append("peer_evidence_missing")
        if reasons and not item.user_selected:
            rejected[item.identity.security_id] = tuple(reasons)
            continue
        if item.revenue is None or target.revenue in (None, Decimal("0")):
            distance = Decimal("Infinity")
        else:
            distance = abs(item.revenue / target.revenue - Decimal("1"))
        scored.append((distance, item.identity.security_id, item))

    selected = [item for _, _, item in sorted(scored, key=lambda row: (row[0], row[1]))[:max_peers]]
    selected_ids = {item.identity.security_id for item in selected}
    decisions: list[PeerDecision] = []
    for item in values:
        if item.identity.security_id in selected_ids:
            kind = (
                PeerDecisionKind.USER_SELECTED
                if item.user_selected
                else PeerDecisionKind.RULE_SELECTED
            )
            reasons = ("user_selected",) if item.user_selected else ("bounded_business_comparability",)
            decisions.append(
                PeerDecision(
                    item.identity.security_id,
                    item.identity.canonical_ticker,
                    True,
                    kind,
                    reasons,
                    item.evidence_ids,
                )
            )
        else:
            decisions.append(
                PeerDecision(
                    item.identity.security_id,
                    item.identity.canonical_ticker,
                    False,
                    None,
                    rejected.get(item.identity.security_id, ("outside_bounded_top_six",)),
                    item.evidence_ids,
                )
            )
    operating = tuple(item.identity.security_id for item in selected)
    pe = tuple(
        item.identity.security_id
        for item in selected
        if item.net_profit is not None and item.net_profit > 0
    )
    return PeerSelection(
        target.identity.security_id,
        tuple(decisions),
        operating,
        {"operating": operating, "pe": pe},
    )
