"""冻结轻量证据的报告投影；只生成可追溯观察，不确认预测或评级。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from analysis.models import ClaimKind, ClaimRecord, EvidenceSpan, EventRecord, PeerSetVersion, SourceRecord
from .research_lite import _format_number, _period_relevant, read_evidence
from .storage import canonical_sha256

VERSION = "lite-report-semantics-v1.0.0"
SLOTS = {
    "A.business_model": ("business", "经营模式与收入确认", "销售渠道与收入确认政策不能单独证明终端需求；需结合回款、合同负债和渠道库存核对。"),
    "A.segments_and_channels": ("business", "分部与渠道", "渠道调整会影响销售结构，尚缺终端动销和渠道利润资料，不能仅凭公司描述确认护城河。"),
    "C.audit_and_control": ("risk", "审计与内控", "核查范围限于该报告页；年度内控表述不能替代期后高管及治理事项核查。"),
    "C.cash_debt_and_risk": ("quality", "现金与负债", "原文报表主体、列标题和附注必须联合核对，不能把截取的单页数字直接加入公式。"),
    "D.governance": ("governance", "关联交易与治理", "关联方交易需要进一步核对定价、余额及资金可支配性，披露金额本身不证明交易公允。"),
    "D.capital_actions": ("capital", "资本与承诺事项", "计划、承诺和实际执行分别跟踪；原文披露不等于后续事项均已完成。"),
    "E.industry_drivers": ("industry", "行业与经营驱动", "这里是公司经营披露，不能替代独立的行业供需、渠道库存或竞争份额证据。"),
}


def full_coverage(manifest, read_rows):
    """按完整 requirement/期间保留状态；不同投影出现不一致时保守留缺口。"""
    grouped = defaultdict(list)
    for source in manifest.get("source_inputs", []):
        descriptor = source.get("files", {}).get("question-coverage.jsonl")
        if descriptor:
            for row in read_rows(Path(descriptor["path"])):
                if row.get("company") != manifest["ticker"]:
                    raise ValueError("report_coverage_company_mismatch")
                if _period_relevant(row.get("period"), manifest["periods"]):
                    grouped[(row["requirement_id"], row["period"])].append(row)
    result = []
    for key, rows in sorted(grouped.items()):
        states = {r["state"] for r in rows}
        state = next(iter(states)) if len(states) == 1 else "pending"
        result.append({
            "requirement_id": key[0], "period": key[1], "question_id": rows[0]["question_id"],
            "state": state, "reason": "; ".join(sorted({str(r.get("reason") or "") for r in rows})),
            "fact_ids": sorted({i for r in rows for i in r.get("fact_ids", [])}),
            "text_evidence_ids": sorted({i for r in rows for i in r.get("text_evidence_ids", [])}),
            "next_actions": sorted({str(r.get("next_action")) for r in rows if r.get("next_action")}),
        })
    return result


def semantic_inputs(pack, manifest, core, as_of, read_rows):
    ticker = manifest["ticker"]
    claims, sources, events, peers = [], [], [], []
    gaps = ["预测与估值：缺少用户确认的增长、折现率、终值和情景概率；不生成目标价。"]
    evidence_index = {}

    def claim(category, text, *, evidence=(), facts=(), source_ids=(), spans=(), unknowns=()):
        identity = [VERSION, ticker, category, text, list(evidence), list(facts)]
        result = ClaimRecord(
            claim_id="report-claim-" + canonical_sha256(identity)[:24], ticker=ticker,
            category=category, text=text, claim_kind=ClaimKind.ANALYST_JUDGEMENT,
            evidence_source_ids=list(source_ids), evidence_fact_ids=list(facts),
            evidence_ids=list(evidence), evidence_spans=list(spans), as_of=as_of,
            counter_evidence=["未完成系统性反证检索；不能据当前材料认定不存在反证。"],
            unknowns=list(unknowns), invalidation_conditions=["来源更正、口径变化或新增相反披露时重新评估。"],
        )
        claims.append(result)
        return result

    for item in core.get("evidence", []):
        evidence_id = item["evidence_id"]
        verified = read_evidence(pack_dir=pack, evidence_id=evidence_id)
        if not verified["original_hash_verified"]:
            raise ValueError(f"report_original_hash_unverified:{evidence_id}")
        # 完整原文由索引哈希验证；正文保存定位片段，不把片段数字转为新财务事实。
        source_id = "report-document-" + item["original_sha256"][:24]
        if source_id not in {s.source_id for s in sources}:
            sources.append(SourceRecord(
                source_id=source_id, name=f"{core.get('company_name', ticker)} {item['period']} {item['document_class']}",
                source_type="disclosure", upstream_source_id="company:" + ticker,
                url=item.get("source_url"), document_hash=item["original_sha256"],
                retrieved_at=as_of, authority_level=1,
                notes="冻结包当前研究投影；原解析索引未登记精确可得时间，不作严格历史已知证明。",
                metadata={"original_path": item.get("original_path"), "availability_unverified": True},
            ))
        page = int(item["locator"].split(":")[1]) if item.get("locator", "").startswith("page:") else None
        span = EvidenceSpan(document_id=item["original_sha256"], page=page, text=item["excerpt"])
        evidence_index[evidence_id] = {**item, "source_id": source_id}
        category, label, implication = SLOTS.get(item.get("slot_id"), ("risk", "待核查原文", "原文尚未形成完整问题结论。"))
        excerpt = item["excerpt"]
        markers = ("留置", "商标使用", "股权激励", "合同负债", "销售模式", "动态价格", "重大缺陷")
        positions = [excerpt.find(m) for m in markers if m in excerpt]
        start = max(0, min(positions) - 65) if positions else 0
        quote = excerpt[start:start+240]
        claim(category, f"{item['period']} {item['locator']}的{label}原文节选：『{quote}』。{implication}",
              evidence=[evidence_id], source_ids=[source_id], spans=[span],
              unknowns=[implication, "精确发布时间和可得时间尚未在解析索引登记。"])

    if evidence_index:
        gaps.append("定位原文：精确可得时间尚未登记；当前研究可读，严格历史语义未验收。")

    # 数值观察只使用包内已选事实，期间显式进入索引。
    ready = [m for m in core.get("metrics", []) if m.get("state") == "ready"]
    annual = manifest["periods"]["annual"]
    for metric in ("operating_income", "parent_net_profit", "operating_cash_flow", "cash_profit_ratio"):
        rows = [m for m in ready if m.get("metric_id") == metric and m.get("period_type") == "cumulative" and m["period"] in annual]
        rows.sort(key=lambda m: m["period"])
        if rows:
            description = "；".join(f"{m['period']} {m.get('label', metric)}{_format_number(m['fact']['value'],m['fact']['unit'],metric)}" for m in rows)
            claim("financial", description + "。以上均为年度累计；需结合单季变化判断趋势，不把年末单季当全年。",
                  facts=[m["fact"]["fact_id"] for m in rows], unknowns=["变化归因仍需价格、销量和渠道证据。"])

    market = [m for m in ready if m.get("group") == "F"]
    if market:
        claim("valuation", "；".join(f"{m['label']}={_format_number(m['fact']['value'],m['fact']['unit'],m['metric_id'])}（数据期{m['fact']['period_end']}）" for m in market)
              + "。这是市场定价快照，不是合理价值；缺少确认的预测和折现假设，暂不估值。",
              facts=[m["fact"]["fact_id"] for m in market], unknowns=["历史估值分位、可比调整和前瞻盈利假设未完成。"])

    for item in core.get("governance", []):
        available = datetime.fromisoformat(item["available_at"].replace("Z", "+00:00"))
        if available > as_of:
            raise ValueError("report_governance_future_record")
        sid = "report-record-" + item["record_id"]
        sources.append(SourceRecord(source_id=sid, name=item["dataset_id"], source_type="supplier-structured",
            upstream_source_id="eastmoney", retrieved_at=available, available_at=available,
            raw_resource_snapshot_id=item["snapshot_id"],
            metadata={"record_id":item["record_id"], "row_key":item["row_key"], "fields":item["fields"]}))
        fields = item["fields"]
        text = "；".join(f"{k}={v}" for k,v in fields.items() if v is not None)
        category = "capital" if item["dataset_id"] == "dividend" else "governance"
        claim(category, text + "。记录时间与所标报告期分列；名册本身不证明任职期间的履职质量。" if category == "governance" else text + "。预披露不作为已支付分红；每十股口径不直接当每股金额。",
              evidence=[item["record_id"]], source_ids=[sid], unknowns=["精确公告时间、后续变更与独立原件仍需核对。"])
        if category == "capital":
            state = str(fields.get("ASSIGN_PROGRESS") or "unknown")
            events.append(EventRecord(event_id="report-event-" + item["record_id"], ticker=ticker,
                event_type="dividend", announced_at=available, available_at=available,
                lifecycle_state=state, summary=text, source_ids=[sid],
                status_updated_at=available, data_snapshot_id=item["snapshot_id"],
                metadata={"record_id":item["record_id"], "row_key":item["row_key"],
                    "announced_at_basis":"observation_upper_bound_not_publication_time", "source_period":item.get("period")},
                event_terms=fields))

    peer_items = core.get("peers", [])
    if peer_items:
        peer_source_ids = []
        for descriptor in manifest.get("peer_inputs", []):
            sid = "report-peer-source-" + descriptor["sha256"][:24]
            peer_source_ids.append(sid)
            sources.append(SourceRecord(source_id=sid,name="冻结同行投影",source_type="local-projection",
                retrieved_at=as_of, document_hash=descriptor["sha256"], metadata=descriptor))
        peers.append(PeerSetVersion(peer_set_id="report-peers-" + canonical_sha256(peer_items)[:24],
            target_ticker=ticker, as_of=as_of, created_at=as_of,
            included_tickers=[p["ticker"] for p in peer_items],
            inclusion_reason={p["ticker"]:p["comparability_basis"] for p in peer_items},
            selection_dimensions=["同一已验证白酒画像", "冻结期间与指标口径"],
            industry_taxonomy_version=manifest["profile_id"], source_ids=peer_source_ids,
            data_snapshot_id=manifest["pack_id"], metadata={"peers":peer_items,"limitations":"品牌带、渠道、产品结构与区域差异未标准化；集合未经用户确认。"}))
        claim("industry", "同行观察集为" + "、".join(p["name"] for p in peer_items) + "；只满足同一白酒画像筛选，品牌带、渠道和产品结构差异未调整，不直接用其倍数推算公司价值。",
              source_ids=peer_source_ids, unknowns=["独立行业供需与可比性调整不足。"])
    gaps.append("行业：现有公司经营披露和同行投影不能替代独立行业序列；不构造无依据的 IndustryFact 数值。")
    anchor = [m["fact"]["fact_id"] for m in market[:1]]
    claim("scenario", "当前保留历史事实观察，未来增长、折现率、终值和情景概率尚未经用户确认，三情景和评级保持待补。跟踪回款、收入确认、渠道变化及资本事项实际执行，再决定是否更新假设。",
          facts=anchor, unknowns=["用户确认的预测假设与有效期。"])
    return {"claims":claims,"sources":sources,"events":events,"peer_sets":peers,
            "gaps":gaps,"evidence_index":evidence_index,"display_metrics":core.get("metrics",[])}


def apply_sections(request, sections):
    """报告主体展示期间明确的研究表，完整事实仍留冻结 JSON/Excel。"""
    metrics = request.input_metadata.get("display_metrics")
    if not metrics or not any(m.get("group") for m in metrics):
        return
    periods = request.research_coverage.get("periods", {})
    for section in sections:
        if section.claims:
            section.summary=f"以下研究观察来自冻结事实和定位原文；披露、判断与尚待核查事项分别列示。"
        if section.number in {1, 2}:
            section.facts = []
            section.tables = []
        if section.number == 2:
            for label, kind, dates in [("年度累计", "cumulative", periods.get("annual",[])), ("季度单季", "single_quarter", periods.get("quarters",[]))]:
                for offset in range(0,len(dates),4):
                    selected_dates = dates[offset:offset+4]
                    grouped = {}
                    for m in metrics:
                        if m.get("group") not in {"B","C"} or m["period"] not in selected_dates or m["period_type"] not in {kind,"instant"}:
                            continue
                        row=grouped.setdefault(m["metric_id"],{"指标":m["label"],"口径":"期末存量" if m["period_type"]=="instant" else label})
                        row[m["period"]] = (_format_number(m["fact"]["value"],m["fact"]["unit"],m["metric_id"]) + f" [{m['fact_ref']}]" if m["state"]=="ready" else "待补")
                    section.tables.append({"name":label,"rows":list(grouped.values())})
        questions=[q for q in request.research_coverage.get("questions",[]) if q["step_id"]==f"ES0{section.number}"]
        missing=sorted({r for q in questions for r in q.get("missing_requirement_ids",[])})
        if missing:
            section.warnings.append(f"本节仍有{len(missing)}项完整研究要求待补；原文观察不自动关闭问题，具体要求及原因见冻结覆盖。")
        if not section.claims:
            section.summary=f"本节尚不能形成完整判断；待补要求：{', '.join(missing) or '尚无可追溯语义输入'}。"
