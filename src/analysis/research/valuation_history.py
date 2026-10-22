"""Explicit cached valuation projection; dated market ratios are not forecasts or PIT backtests."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis.acquisition.bootstrap import ROOT_MARKER_NAME
from analysis.acquisition.repository import AcquisitionRepository
from analysis.structured.consumption import is_fact_consumable
from analysis.structured.materialization import StructuredFactMaterializer
from analysis.structured.research_lite import _write_json as write_json
from analysis.structured.storage import StructuredStorage
from .workspace import ResearchError, ResearchWorkspace, digest, read_json, sha

VERSION = "valuation-history-v2"
METRICS = {"eastmoney_pe_ttm": "ratio", "eastmoney_pb_mrq": "ratio", "eastmoney_ps_ttm": "ratio",
           "market_price": "CNY_per_share", "market_cap": "CNY", "total_shares": "shares"}
RATIO_METRICS = ("eastmoney_pe_ttm", "eastmoney_pb_mrq", "eastmoney_ps_ttm")
COMPARISON_METRICS = ("eastmoney_pe_ttm", "eastmoney_pb_mrq", "market_price")


def readonly(db: Path):
    connection = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def quantile(values: list[Decimal], q: Decimal) -> Decimal:
    """Type-7 linear quantile, with equal weight per observed trading date."""
    ordered = sorted(values)
    if not ordered:
        raise ResearchError("empty_valuation_series")
    position = (len(ordered) - 1) * q
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def summarize(series: dict, target: Decimal) -> dict:
    values = [Decimal(row["value"]) for row in series.values()]
    positive = [v for v in values if v.is_finite() and v > 0]
    if not positive:
        return {"status": "no_positive_pe", "observations": len(values)}
    rank = (sum(v < target for v in positive) + Decimal(sum(v == target for v in positive)) / 2) / len(positive)
    return {"status": "ready", "observations": len(values), "positive_pe_observations": len(positive),
        "excluded_nonpositive_pe": len(values) - len(positive), "first_date": min(series), "last_date": max(series),
        "p10": str(quantile(positive, Decimal('.1'))), "median": str(quantile(positive, Decimal('.5'))),
        "p90": str(quantile(positive, Decimal('.9'))), "reference_multiple": str(target),
        "reference_midrank_percent": str(rank * 100),
        "method": "positive PE only; type-7 quantiles; midrank=(below+0.5*equal)/N; equal date weights"}


def distribution(series: dict, metric_id: str) -> dict:
    """Describe positive multiples and rank the latest observed multiple, not a fair-value assumption."""
    values = {d: Decimal(row["value"]) for d, row in series.items()}
    valid = {d: v for d, v in values.items() if v.is_finite() and v > 0}
    counts = {"observations": len(values), "valid_observations": len(valid),
              "excluded_nonpositive": sum(v.is_finite() and v <= 0 for v in values.values()),
              "excluded_nonfinite": sum(not v.is_finite() for v in values.values())}
    if not valid:
        return {"status": "no_positive_multiple", "metric_id": metric_id, **counts}
    ordered = list(valid.values())
    latest_date = max(values)
    latest = values[latest_date]
    eligible = latest.is_finite() and latest > 0
    rank = ((sum(v < latest for v in ordered) + Decimal(sum(v == latest for v in ordered)) / 2)
            / len(ordered) * 100) if eligible else None
    return {"status": "ready", "metric_id": metric_id, **counts,
            "first_date": min(valid), "last_date": max(valid), "latest_date": latest_date,
            "latest_value": str(latest), "latest_fact_ids": series[latest_date].get("fact_ids", []),
            "minimum": str(min(ordered)), "p10": str(quantile(ordered, Decimal('.1'))),
            "median": str(quantile(ordered, Decimal('.5'))), "p90": str(quantile(ordered, Decimal('.9'))),
            "maximum": str(max(ordered)), "current_midrank_percent": str(rank) if rank is not None else None,
            "rank_status": "ready" if eligible else "latest_multiple_not_positive",
            "method": "positive finite multiples; type-7 quantiles; midrank=(below+0.5*equal)/N; equal observed date weights"}


def compare(series: dict, target: Decimal, *, window_end: str | None = None) -> dict:
    """Intersect complete dates; never forward-fill peers or select each company's latest date."""
    dates = []
    for metrics in series.values():
        complete = set.intersection(*(set(metrics.get(m, {})) for m in COMPARISON_METRICS))
        complete = {d for d in complete if all(Decimal(metrics[m][d]["value"]).is_finite()
                    and Decimal(metrics[m][d]["value"]) > 0 for m in COMPARISON_METRICS)}
        dates.append(complete)
    common = set.intersection(*dates) if dates else set()
    day = max(common) if common else None
    sensitivity, metric_sensitivity = {}, {}
    if day:
        end = date.fromisoformat(window_end or day)
        for years in (1, 3):
            try:
                start = end.replace(year=end.year-years).isoformat()
            except ValueError:
                start = end.replace(year=end.year-years, day=28).isoformat()
            sensitivity[str(years) + "y"] = {ticker: summarize(
                {d: row for d, row in values.get("eastmoney_pe_ttm", {}).items() if start <= d <= end.isoformat()}, target)
                for ticker, values in series.items()}
            metric_sensitivity[str(years) + "y"] = {ticker: {metric: distribution(
                {d: row for d, row in values.get(metric, {}).items() if start <= d <= end.isoformat()}, metric)
                for metric in RATIO_METRICS} for ticker, values in series.items()}
    return {"status": "ready" if day else "no_common_complete_date", "comparison_date": day,
        "rows": {ticker: {m: dates[day] for m, dates in values.items() if day in dates}
                 for ticker, values in series.items()} if day else {},
        "history": {ticker: summarize(values.get("eastmoney_pe_ttm", {}), target) for ticker, values in series.items()},
        "window_sensitivity": sensitivity,
        "metric_history": {ticker: {metric: distribution(values.get(metric, {}), metric)
            for metric in RATIO_METRICS} for ticker, values in series.items()},
        "metric_window_sensitivity": metric_sensitivity,
        "window_end": window_end or day,
        "reference_multiple_role": "comparison_reference_only"}


