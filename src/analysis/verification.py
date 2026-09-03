from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import (
    ClaimKind,
    ClaimRecord,
    FactRecord,
    SourceRecord,
    VerificationRecord,
    VerificationStatus,
    new_id,
)
from .policies import load_source_policy


@dataclass
class VerificationOutcome:
    status: VerificationStatus
    accepted_value: float | None
    relative_difference: float | None = None
    reasons: list[str] = field(default_factory=list)
    accepted_fact_id: str | None = None
    conflict_dimensions: list[str] = field(default_factory=list)


def are_independent(left: SourceRecord, right: SourceRecord) -> bool:
    left_upstream = left.upstream_source_id or left.source_id
    right_upstream = right.upstream_source_id or right.source_id
    return left_upstream != right_upstream


def _is_official(source: SourceRecord) -> bool:
    policy = load_source_policy()
    return source.source_type in policy.official_source_types or source.authority_level <= 2


def _is_independent_secondary(source: SourceRecord) -> bool:
    policy = load_source_policy()
    return not _is_official(source) and source.source_type in policy.independent_source_types


def _has_required_critical_pair(
    left_sources: list[SourceRecord], right_sources: list[SourceRecord]
) -> bool:
    for left_source in left_sources:
        for right_source in right_sources:
            if not are_independent(left_source, right_source):
                continue
            if (_is_official(left_source) and _is_independent_secondary(right_source)) or (
                _is_official(right_source) and _is_independent_secondary(left_source)
            ):
                return True
    return False


def verify_pair(
    left: FactRecord,
    right: FactRecord,
    sources: dict[str, SourceRecord],
    relative_tolerance: float | None = None,
) -> VerificationOutcome:
    if relative_tolerance is None:
        relative_tolerance = load_source_policy().default_relative_tolerance
    mismatches = []
    for field_name in ("metric_id", "unit", "currency", "period_start", "period_end", "period_type", "scope"):
        if getattr(left, field_name) != getattr(right, field_name):
            mismatches.append(field_name)
    if mismatches:
        return VerificationOutcome(
            VerificationStatus.PENDING,
            None,
            reasons=[f"口径不一致: {', '.join(mismatches)}"],
            conflict_dimensions=mismatches,
        )
    if left.value is None or right.value is None:
        return VerificationOutcome(VerificationStatus.UNAVAILABLE, None, reasons=["至少一个来源没有数值"])
    left_sources = [sources[item] for item in left.source_ids if item in sources]
    right_sources = [sources[item] for item in right.source_ids if item in sources]
    independent = any(are_independent(a, b) for a in left_sources for b in right_sources)
    selected = _preferred_fact(left, right, sources)
    policy = load_source_policy()
    critical_pair_missing = (
        left.metric_id in policy.critical_metrics
        and not _has_required_critical_pair(left_sources, right_sources)
    )
    if critical_pair_missing:
        any_official = any(_is_official(item) for item in [*left_sources, *right_sources])
        if any_official and not independent:
            return VerificationOutcome(
                VerificationStatus.AUTHORITATIVE_SINGLE,
                selected.value,
                reasons=["关键字段缺少非正式独立上游复核，保留正式披露为权威单源"],
                accepted_fact_id=selected.fact_id,
            )
        if not any_official:
            return VerificationOutcome(
                VerificationStatus.PENDING,
                None,
                reasons=["关键字段缺少正式披露主源，不能由两个免费接口替代正式披露核验"],
            )
    if not independent:
        return VerificationOutcome(
            VerificationStatus.AUTHORITATIVE_SINGLE,
            selected.value,
            reasons=["两个记录没有独立上游来源，按最高权威来源保留单一事实"],
            accepted_fact_id=selected.fact_id,
        )
    denominator = max(abs(left.value), abs(right.value), 1.0)
    difference = abs(left.value - right.value) / denominator
    if critical_pair_missing:
        if difference <= relative_tolerance:
            return VerificationOutcome(
                VerificationStatus.AUTHORITATIVE_SINGLE,
                selected.value,
                difference,
                ["两个正式来源一致，但缺少非正式独立上游复核，保留权威单源状态"],
                accepted_fact_id=selected.fact_id,
            )
        return VerificationOutcome(
            VerificationStatus.PENDING,
            None,
            difference,
            [f"正式来源间差异 {difference:.4%} 超过容差 {relative_tolerance:.4%}"],
        )
    if difference <= relative_tolerance:
        return VerificationOutcome(
            VerificationStatus.DUAL_SOURCE,
            selected.value,
            difference,
            ["双源在容差内一致；不取均值，保留权威等级更高的来源值"],
            accepted_fact_id=selected.fact_id,
        )
    return VerificationOutcome(
        VerificationStatus.PENDING,
        None,
        difference,
        [f"双源差异 {difference:.4%} 超过容差 {relative_tolerance:.4%}，禁止静默取平均"],
    )


