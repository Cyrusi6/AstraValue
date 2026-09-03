from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .models import DocumentRecord, EventRecord, EvidenceSpan


NUMBER = r"(?P<number>[\d,，]+(?:\.\d+)?)"
AMOUNT_UNIT = r"(?P<unit>亿元|万元|元)"
SHARE_UNIT = r"(?P<unit>亿股|万股|股)"
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def enrich_event_from_document(
    event: EventRecord,
    document: DocumentRecord,
) -> EventRecord:
    text = Path(document.text_path).read_text(encoding="utf-8", errors="replace")
    event_terms = dict(event.event_terms)
    evidence_spans = list(event.evidence_spans)
    extracted_fields: list[str] = []

    amount_fields = _amount_patterns(event)
    amount_values = _extract_fields(
        text,
        document.document_id,
        amount_fields,
        _amount_value,
        evidence_spans,
    )
    event_terms.update(amount_values)
    extracted_fields.extend(amount_values)

    share_values = _extract_fields(
        text,
        document.document_id,
        _share_patterns(event),
        _share_value,
        evidence_spans,
    )
    event_terms.update(share_values)
    extracted_fields.extend(share_values)

    ratio_values = _extract_fields(
        text,
        document.document_id,
        _ratio_patterns(event),
        _ratio_value,
        evidence_spans,
    )
    event_terms.update(ratio_values)
    extracted_fields.extend(ratio_values)

    date_values = _extract_fields(
        text,
        document.document_id,
        _date_patterns(event.event_type),
        _date_value,
        evidence_spans,
    )
    event_terms.update(date_values)
    extracted_fields.extend(date_values)

    scalar_values = _extract_fields(
        text,
        document.document_id,
        _scalar_patterns(event),
        _scalar_value,
        evidence_spans,
    )
    event_terms.update(scalar_values)
    extracted_fields.extend(scalar_values)

    if event.event_type == "repurchase":
        plan_date = event_terms.get("plan_disclosure_date")
        if isinstance(plan_date, str):
            event_terms["plan_id"] = plan_date
            extracted_fields.append("plan_id")
        elif _is_repurchase_plan_document(event):
            event_terms["plan_id"] = event.announced_at.astimezone(
                CHINA_TZ
            ).date().isoformat()
            extracted_fields.append("plan_id")
        purpose = _extract_repurchase_purpose(
            text,
            document.document_id,
            evidence_spans,
        )
        if purpose:
            event_terms["repurchase_purpose"] = purpose
            extracted_fields.append("repurchase_purpose")

    parties = list(event.parties)
    if event.event_type == "management_change":
        party = _extract_management_party(text, document.document_id, evidence_spans)
        if party and party not in parties:
            parties.append(party)
            extracted_fields.extend(
                key for key in ("person_name", "position") if party.get(key)
            )

    primary_amount = _first_present(
        event_terms,
        {
            "dividend": ["total_cash_dividend"],
            "repurchase": ["repurchase_amount"],
            "holding_change": ["holding_change_amount"],
            "financing": ["gross_proceeds", "net_proceeds"],
            "merger_acquisition": ["transaction_consideration"],
            "regulatory_penalty": ["penalty_amount"],
            "litigation": ["litigation_amount"],
            "related_party_transaction": ["related_transaction_amount"],
        }.get(event.event_type, []),
    )
    primary_shares = _first_present(
        event_terms,
        {
            "repurchase": ["repurchased_shares"],
            "pledge": ["released_shares", "pledged_shares"],
            "holding_change": ["changed_shares"],
            "financing": ["issued_shares"],
        }.get(event.event_type, []),
    )
    primary_ratio = _first_present(
        event_terms,
        {
            "repurchase": ["repurchased_share_ratio"],
            "pledge": ["shares_to_total_equity_ratio"],
            "holding_change": ["holding_change_ratio"],
        }.get(event.event_type, []),
    )

    metadata = dict(event.metadata)
    metadata.update(
        {
            "field_extraction_method": "deterministic_regex",
            "field_extraction_document_id": document.document_id,
            "extracted_fields": sorted(set(extracted_fields)),
        }
    )
    return EventRecord.model_validate(
        {
            **event.model_dump(mode="python"),
            "amount": primary_amount if primary_amount is not None else event.amount,
            "shares": primary_shares if primary_shares is not None else event.shares,
            "ratio": primary_ratio if primary_ratio is not None else event.ratio,
            "parties": parties,
            "event_terms": event_terms,
            "evidence_spans": _deduplicate_spans(evidence_spans),
            "metadata": metadata,
        }
    )


