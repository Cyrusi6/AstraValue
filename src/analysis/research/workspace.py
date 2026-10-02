from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from difflib import get_close_matches
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from analysis.structured.identity import CompanyResolver, SecurityIdentity
from analysis.structured.research_lite import build_lite_pack, read_evidence
from analysis.structured.scope import load_research_profile

ROOT = Path(__file__).resolve().parents[3]
POLICY = "先读取get_research_prompt统一买方研究规则，以get_research_brief取得写作材料。模型自主分析、假设、估值、评级和写作，允许有依据的情景推演；代码计算。风险与失效条件集中一次，不要求逐章反证；技术质检留在任务记录，知识按需读取。"
TOPICS = {
    "financials": {"B"}, "cash_flow_quality": {"B", "C"},
    "business": {"A"}, "governance": {"C", "D"},
    "capital": {"D"}, "industry": {"E"}, "valuation": {"F"},
}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ResearchError(ValueError):
    pass


def event_reading_rows(pack: dict, ticker: str, as_of: str) -> list[dict]:
    """Route existing EventRecords without deriving conclusions or changing their terms."""
    from analysis.event_state import load_event_taxonomy
    from analysis.models import EventRecord, aware_utc
    if not pack.get('events'):
        return []
    taxonomy = load_event_taxonomy()['event_types']
    cutoff = datetime.combine(date.fromisoformat(as_of), time.max, ZoneInfo('Asia/Shanghai'))
    rows = []
    for record in pack['events']:
        event = EventRecord.model_validate(record)
        if event.ticker != ticker:
            raise ResearchError('event_company_mismatch')
        if aware_utc(event.available_at) > cutoff:
            continue
        definition = taxonomy.get(event.event_type, {})
        group = 'D' if 4 in definition.get('report_steps', []) else 'C'
        rows.append({'event_id': event.event_id, 'group': group,
            'period': record.get('period_end') or event.announced_at.date().isoformat(),
            'event': record, 'read_entry': {'tool': 'read_evidence', 'evidence_id': event.event_id}})
    return rows


def validate_integer_parameter(value, field: str, code: str, maximum: int | None = None):
    """Keep direct Python/CLI calls as bounded as the published MCP schema."""
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        allowed = f"1..{maximum}" if maximum is not None else ">=1"
        raise ResearchError(f"{code}:{field} must be an integer {allowed}; received {value!r}; follow next_page for continuation")