def admit(result, repository, data_root: Path, verified: dict) -> tuple[dict, list]:
    selected = frozenset(result.selected_fact_ids)
    rows = {m: {} for m in METRICS}
    facts = []
    for fact in result.facts:
        if fact.metric_id not in METRICS or not is_fact_consumable(fact, materialization_selected_ids=selected):
            continue
        if fact.unit != METRICS[fact.metric_id] or fact.period_type != "market_quote" or fact.scope != "consolidated":
            raise ResearchError("valuation_semantics_mismatch")
        meta = fact.metadata
        value = Decimal(str(meta["original_value"])) * Decimal(meta["multiplier"])
        if not value.is_finite() or value != Decimal(meta["decimal_value"]) or float(value) != fact.value:
            raise ResearchError("valuation_independent_numeric_mismatch")
        snapshot = repository.get_raw_resource_snapshot(meta["structured_snapshot_id"])
        path = (data_root / snapshot.archive_relative_path).resolve()
        if not path.is_relative_to(data_root):
            raise ResearchError("snapshot_path_escape")
        if str(path) not in verified:
            verified[str(path)] = sha(path)
        if verified[str(path)] != snapshot.sha256:
            raise ResearchError("snapshot_bytes_mismatch")
        rows[fact.metric_id][fact.period_end.isoformat()] = {"value": str(value), "unit": fact.unit,
            "fact_ids": [fact.fact_id], "definition": meta["interpretation_definition_id"]}
        facts.append(fact.model_dump(mode="json"))
    return rows, facts


def merge_series(target: dict, incoming: dict):
    for metric, dates in incoming.items():
        for day, row in dates.items():
            old = target.setdefault(metric, {}).get(day)
            if old:
                if any(old[k] != row[k] for k in ("value", "unit", "definition")):
                    raise ResearchError("conflicting_valuation_facts:" + metric + ":" + day)
                old["fact_ids"] = sorted(set(old["fact_ids"] + row["fact_ids"]))
            else:
                target[metric][day] = row