def _amount_patterns(event: EventRecord) -> dict[str, list[str]]:
    event_type = event.event_type
    if _is_repurchase_plan_document(event):
        return {
            "planned_repurchase_amount_min": [
                rf"回购(?:股份)?金额[：:\s]*不低于(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"回购金额不低于(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"预计回购金额[\s\S]{{0,30}}?(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
            "planned_repurchase_amount_max": [
                rf"回购(?:股份)?金额[^。；]{{0,80}}?不超过(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"预计回购金额\s*(?:人民币)?[\d,，]+(?:\.\d+)?\s*(?:亿元|万元|元)[^。；\n]{{0,30}}?[—～~至-]\s*(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        }
    common = {
        "dividend": {
            "total_cash_dividend": [
                rf"(?:共计|合计|总计)\s*(?:派\s*发|发\s*放)?\s*现金\s*红利(?:总额)?(?:为|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"现金\s*分红\s*总额(?:为|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "repurchase": {
            "repurchase_amount": [
                rf"累计\s*已\s*回购\s*金额\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"(?:已支付|累计支付|支付)的?资金总额(?:为)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"回购(?:支付的)?总金额(?:为)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
                rf"(?:实际\s*回购\s*金额|使用\s*资金\s*总额|已\s*支付的?\s*总\s*金额)(?:为)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "holding_change": {
            "holding_change_amount": [
                rf"(?:累计)?(?:增持|减持)金额(?:为|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "financing": {
            "gross_proceeds": [
                rf"募集资金总额(?:为)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
            "net_proceeds": [
                rf"募集资金净额(?:为)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "merger_acquisition": {
            "transaction_consideration": [
                rf"(?:交易对价|交易价格|收购价款)(?:为|合计|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "regulatory_penalty": {
            "penalty_amount": [
                rf"(?:处以|决定处以|罚款)(?:人民币)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "litigation": {
            "litigation_amount": [
                rf"(?:涉案金额|诉讼金额|仲裁金额)(?:为|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
        "related_party_transaction": {
            "related_transaction_amount": [
                rf"(?:关联交易金额|交易金额|预计金额)(?:为|不超过|约)?\s*{NUMBER}\s*{AMOUNT_UNIT}",
            ],
        },
    }
    return common.get(event_type, {})


def _share_patterns(event: EventRecord) -> dict[str, list[str]]:
    event_type = event.event_type
    if _is_repurchase_plan_document(event):
        return {}
    return {
        "repurchase": {
            "repurchased_shares": [
                rf"累计\s*已\s*回购\s*股(?:数|份数|份数量)?\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"已\s*累计\s*回购(?:公司)?\s*股\s*份(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"累计\s*回购(?:公司)?\s*股\s*份(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"实际\s*回购\s*股(?:份数|数|份数量)\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"本次\s*回购(?:公司)?\s*股\s*份(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
            ],
        },
        "pledge": {
            "released_shares": [
                rf"本次(?:解除质押|解押)(?:股份)?(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
            ],
            "pledged_shares": [
                rf"本次(?:质押|补充质押)(?:股份)?(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
            ],
        },
        "holding_change": {
            "changed_shares": [
                rf"(?:累计|本次)?(?:增持|减持)(?:公司)?股份(?:数量)?(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"(?:累计|本次)?(?:增持|减持)\s*{NUMBER}\s*{SHARE_UNIT}",
                rf"(?:增持|减持)(?:了|公司)?\s*{NUMBER}\s*{SHARE_UNIT}",
            ],
        },
        "financing": {
            "issued_shares": [
                rf"(?:本次)?发行(?:股票|股份)?数量(?:为)?\s*{NUMBER}\s*{SHARE_UNIT}",
            ],
        },
    }.get(event_type, {})


def _ratio_patterns(event: EventRecord) -> dict[str, list[str]]:
    event_type = event.event_type
    if _is_repurchase_plan_document(event):
        return {}
    return {
        "repurchase": {
            "repurchased_share_ratio": [
                rf"累计\s*已\s*回购\s*股数\s*占\s*总\s*股本\s*比例\s*{NUMBER}\s*%",
                rf"(?:已累计|累计|本次)?回购[^。；\n]{{0,40}}占(?:公司)?总股本(?:的)?(?:比例)?(?:为)?\s*{NUMBER}\s*%",
                rf"占(?:公司)?总股本(?:的)?(?:比例)?(?:为)?\s*{NUMBER}\s*%",
            ],
        },
        "pledge": {
            "shares_to_total_equity_ratio": [
                rf"(?:本次质押|本次解除质押)[^。；\n]{{0,60}}占(?:公司)?总股本(?:的)?(?:比例)?(?:为)?\s*{NUMBER}\s*%",
            ],
            "shares_to_holder_ratio": [
                rf"(?:本次质押|本次解除质押)[^。；\n]{{0,60}}占其所持股份(?:的)?(?:比例)?(?:为)?\s*{NUMBER}\s*%",
            ],
        },
        "holding_change": {
            "holding_change_ratio": [
                rf"(?:本次|累计)?(?:增持|减持)[^。；\n]{{0,40}}占(?:公司)?总股本(?:的)?(?:比例)?(?:为)?\s*{NUMBER}\s*%",
            ],
        },
    }.get(event_type, {})


def _date_patterns(event_type: str) -> dict[str, list[str]]:
    common = {
        "dividend": {
            "record_date": [
                r"股权登记日(?:为|：|:)?\s*(?P<date>20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
                r"股权登记日[\s\S]{0,100}?[ＡA]\s*股\s*(?P<date>20\d{2}/\d{1,2}/\d{1,2})",
            ],
            "ex_dividend_date": [
                r"除权(?:（?息）?|除息)?日(?:为|：|:)?\s*(?P<date>20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
                r"股权登记日[\s\S]{0,100}?[ＡA]\s*股\s*20\d{2}/\d{1,2}/\d{1,2}\s*[－—-]\s*(?P<date>20\d{2}/\d{1,2}/\d{1,2})",
            ],
            "payment_date": [
                r"现金红利发放日(?:为|：|:)?\s*(?P<date>20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
                r"股权登记日[\s\S]{0,100}?[ＡA]\s*股\s*20\d{2}/\d{1,2}/\d{1,2}\s*[－—-]\s*20\d{2}/\d{1,2}/\d{1,2}\s*(?P<date>20\d{2}/\d{1,2}/\d{1,2})",
            ],
        },
        "accounting_governance": {
            "effective_date": [
                r"自\s*(?P<date>20\d{2}年\d{1,2}月\d{1,2}日)\s*起(?:施行|执行|生效)",
            ],
        },
        "repurchase": {
            "plan_disclosure_date": [
                r"回购方案首次披露日\s*(?P<date>20\d{2}/\d{1,2}/\d{1,2})",
                r"首次披露(?:回购股份事项)?(?:日期|日)?(?:为|：|:)?\s*(?P<date>20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
            ],
        },
    }
    return common.get(event_type, {})


def _scalar_patterns(event: EventRecord) -> dict[str, list[str]]:
    event_type = event.event_type
    if event_type == "dividend":
        return {
            "cash_dividend_per_share": [
                rf"每\s*(?P<basis>10|1)\s*股(?:派发|派送|分配)(?:现金)?红利(?:人民币)?\s*{NUMBER}\s*元",
                rf"(?:[ＡA]\s*股)?每\s*股(?:派\s*发)?现金\s*红利(?:人民币)?\s*{NUMBER}\s*元",
            ],
        }
    if event_type == "repurchase":
        if _is_repurchase_plan_document(event):
            return {
                "repurchase_price_cap": [
                    rf"回购价格(?:上限|不超过)(?:为)?\s*{NUMBER}\s*元\s*/\s*股",
                    rf"回购价格不超过\s*{NUMBER}\s*元\s*/\s*股",
                ],
            }
        return {
            "highest_repurchase_price": [
                rf"实际\s*回购\s*价格\s*区间\s*[\d,，]+(?:\.\d+)?\s*元\s*/\s*股\s*[～~至—-]\s*{NUMBER}\s*元\s*/\s*股",
                rf"(?:回购)?最高(?:成交)?价(?:为)?\s*{NUMBER}\s*元/股",
                rf"(?:回购)?最高(?:成交)?价(?:格)?(?:为)?\s*{NUMBER}\s*元\s*/\s*股",
                rf"实际回购价格区间\s*[\d,，]+(?:\.\d+)?\s*元/股\s*[～~至-]\s*{NUMBER}\s*元/股",
            ],
            "lowest_repurchase_price": [
                rf"实际\s*回购\s*价格\s*区间\s*{NUMBER}\s*元\s*/\s*股\s*[～~至—-]\s*[\d,，]+(?:\.\d+)?\s*元\s*/\s*股",
                rf"(?:回购)?最低(?:成交)?价(?:为)?\s*{NUMBER}\s*元/股",
                rf"(?:回购)?最低(?:成交)?价(?:格)?(?:为)?\s*{NUMBER}\s*元\s*/\s*股",
                rf"实际回购价格区间\s*{NUMBER}\s*元/股\s*[～~至-]\s*[\d,，]+(?:\.\d+)?\s*元/股",
            ],
        }
    return {}


def _is_repurchase_plan_document(event: EventRecord) -> bool:
    return event.event_type == "repurchase" and (
        event.lifecycle_state == "plan"
        or event.event_subtype in {"plan", "plan_document"}
    )


def _extract_fields(
    text: str,
    document_id: str,
    patterns_by_field: dict[str, list[str]],
    converter: Callable[[re.Match[str]], object],
    evidence_spans: list[EvidenceSpan],
) -> dict[str, object]:
    values: dict[str, object] = {}
    for field_name, patterns in patterns_by_field.items():
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is None:
                continue
            try:
                value = converter(match)
            except (ValueError, TypeError):
                continue
            values[field_name] = value
            evidence_spans.append(_evidence_span(document_id, text, match))
            break
    return values


def _amount_value(match: re.Match[str]) -> float:
    value = _number(match.group("number"))
    scale = {"元": 1.0, "万元": 10_000.0, "亿元": 100_000_000.0}
    return value * scale[match.group("unit")]


def _share_value(match: re.Match[str]) -> float:
    value = _number(match.group("number"))
    scale = {"股": 1.0, "万股": 10_000.0, "亿股": 100_000_000.0}
    return value * scale[match.group("unit")]


def _ratio_value(match: re.Match[str]) -> float:
    return _number(match.group("number")) / 100.0


def _date_value(match: re.Match[str]) -> str:
    value = re.sub(r"\s+", "", match.group("date"))
    parsed = re.fullmatch(
        r"(?P<year>20\d{2})(?:年|/)(?P<month>\d{1,2})(?:月|/)(?P<day>\d{1,2})(?:日)?",
        value,
    )
    if parsed is None:
        raise ValueError("日期格式不匹配")
    return date(
        int(parsed.group("year")),
        int(parsed.group("month")),
        int(parsed.group("day")),
    ).isoformat()


def _scalar_value(match: re.Match[str]) -> float:
    value = _number(match.group("number"))
    basis = match.groupdict().get("basis")
    return value / float(basis) if basis else value


def _number(value: str) -> float:
    return float(value.replace(",", "").replace("，", ""))


def _extract_management_party(
    text: str,
    document_id: str,
    evidence_spans: list[EvidenceSpan],
) -> dict[str, str] | None:
    patterns = [
        r"聘任\s*(?P<name>[\u4e00-\u9fff·]{2,12})(?:先生|女士)?\s*为(?:公司)?(?P<position>[^，。；\n]{2,20})",
        r"选举\s*(?P<name>[\u4e00-\u9fff·]{2,12})(?:先生|女士)?\s*为(?:公司)?(?P<position>[^，。；\n]{2,20})",
        r"(?P<name>[\u4e00-\u9fff·]{2,12})(?:先生|女士)?\s*辞去(?:公司)?(?P<position>[^，。；\n]{2,20})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        evidence_spans.append(_evidence_span(document_id, text, match))
        return {
            "person_name": re.sub(
                r"(?:先生|女士)$",
                "",
                match.group("name").strip(),
            ),
            "position": match.group("position").strip(),
        }
    return None


def _extract_repurchase_purpose(
    text: str,
    document_id: str,
    evidence_spans: list[EvidenceSpan],
) -> str | None:
    purpose_patterns = [
        ("capital_reduction", r"[√☑]\s*(?:用于)?(?:注销并)?减少注册资本"),
        ("employee_plan_or_incentive", r"[√☑]\s*用于员工持股计划或股权激励"),
        ("value_maintenance", r"[√☑]\s*为维护公司价值及股东权益"),
    ]
    for purpose, pattern in purpose_patterns:
        match = re.search(pattern, text)
        if match is not None:
            evidence_spans.append(_evidence_span(document_id, text, match))
            return purpose
    return None


def _evidence_span(
    document_id: str,
    text: str,
    match: re.Match[str],
) -> EvidenceSpan:
    page = _page_at_offset(text, match.start())
    start = max(0, match.start() - 30)
    end = min(len(text), match.end() + 30)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    return EvidenceSpan(
        document_id=document_id,
        page=page,
        text=quote,
        start_offset=match.start(),
        end_offset=match.end(),
    )


def _page_at_offset(text: str, offset: int) -> int | None:
    page = None
    for marker in re.finditer(r"--- page (?P<page>\d+) ---", text[:offset]):
        page = int(marker.group("page"))
    return page


def _first_present(values: dict[str, object], keys: list[str]) -> float | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _deduplicate_spans(spans: list[EvidenceSpan]) -> list[EvidenceSpan]:
    result = []
    seen = set()
    for span in spans:
        key = (
            span.document_id,
            span.page,
            span.start_offset,
            span.end_offset,
            span.text,
        )
        if key not in seen:
            seen.add(key)
            result.append(span)
    return result
