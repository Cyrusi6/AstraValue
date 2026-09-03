from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import FactRecord, SourceRecord, VerificationStatus


METHOD_REF = "VAL.RELATIVE@1.0.0"
PRICE_VALUE_UPSTREAM_PRIORITY = (
    "eastmoney-market",
    "sina-market",
    "wind-market",
)
RATIO_VENDOR_UPSTREAMS = {
    "baostock-market",
    "tushare-pro-daily-basic",
}


@dataclass(frozen=True)
class MarketMultipleDerivation:
    facts: list[FactRecord]
    warnings: list[str]


def derive_verified_market_multiples(
    consolidated_facts: list[FactRecord],
    raw_facts: list[FactRecord],
    sources: list[SourceRecord],
) -> MarketMultipleDerivation:
    """Deterministically calculate PE TTM and PB from verified inputs.

    ``consolidated_facts`` is the result of the first verification pass.  The
    returned facts intentionally point to raw lineage anchors so a second
    consolidation pass can remap them to the final consolidated parent facts.
    BaoStock and Tushare ratio feeds are excluded from the formula value path;
    they remain independent external checks for the calculated multiples.
    """

    source_map = {item.source_id: item for item in sources}
    verified = [
        item
        for item in consolidated_facts
        if item.value is not None
        and item.verification_status == VerificationStatus.DUAL_SOURCE
    ]
    warnings: list[str] = []

    price_fact, raw_price = _verified_formula_price(verified, raw_facts, source_map)
    if price_fact is None or raw_price is None:
        return MarketMultipleDerivation(
            facts=[],
            warnings=[
                "未生成确定性PE/PB：缺少双源一致且可由非估值供应商提供原始值的收盘价"
            ],
        )

    shares = _latest_metric(verified, "shares_outstanding", unit="股")
    if shares is None or shares.value is None or shares.value <= 0:
        return MarketMultipleDerivation(
            facts=[],
            warnings=["未生成确定性PE/PB：缺少双源一致且为正的期末总股本"],
        )

    price = float(raw_price.value)
    source_ids = _direct_source_ids(raw_price, source_map)
    common_inputs = {
        "market_price": price_fact,
        "shares_outstanding": shares,
    }
    common_anchors = {
        "market_price": raw_price.fact_id,
        "shares_outstanding": _lineage_anchor(shares),
    }
    results: list[FactRecord] = []

    ttm_value, ttm_inputs, ttm_warning = _ttm_parent_net_income(verified)
    if ttm_warning:
        warnings.append(f"未生成确定性PE TTM：{ttm_warning}")
    elif ttm_value is None or ttm_value <= 0:
        warnings.append("未生成确定性PE TTM：TTM归母净利润非正")
    else:
        pe_inputs = {**common_inputs, **ttm_inputs}
        pe_anchors = {**common_anchors}
        for name, item in ttm_inputs.items():
            pe_anchors[name] = _lineage_anchor(item)
        pe_source_ids = sorted(
            set(source_ids)
            | {
                source_id
                for item in [shares, *ttm_inputs.values()]
                for source_id in _direct_source_ids(item, source_map)
            }
        )
        results.append(
            _multiple_fact(
                metric_id="pe_ttm",
                value=price * float(shares.value) / ttm_value,
                price_fact=price_fact,
                raw_price=raw_price,
                source_ids=pe_source_ids,
                inputs=pe_inputs,
                anchors=pe_anchors,
                formula="market_price * shares_outstanding / ttm_parent_net_income",
                extra_metadata={
                    "ttm_parent_net_income": ttm_value,
                    "ttm_formula": (
                        "latest_ytd + previous_fy - previous_comparable_ytd"
                        if len(ttm_inputs) == 3
                        else "latest_fy"
                    ),
                },
            )
        )

    equity = _latest_metric(verified, "total_parent_equity", unit="元")
    if equity is None:
        warnings.append("未生成确定性PB：缺少双源一致的归属于母公司股东权益")
    elif equity.value is None or equity.value <= 0:
        warnings.append("未生成确定性PB：归属于母公司股东权益非正")
    else:
        pb_inputs = {**common_inputs, "total_parent_equity": equity}
        pb_anchors = {
            **common_anchors,
            "total_parent_equity": _lineage_anchor(equity),
        }
        pb_source_ids = sorted(
            set(source_ids)
            | {
                source_id
                for item in (shares, equity)
                for source_id in _direct_source_ids(item, source_map)
            }
        )
        results.append(
            _multiple_fact(
                metric_id="pb",
                value=price * float(shares.value) / float(equity.value),
                price_fact=price_fact,
                raw_price=raw_price,
                source_ids=pb_source_ids,
                inputs=pb_inputs,
                anchors=pb_anchors,
                formula="market_price * shares_outstanding / total_parent_equity",
            )
        )

    return MarketMultipleDerivation(facts=results, warnings=warnings)


def _verified_formula_price(
    verified: list[FactRecord],
    raw_facts: list[FactRecord],
    source_map: dict[str, SourceRecord],
) -> tuple[FactRecord | None, FactRecord | None]:
    prices = sorted(
        (
            item
            for item in verified
            if item.metric_id == "market_price"
            and item.period_type == "market_quote"
            and item.period_end is not None
            and item.value is not None
            and item.value > 0
        ),
        key=lambda item: (item.period_end, item.as_of),
        reverse=True,
    )
    priority = {upstream: rank for rank, upstream in enumerate(PRICE_VALUE_UPSTREAM_PRIORITY)}
    for price_fact in prices:
        candidates = []
        for raw in raw_facts:
            if not _same_fact_key(raw, price_fact) or raw.value is None or raw.value <= 0:
                continue
            upstreams = {
                source_map[source_id].upstream_source_id or source_id
                for source_id in raw.source_ids
                if source_id in source_map
            }
            ranks = [priority[item] for item in upstreams if item in priority]
            if not ranks or upstreams & RATIO_VENDOR_UPSTREAMS:
                continue
            denominator = max(abs(float(raw.value)), abs(float(price_fact.value)), 1.0)
            if abs(float(raw.value) - float(price_fact.value)) / denominator > 0.001:
                continue
            candidates.append((min(ranks), -raw.as_of.timestamp(), raw.fact_id, raw))
        if candidates:
            return price_fact, min(candidates)[-1]
    return None, None


