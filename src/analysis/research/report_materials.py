"""Resumable report materials: two complete Chinese filings and located passages.

The query checkpoint, raw-body availability and reading gaps are independent.
Existing inputs are read-only; all new projections are written to output_root.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib
import html
import json
import re
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from analysis.documents import parse_research_original
from analysis.filing_parser import detect_filing_period
from analysis.structured.research_coverage import _load_registry
from analysis.structured.scope import LITE_PROFILE_ID, load_research_profile, load_scope
from analysis.structured.storage import canonical_sha256
from .report_acquisition import ReportAcquisition, ReportCheckpoint


VERSION = "report-materials-v1.0.0"
PARSER_VERSION = "research-parser-v1.0.2"
TARGETS = {"D01": "latest_complete_chinese_annual", "D02": "latest_disclosed_chinese_interim"}
DOCUMENT_REQUIREMENTS = {"lite.context.latest_annual_original": "D01",
                         "lite.context.latest_interim_original": "D02"}
# Literal retrieval terms implement the registered RD routes. They identify
# passages only; a match never establishes a financial fact or a negative claim.
ROUTE_TERMS = {
    "RD01": ("经营模式", "销售模式", "主要业务", "收入确认", "商业模式"),
    "RD02": ("分产品", "分地区", "分渠道", "前五名客户", "前五名供应商", "主营业务分行业"),
    "RD03": ("产销量", "生产量", "销售量", "产能", "在建工程", "产销情况"),
    "RD04": ("研发投入", "研发人员", "研发项目"),
    "RD05": ("合并范围", "资产减值", "会计估计变更", "会计政策变更", "递延所得税"),
    "RD06": ("受限资产", "受限资金", "使用权受到限制", "短期借款", "长期借款", "现金流量表补充资料"),
    "RD07": ("关联交易", "实际控制人", "关联方", "公司治理", "承诺履行"),
    "RD08": ("利润分配", "股份回购", "募集资金", "权益分派"),
    "RD09": ("行业情况", "行业经营性信息", "行业发展", "行业格局", "行业政策"),
    "RD10": ("可能面对的风险", "可能面临的风险", "未来发展", "发展战略", "经营计划"),
    "RD11": ("股权激励", "员工持股", "股份支付", "限制性股票"),
    "RD12": ("审计意见", "内部控制", "关键审计事项", "会计师事务所"),
}


def _json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write(path: Path, value, *, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in value)
            if lines else json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bound(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ReportCheckpoint("report_materials_time_bound")


def _routes(profile, requirements):
    registry = _load_registry()
    registered = {r["route_id"] for r in registry["reading_routes"]}
    slots = {r["slot_id"]: r for r in profile["evidence_slots"]}
    defaults = {route for slot in slots.values() for route in slot["routes"]}
    if not requirements:
        return defaults, list(slots.values())
    chosen, selected_slots = set(), set()
    by_requirement = {r["requirement_id"]: r for r in registry["requirements"]}
    for item in requirements:
        ident = str(item.get("requirement_id") or "")
        slot_id = str(item.get("slot_id") or ident.removeprefix("lite.evidence."))
        if slot_id in slots:
            chosen.update(slots[slot_id]["routes"])
            selected_slots.add(slot_id)
        hints = item.get("task_hint") or {}
        document_kind = DOCUMENT_REQUIREMENTS.get(ident)
        if document_kind:
            if set(hints.get("document_classes", [document_kind])) != {document_kind}:
                raise ValueError("report_materials_requirement_document_class_mismatch")
            periods = hints.get("report_periods") or ([item["period"]] if item.get("period") else [])
            suffix = "-12-31" if document_kind == "D01" else "-06-30"
            if (any(not str(value).endswith(suffix) for value in periods) or len(set(periods)) > 1 or
                    item.get("period") and periods != [item["period"]]):
                raise ValueError("report_materials_requirement_period_mismatch")
            for value in periods:
                date.fromisoformat(value)
            chosen.update(defaults)
            selected_slots.update(slots)
        for route in [item.get("route_id"), *(item.get("routes") or []), *(hints.get("route_ids") or [])]:
            if route:
                if route not in registered:
                    raise ValueError("report_materials_unknown_reading_route:" + str(route))
                chosen.add(route)
        registered_requirement = by_requirement.get(ident, {})
        for path in registered_requirement.get("paths", []):
            if path.get("kind") == "reading_section":
                chosen.add(path["route_id"])
        path = item.get("input") or {}
        if isinstance(path, Mapping) and path.get("kind") == "reading_section":
            route = path.get("route_id")
            if route not in registered:
                raise ValueError("report_materials_unknown_reading_route:" + str(route))
            chosen.add(route)
    if not chosen:
        raise ValueError("report_materials_no_registered_reading_requirement")
    selected_slots.update(s for s, slot in slots.items() if chosen.intersection(slot["routes"]))
    return chosen, [slots[s] for s in sorted(selected_slots)]


def _candidate(row, ticker, cutoff):
    if row.get("ticker", (row.get("metadata") or {}).get("ticker")) != ticker:
        return None
    url = row.get("url") or row.get("resource_url") or ""
    parsed_url = urlsplit(url)
    if (parsed_url.scheme != "https" or parsed_url.hostname not in {"static.cninfo.com.cn", "www.cninfo.com.cn"}
            or parsed_url.username or parsed_url.password or parsed_url.fragment):
        return None
    if not str(row.get("resource_id") or row.get("canonical_resource_id") or "").startswith("cninfo:"):
        return None
    title = html.unescape(re.sub(r"<[^>]*>", "", str(row.get("title") or "")))
    compact = re.sub(r"\s+", "", title)
    if (row.get("language") == "en" or re.search(
            r"摘要|英文|english|annualreport|关于|提示|公告|通知|决议|取消|说明|社会责任|可持续|内部控制", compact, re.I)):
        return None
    if re.search(r"更正|修订|补充", compact) and not re.search(r"报告[（(](?:更正|修订|更新).{0,6}[）)]$", compact):
        return None
    period = detect_filing_period(compact)
    if period is None or not re.search(r"年度报告|中期报告|半年报|年报", compact):
        return None
    end = period.period_end
    kind = "D01" if end.month == 12 else "D02" if end.month == 6 else None
    if kind is None or end > cutoff:
        return None
    published = row.get("published_at")
    if not published:
        return None
    try:
        stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        published_day = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date() if stamp.tzinfo else stamp.date()
    except ValueError:
        return None
    if published_day > cutoff:
        return None
    return {**row, "ticker": ticker, "title": title, "period": end.isoformat(), "document_class": kind,
            "resource_id": row.get("resource_id") or row.get("canonical_resource_id"),
            "url": row.get("url") or row.get("resource_url"), "language": "zh", "selected": True}


def _load_inputs(roots, ticker, cutoff):
    merged, requests, evidence, catalogs = {}, [], [], []
    for root in roots:
        payload = _json(root / "live-documents.json", {})
        requests.extend((root, r) for r in payload.get("requests", []))
        catalogs.extend(r for r in payload.get("catalogs", []) if r.get("ticker") == ticker)
        for row in payload.get("documents", []):
            if row.get("ticker") != ticker:
                continue
            key = row.get("resource_id") or row.get("canonical_resource_id")
            if not key:
                continue
            previous = merged.get(key, {})
            url = row.get("url") or row.get("resource_url")
            if previous.get("url") and url and previous["url"] != url:
                raise ValueError("report_materials_resource_url_conflict:" + key)
            # A catalog supplies publication dates; a body index supplies hashes
            # and paths. Empty later fields must not erase either provenance.
            merged[key] = {**previous, **{k: v for k, v in row.items() if v not in (None, "", [])},
                           "_roots": [*previous.get("_roots", []), root], "url": url}
        source = root / "document-evidence.jsonl"
        if source.is_file():
            for line in source.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row.get("company") == ticker:
                        evidence.append(row)
    candidates = [value for row in merged.values() if (value := _candidate(row, ticker, cutoff))]
    return candidates, requests, evidence, catalogs


def _select(candidates):
    selected = {}
    for row in candidates:
        key = row["document_class"]
        rank = (row["period"], str(row["published_at"]), str(row["resource_id"]))
        prior = selected.get(key)
        if prior is None or rank > (prior["period"], str(prior["published_at"]), str(prior["resource_id"])):
            selected[key] = row
    return selected


def _local_original(document, requests):
    pairs = []
    if document.get("original_sha256") and document.get("original_path"):
        raw_path = Path(document["original_path"])
        locations = [raw_path] if raw_path.is_absolute() else [r / raw_path for r in document.get("_roots", [])]
        pairs.extend((p, document["original_sha256"]) for p in locations)
    for root, request in requests:
        wire = request.get("request") or {}
        if request.get("http_status") == 200 and wire.get("url") == document["url"] and not wire.get("data"):
            relative = request.get("relative_path")
            if relative and request.get("sha256"):
                for base in (root, root / "acquisition"):
                    path = (base / relative).resolve()
                    if not path.is_relative_to(base.resolve()):
                        raise ValueError("report_original_path_escape")
                    pairs.append((path, request["sha256"]))
    mismatch = False
    for path, expected in pairs:
        if path.is_file():
            if _sha(path) == expected:
                return {"original_path": str(path.resolve()), "original_sha256": expected,
                        "download_status": "cached", "cache_reused": True}
            mismatch = True
    if mismatch:
        raise ValueError("report_original_hash_mismatch")
    return None


def _parse(document, output):
    original = Path(document["original_path"])
    if _sha(original) != document["original_sha256"]:
        raise ValueError("report_original_hash_mismatch")
    cached_path = Path(document["parsed_path"]) if document.get("parsed_path") else None
    if cached_path and cached_path.is_file():
        cached = _json(cached_path)
        content = {k: v for k, v in cached.items() if k != "content_sha256"}
        if (cached.get("parser_version") == PARSER_VERSION and
                cached.get("original_sha256") == document["original_sha256"] and
                canonical_sha256(content) == cached.get("content_sha256")):
            return {**cached, "parsed_path": str(cached_path.resolve()), "cache_reused": True}
    return parse_research_original(original, mime_type=document.get("mime_type") or "application/pdf",
                                   output_dir=output / "parsed")


def _verify_report_body(document, parsed):
    pages = [u for u in parsed.get("units", []) if u.get("kind") == "page"]
    if not pages:
        raise ValueError("report_page_locator_missing")
    cover = re.sub(r"\s+", "", pages[0].get("text", ""))
    kind = r"(?:年度报告|年报)" if document["document_class"] == "D01" else r"(?:半年度报告|中期报告|半年报)"
    pattern = document["period"][:4] + r"年?" + kind
    if not re.search(pattern, cover) or re.search(kind + "摘要", cover):
        raise ValueError("report_title_body_period_mismatch")


def _readable(document):
    if document.get("parse_status") != "parsed" or not document.get("original_sha256") or not document.get("original_path"):
        return False
    try:
        path = Path(document["original_path"])
        return path.is_file() and _sha(path) == document["original_sha256"]
    except OSError:
        return False


def _passages(document, parsed, routes, prior):
    units = {u["locator"]: u for u in parsed.get("units", []) if u.get("kind") == "page"}
    rows = []
    for route in sorted(routes):
        # Existing evidence IDs remain immutable when a valid parser page
        # exactly matches their text. This also avoids duplicate pack entries.
        reused = [r for r in prior if r.get("route_id") == route and
                  r.get("original_sha256") == document["original_sha256"] and
                  r.get("period") == document["period"] and r.get("parser_version") == parsed["parser_version"] and
                  r.get("text") and units.get(r.get("locator"), {}).get("text") == r.get("text")]
        if reused:
            rows.extend(reused)
            continue
        scored = []
        for locator, unit in units.items():
            text = str(unit.get("text") or "")
            compact = re.sub(r"\s+", "", text)
            if len(compact) < 80 or "目录" in compact[:100] or len(re.findall(r"\.{4,}|…{2,}", text)) > 3:
                continue
            matched = [term for term in ROUTE_TERMS[route] if term in compact]
            if matched:
                scored.append((len(matched), -int(locator.split(":")[1]), locator, text, matched))
        for _, _, locator, text, matched in sorted(scored, reverse=True)[:3]:
            identity = [VERSION, document["ticker"], document["period"], document["original_sha256"],
                        parsed["parser_version"], route, locator, hashlib.sha256(text.encode()).hexdigest()]
            rows.append({"evidence_id": "document-evidence-" + canonical_sha256(identity)[:24],
                "company": document["ticker"], "period": document["period"], "route_id": route,
                "document_class": document["document_class"], "original_sha256": document["original_sha256"],
                "original_url": document["url"], "published_at": document["published_at"],
                "parser_version": parsed["parser_version"], "locator": locator, "text": text,
                "matched_terms": matched, "semantic_status": "organized_literal_source_passage",
                "question_answer_status": "not_evaluated", "data_nature": "source_text",
                "consumption_status": "source_text_evidence", "numeric_formula_eligible": False})
    return rows


def refresh_report_materials(*, ticker: str, cutoff: date, output_root: Path, db_path: Path, data_root: Path,
        evidence_roots: Sequence[Path] = (), requirements: Sequence[Mapping[str, Any]] = (),
        allow_network: bool = True, max_documents: int = 2, max_seconds: float | None = None,
        profile_id: str = LITE_PROFILE_ID, refresh: bool = False) -> dict[str, Any]:
    """Run one bounded round; repeat with the same output to resume durable work.

    completed/query_complete describes the query workflow. materials_ready and
    missing_documents/missing_slots separately describe usable source material.
    max_seconds is cooperative between documents and at every network boundary.
    """
    started = time.monotonic()
    if not re.fullmatch(r"\d{6}", ticker) or not 1 <= max_documents <= 2:
        raise ValueError("report_materials_invalid_ticker_or_document_bound")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("report_materials_invalid_time_bound")
    deadline = started + max_seconds if max_seconds is not None else None
    output = Path(output_root).resolve()
    roots = list(dict.fromkeys(Path(p).resolve() for p in evidence_roots))
    if output in roots:
        raise ValueError("report_materials_output_must_not_overwrite_input")
    profile = load_research_profile(profile_id)
    routes, slots = _routes(profile, requirements)
    identity = {"version": VERSION, "ticker": ticker, "cutoff": cutoff.isoformat(), "profile_id": profile_id,
                "profile_sha256": profile["content_sha256"], "scope_sha256": load_scope()["content_sha256"],
                "db_path": str(Path(db_path).resolve()), "data_root": str(Path(data_root).resolve())}
    checkpoint = output / "report-materials-state.json"
    state = _json(checkpoint, {"identity": identity, "catalog": {}, "bodies": {}, "failures": []})
    if state["identity"] != identity:
        raise ValueError("report_materials_checkpoint_identity_changed")
    if refresh and (state.get("status") == "completed" or not state.get("refresh_started")):
        if state["catalog"]:
            state.setdefault("previous_catalogs", []).append(state["catalog"])
        if state["bodies"]:
            state.setdefault("previous_bodies", []).append(state["bodies"])
        state["catalog"] = {}
        state["bodies"] = {}
        state["refresh_started"] = True
        state["refresh_pending"] = list(TARGETS)
    previous_output = _json(output / "live-documents.json", {})
    previous_evidence = []
    if (output / "document-evidence.jsonl").is_file():
        previous_evidence = [json.loads(line) for line in (output / "document-evidence.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
    state["status"] = "running"
    state["requested_routes"] = sorted(routes)
    def save():
        _write(checkpoint, state)
    save()
    backend = None
    counts = {"reused": 0, "fetched": 0, "parsed": 0, "parse_reused": 0, "evidence": 0}
    failures, input_issues = [], []
    documents = {d["document_class"]: d for d in previous_output.get("documents", [])}
    evidence, catalogs = previous_evidence, previous_output.get("catalogs", [])
    selected, query_complete, stopped = {}, False, False
    source_halt = None
    def get_backend():
        nonlocal backend
        if backend is None:
            backend = ReportAcquisition(db_path=Path(db_path), data_root=Path(data_root), deadline=deadline)
        return backend
    def emit():
        requests = list(state.get("requests", []))
        if backend is not None:
            requests.extend(backend.requests)
        _write(output / "document-evidence.jsonl", sorted(
            {r["evidence_id"]: r for r in evidence}.values(), key=lambda r: r["evidence_id"]), lines=True)
        _write(output / "live-documents.json", {"profile_id": profile_id,
            "profile_sha256": profile["content_sha256"], "scope_sha256": identity["scope_sha256"],
            "documents": list(documents.values()), "catalogs": catalogs, "requests": requests,
            "performed_network_io": bool(requests), "manual_acceptance": "pending"})
    try:
        candidates, requests, prior, old_catalogs = _load_inputs([*roots, output], ticker, cutoff)
        selected = _select(candidates)
        local = {}
        for kind, document in selected.items():
            try:
                local[kind] = _local_original(document, requests)
            except ValueError as exc:
                input_issues.append({"document_class": kind, "resource_id": document["resource_id"], "reason": str(exc)})
        expected = {"D01": f"{cutoff.year - (1 if cutoff >= date(cutoff.year, 4, 30) else 2)}-12-31",
                    "D02": f"{cutoff.year if cutoff >= date(cutoff.year, 8, 31) else cutoff.year - 1}-06-30"}
        local_complete = all(local.get(k) and selected[k]["period"] >= expected[k] for k in TARGETS)
        catalog_result = None
        if (not local_complete or state.get("refresh_pending") or state["catalog"].get("run_id")) and allow_network:
            _bound(deadline)
            try:
                catalog_result = get_backend().catalog(ticker=ticker, cutoff=cutoff,
                                                       state=state["catalog"], save=save)
                query_complete = bool(catalog_result["query_complete"])
                if query_complete:
                    remote = [value for row in catalog_result["entries"]
                              if (value := _candidate({**row, "ticker": ticker}, ticker, cutoff))]
                    selected = _select([*candidates, *remote])
                    state["refresh_pending"] = [k for k in state.get("refresh_pending", []) if k in selected]
                    # Preserve a verified body when its resource identity agrees.
                    for kind, document in selected.items():
                        original = next((r for r in candidates if r["resource_id"] == document["resource_id"]), {})
                        selected[kind] = {**original, **document}
                    catalogs = [{"ticker": ticker, "scope": "report_documents", "terminal": True,
                        "query_start": f"{cutoff.year - 1}-01-01", "query_end": cutoff.isoformat(),
                        "count": len(catalog_result["entries"]), "run_id": state["catalog"].get("run_id"),
                        "body_downloaded": False}]
                else:
                    failures.append({"stage": "catalog", "reason": catalog_result.get("reason", "catalog_incomplete")})
            except Exception as exc:
                failures.append({"stage": "catalog", "reason": str(exc)})
        else:
            query_complete = local_complete
            catalogs = [r for r in old_catalogs if r.get("scope") == "report_documents"]
        previous_documents = documents
        documents = {}
        previous_evidence = evidence
        evidence = []
        for kind in TARGETS:
            if kind not in selected:
                continue
            document = {k: v for k, v in selected[kind].items() if not k.startswith("_")}
            document.update(scope_id=profile_id, selected=True, semantic_status="not_started",
                            parse_status="not_requested", download_status="not_requested")
            previous = previous_documents.get(kind, {})
            if (local.get(kind) and previous.get("resource_id") == document["resource_id"] and
                    previous.get("original_sha256") == local[kind]["original_sha256"] and
                    previous.get("parse_status") == "parsed"):
                document.update(previous)
                evidence.extend(r for r in previous_evidence if r.get("original_sha256") == document["original_sha256"]
                                and r.get("route_id") in routes)
            documents[kind] = document
        done = 0
        for kind, document in documents.items():
            _bound(deadline)
            previous_document = previous_documents.get(kind, {})
            already_processed = (previous_document.get("resource_id") == document["resource_id"] and
                                 previous_document.get("parse_status") == "parsed")
            if done >= max_documents and not already_processed:
                stopped = True
                continue
            done += not already_processed
            try:
                try:
                    available = (None if allow_network and kind in state.get("refresh_pending", [])
                                 else _local_original(selected[kind], requests))
                except ValueError:
                    if not (allow_network and catalog_result and catalog_result.get("bundle")):
                        raise
                    available = None
                if available is not None:
                    document.update(available)
                    counts["reused"] += 1
                elif source_halt:
                    document["reason"] = source_halt
                    continue
                elif allow_network and catalog_result and catalog_result.get("bundle"):
                    entry = next((r for r in catalog_result["entries"]
                                  if r["canonical_resource_id"] == document["resource_id"]), None)
                    if entry is None:
                        raise ValueError("selected_report_not_in_verified_catalog")
                    body_state = state["bodies"].setdefault(document["resource_id"], {})
                    document.update(get_backend().body(bundle=catalog_result["bundle"], entry=entry,
                                                       state=body_state, save=save))
                    if kind in state.get("refresh_pending", []):
                        state["refresh_pending"].remove(kind)
                    # A different body hash invalidates a formerly indexed path.
                    document.pop("parsed_path", None)
                    counts["fetched"] += 1
                    evidence = [r for r in evidence if r.get("document_class") != kind]
                    emit()
                else:
                    document["reason"] = "network_disabled" if not allow_network else "verified_catalog_required"
                    continue
                parsed = _parse(document, output)
                document.update({k: parsed[k] for k in ("parsed_path", "parser_version", "parse_status", "content_sha256")})
                document["page_count"] = sum(u.get("kind") == "page" for u in parsed.get("units", []))
                counts["parse_reused" if parsed.get("cache_reused") else "parsed"] += 1
                if parsed["parse_status"] != "parsed":
                    document["reason"] = parsed.get("reason") or "report_parse_pending"
                else:
                    _verify_report_body(document, parsed)
                    passages = _passages(document, parsed, routes, prior)
                    evidence.extend(passages)
                    document.update(semantic_status="source_passages_organized", question_answer_status="not_evaluated",
                                    consumption_status="source_text_evidence", organized_passage_count=len(passages))
                emit()
                save()
            except Exception as exc:
                document.update(reason=str(exc), parse_status="failed" if document.get("original_sha256") else "not_requested")
                failures.append({"document_class": kind, "resource_id": document["resource_id"], "reason": str(exc)})
                if any(reason in str(exc) for reason in ("upstream_bot_challenge", "challenge_page_detected", "http_403")):
                    source_halt = "report_source_access_halted"
                emit()
                save()
    except (ReportCheckpoint, KeyboardInterrupt) as exc:
        stopped = True
        state["stop_reason"] = str(exc) or "interrupted"
    except Exception as exc:
        failures.append({"stage": "service", "reason": str(exc)})
    finally:
        if backend is not None:
            state.setdefault("requests", []).extend(backend.requests)
            backend.close()
            backend = None
    ready = {k for k, d in documents.items() if _readable(d)}
    missing_documents = [{"document_class": k, "target": target,
                          "reason": documents.get(k, {}).get("reason") or ("catalog_report_missing" if query_complete
                                     and k not in documents else "report_not_ready")}
                         for k, target in TARGETS.items() if k not in ready]
    missing_slots = [{"slot_id": slot["slot_id"], "routes": slot["routes"], "reason": "located_source_passage_missing"}
                     for slot in slots if not any(r["route_id"] in slot["routes"] for r in evidence)]
    unprocessed = any(k not in ready for k in documents)
    refresh_pending = bool(state.get("refresh_pending"))
    status = ("checkpointed" if stopped else "partial" if failures or unprocessed or refresh_pending or (missing_documents and not query_complete)
              else "completed")
    if failures and not ready and not stopped:
        status = "failed"
    counts["evidence"] = len({r["evidence_id"] for r in evidence})
    body_queries_complete = all(d.get("download_status") in {"cached", "downloaded"} for d in documents.values())
    result = {"status": status, "query_complete": query_complete and body_queries_complete and not stopped and not refresh_pending,
        "materials_ready": not missing_documents, "resumable": stopped or bool(missing_documents) or bool(failures) or refresh_pending,
        "output_root": str(output), "document_evidence_path": str(output / "document-evidence.jsonl"),
        "live_documents_path": str(output / "live-documents.json"), "checkpoint_path": str(checkpoint),
        "profile_id": profile_id, "profile_sha256": profile["content_sha256"], "scope_sha256": identity["scope_sha256"],
        "selected_resources": [{"document_class": k, "resource_id": r["resource_id"], "period": r["period"]}
                               for k, r in selected.items()], "counts": counts, "failures": failures,
        "input_issues": input_issues,
        "source_halt": source_halt,
        "missing_documents": missing_documents, "missing_slots": missing_slots,
        "remaining": missing_documents + missing_slots,
        "performed_network_io": len(state.get("requests", [])) > state.get("previous_request_count", 0)}
    state.update(status=status, last_result=result, previous_request_count=len(state.get("requests", [])))
    state["failures"].extend(failures)
    emit()
    save()
    return result