def consolidate_facts(
    facts: list[FactRecord],
    sources: list[SourceRecord],
    relative_tolerance: float | None = None,
) -> tuple[list[FactRecord], list[VerificationRecord]]:
    """按可比口径合并同步结果；冲突事实保留为待核验且绝不求平均。"""

    source_map = {item.source_id: item for item in sources}
    grouped: dict[tuple, list[FactRecord]] = defaultdict(list)
    for fact in facts:
        key = (
            fact.ticker,
            fact.metric_id,
            fact.unit,
            fact.currency,
            fact.period_start,
            fact.period_end,
            fact.period_type,
            fact.scope,
        )
        grouped[key].append(fact)

    consolidated: list[FactRecord] = []
    records: list[VerificationRecord] = []
    replacement_ids: dict[str, str] = {}
    tolerance = relative_tolerance
    if tolerance is None:
        tolerance = load_source_policy().default_relative_tolerance

    policy = load_source_policy()
    for group in grouped.values():
        active_group, superseded_fact_ids = _select_active_versions(group, source_map)
        if len(active_group) == 1:
            raw_item = active_group[0]
            item = raw_item
            authorities = [source_map[source_id].authority_level for source_id in item.source_ids if source_id in source_map]
            sources_for_item = [source_map[source_id] for source_id in item.source_ids if source_id in source_map]
            if authorities and min(authorities) <= 2 and item.value is not None:
                item = item.model_copy(update={"verification_status": VerificationStatus.AUTHORITATIVE_SINGLE})
            elif item.metric_id in policy.critical_metrics and item.value is not None:
                metadata = dict(item.metadata)
                metadata["verification_requirement"] = "正式披露+非正式独立上游"
                metadata["verification_gap"] = (
                    "缺少独立复核源" if any(_is_official(source) for source in sources_for_item) else "缺少正式披露主源"
                )
                item = item.model_copy(
                    update={"verification_status": VerificationStatus.PENDING, "metadata": metadata}
                )
            metadata = dict(item.metadata)
            metadata["consolidated_from_fact_ids"] = sorted(fact.fact_id for fact in group)
            if superseded_fact_ids:
                metadata["superseded_fact_ids"] = superseded_fact_ids
            item = item.model_copy(update={"fact_id": new_id(), "metadata": metadata})
            consolidated.append(item)
            for original in group:
                replacement_ids[original.fact_id] = item.fact_id
            continue

        ordered = sorted(active_group, key=lambda item: _fact_authority(item, source_map))
        accepted = ordered[0]
        all_source_ids = sorted({source_id for item in ordered for source_id in item.source_ids})
        final_status = VerificationStatus.AUTHORITATIVE_SINGLE
        conflict_reasons: list[str] = []
        verified_fact_ids = [accepted.fact_id]
        for candidate in ordered[1:]:
            outcome = verify_pair(accepted, candidate, source_map, tolerance)
            record = VerificationRecord(
                left_fact_id=accepted.fact_id,
                right_fact_id=candidate.fact_id,
                source_ids=sorted(set(accepted.source_ids + candidate.source_ids)),
                status=outcome.status,
                accepted_fact_id=outcome.accepted_fact_id,
                accepted_value=outcome.accepted_value,
                relative_difference=outcome.relative_difference,
                tolerance=tolerance,
                conflict_dimensions=outcome.conflict_dimensions,
                reasons=outcome.reasons,
            )
            records.append(record)
            if outcome.status == VerificationStatus.PENDING:
                final_status = VerificationStatus.PENDING
                conflict_reasons.extend(outcome.reasons)
            elif final_status != VerificationStatus.PENDING and outcome.status == VerificationStatus.DUAL_SOURCE:
                final_status = VerificationStatus.DUAL_SOURCE
                verified_fact_ids.append(candidate.fact_id)
            if outcome.accepted_fact_id == candidate.fact_id:
                accepted = candidate

        metadata = dict(accepted.metadata)
        metadata.update(
            {
                "verified_from_fact_ids": sorted(set(verified_fact_ids)),
                "consolidated_from_fact_ids": sorted(item.fact_id for item in group),
                "verification_conflicts": conflict_reasons,
            }
        )
        if superseded_fact_ids:
            metadata["superseded_fact_ids"] = superseded_fact_ids
        consolidated_item = accepted.model_copy(
                update={
                    "fact_id": new_id(),
                    "source_ids": all_source_ids,
                    "verification_status": final_status,
                    "metadata": metadata,
                }
        )
        consolidated.append(consolidated_item)
        for item in group:
            replacement_ids[item.fact_id] = consolidated_item.fact_id

    rewritten = []
    for item in consolidated:
        parent_ids = []
        for parent_id in item.derived_from_fact_ids:
            replacement = replacement_ids.get(parent_id, parent_id)
            if replacement != item.fact_id and replacement not in parent_ids:
                parent_ids.append(replacement)
        if parent_ids != item.derived_from_fact_ids:
            metadata = dict(item.metadata)
            metadata["lineage_ids_remapped_after_verification"] = True
            item = item.model_copy(
                update={"derived_from_fact_ids": parent_ids, "metadata": metadata}
            )
        rewritten.append(item)
    return rewritten, records


