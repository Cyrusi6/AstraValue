from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from analysis.structured.identity import CompanyResolver, SecurityIdentity
from analysis.structured.research_lite import build_lite_pack, read_evidence
from analysis.structured.scope import load_research_profile

ROOT = Path(__file__).resolve().parents[3]
POLICY = "模型自主研读、提出假设、估值、评级和写作；代码执行计算。事实、假设与判断分开，缺口明确说明影响；知识按需读取。旧输入包的禁止评级提示不适用于本研究。"
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

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _save(self, state: dict, expected_revision: int):
        with self.connect() as con:
            changed = con.execute(
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
        for name in ("core-pack.md", "core-pack.json", "core-coverage.json", "evidence-index.jsonl", "next-work.json"):
            if manifest.get("output_hashes", {}).get(name) != sha(directory / name):
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
                roots = [self.path(p) for p in self.config["projection_roots"]]
                existing = [p for p in roots if (p / identity.security_code).is_dir()]
                if not existing:
                    current.update(status="acquisition_required", reason="registered_projection_missing")
                    self._save(current, revision)
                    return self.get_task(current["research_id"])
                supplements = existing[1:] + [self.path(p) / cutoff.isoformat() for p in self.config["evidence_roots"]]
                result = build_lite_pack(input_root=existing[0], ticker=identity.security_code, as_of=cutoff,
                                         output_root=self.state / "packs", supplements=supplements,
                                         profile_id=self.config["profile_id"])
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

    def query_research(self, research_id: str, topic: str = "financials", period_type: str = "cumulative",
                       metric_ids: list[str] | None = None, question: str = "", page: int = 1, page_size: int = 20):
        if topic not in TOPICS and topic not in {"peers", "evidence", "gaps"}:
            return {"status": "capability_gap", "supported_topics": [*TOPICS, "peers", "evidence", "gaps"]}
        if page < 1 or not 1 <= page_size <= 40:
            raise ResearchError("invalid_pagination")
        state, path, payload = self.pack(research_id)
        if topic == "peers":
            rows = payload["peers"]
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
        else:
            if period_type not in {"cumulative", "single_quarter", "instant", "current", "ttm", "ratio", "all"}:
                raise ResearchError("unknown_period_type")
            unknown = set(metric_ids or []) - {i["metric_id"] for i in payload["metrics"]}
            if unknown:
                return {"status": "capability_gap", "research_id": research_id, "snapshot_id": state["snapshot_id"],
                        "unknown_metric_ids": sorted(unknown), "next_action": "request_materials"}
            rows = [i for i in payload["metrics"] if i.get("group") in TOPICS[topic]
                    and (period_type == "all" or i.get("period_type") == period_type)
                    and (not metric_ids or i["metric_id"] in metric_ids)]
        start = (page - 1) * page_size
        return {"research_id": research_id, "snapshot_id": state["snapshot_id"], "topic": topic,
                "question": question, "total": len(rows), "rows": rows[start:start+page_size],
                "next_page": page + 1 if start + page_size < len(rows) else None}

    def read_evidence(self, research_id: str, evidence_id: str, page: int = 1, max_tokens: int = 2000):
        if not 1 <= max_tokens <= 4000:
            raise ResearchError("evidence_budget_out_of_range")
        state, path, payload = self.pack(research_id)
        item = next((x for x in payload.get("supplemental_evidence", []) if x["evidence_id"] == evidence_id), None)
        if item:
            from analysis.structured.research_lite import _split_utf8
            chunks = _split_utf8(item["content"], max_tokens * 2)
            if not 1 <= page <= len(chunks):
                raise ResearchError("text_page_out_of_range")
            return {**{k:v for k,v in item.items() if k not in {"content", "selection"}},
                    "content":chunks[page-1], "page":page,"next_page":page+1 if page<len(chunks) else None,
                    "original_hash_verified":True,"research_id":research_id,"snapshot_id":state["snapshot_id"]}
        result = read_evidence(pack_dir=path, evidence_id=evidence_id, page=page, max_tokens=max_tokens)
        if not result["original_hash_verified"]:
            raise ResearchError("original_evidence_integrity_failed")
        return {**result, "research_id": research_id, "snapshot_id": state["snapshot_id"]}

    def artifact(self, research_id: str, kind: str, payload: dict) -> dict:
        state, _, _ = self.pack(research_id)
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

    def read_document_page(self, research_id: str, evidence_id: str, document_page: int,
                           page: int = 1, max_tokens: int = 2000):
        """Read an adjacent physical PDF page from a verified evidence source, with bounded continuation."""
        import fitz
        from analysis.structured.research_lite import _split_utf8
        if not 1 <= max_tokens <= 4000 or document_page < 1 or page < 1:
            raise ResearchError("invalid_document_page_or_budget")
        source = self.read_evidence(research_id, evidence_id, max_tokens=2000)
        with fitz.open(source["original_path"]) as document:
            if document_page > len(document):
                raise ResearchError("document_page_out_of_range")
            body = document[document_page-1].get_text()
        if not body.strip():
            return {"status": "processing_gap", "reason": "page_requires_ocr", "document_page": document_page}
        chunks = _split_utf8(body, max_tokens * 2)
        if page > len(chunks):
            raise ResearchError("text_page_out_of_range")
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
