from datetime import date, datetime, timezone

from analysis.models import ClaimKind, ClaimRecord, FactRecord, SourceRecord, VerificationStatus
from analysis.verification import consolidate_facts, validate_claims, verify_pair


def _source(source_id: str, upstream: str, authority: int) -> SourceRecord:
    return SourceRecord(source_id=source_id, name=source_id, upstream_source_id=upstream, authority_level=authority)


def _fact(fact_id: str, value: float, source_id: str) -> FactRecord:
    return FactRecord(
        fact_id=fact_id,
        ticker="000001",
        metric_id="revenue",
        value=value,
        unit="CNY",
        as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_ids=[source_id],
    )


def test_dual_source_keeps_authoritative_value_instead_of_average():
    weak = _source("weak", "upstream-a", 4)
    official = _source("official", "upstream-b", 1)
    left = _fact("left", 100.05, "weak")
    right = _fact("right", 100.0, "official")
    outcome = verify_pair(left, right, {"weak": weak, "official": official}, relative_tolerance=0.001)
    assert outcome.status == VerificationStatus.DUAL_SOURCE
    assert outcome.accepted_value == 100.0
    assert outcome.accepted_fact_id == "right"


def test_same_upstream_is_not_independent_and_conflict_blocks():
    a = _source("a", "same", 1)
    b = _source("b", "same", 4)
    same = verify_pair(_fact("fa", 100, "a"), _fact("fb", 100, "b"), {"a": a, "b": b})
    assert same.status == VerificationStatus.AUTHORITATIVE_SINGLE
    c = _source("c", "other", 2)
    conflict = verify_pair(_fact("fa", 100, "a"), _fact("fc", 120, "c"), {"a": a, "c": c})
    assert conflict.status == VerificationStatus.PENDING
    assert conflict.accepted_value is None


def test_consolidation_and_claim_validation():
    a = _source("a", "one", 1)
    b = _source("b", "two", 3)
    facts, records = consolidate_facts([_fact("fa", 100, "a"), _fact("fb", 100, "b")], [a, b])
    assert len(facts) == 1
    assert facts[0].verification_status == VerificationStatus.DUAL_SOURCE
    assert len(records) == 1
    numeric_claim = ClaimRecord(
        ticker="000001",
        category="business",
        text="收入增长10%",
        claim_kind=ClaimKind.ANALYST_JUDGEMENT,
    )
    assert validate_claims([numeric_claim], facts, [a, b])


def test_latest_official_restatement_supersedes_original_before_dual_source_check():
    original_source = SourceRecord(
        source_id="official-original",
        name="原始年报",
        source_type="official-document",
        upstream_source_id="official-document:original",
        published_at=datetime(2023, 3, 31, tzinfo=timezone.utc),
        authority_level=1,
    )
    restated_source = SourceRecord(
        source_id="official-restated",
        name="下一年年报比较数",
        source_type="official-document",
        upstream_source_id="official-document:restated",
        published_at=datetime(2024, 4, 3, tzinfo=timezone.utc),
        authority_level=1,
    )
    secondary = SourceRecord(
        source_id="secondary",
        name="东方财富结构化数据",
        source_type="public-adapter",
        upstream_source_id="eastmoney-financial-statements",
        authority_level=4,
    )

    def liability_fact(
        fact_id: str,
        value: float,
        source: SourceRecord,
        *,
        restated: bool = False,
    ) -> FactRecord:
        return FactRecord(
            fact_id=fact_id,
            ticker="600519",
            metric_id="total_liabilities",
            value=value,
            unit="元",
            period_end=date(2022, 12, 31),
            period_type="instant",
            disclosed_at=source.published_at,
            as_of=source.published_at or datetime(2024, 4, 3, tzinfo=timezone.utc),
            source_ids=[source.source_id],
            is_restated=restated,
            restatement_version="2023-annual-comparative" if restated else None,
        )

    original = liability_fact("original", 49_400_116_741.17, original_source)
    restated = liability_fact(
        "restated", 49_562_744_832.16, restated_source, restated=True
    )
    independent = liability_fact("secondary", 49_562_744_832.16, secondary, restated=True)
    consolidated, records = consolidate_facts(
        [original, restated, independent],
        [original_source, restated_source, secondary],
    )

    assert len(consolidated) == 1
    assert consolidated[0].value == restated.value
    assert consolidated[0].verification_status == VerificationStatus.DUAL_SOURCE
    assert consolidated[0].metadata["superseded_fact_ids"] == [original.fact_id]
    assert original_source.source_id not in consolidated[0].source_ids
    assert len(records) == 1


def test_consolidation_remaps_derived_parent_ids_to_consolidated_facts():
    official = SourceRecord(
        source_id="official-lineage",
        name="正式年报",
        source_type="official-document",
        upstream_source_id="official-document:lineage",
        authority_level=1,
    )
    secondary = SourceRecord(
        source_id="secondary-lineage",
        name="独立结构化源",
        source_type="public-adapter",
        upstream_source_id="secondary:lineage",
        authority_level=4,
    )
    period_end = date(2025, 9, 30)

    def flow_fact(
        fact_id: str,
        value: float,
        period_type: str,
        source_id: str,
        parents: list[str] | None = None,
    ) -> FactRecord:
        return FactRecord(
            fact_id=fact_id,
            ticker="600519",
            metric_id="revenue",
            value=value,
            unit="元",
            period_start=date(2025, 1, 1),
            period_end=period_end,
            period_type=period_type,
            as_of=datetime(2025, 10, 30, tzinfo=timezone.utc),
            source_ids=[source_id],
            derived_from_fact_ids=parents or [],
        )

    parent_official = flow_fact("parent-official", 100, "cumulative", official.source_id)
    parent_secondary = flow_fact("parent-secondary", 100, "cumulative", secondary.source_id)
    child_official = flow_fact(
        "child-official", 40, "single_quarter", official.source_id, [parent_official.fact_id]
    )
    child_secondary = flow_fact(
        "child-secondary", 40, "single_quarter", secondary.source_id, [parent_secondary.fact_id]
    )

    consolidated, _ = consolidate_facts(
        [child_official, child_secondary, parent_official, parent_secondary],
        [official, secondary],
    )
    parent = next(item for item in consolidated if item.period_type == "cumulative")
    child = next(item for item in consolidated if item.period_type == "single_quarter")
    assert child.derived_from_fact_ids == [parent.fact_id]
    assert child.metadata["lineage_ids_remapped_after_verification"] is True