class ResearchWorkspace:
    def __init__(self, root: Path = ROOT, config: dict | None = None):
        self.root = root.resolve()
        self.config = config or read_json(self.root / "config/research_workspace.json")
        self.state = self.path(self.config["state_root"])
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = self.state / "research-tasks.sqlite"
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS research_tasks (
                  id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                  state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS research_artifacts (
                  id TEXT PRIMARY KEY, research_id TEXT NOT NULL, kind TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def path(self, value: str) -> Path:
        return (self.root / value).resolve()

    def material_input_roots(self, cutoff: date) -> tuple[list[Path], list[Path]]:
        """Resolve registered inputs, including reusable earlier document indexes."""
        projections = list(dict.fromkeys(self.path(p) for p in self.config.get("projection_roots", [])))
        evidence = []
        for value in self.config.get("evidence_roots", []):
            root = self.path(value)
            # Accept a concrete evidence directory as well as a dated collection.
            # Only immediate ISO-date children are eligible; archives are not searched.
            candidates = [root]
            if root.is_dir():
                for child in sorted(root.iterdir(), reverse=True):
                    if not child.is_dir():
                        continue
                    try:
                        recorded_date = date.fromisoformat(child.name)
                    except ValueError:
                        continue
                    if recorded_date <= cutoff:
                        candidates.append(child)
            for candidate in candidates:
                try:
                    if date.fromisoformat(candidate.name) > cutoff:
                        continue
                except ValueError:
                    pass
                if any((candidate / name).is_file() for name in
                       ("document-evidence.jsonl", "live-documents.json", "official-table-facts.jsonl")):
                    evidence.append(candidate)
        return projections, list(dict.fromkeys(evidence))

    def peer_input_roots(self) -> list[Path]:
        """Peer-only inputs must not inject stale target-company facts."""
        return list(dict.fromkeys(self.path(p) for p in self.config.get("peer_projection_roots", [])))

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _save(self, state: dict, expected_revision: int, *, connection: sqlite3.Connection | None = None):
        if connection is None:
            with self.connect() as con:
                self._save(state, expected_revision, connection=con)
            return
        changed = connection.execute(
            "UPDATE research_tasks SET state_json=?,revision=revision+1 WHERE id=? AND revision=?",
            (encode(state), state["research_id"], expected_revision),
        ).rowcount
        if not changed:
            raise ResearchError("concurrent_task_update:reload_and_retry")

    def task(self, research_id: str) -> tuple[dict, int]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM research_tasks WHERE id=?", (research_id,)).fetchone()
        if row is None:
            raise ResearchError("unknown_research_id")
        return json.loads(row["state_json"]), row["revision"]

    def resolve(self, company: str, cutoff: date):
        path = self.path(self.config["identity_file"])
        if not path.exists():
            raise ResearchError("identity_catalog_missing:configure_or_acquire_security_master")
        identities = []
        for row in read_json(path)["stockList"]:
            if row.get("category") != "A股":
                continue
            # Market is asserted by the source orgId, not inferred from code digits.
            org = row.get("orgId", "")
            market = "SSE" if org.startswith("gssh") else "SZSE" if org.startswith("gssz") else None
            if not market:
                continue
            code = row["code"]
            canonical = code + (".SH" if market == "SSE" else ".SZ")
            identities.append(SecurityIdentity(
                company_id="company:" + code, security_id=canonical, canonical_ticker=canonical,
                security_code=code, market=market, current_name=row["zwjc"], security_type="A",
                source_record_ids=("cninfo-stock-list:" + org,),
            ))
        return CompanyResolver(identities).resolve(company, as_of=cutoff)

    def _verify_pack(self, directory: Path) -> dict:
        manifest = read_json(directory / "manifest.json")
        outputs = manifest.get("output_hashes", {})
        if not isinstance(outputs, dict):
            raise ResearchError("pack_manifest_outputs_invalid")
        required = ("core-pack.md", "core-pack.json", "core-coverage.json", "evidence-index.jsonl", "next-work.json")
        for name in required:
            if name not in outputs:
                raise ResearchError("pack_integrity_failed:" + name)
        root = directory.resolve()
        for name, expected in outputs.items():
            relative = Path(name)
            output = (root / relative).resolve()
            if relative.is_absolute() or PureWindowsPath(name).drive or '..' in name.replace('\\','/').split('/') or output == root or not output.is_relative_to(root):
                raise ResearchError("pack_output_path_invalid:" + name)
            try:
                actual = sha(output)
            except OSError as exc:
                raise ResearchError("pack_integrity_failed:" + name) from exc
            if expected != actual:
                raise ResearchError("pack_integrity_failed:" + name)
        return manifest

    def _select_pack(self, ticker: str, cutoff: date) -> Path | None:
        for root in self.config["pack_roots"]:
            base = self.path(root) / ticker / cutoff.isoformat()
            options = []
            for path in base.glob("lite-pack-*/manifest.json"):
                manifest = self._verify_pack(path.parent)
                if manifest["ticker"] != ticker or manifest["as_of"] != cutoff.isoformat():
                    raise ResearchError("pack_identity_mismatch")
                if manifest["profile_id"] == self.config["profile_id"]:
                    version = tuple(int(v) for v in manifest["pack_version"].rsplit("v", 1)[1].split("."))
                    options.append((version, path.parent))
            if options:
                version = max(v for v, _ in options)
                matching = [p for v, p in options if v == version]
                if len(matching) != 1:
                    audit_path = base / "last-run-audit.json"
                    audit = read_json(audit_path) if audit_path.exists() else {}
                    selected = [p for p in matching if p.name == audit.get("pack_id")]
                    if len(selected) == 1:
                        return selected[0]
                    # Rebuild from declared projections instead of guessing among revisions.
                    return None
                return matching[0]
        return None

    def prepare_research(self, company: str, as_of: str = "latest", scope: str = "eight_step") -> dict:
        if scope != "eight_step":
            return {"status": "capability_gap", "reason": "unsupported_scope", "supported": ["eight_step"]}
        cutoff = date.today() if as_of == "latest" else date.fromisoformat(as_of)
        if cutoff > date.today():
            raise ResearchError("future_cutoff_not_allowed")
        resolved = self.resolve(company, cutoff)
        if resolved.identity is None:
            return {"status": resolved.status.value, "reasons": list(resolved.reason_codes),
                    "candidates": [{"ticker": i.canonical_ticker, "name": i.current_name} for i in resolved.candidates]}
        identity = resolved.identity
        profile = load_research_profile(self.config["profile_id"])
        if identity.security_code not in profile["industry_profile"]["supported_tickers"]:
            return {"status": "capability_gap", "ticker": identity.canonical_ticker,
                    "reason": profile["industry_profile"]["unsupported_message"]}
        request_key = digest([identity.canonical_ticker, cutoff.isoformat(), scope, profile["content_sha256"]])
        state = {"research_id": "r_" + request_key[:24], "ticker": identity.security_code,
                 "company": identity.current_name, "canonical_ticker": identity.canonical_ticker,
                 "as_of": cutoff.isoformat(), "scope": scope, "status": "preparing",
                 "snapshot_id": None, "pack_path": None,
                 "stages": {"acquisition": "pending", "processing": "pending", "analysis": "pending", "rendering": "pending"},
                 "created_at": datetime.now(timezone.utc).isoformat()}
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO research_tasks(id,request_key,state_json) VALUES(?,?,?)",
                        (state["research_id"], request_key, encode(state)))
        current, revision = self.task(state["research_id"])
        if current["pack_path"]:
            return self.get_task(current["research_id"], include_pack=True)
        try:
            pack = self._select_pack(identity.security_code, cutoff)
            if pack is None:
                roots, evidence_roots = self.material_input_roots(cutoff)
                existing = [p for p in roots if (p / identity.security_code).is_dir()]
                if not existing:
                    current.update(status="acquisition_required", reason="registered_projection_missing")
                    self._save(current, revision)
                    return self.get_task(current["research_id"])
                supplements = [p for p in roots if p != existing[0]]
                result = build_lite_pack(input_root=existing[0], ticker=identity.security_code, as_of=cutoff,
                                         output_root=self.state / "packs", supplements=supplements,
                                         profile_id=self.config["profile_id"], peer_roots=self.peer_input_roots(),
                                         evidence_roots=evidence_roots)
                pack = Path(result["pack_dir"])
            manifest = self._verify_pack(pack)
            current.update(snapshot_id=manifest["pack_id"], pack_path=str(pack),
                           manifest_sha256=sha(pack / "manifest.json"), status="analysis_pending")
            current.pop("reason", None)
            coverage = read_json(pack / "core-coverage.json")
            missing = any(x.get("required") and x["state"] == "pending" for x in coverage["requirements"])
            current["stages"].update(acquisition="partial" if missing else "core_available",
                                     processing=manifest["status"])
            if manifest["status"] != "ready":
                current["status"] = manifest["status"]
            self._save(current, revision)
        except (OSError, ValueError) as exc:
            current.update(status="preparation_failed", reason=str(exc))
            self._save(current, revision)
        return self.get_task(current["research_id"], include_pack=True)

    def pack(self, research_id: str):
        state, _ = self.task(research_id)
        if not state["pack_path"]:
            raise ResearchError("research_pack_not_ready")
        path = Path(state["pack_path"])
        if sha(path / "manifest.json") != state["manifest_sha256"]:
            raise ResearchError("frozen_manifest_changed")
        self._verify_pack(path)
        payload = read_json(path / "core-pack.json")
        for item in payload.get("supplemental_evidence", []):
            if sha(Path(item["original_path"])) != item["original_sha256"]:
                raise ResearchError("supplement_integrity_failed")
        for item in payload.get("processing_attachments", []):
            if sha(Path(item["path"])) != item["sha256"]:
                raise ResearchError("processing_attachment_integrity_failed")
        return state, path, payload

    def get_task(self, research_id: str, include_pack: bool = False) -> dict:
        state, revision = self.task(research_id)
        result = {k: v for k, v in state.items() if k not in {"pack_path", "manifest_sha256"}}
        result.update(revision=revision, research_policy=POLICY)
        if state["pack_path"]:
            result.pop("reason", None)
            _, path, payload = self.pack(research_id)
            requirements = payload["coverage_requirements"]
            result["coverage"] = {"core": dict(Counter(i["state"] for i in requirements)),
                                  "required_core": dict(Counter(i["state"] for i in requirements if i.get("required"))),
                                  "research_questions": "pending_until_agent_assessment"}
            pending = [i for i in requirements if i["state"] == "pending"]
            groups = {}
            for item in pending:
                key = item.get("metric_id") or item.get("requirement_id", "unknown")
                row = groups.setdefault(key, {"input": key, "reason": item.get("reason"), "periods": [], "required": item.get("required")})
                if item.get("period"):
                    row["periods"].append(item["period"])
            result["core_gaps"] = list(groups.values())
            if include_pack and state["stages"]["processing"] == "ready":
                # A presentation overlay, not a mutation of a frozen historical package.
                content = (path / "core-pack.md").read_text(encoding="utf-8")
                result["content"] = content.replace("不自动评级", "评级按当前research_policy由模型自主提出")
                result["token_count"] = payload["token_count"]
                result["token_count_scope"] = "frozen_core_only; policy overlay and envelope additional"
        return result

    def query_research(self, research_id: str, topic: str = "financials",
                       period_type: Literal["cumulative", "single_quarter", "instant", "current", "ttm", "ratio", "all"] = "cumulative",
                       metric_ids: list[str] | None = None, question: str = "",
                       page: Annotated[int, Field(ge=1)] = 1,
                       page_size: Annotated[int, Field(ge=1, le=40)] = 20):
        """Query registered metric IDs; page_size is 1..40. Values are in rows[].fact.value.

        period_type filters the reading view without changing it. Current market rows use
        current, not instant; their actual observation date is fact.period_end, not row.period.
        Governance/capital/evidence rows also expose frozen EventRecords in rows[].event.
        Follow next_page with the same filters and page_size.
        """
        if topic not in TOPICS and topic not in {"peers", "evidence", "gaps"}:
            return {"status": "capability_gap", "supported_topics": [*TOPICS, "peers", "evidence", "gaps"]}
        validate_integer_parameter(page, "page", "invalid_pagination")
        validate_integer_parameter(page_size, "page_size", "invalid_pagination", 40)
        state, path, payload = self.pack(research_id)
        guidance = {}
        if topic == "peers":
            from .briefing import aligned_peers
            rows, _ = aligned_peers(payload["peers"])
        elif topic == "gaps":
            work = read_json(path / "next-work.json")["items"]
            grouped = {}
            for item in work:
                key = (item.get("stage"), item.get("dataset_id"), item.get("raw_name"), item.get("reason"))
                row = grouped.setdefault(key, {"stage": key[0], "dataset": key[1], "field": key[2], "reason": key[3],
                                               "questions": set(), "periods": set(), "dependency_count": 0})
                row["dependency_count"] += 1
                if item.get("question_id"): row["questions"].add(item["question_id"])
                if item.get("period"): row["periods"].add(item["period"])
            rows = [{**x, "questions": sorted(x["questions"]), "periods": sorted(x["periods"])} for x in grouped.values()]
        elif topic == "evidence" or topic in {"business", "governance", "capital", "industry"}:
            rows = [{k: i.get(k) for k in ("evidence_id", "group", "period", "locator", "excerpt", "source_url")}
                    for i in payload["evidence"] + payload.get("supplemental_evidence", []) if topic == "evidence" or i.get("group") in TOPICS[topic]]
            if topic in {"evidence", "governance", "capital"}:
                rows.extend(row for row in event_reading_rows(payload,state['ticker'],state['as_of'])
                            if topic == 'evidence' or row['group'] in TOPICS[topic])
        else:
            if period_type not in {"cumulative", "single_quarter", "instant", "current", "ttm", "ratio", "all"}:
                raise ResearchError("unknown_period_type:period_type must be cumulative, single_quarter, instant, current, ttm, ratio or all")
            topic_rows = [i for i in payload["metrics"] if i.get("group") in TOPICS[topic]]
            available_ids = sorted({i["metric_id"] for i in topic_rows})
            unknown = set(metric_ids or []) - {i["metric_id"] for i in payload["metrics"]}
            if unknown:
                return {"status": "capability_gap", "research_id": research_id, "snapshot_id": state["snapshot_id"],
                        "unknown_metric_ids": sorted(unknown), "available_metric_ids": available_ids,
                        "similar_metric_ids": {name: get_close_matches(name, available_ids, n=3, cutoff=.45)
                                               for name in sorted(unknown)},
                        "reason": "metric_id_not_registered; choose a registered ID before assessing a data gap",
                        "next_action": "list_materials",
                        "read_entry": {"tool": "list_materials", "research_id": research_id,
                                       "category": "valuation" if topic == "valuation" else "metrics"}}
            matched = [i for i in topic_rows if not metric_ids or i["metric_id"] in metric_ids]
            rows = [i for i in matched if period_type == "all" or i.get("period_type") == period_type]
            if matched and not rows:
                examples = []
                for item in matched:
                    fact_period = (item.get("fact") or {}).get("period_end")
                    if fact_period:
                        examples.append({"metric_id": item["metric_id"], "period_type": item["period_type"],
                                         "period": item["period"], "fact_period_end": fact_period})
                    if len(examples) == 3:
                        break
                guidance = {"query_hint": {"reason": "period_type_filter_has_no_matches",
                    "requested_period_type": period_type,
                    "available_period_types": sorted({i["period_type"] for i in matched}),
                    "next_action": "query_research",
                    "note": "查询未自动改写；选择可用period_type重试。current行情的period是读取视图日期，实际行情日期读取fact.period_end。",
                    "fact_date_examples": examples}}
        start = (page - 1) * page_size
        return {"research_id": research_id, "snapshot_id": state["snapshot_id"], "topic": topic,
                "question": question, "total": len(rows), "rows": rows[start:start+page_size],
                "next_page": page + 1 if start + page_size < len(rows) else None, **guidance}

    def read_evidence(self, research_id: str, evidence_id: str,
                      page: Annotated[int, Field(ge=1)] = 1,
                      max_tokens: Annotated[int, Field(ge=1, le=4000)] = 2000):
        """Read bounded evidence text; keep max_tokens unchanged while following next_page."""
        validate_integer_parameter(page, "page", "text_page_out_of_range")
        validate_integer_parameter(max_tokens, "max_tokens", "evidence_budget_out_of_range", 4000)
        state, path, payload = self.pack(research_id)
        item = next((x for x in payload.get("supplemental_evidence", []) if x["evidence_id"] == evidence_id), None)
        if item:
            from analysis.structured.research_lite import _split_utf8
            chunks = _split_utf8(item["content"], max_tokens * 2)
            if not 1 <= page <= len(chunks):
                raise ResearchError(f"text_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}")
            return {**{k:v for k,v in item.items() if k not in {"content", "selection"}},
                    "content":chunks[page-1], "page":page,"next_page":page+1 if page<len(chunks) else None,
                    "original_hash_verified":True,"research_id":research_id,"snapshot_id":state["snapshot_id"]}
        event = next((row for row in event_reading_rows(payload,state['ticker'],state['as_of'])
                      if row['event_id'] == evidence_id), None)
        if event:
            from analysis.structured.research_lite import _split_utf8
            chunks = _split_utf8(encode(event['event']), max_tokens * 2)
            if page > len(chunks):
                raise ResearchError(f"text_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}")
            proof = self.artifact(research_id,'evidence_read',{
                'event_id':evidence_id,'source_record':event['event'],'source_role':'structured_event_record',
                'source_ids':event['event']['source_ids'],'data_snapshot_id':event['event']['data_snapshot_id'],
                'content':chunks[page-1],'locator':'event:'+evidence_id,'source_url':None,
                'original_path':str(path/'core-pack.json'),'original_sha256':sha(path/'core-pack.json'),
                'original_hash_verified':True,'numeric_admission':False})
            return {k:v for k,v in proof.items() if k not in {'artifact_id','source_record'}} | {
                'evidence_id':proof['artifact_id'],'page':page,'next_page':page+1 if page<len(chunks) else None}
        result = read_evidence(pack_dir=path, evidence_id=evidence_id, page=page, max_tokens=max_tokens)
        if not result["original_hash_verified"]:
            raise ResearchError("original_evidence_integrity_failed")
        return {**result, "research_id": research_id, "snapshot_id": state["snapshot_id"]}

    def artifact(self, research_id: str, kind: str, payload: dict, *, expected_snapshot_id: str | None = None) -> dict:
        state, _, _ = self.pack(research_id)
        if expected_snapshot_id is not None and state["snapshot_id"] != expected_snapshot_id:
            raise ResearchError("snapshot_changed_during_artifact_write")
        value = {**payload, "research_id": research_id, "snapshot_id": state["snapshot_id"]}
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if kind in {"section", "conclusion"}:
                value["revision"] = con.execute(
                    "SELECT COUNT(*) FROM research_artifacts WHERE research_id=? AND kind=? AND snapshot_id=?",
                    (research_id, kind, state["snapshot_id"])).fetchone()[0] + 1
            live = con.execute("SELECT state_json FROM research_tasks WHERE id=?", (research_id,)).fetchone()
            if json.loads(live[0])["snapshot_id"] != state["snapshot_id"]:
                raise ResearchError("snapshot_changed_during_artifact_write")
            ident = kind + "_" + digest(value)[:24]
            con.execute("INSERT OR IGNORE INTO research_artifacts VALUES(?,?,?,?,?)",
                        (ident, research_id, kind, state["snapshot_id"], encode(value)))
        return {"artifact_id": ident, **value}

    def read_document_page(self, research_id: str, evidence_id: str,
                           document_page: Annotated[int, Field(ge=1)],
                           page: Annotated[int, Field(ge=1)] = 1,
                           max_tokens: Annotated[int, Field(ge=1, le=4000)] = 2000):
        """Read an adjacent physical PDF page from a verified evidence source, with bounded continuation."""
        import fitz
        from analysis.structured.research_lite import _split_utf8
        validate_integer_parameter(document_page, "document_page", "invalid_document_page_or_budget")
        validate_integer_parameter(page, "page", "invalid_document_page_or_budget")
        validate_integer_parameter(max_tokens, "max_tokens", "invalid_document_page_or_budget", 4000)
        source = self.read_evidence(research_id, evidence_id, max_tokens=2000)
        with fitz.open(source["original_path"]) as document:
            if document_page > len(document):
                raise ResearchError(f"document_page_out_of_range:document_page must be 1..{len(document)}; received {document_page}")
            body = document[document_page-1].get_text()
        if not body.strip():
            return {"status": "processing_gap", "reason": "page_requires_ocr", "document_page": document_page}
        chunks = _split_utf8(body, max_tokens * 2)
        if page > len(chunks):
            raise ResearchError(f"text_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}")
        proof = self.artifact(research_id, "evidence_read", {
            "parent_evidence_id": evidence_id, "locator": f"page:{document_page}",
            "source_url": source["source_url"], "original_path": source["original_path"],
            "original_sha256": source["original_sha256"], "original_hash_verified": True,
            "content": body, "status": "source_text_available"})
        return {k: v for k,v in proof.items() if k != "content"} | {
            "content": chunks[page-1], "page": page, "next_page": page+1 if page < len(chunks) else None}

    def artifacts(self, research_id: str, kind: str) -> list[dict]:
        state, _, _ = self.pack(research_id)
        with self.connect() as con:
            rows = con.execute("SELECT id,payload FROM research_artifacts WHERE research_id=? AND kind=? AND snapshot_id=? ORDER BY rowid",
                               (research_id, kind, state["snapshot_id"])).fetchall()
        return [{"artifact_id": x["id"], **json.loads(x["payload"])} for x in rows]

    def save_business_profile(self, research_id: str, profile: dict) -> dict:
        """Bind a verified business profile to the current research snapshot.

        The profile is a research artifact, rather than a second report entry point.
        Its source manifest and immutable fact identifiers remain in the payload so
        the reporting bridge can cite it as evidence when the host explicitly does so.
        """
        from .business_profile import BusinessProfiles
        state, _, _ = self.pack(research_id)
        verified = BusinessProfiles(self).validate_for_save(research_id, profile)
        return self.artifact(research_id, "business_profile", verified, expected_snapshot_id=state["snapshot_id"])

    def business_profiles(self, research_id: str) -> list[dict]:
        """Return profiles bound to the current snapshot in insertion order."""
        from .business_profile import BusinessProfiles
        profiles = BusinessProfiles(self)
        return [profiles.validate_saved(research_id, value)
                for value in self.artifacts(research_id, "business_profile")]
