from datetime import date
from decimal import Decimal

from analysis.structured.identity import (
    CompanyResolver,
    IndustryProfileRouter,
    IndustrySignals,
    PeerCandidate,
    ProfileDecisionStatus,
    ResolutionStatus,
    SecurityAlias,
    SecurityIdentity,
    SegmentProfileEvidence,
    select_peers,
)


def identity(
    code: str,
    market: str,
    name: str,
    *,
    security_id: str | None = None,
    company_id: str | None = None,
    security_type: str = "A_SHARE",
    aliases=(),
    delisting_date=None,
):
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[market]
    return SecurityIdentity(
        company_id or f"company:{code}",
        security_id or f"security:{code}:{suffix}",
        f"{code}.{suffix}",
        code,
        market,
        name,
        security_type,
        date(2001, 1, 1),
        delisting_date,
        tuple(aliases),
        (f"B08:{code}",),
    )


def test_resolver_handles_code_exchange_name_and_former_name_without_io():
    target = identity(
        "600519",
        "SSE",
        "贵州茅台",
        aliases=(SecurityAlias("贵州茅台酒", valid_to=date(2005, 12, 31)),),
    )
    resolver = CompanyResolver((target,))
    for query in ("600519", "600519.SH", "SH600519", "贵州茅台"):
        result = resolver.resolve(query, as_of=date(2026, 9, 8))
        assert result.status == ResolutionStatus.RESOLVED
        assert result.identity == target
        assert result.performed_io is False
    historical = resolver.resolve("贵州茅台酒", as_of=date(2004, 1, 1))
    assert historical.identity == target


def test_resolver_never_guesses_ambiguous_name_market_or_cache_miss():
    a = identity("600001", "SSE", "重名公司")
    b = identity("000001", "SZSE", "重名公司")
    resolver = CompanyResolver((a, b))
    assert resolver.resolve("重名公司", as_of=date.today()).status == ResolutionStatus.AMBIGUOUS
    conflict = resolver.resolve("600001.SZ", as_of=date.today())
    assert conflict.status == ResolutionStatus.AMBIGUOUS
    miss = resolver.resolve("999999", as_of=date.today())
    assert miss.status == ResolutionStatus.PREREQUISITE_REQUIRED
    assert miss.prerequisite_dataset_ids == ("B08", "C01")


def test_resolver_rejects_non_a_share_and_unverified_bse_but_keeps_delisted_identity():
    fund = identity("510300", "SSE", "ETF", security_type="FUND")
    bse = identity("430001", "BSE", "北交公司")
    delisted = identity("600002", "SSE", "退市公司", delisting_date=date(2020, 1, 1))
    resolver = CompanyResolver((fund, bse, delisted))
    assert resolver.resolve("510300", as_of=date.today()).status == ResolutionStatus.UNSUPPORTED
    assert resolver.resolve("430001", as_of=date.today()).reason_codes == (
        "bse_protocol_support_unverified",
    )
    result = resolver.resolve("600002", as_of=date.today())
    assert result.status == ResolutionStatus.RESOLVED
    assert "delisted_identity_resolved_history_may_be_limited" in result.reason_codes


def test_industry_unknown_and_conflict_do_not_fall_back_to_general():
    router = IndustryProfileRouter(("general", "consumer", "bank", "preprofit"))
    unknown = router.select(IndustrySignals("company:1"))
    assert unknown.status == ProfileDecisionStatus.PENDING
    assert unknown.profile_ids == ()
    assert unknown.general_fallback_used is False

    conflict = router.select(
        IndustrySignals(
            "company:1",
            source_profile_ids=("consumer",),
            source_evidence_ids=("C01:industry",),
            company_type_profile_id="bank",
            company_type_evidence_ids=("EMF:companyType",),
        )
    )
    assert conflict.status == ProfileDecisionStatus.CONFLICT
    assert conflict.profile_ids == ()
    assert conflict.common_acquisition_allowed is True


def test_two_period_segments_support_mixed_profiles_and_preprofit_overlay():
    router = IndustryProfileRouter(
        ("general", "consumer", "manufacturing", "preprofit")
    )
    signals = IndustrySignals(
        "company:1",
        segments=(
            SegmentProfileEvidence("Y2024", "consumer", Decimal("0.6"), ("seg:1",)),
            SegmentProfileEvidence("Y2025", "consumer", Decimal("0.55"), ("seg:2",)),
            SegmentProfileEvidence("Y2024", "manufacturing", Decimal("0.7"), ("seg:3",)),
            SegmentProfileEvidence("Y2025", "manufacturing", Decimal("0.65"), ("seg:4",)),
        ),
        preprofit=True,
        preprofit_evidence_ids=("income:loss",),
    )
    decision = router.select(signals)
    assert decision.status == ProfileDecisionStatus.MIXED
    assert decision.profile_ids == ("consumer", "manufacturing", "preprofit")


def peer(
    value: SecurityIdentity,
    *,
    revenue="100",
    profit="10",
    profile="consumer",
    model=True,
    region=True,
    segment_share="0.8",
    period="Y2025",
    evidence=("C02:segment",),
    user_selected=False,
):
    return PeerCandidate(
        value,
        (profile,),
        ("baijiu",) if segment_share is not None else (),
        None if segment_share is None else Decimal(segment_share),
        model,
        region,
        period,
        None if revenue is None else Decimal(revenue),
        None if profit is None else Decimal(profit),
        tuple(evidence),
        user_selected,
    )


def test_peer_selection_is_bounded_nonrecursive_and_keeps_loss_peer_for_operations():
    target = peer(identity("600519", "SSE", "目标"), revenue="1000")
    candidates = [
        peer(
            identity(f"60{index:04d}", "SSE", f"同行{index}"),
            revenue=str(1000 - index),
            profit="-1" if index == 1 else "10",
        )
        for index in range(1, 9)
    ]
    result = select_peers(
        target=target,
        candidates=candidates,
        company_scope="company-with-peers",
    )
    assert len(result.selected_security_ids) == 6
    assert result.recursive_expansion is False
    loss_id = candidates[0].identity.security_id
    assert loss_id in result.metric_subsets["operating"]
    assert loss_id not in result.metric_subsets["pe"]


def test_peer_gaps_do_not_block_target_and_company_only_never_expands():
    target = peer(identity("600519", "SSE", "目标"), revenue="1000")
    uncertain = peer(
        identity("000858", "SZSE", "候选"),
        model=None,
        region=None,
        evidence=(),
    )
    result = select_peers(
        target=target,
        candidates=(uncertain,),
        company_scope="company-with-peers",
    )
    assert result.selected_security_ids == ()
    assert "business_model_unconfirmed" in result.decisions[0].reasons
    company_only = select_peers(
        target=target,
        candidates=(peer(identity("000568", "SZSE", "同行")),),
        company_scope="company-only",
    )
    assert company_only.selected_security_ids == ()
    assert company_only.decisions[0].reasons == ("company_only_scope",)