class ValuationHistory:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def query_valuation(self, research_id: str, start_date: str, end_date: str,
                        peers: list[str] | None = None, reference_multiple: str = "20") -> dict:
        """Materialize cached history for an explicit window; return PE distribution and same-day peers.

        Reference multiple is a model assumption, not a justified fair multiple. No network or pack mutation.
        """
        state, _, _ = self.w.pack(research_id)
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end or end.isoformat() > state["as_of"]:
            raise ResearchError("invalid_valuation_window")
        try:
            target = Decimal(reference_multiple)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ResearchError("invalid_reference_multiple") from exc
        if not target.is_finite() or target <= 0:
            raise ResearchError("invalid_reference_multiple")
        if len(peers or []) > 6:
            raise ResearchError("too_many_peers:max_6")
        tickers = {state["ticker"]}
        for company in peers or []:
            resolved = self.w.resolve(company, end)
            if resolved.identity is None:
                raise ResearchError("peer_identity_unresolved:" + company)
            tickers.add(resolved.identity.security_code)
        caches = self.w.config.get("valuation_caches", [])
        if not caches:
            return {"status": "processing_gap", "reason": "valuation_cache_not_registered"}
        series = {ticker: {m: {} for m in METRICS} for ticker in sorted(tickers)}
        evidence, manifests, proofs, verified, sources = [], [], [], {}, {}
        cutoff = datetime.combine(end, time.max, tzinfo=ZoneInfo("Asia/Shanghai"))
        for cache in caches:
            db, root = self.w.path(cache["db"]), self.w.path(cache["data_root"])
            if not db.exists() or not root.exists():
                raise ResearchError("registered_valuation_cache_missing")
            with readonly(db) as connection:
                namespaces = connection.execute("SELECT * FROM storage_namespaces").fetchall()
                if len(namespaces) != 1:
                    raise ResearchError("ambiguous_cache_namespace")
                ns = dict(namespaces[0])
                marker = read_json(root / ROOT_MARKER_NAME)
                for key, path in (("database_identity_hash", db), ("data_root_identity_hash", root)):
                    expected = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()
                    if ns[key] != expected or marker.get(key) != expected:
                        raise ResearchError("cache_binding_mismatch")
                if (marker.get("state") != "bound" or marker.get("namespace_id") != ns["namespace_id"]
                        or marker.get("binding_nonce") != ns["binding_nonce"]):
                    raise ResearchError("cache_binding_mismatch")
                paths = [p for p in (db, db.with_name(db.name + "-wal"), root / ROOT_MARKER_NAME) if p.exists()]
                before = {str(p): sha(p) for p in paths}
                attempts = connection.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0]
                runs = connection.execute("SELECT run_id,ticker FROM structured_run_contexts ORDER BY ticker,run_id").fetchall()
                storage = StructuredStorage(db, ns["namespace_id"], initialize=False)
                repository = AcquisitionRepository(db, initialize=False)
                for run in runs:
                    if run["ticker"] not in tickers:
                        continue
                    result = StructuredFactMaterializer(storage, repository).materialize(run["run_id"],
                        as_of=cutoff, research_scope=True, valuation_window=(start.isoformat(), end.isoformat()),
                        interpretation_contract="eastmoney-financial-interpretation-v1.0.0")
                    rows, facts = admit(result, repository, root, verified)
                    merge_series(series[run["ticker"]], rows)
                    evidence.extend(facts)
                    sources.update({s.source_id: s.model_dump(mode="json") for s in result.sources})
                    manifests.append({"ticker": run["ticker"], "run_id": run["run_id"],
                        "namespace_id": ns["namespace_id"], "contract_hash": result.contract_hash,
                        "materialization_hash": result.materialization_hash,
                        "selected_fact_ids": list(result.selected_fact_ids),
                        "gap_counts": dict(Counter(x["reason"] for x in result.field_gaps))})
                after_paths = [p for p in (db, db.with_name(db.name + "-wal"), root / ROOT_MARKER_NAME) if p.exists()]
                if before != {str(p): sha(p) for p in after_paths}:
                    raise ResearchError("source_database_changed_during_readonly_run")
                if connection.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] != attempts:
                    raise ResearchError("acquisition_attempts_changed")
                proofs.append({"files_unchanged": before, "acquisition_attempts": attempts, "new_requests": 0})
        if any(sha(Path(path)) != value for path, value in verified.items()):
            raise ResearchError("source_snapshot_changed")
        payload = {"version": VERSION, "start_date": start_date, "end_date": end_date,
            "series": series, "facts": evidence, "sources": [sources[k] for k in sorted(sources)],
            "manifests": manifests, "proofs": proofs,
            "verified_source_hashes": verified, "summary": compare(series, target, window_end=end_date),
            "boundary": "current cached vendor history; not proven point-in-time history; PE TTM is not forecast PE"}
        ident = digest(payload)
        path = self.w.state / "valuation-history" / (ident + ".json")
        write_json(path, payload)
        return self.w.artifact(research_id, "valuation_history", {**payload["summary"],
            "start_date": start_date, "end_date": end_date, "version": VERSION,
            "payload_path": str(path), "payload_sha256": sha(path), "supplemental_projection": True,
            "original_pack_unchanged": True, "boundary": payload["boundary"]})