def _ttm_parent_net_income(
    verified: list[FactRecord],
) -> tuple[float | None, dict[str, FactRecord], str | None]:
    flows = sorted(
        (
            item
            for item in verified
            if item.metric_id == "net_income_parent"
            and item.period_type in {"annual", "cumulative"}
            and item.period_end is not None
            and item.unit == "元"
        ),
        key=lambda item: (item.period_end, item.as_of),
        reverse=True,
    )
    if not flows:
        return None, {}, "缺少双源一致的归母净利润"
    latest = flows[0]
    if latest.period_type == "annual":
        return float(latest.value), {"latest_fy_parent_net_income": latest}, None

    latest_end = latest.period_end
    if (latest_end.month, latest_end.day) not in {(3, 31), (6, 30), (9, 30)}:
        return None, {}, f"最新累计归母净利润期间{latest_end.isoformat()}不是标准季度末"
    previous_fy_end = date(latest_end.year - 1, 12, 31)
    previous_comparable_end = date(
        latest_end.year - 1, latest_end.month, latest_end.day
    )
    previous_fy = _exact_metric(
        flows, "net_income_parent", previous_fy_end, "annual"
    )
    previous_comparable = _exact_metric(
        flows, "net_income_parent", previous_comparable_end, "cumulative"
    )
    missing = []
    if previous_fy is None:
        missing.append(f"{previous_fy_end.isoformat()}全年归母净利润")
    if previous_comparable is None:
        missing.append(f"{previous_comparable_end.isoformat()}同期累计归母净利润")
    if missing:
        return None, {}, "缺少" + "、".join(missing)
    value = float(latest.value) + float(previous_fy.value) - float(
        previous_comparable.value
    )
    return (
        value,
        {
            "latest_ytd_parent_net_income": latest,
            "previous_fy_parent_net_income": previous_fy,
            "previous_comparable_ytd_parent_net_income": previous_comparable,
        },
        None,
    )


def _multiple_fact(
    *,
    metric_id: str,
    value: float,
    price_fact: FactRecord,
    raw_price: FactRecord,
    source_ids: list[str],
    inputs: dict[str, FactRecord],
    anchors: dict[str, str],
    formula: str,
    extra_metadata: dict | None = None,
) -> FactRecord:
    input_periods = {
        name: item.period_end.isoformat() if item.period_end else None
        for name, item in inputs.items()
    }
    input_values = {name: item.value for name, item in inputs.items()}
    return FactRecord(
        ticker=price_fact.ticker,
        metric_id=metric_id,
        value=value,
        unit="x",
        period_end=price_fact.period_end,
        period_type="market_quote",
        as_of=max(item.as_of for item in [raw_price, *inputs.values()]),
        scope="consolidated",
        audited=False,
        original_label="双源核验输入确定性复算",
        source_ids=source_ids,
        verification_status=VerificationStatus.PENDING,
        method_ref=METHOD_REF,
        derived_from_fact_ids=list(dict.fromkeys(anchors.values())),
        metadata={
            "provider": "deterministic-formula",
            "formula": formula,
            "formula_input_values": input_values,
            "formula_input_periods": input_periods,
            "input_consolidated_fact_ids": {
                name: item.fact_id for name, item in inputs.items()
            },
            "input_lineage_anchors": anchors,
            "market_price_value_fact_id": raw_price.fact_id,
            "market_price_value_source_ids": raw_price.source_ids,
            "excluded_ratio_vendor_upstreams": sorted(RATIO_VENDOR_UPSTREAMS),
            **(extra_metadata or {}),
        },
    )


def _latest_metric(
    facts: list[FactRecord], metric_id: str, *, unit: str
) -> FactRecord | None:
    matches = [
        item
        for item in facts
        if item.metric_id == metric_id
        and item.unit == unit
        and item.period_end is not None
    ]
    return max(matches, key=lambda item: (item.period_end, item.as_of), default=None)


def _exact_metric(
    facts: list[FactRecord], metric_id: str, period_end: date, period_type: str
) -> FactRecord | None:
    matches = [
        item
        for item in facts
        if item.metric_id == metric_id
        and item.period_end == period_end
        and item.period_type == period_type
    ]
    return max(matches, key=lambda item: item.as_of, default=None)


def _lineage_anchor(fact: FactRecord) -> str:
    verified = fact.metadata.get("verified_from_fact_ids")
    if isinstance(verified, list) and verified:
        return str(verified[0])
    consolidated = fact.metadata.get("consolidated_from_fact_ids")
    if isinstance(consolidated, list) and consolidated:
        return str(consolidated[0])
    return fact.fact_id


def _direct_source_ids(
    fact: FactRecord, source_map: dict[str, SourceRecord]
) -> list[str]:
    return [
        source_id
        for source_id in fact.source_ids
        if source_id in source_map
        and (source_map[source_id].upstream_source_id or source_id)
        not in RATIO_VENDOR_UPSTREAMS
    ]


def _same_fact_key(left: FactRecord, right: FactRecord) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "ticker",
            "metric_id",
            "unit",
            "currency",
            "period_start",
            "period_end",
            "period_type",
            "scope",
        )
    )