def _select_active_versions(
    group: list[FactRecord], sources: dict[str, SourceRecord]
) -> tuple[list[FactRecord], list[str]]:
    """Keep the latest disclosed version in each evidence stream.

    A later formal comparative/restatement supersedes the originally filed
    value for deterministic analysis.  The older fact remains in ``raw_facts``
    and is referenced from ``superseded_fact_ids``; it must not be treated as a
    live cross-source conflict with the newer value.
    """

    streams: dict[tuple[str, str], list[FactRecord]] = defaultdict(list)
    for fact in group:
        fact_sources = [sources[item] for item in fact.source_ids if item in sources]
        official = any(_is_official(item) for item in fact_sources)
        if official:
            # Formal filings form one versioned stream for a company/metric/
            # period, even when the same disclosure is mirrored by an exchange
            # and CNInfo.
            key = ("official", "official")
        else:
            upstreams = sorted(
                {
                    item.upstream_source_id or item.source_id
                    for item in fact_sources
                }
            )
            key = ("secondary", "|".join(upstreams) or fact.fact_id)
        streams[key].append(fact)

    active: list[FactRecord] = []
    for stream in streams.values():
        best_rank = max(_version_rank(item) for item in stream)
        active.extend(item for item in stream if _version_rank(item) == best_rank)

    active_ids = {item.fact_id for item in active}
    superseded = sorted(item.fact_id for item in group if item.fact_id not in active_ids)
    return active, superseded


def _version_rank(fact: FactRecord) -> tuple[int, float, float, str]:
    disclosed = fact.disclosed_at or fact.as_of
    return (
        1 if fact.is_restated else 0,
        disclosed.timestamp(),
        fact.as_of.timestamp(),
        fact.restatement_version or "",
    )


def _fact_authority(fact: FactRecord, sources: dict[str, SourceRecord]) -> tuple[int, float, str]:
    levels = [sources[item].authority_level for item in fact.source_ids if item in sources]
    available_at = fact.disclosed_at or fact.as_of
    timestamp = available_at.timestamp()
    return (min(levels) if levels else 99, -timestamp, fact.fact_id)


def _preferred_fact(left: FactRecord, right: FactRecord, sources: dict[str, SourceRecord]) -> FactRecord:
    return min((left, right), key=lambda item: _fact_authority(item, sources))


def validate_claims(claims: list[ClaimRecord], facts: list[FactRecord], sources: list[SourceRecord]) -> list[str]:
    fact_ids = {fact.fact_id for fact in facts}
    source_ids = {source.source_id for source in sources}
    errors: list[str] = []
    for claim in claims:
        missing_facts = set(claim.evidence_fact_ids) - fact_ids
        missing_sources = set(claim.evidence_source_ids) - source_ids
        if missing_facts:
            errors.append(f"{claim.claim_id}: 引用了不存在的fact {sorted(missing_facts)}")
        if missing_sources:
            errors.append(f"{claim.claim_id}: 引用了不存在的source {sorted(missing_sources)}")
        if claim.claim_kind in {ClaimKind.DISCLOSED_FACT, ClaimKind.CODE_DERIVED} and not (
            claim.evidence_fact_ids or claim.evidence_source_ids
        ):
            errors.append(f"{claim.claim_id}: {claim.claim_kind.value}必须有证据")
        if re.search(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", claim.text) and not (
            claim.evidence_fact_ids or claim.evidence_source_ids
        ):
            errors.append(f"{claim.claim_id}: 含数字的结论必须关联事实、来源或明确假设")
        referenced_tickers = {fact.ticker for fact in facts if fact.fact_id in claim.evidence_fact_ids}
        if referenced_tickers and referenced_tickers != {claim.ticker}:
            errors.append(f"{claim.claim_id}: 引用了其他公司的事实 {sorted(referenced_tickers)}")
    return errors


def evidence_scores(facts: list[FactRecord], claims: list[ClaimRecord]) -> tuple[float, float]:
    if not facts and not claims:
        return 0.0, 0.0
    fact_complete = sum(
        fact.value is not None and fact.verification_status not in {VerificationStatus.PENDING, VerificationStatus.UNAVAILABLE}
        for fact in facts
    )
    claim_complete = sum(bool(claim.evidence_fact_ids or claim.evidence_source_ids) for claim in claims)
    completeness = (fact_complete + claim_complete) / (len(facts) + len(claims))
    fact_weights = {
        VerificationStatus.DUAL_SOURCE: 1.0,
        VerificationStatus.AUTHORITATIVE_SINGLE: 0.9,
        VerificationStatus.ESTIMATED: 0.6,
        VerificationStatus.NOT_DISCLOSED: 0.2,
        VerificationStatus.UNAVAILABLE: 0.0,
        VerificationStatus.NOT_APPLICABLE: 1.0,
        VerificationStatus.PENDING: 0.0,
    }
    fact_confidence = sum(fact_weights[fact.verification_status] for fact in facts)
    claim_confidence = sum(claim.confidence for claim in claims)
    confidence = (fact_confidence + claim_confidence) / (len(facts) + len(claims))
    return round(completeness, 4), round(confidence, 4)
