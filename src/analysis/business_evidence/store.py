"""Append-only assertion storage. Different values remain conflicts until corrected."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import re
import sqlite3

from .models import ReviewBatch, canonical, compact, digest


SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE facts (id TEXT PRIMARY KEY, fact_key TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX fact_key_index ON facts(fact_key);
CREATE TABLE citations (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE fact_citations (id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id),
 citation_id TEXT NOT NULL REFERENCES citations(id), payload TEXT NOT NULL);
CREATE TABLE questions (id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id),
 question_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE aliases (id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id), payload TEXT NOT NULL);
CREATE TABLE corrections (id TEXT PRIMARY KEY, previous_id TEXT NOT NULL UNIQUE REFERENCES facts(id),
 replacement_id TEXT NOT NULL REFERENCES facts(id), citation_id TEXT NOT NULL REFERENCES citations(id), payload TEXT NOT NULL);
"""
TABLES = ("metadata", "batches", "facts", "citations", "fact_citations", "questions", "aliases", "corrections")


def aware(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("as_of_requires_timezone")
    return result.astimezone(timezone.utc)


def _put(db, table, identifier, payload, **columns):
    encoded = canonical(payload)
    old = db.execute(f"SELECT payload FROM {table} WHERE id=?", (identifier,)).fetchone()
    if old:
        if old[0] != encoded:
            raise ValueError(f"immutable_{table}_collision:{identifier}")
        return False
    fields = ["id", "payload", *columns]
    db.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
               (identifier, encoded, *columns.values()))
    return True


class FactStore:
    def __init__(self, path: Path, namespace_id: str, *, create: bool = False):
        self.path = Path(path).resolve()
        if not self.path.exists() and not create:
            raise ValueError("fact_store_missing")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                triggers = "\n".join(f"CREATE TRIGGER immutable_{t}_{op.lower()} BEFORE {op} ON {t} "
                    "BEGIN SELECT RAISE(ABORT, 'business_evidence_is_append_only'); END;"
                    for t in TABLES for op in ("UPDATE", "DELETE"))
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + triggers + "\nPRAGMA user_version=1;")
                db.execute("INSERT INTO metadata VALUES ('namespace_id', ?)", (namespace_id,))
                db.commit()
            elif version != 1:
                raise ValueError("unsupported_fact_store_schema")
            row = db.execute("SELECT value FROM metadata WHERE key='namespace_id'").fetchone()
            if not row or row[0] != namespace_id:
                raise ValueError("fact_store_namespace_mismatch")

    def import_batch(self, batch: ReviewBatch, corpus) -> dict:
        corpus.validate()
        names = [r.record_id for r in batch.records]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_review_record_id")
        prepared = []
        for record in batch.records:
            fact = record.fact()
            citations = [corpus.citation(c, record.company_id) for c in record.citations]
            if record.value_type == "decimal":
                for citation in citations:
                    numbers = re.findall(r"-?\d[\d,]*(?:\.\d+)?", citation["quote"])
                    if Decimal(fact["value"]) not in {Decimal(n.replace(",", "")) for n in numbers}:
                        raise ValueError(f"numeric_value_missing_from_quote:{record.record_id}")
            elif any(compact(record.value) not in compact(c["quote"]) for c in citations):
                raise ValueError(f"text_value_missing_from_quote:{record.record_id}")
            prepared.append((record, fact, citations))
        correction_inputs = []
        company_by_alias = {r.record_id: r.company_id for r in batch.records}
        for correction in batch.corrections:
            company_id = company_by_alias.get(correction.replacement_record_id)
            if company_id is None:
                raise ValueError("correction_replacement_must_be_in_batch")
            citation = corpus.citation(correction.citation, company_id)
            if not re.search(r"更正|修正|修订|correct|restat", citation["title"] + citation["quote"], re.I):
                raise ValueError("correction_requires_explicit_notice")
            correction_inputs.append((correction, citation))
        payload = {"review": batch.model_dump(mode="json"), "manifest_id": corpus.manifest.manifest_id,
                   "manifest_hash": corpus.manifest.manifest_hash, "schema_version": "1.0.0",
                   "consumer_selection_policy": corpus.selection_policy}
        batch_id = "batch-" + digest(payload)
        stats = {"batch_id": batch_id, "input_records": len(prepared), "new_facts": 0,
                 "new_citations": 0, "new_corrections": 0, "human_review": False}
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for record, fact, citations in prepared:
                fact_id = fact["fact_id"]
                stats["new_facts"] += _put(db, "facts", fact_id, fact, fact_key=fact["fact_key"])
                _put(db, "aliases", record.record_id, {"fact_id": fact_id}, fact_id=fact_id)
                for question in record.question_ids:
                    link = {"fact_id": fact_id, "question_id": question}
                    _put(db, "questions", digest(link), link, **link)
                for citation in citations:
                    cid = "citation-" + digest(citation)
                    stats["new_citations"] += _put(db, "citations", cid, citation)
                    link = {"fact_id": fact_id, "citation_id": cid}
                    _put(db, "fact_citations", digest(link), link, **link)
            for correction, citation in correction_inputs:
                def resolve(alias):
                    row = db.execute("SELECT fact_id FROM aliases WHERE id=?", (alias,)).fetchone()
                    if not row:
                        raise ValueError("correction_target_missing")
                    return json.loads(db.execute("SELECT payload FROM facts WHERE id=?", (row[0],)).fetchone()[0])
                old = resolve(correction.previous_record_id)
                new = resolve(correction.replacement_record_id)
                if old["fact_key"] != new["fact_key"] or old["fact_id"] == new["fact_id"]:
                    raise ValueError("correction_identity_or_version_mismatch")
                old_times = [aware(json.loads(r[0])["available_at"]) for r in db.execute(
                    "SELECT c.payload FROM citations c JOIN fact_citations fc ON fc.citation_id=c.id WHERE fc.fact_id=?",
                    (old["fact_id"],))]
                if aware(citation["available_at"]) < min(old_times):
                    raise ValueError("correction_predates_original_evidence")
                # The correction must support the replacement, not just mention a correction elsewhere.
                cid = "citation-" + digest(citation)
                if not db.execute("SELECT 1 FROM fact_citations WHERE fact_id=? AND citation_id=?", (new["fact_id"], cid)).fetchone():
                    raise ValueError("correction_not_cited_by_replacement")
                edges = {r[0]: r[1] for r in db.execute("SELECT previous_id,replacement_id FROM corrections")}
                if old["fact_id"] in edges and edges[old["fact_id"]] != new["fact_id"]:
                    raise ValueError("correction_branch_conflict")
                cursor = new["fact_id"]
                while cursor in edges:
                    if cursor == old["fact_id"]:
                        raise ValueError("correction_cycle")
                    cursor = edges[cursor]
                if cursor == old["fact_id"]:
                    raise ValueError("correction_cycle")
                event = {"previous_id": old["fact_id"], "replacement_id": new["fact_id"],
                         "citation_id": cid, "available_at": citation["available_at"], "reason": correction.reason}
                stats["new_corrections"] += _put(db, "corrections", "correction-" + digest(event), event,
                    previous_id=old["fact_id"], replacement_id=new["fact_id"], citation_id=cid)
            _put(db, "batches", batch_id, payload)
        stats["reused_facts"] = stats["input_records"] - stats["new_facts"]
        return stats

    def query(self, *, as_of: str, company_id: str | None = None, question_id: str | None = None) -> dict:
        cutoff = aware(as_of)
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            facts = {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,payload FROM facts ORDER BY id")}
            citations = {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,payload FROM citations ORDER BY id")}
            topics = {}
            for fid, qid in db.execute("SELECT fact_id,question_id FROM questions ORDER BY question_id"):
                topics.setdefault(fid, []).append(qid)
            links = {}
            for fid, cid in db.execute("SELECT fact_id,citation_id FROM fact_citations ORDER BY citation_id"):
                if aware(citations[cid]["available_at"]) <= cutoff:
                    links.setdefault(fid, []).append({"citation_id": cid, **citations[cid]})
            corrections = [json.loads(r[0]) for r in db.execute("SELECT payload FROM corrections ORDER BY id")
                           if aware(json.loads(r[0])["available_at"]) <= cutoff]
        superseded = {e["previous_id"] for e in corrections}
        by_key = {}
        for fid, fact in facts.items():
            if fid in links and fid not in superseded:
                by_key.setdefault(fact["fact_key"], []).append(fid)
        rows = []
        for fid, fact in facts.items():
            if fid not in links or (company_id and fact["company_id"] != company_id):
                continue
            if question_id and question_id not in topics.get(fid, []):
                continue
            status = "superseded" if fid in superseded else "conflict" if len(by_key.get(fact["fact_key"], [])) > 1 else "current"
            rows.append({**fact, "status": status, "question_ids": topics.get(fid, []),
                         "citations": links[fid], "available_at": min((c["available_at"] for c in links[fid]), key=aware),
                         "corrections": [e for e in corrections if fid in (e["previous_id"], e["replacement_id"])]})
        rows.sort(key=lambda f: (f["company_id"], f["period"], f["metric"], f["fact_id"]))
        return {"schema_version": "1.0.0", "as_of": cutoff.isoformat(), "human_review": False,
                "facts": rows, "counts": {s: sum(f["status"] == s for f in rows) for s in ("current", "superseded", "conflict")}}
