"""八步轻量核心研究包：只读消费既有投影和原文索引。"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .scope import LITE_PROFILE_ID, ROOT, load_research_profile, load_scope
from .storage import canonical_json, canonical_sha256


PACK_VERSION = "eight-step-lite-pack-v1.0.6"
FORMULA_VERSION = "eight-step-lite-formulas-v1.0.0"
STOCK_METRICS = {
    "cash",
    "total_assets",
    "total_liabilities",
    "accounts_receivable",
    "inventory",
    "contract_assets",
    "contract_liabilities",
    "goodwill",
}
RATIO_DEPENDENCIES = {
    "gross_margin": ("operating_cost", "operating_income", "one_minus_ratio"),
    "parent_net_margin": ("parent_net_profit", "operating_income", "ratio"),
    "selling_expense_ratio": ("selling_expense", "operating_income", "ratio"),
    "administrative_expense_ratio": (
        "administrative_expense",
        "operating_income",
        "ratio",
    ),
    "rd_ratio": ("research_expense", "operating_income", "ratio"),
    "finance_expense_ratio": ("finance_expense", "operating_income", "ratio"),
    "cash_profit_ratio": ("operating_cash_flow", "net_profit", "ratio"),
    "operating_cash_flow_less_asset_purchase_proxy": (
        "operating_cash_flow",
        "long_asset_cash_purchase",
        "difference",
    ),
}
STAGE_ORDER = {
    "acquisition": 0,
    "parsing": 1,
    "semantic_processing": 2,
    "document_reading": 3,
    "deterministic_calculation": 4,
    "research_context": 5,
}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, canonical_json(value) + "\n")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    _write_text(path, "".join(canonical_json(value) + "\n" for value in values))


def materialize_cache(
    db: Path,
    data_root: Path,
    output: Path,
    as_of: date,
    tickers: Sequence[str] | None = None,
    recompute: bool = False,
    research_profile_id: str | None = None,
) -> dict[str, Any]:
    """Materialize structured runs into the lite projection used by research.

    This is deliberately a small, read-only orchestration helper.  The old
    research orchestration mixed materialization with broad report
    downloading and ad-hoc coverage generation; those concerns now belong to
    the acquisition runtime and the question-scoped lite pack respectively.
    """
    import sqlite3

    from .materialization import StructuredFactMaterializer
    from .materialization_replay import _export_result, _hash_file, PROJECTION_SCHEMA_VERSION, PROJECTION_FILES, RESEARCH_FILES
    from .research_projection import build_research_projection, RESEARCH_PROJECTION_VERSION
    from .interpretation import load_interpretation
    from .storage import StructuredStorage
    from analysis.acquisition.repository import AcquisitionRepository

    db = Path(db).resolve()
    data_root = Path(data_root).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_relative_to(data_root) or db.is_relative_to(output):
        raise ValueError("projection output must be separate from the source cache")
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        row = connection.execute(
            "SELECT namespace_id FROM storage_namespaces ORDER BY namespace_id LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("structured_storage_namespace_missing")
        namespace = row[0]
        runs = connection.execute(
            "SELECT run_id,ticker FROM structured_run_contexts ORDER BY ticker,run_id"
        ).fetchall()
        source_db_sha256 = _hash_file(db)
        wal = db.with_name(db.name + "-wal")
        source_wal_sha256 = _hash_file(wal) if wal.is_file() else None
        effective_as_of = datetime.combine(as_of, time.max, tzinfo=timezone(timedelta(hours=8)))
        storage = StructuredStorage(db, namespace, initialize=False)
        repository = AcquisitionRepository(db, initialize=False)
        results: list[dict[str, Any]] = []
        seen_tickers: set[str] = set()
        for run in runs:
            ticker = str(run["ticker"])
            if tickers and ticker not in set(tickers):
                continue
            folder = output / ticker
            if ticker in seen_tickers:
                folder = output / ticker / "runs" / str(run["run_id"])
            seen_tickers.add(ticker)
            stamp = folder / "cache-binding.json"
            context = storage.get_run_context(str(run["run_id"]))
            profile_id = research_profile_id or context.frozen_config.get("research_profile_id")
            interpretation = load_interpretation("eastmoney-financial-interpretation-v1.0.0", context)
            identity = {
                "source_db_sha256": source_db_sha256,
                "source_wal_sha256": source_wal_sha256,
                "run_id": str(run["run_id"]),
                "scope_hash": load_scope()["content_sha256"],
                "research_profile_id": profile_id,
                "profile_sha256": load_research_profile(profile_id)["content_sha256"] if profile_id else None,
                "interpretation_sha256": interpretation["content_sha256"],
                "as_of": effective_as_of.isoformat(),
                "projection_schema_version": PROJECTION_SCHEMA_VERSION,
                "research_projection_version": RESEARCH_PROJECTION_VERSION,
            }
            reusable = (
                not recompute
                and stamp.exists()
                and json.loads(stamp.read_text(encoding="utf-8")) == identity
                and (folder / "manifest.json").exists()
            )
            if reusable:
                cached = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                declared = cached.get("exports") or {}
                reusable = (cached.get("research_projection_version") == RESEARCH_PROJECTION_VERSION
                    and all(name in declared and (folder / name).is_file()
                            and _hash_file(folder / name) == declared[name].get("sha256")
                            for name in (*PROJECTION_FILES, *RESEARCH_FILES)))
            if not reusable:
                result = StructuredFactMaterializer(storage, repository).materialize(
                    str(run["run_id"]),
                    interpretation_contract="eastmoney-financial-interpretation-v1.0.0",
                    research_scope=True,
                    research_profile_id=profile_id,
                    as_of=effective_as_of,
                )
                research = build_research_projection(storage, repository, str(run["run_id"]),
                    as_of=effective_as_of, research_profile_id=profile_id)
                _export_result(result, folder, namespace, research_projection=research)
                _write_json(stamp, identity)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            results.append(
                {
                    "ticker": ticker,
                    "run_id": str(run["run_id"]),
                    "materialization_hash": manifest.get("materialization_hash"),
                    "reused": reusable,
                    "manifest": str((folder / "manifest.json").resolve()),
                }
            )
        if _hash_file(db) != source_db_sha256:
            raise ValueError("source_database_changed_during_readonly_run")
    finally:
        connection.close()
    summary = {
        "scope": research_profile_id or load_scope()["scope_id"],
        "source_database_unchanged": True,
        "source_db_sha256": source_db_sha256,
        "companies": results,
        "cache_replay": True,
        "current_network": False,
        "manual_acceptance": "pending",
        "as_of": as_of.isoformat(),
    }
    _write_json(output / "cache-summary.json", summary)
    return summary


def _quarter_end(serial: int) -> date:
    year, quarter = divmod(serial, 4)
    month_day = ((3, 31), (6, 30), (9, 30), (12, 31))[quarter]
    return date(year, *month_day)


def lite_periods(as_of: date, profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or load_research_profile(LITE_PROFILE_ID)
    annual_year = as_of.year - (1 if as_of >= date(as_of.year, 4, 30) else 2)
    annual_count = int(profile["windows"]["complete_annual_years"])
    annual = [
        date(year, 12, 31).isoformat()
        for year in range(annual_year - annual_count + 1, annual_year + 1)
    ]
    if as_of >= date(as_of.year, 10, 31):
        latest = date(as_of.year, 9, 30)
    elif as_of >= date(as_of.year, 8, 31):
        latest = date(as_of.year, 6, 30)
    elif as_of >= date(as_of.year, 4, 30):
        latest = date(as_of.year, 3, 31)
    else:
        latest = date(as_of.year - 1, 9, 30)
    latest_serial = latest.year * 4 + (latest.month // 3 - 1)
    quarter_count = int(profile["windows"]["required_quarters"])
    quarters = [
        _quarter_end(serial).isoformat()
        for serial in range(latest_serial - quarter_count + 1, latest_serial + 1)
    ]
    dependencies = sorted(
        {date(int(period[:4]) - 1, int(period[5:7]), int(period[8:10])).isoformat() for period in annual + quarters}
        | {date(int(annual[0][:4]) - 1, 12, 31).isoformat()}
    )
    return {
        "annual": annual,
        "quarters": quarters,
        "current": as_of.isoformat(),
        "dependencies": dependencies,
        "dependency_policy": profile["windows"]["dependency_policy"],
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _fact_id(fact: Mapping[str, Any]) -> str:
    return str(fact.get("fact_id") or fact.get("dimensional_fact_id") or "")


def _fact_value(fact: Mapping[str, Any]) -> Decimal | None:
    metadata = fact.get("metadata") or {}
    return _decimal(metadata.get("decimal_value", fact.get("value")))


def _available_at(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata") or {}
    return str(metadata.get("available_at") or value.get("available_at") or value.get("as_of") or "")


def _public_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    metadata = fact.get("metadata") or {}
    result = {
        "fact_id": _fact_id(fact),
        "metric_id": fact.get("metric_id"),
        "value": str(_fact_value(fact)) if _fact_value(fact) is not None else None,
        "unit": fact.get("unit"),
        "currency": fact.get("currency"),
        "period_start": fact.get("period_start"),
        "period_end": fact.get("period_end"),
        "period_type": fact.get("period_type"),
        "scope": fact.get("scope"),
        "verification_status": fact.get("verification_status"),
        "source_ids": list(fact.get("source_ids") or ()),
        "derived_from_fact_ids": list(fact.get("derived_from_fact_ids") or ()),
        "method_ref": fact.get("method_ref"),
        "available_at": _available_at(fact),
        "source_namespace_id": metadata.get("storage_namespace_id")
        or fact.get("_source_namespace_id"),
        "source_projection_role": fact.get("_projection_role"),
    }
    if fact.get("dimension_type"):
        result.update(
            dimension_type=fact.get("dimension_type"),
            dimension_code=fact.get("dimension_code"),
            dimension_name=fact.get("dimension_name"),
        )
    return result


def _payload_without_internal(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _materialized_facts(directory: Path, manifest: Mapping[str, Any], ticker: str) -> list[dict[str, Any]]:
    """Read the materializer's selected revisions without restoring the old exporter."""
    from analysis.models import DimensionalFactRecord, FactRecord
    from .consumption import CONSUMABLE_STATUSES, is_fact_consumable

    if not manifest.get("materialization_version"):
        raise ValueError(f"lite_materialization_manifest_missing:{directory}")
    facts = []
    for name, id_key, selection_key, count_key, model in (
        ("facts.jsonl", "fact_id", "selected_fact_ids", "fact_count", FactRecord),
        ("dimensional-facts.jsonl", "dimensional_fact_id", "selected_dimensional_fact_ids",
         "dimensional_fact_count", DimensionalFactRecord),
    ):
        selected = manifest.get(selection_key)
        if not isinstance(selected, list):
            raise ValueError(f"lite_materialization_selection_missing:{directory}:{selection_key}")
        if len(set(selected)) != len(selected):
            raise ValueError(f"lite_materialization_selection_duplicate:{directory}:{selection_key}")
        selected = frozenset(selected)
        path = directory / name
        if not path.is_file():
            raise ValueError(f"lite_materialization_file_missing:{path}")
        seen = set()
        for fact in _read_jsonl(path):
            if fact.get("ticker") != ticker:
                raise ValueError("lite_projection_company_mismatch")
            ident = fact[id_key]
            if ident in seen:
                raise ValueError(f"lite_materialization_duplicate_fact:{ident}")
            seen.add(ident)
            if ident not in selected:
                continue
            value = model.model_validate(fact)
            eligible = (is_fact_consumable(value, materialization_selected_ids=selected)
                        if model is FactRecord else
                        value.value is not None and value.verification_status in CONSUMABLE_STATUSES
                        and (value.verification_status.value != "供应商直采" or
                             bool(value.source_ids and value.structured_admission
                                  and value.structured_admission.programmatic_eligible)))
            if eligible:
                facts.append(fact)
        if not selected.issubset(seen) or manifest.get(count_key) != len(seen):
            raise ValueError(f"lite_materialization_incomplete:{path}")
    return facts


def _discover_projection(root: Path, ticker: str, role: str, priority: int) -> dict[str, Any]:
    company = root / ticker
    if not company.is_dir():
        raise FileNotFoundError(f"projection_company_missing:{company}")
    files, manifests, facts, records, coverage, work, events = {}, [], [], [], [], [], []
    # materialize_cache stores additional runs here. Archived history is never
    # an active input; an empty reconcile run must not hide the other runs.
    directories = [company, *sorted(p for p in (company / "runs").glob("*") if p.is_dir())]
    for directory in directories:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        if manifest_path.is_file():
            manifests.append({
                "path": str(manifest_path.resolve()), "sha256": _hash_file(manifest_path),
                **{key: manifest.get(key) for key in (
                    "run_id", "source_namespace_id", "contract_hash", "materialization_hash")},
            })
        standalone = manifest.get("materialization_version") or (directory / "facts.jsonl").is_file()
        rows = (_materialized_facts(directory, manifest, ticker) if standalone else
                _read_jsonl(directory / "coverage-facts.jsonl"))
        if standalone:
            # A formal export must never inherit stale legacy auxiliaries from
            # a directory that happened to be used by the removed exporter.
            names = set(manifest.get("exports") or ("facts.jsonl", "dimensional-facts.jsonl", "sources.jsonl", "events.jsonl"))
            if manifest.get("projection_schema_version"):
                from .materialization_replay import PROJECTION_FILES, RESEARCH_FILES
                required = set(PROJECTION_FILES)
                if manifest.get("research_projection_version"):
                    required.update(RESEARCH_FILES)
                if not required.issubset(names):
                    raise ValueError(f"lite_projection_exports_incomplete:{directory}")
            for name in names:
                if Path(name).name != name:
                    raise ValueError(f"lite_projection_export_path_invalid:{name}")
                path = directory / name
                expected = (manifest.get("exports") or {}).get(name, {}).get("sha256")
                if expected and (not path.is_file() or _hash_file(path) != expected):
                    raise ValueError(f"lite_projection_export_hash_mismatch:{path}")
        else:
            names = {"coverage-facts.jsonl", "sources.jsonl", "normalized-records.jsonl",
                     "question-coverage.jsonl", "next-work.json", "question-coverage-summary.json"}
        for name in sorted(names):
            path = directory / name
            if path.is_file():
                files[path.relative_to(company).as_posix()] = {
                    "path": str(path.resolve()), "sha256": _hash_file(path)}
        for fact in rows:
            if fact.get("ticker") != ticker:
                raise ValueError("lite_projection_company_mismatch")
            fact = dict(fact)
            fact.update(_projection_role=role, _projection_priority=priority,
                        _projection_path=str(directory.resolve()))
            if manifest.get("source_namespace_id"):
                fact["_source_namespace_id"] = manifest["source_namespace_id"]
            facts.append(fact)
        for record in (_read_jsonl(directory / "normalized-records.jsonl") if "normalized-records.jsonl" in names else ()):
            if record.get("company") != ticker:
                raise ValueError("lite_record_company_mismatch")
            record = dict(record)
            record.update(_projection_role=role, _projection_priority=priority,
                          _projection_path=str(directory.resolve()))
            records.append(record)
        if "events.jsonl" in names:
            from analysis.models import EventRecord
            run_events = []
            for event in _read_jsonl(directory / "events.jsonl"):
                value = EventRecord.model_validate(event)
                if value.ticker != ticker:
                    raise ValueError("lite_event_company_mismatch")
                run_events.append(value.model_dump(mode="json"))
            if standalone and manifest.get("event_count", 0) != len(run_events):
                raise ValueError(f"lite_materialization_incomplete:{directory / 'events.jsonl'}")
            events.extend(run_events)
        if "question-coverage.jsonl" in names:
            coverage.extend(_read_jsonl(directory / "question-coverage.jsonl"))
        work_path = directory / "next-work.json"
        if "next-work.json" in names and work_path.is_file():
            work.extend(json.loads(work_path.read_text(encoding="utf-8")).get("items", []))
    namespaces = sorted(
        {str(item["source_namespace_id"]) for item in manifests if item.get("source_namespace_id")}
    )
    return {
        "root": str(root.resolve()),
        "company_path": str(company.resolve()),
        "role": role,
        "priority": priority,
        "files": files,
        "manifests": manifests,
        "namespaces": namespaces,
        "facts": facts,
        "records": records,
        "coverage": coverage,
        "work": work,
        "events": events,
    }


def _merge_items(
    projections: Sequence[Mapping[str, Any]], key_name: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    conflicts = []
    for projection in projections:
        for item in projection[key_name]:
            identity = (_fact_id(item) if key_name == "facts" else
                        str(item.get("event_id") if key_name == "events" else item.get("record_id") or ""))
            if not identity:
                continue
            previous = by_id.get(identity)
            if previous is not None:
                if _payload_without_internal(previous) != _payload_without_internal(item):
                    raise ValueError(f"conflicting_immutable_{key_name}_payload:{identity}")
                continue
            by_id[identity] = dict(item)
    return list(by_id.values()), conflicts


def _selected_records_at(records: Sequence[Mapping[str, Any]], as_of: date,
                         conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only explicit ordered revisions replace source rows; conflicts stay visible."""
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone(timedelta(hours=8)))
    eligible = []
    for record in records:
        raw = record.get("available_at")
        if not raw:
            continue
        available = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if available.tzinfo is None:
            raise ValueError("lite_record_availability_timezone_missing")
        if available <= cutoff:
            eligible.append(dict(record))
    grouped = defaultdict(list)
    for row in eligible:
        grouped[(row.get("storage_namespace_id"), row.get("dataset_id"),
                 row.get("source_definition_id"), row.get("source_definition_version"),
                 row.get("row_key") or row["record_id"])].append(row)
    selected = []
    for key, values in grouped.items():
        by_id = {row["record_id"]: row for row in values}
        superseded = set()
        for row in values:
            parent = by_id.get(row.get("supersedes_record_version_id"))
            if parent and (datetime.fromisoformat(parent["available_at"].replace("Z", "+00:00"))
                           < datetime.fromisoformat(row["available_at"].replace("Z", "+00:00"))):
                superseded.add(parent["record_id"])
        leaves = [row for row in values if row["record_id"] not in superseded]
        if len({canonical_sha256(row.get("fields") or {}) for row in leaves}) > 1:
            conflict = {"kind": "source_record_revision_conflict", "dataset_id": key[1],
                        "row_key": key[-1], "record_ids": sorted(row["record_id"] for row in leaves),
                        "resolved": False, "reason": "no_explicit_ordered_revision_chain"}
            conflicts.append({**conflict, "conflict_id": "record-conflict-" + canonical_sha256(conflict)[:20]})
        selected.extend(leaves)
    return selected


def _logical_fact_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact.get("metric_id"),
        fact.get("period_end"),
        fact.get("period_type"),
        fact.get("scope"),
        fact.get("currency"),
        fact.get("unit"),
        fact.get("dimension_type"),
        fact.get("dimension_code") or fact.get("dimension_name"),
    )


def _choose_candidates(
    candidates: Sequence[Mapping[str, Any]], conflicts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not candidates:
        return None
    ordered = sorted(candidates, key=_fact_id)
    ordered.sort(key=_available_at, reverse=True)
    ordered.sort(
        key=lambda item: (
            int(item.get("_projection_priority", 99)),
            len(item.get("derived_from_fact_ids") or ()),
        )
    )
    chosen = dict(ordered[0])
    values = {(str(_fact_value(item)), item.get("unit")) for item in ordered}
    if len(values) > 1:
        conflicts.append(
            {
                "conflict_id": "lite-conflict-"
                + canonical_sha256([_fact_id(item) for item in ordered])[:24],
                "kind": "logical_fact_value_conflict",
                "logical_key": list(_logical_fact_key(chosen)),
                "chosen_fact_id": _fact_id(chosen),
                "candidate_fact_ids": [_fact_id(item) for item in ordered],
                "candidate_values": [str(_fact_value(item)) for item in ordered],
                "selection_rule": "explicit_primary_projection_then_stable_fact_id",
                "resolved": False,
            }
        )
    return chosen


def _fact_lookup(facts: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        metric = fact.get("metric_id")
        period = fact.get("period_end")
        kind = fact.get("period_type")
        if metric and period and kind:
            result[(str(metric), str(period), str(kind))].append(dict(fact))
    return result


def _desired_period_type(metric_id: str, *, annual: bool) -> str:
    if metric_id in STOCK_METRICS:
        return "instant"
    return "cumulative" if annual else "single_quarter"


def _make_derived(
    metric_id: str,
    period: str,
    period_type: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    operation: str,
) -> dict[str, Any] | None:
    left_value, right_value = _fact_value(left), _fact_value(right)
    if left_value is None or right_value is None:
        return None
    if left.get("currency") != right.get("currency") or left.get("scope") != right.get("scope"):
        return None
    if operation in {"ratio", "one_minus_ratio"} and right_value == 0:
        return None
    if operation == "ratio":
        value, unit = left_value / right_value, "ratio"
    elif operation == "one_minus_ratio":
        value, unit = Decimal("1") - left_value / right_value, "ratio"
    else:
        if left.get("unit") != right.get("unit"):
            return None
        value, unit = left_value - right_value, str(left.get("unit"))
    identity = {
        "formula_version": FORMULA_VERSION,
        "metric_id": metric_id,
        "period": period,
        "period_type": period_type,
        "inputs": [_fact_id(left), _fact_id(right)],
    }
    return {
        "fact_id": "lite-derived-" + canonical_sha256(identity)[:32],
        "metric_id": metric_id,
        "value": str(value),
        "unit": unit,
        "currency": left.get("currency"),
        "period_start": left.get("period_start"),
        "period_end": period,
        "period_type": period_type,
        "scope": left.get("scope"),
        "verification_status": "程序计算",
        "source_ids": sorted(set(left.get("source_ids") or ()) | set(right.get("source_ids") or ())),
        "derived_from_fact_ids": identity["inputs"],
        "method_ref": FORMULA_VERSION + ":" + metric_id,
        "as_of": max(_available_at(left), _available_at(right)),
        "metadata": {
            "decimal_value": str(value),
            "available_at": max(_available_at(left), _available_at(right)),
            "formula_version": FORMULA_VERSION,
            "input_fact_ids": identity["inputs"],
            "cash_flow_boundary": (
                "cash flow proxy; not FCFF"
                if metric_id == "operating_cash_flow_less_asset_purchase_proxy"
                else None
            ),
        },
        "_projection_role": "lite_deterministic_derivation",
        "_projection_priority": -1,
        "_source_namespace_id": None,
    }


def _select_metric(
    lookup: Mapping[tuple[str, str, str], list[dict[str, Any]]],
    metric_id: str,
    period: str,
    period_type: str,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    chosen = _choose_candidates(lookup.get((metric_id, period, period_type), ()), conflicts)
    if chosen is not None:
        return chosen
    dependency = RATIO_DEPENDENCIES.get(metric_id)
    if dependency is None:
        return None
    left = _choose_candidates(lookup.get((dependency[0], period, period_type), ()), conflicts)
    right = _choose_candidates(lookup.get((dependency[1], period, period_type), ()), conflicts)
    if left is None or right is None:
        return None
    return _make_derived(metric_id, period, period_type, left, right, dependency[2])


def _metric_entry(
    spec: Mapping[str, Any],
    period: str,
    period_type: str,
    fact: Mapping[str, Any] | None,
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    entry = {
        "requirement_id": f"lite.metric.{spec['metric_id']}.{period}.{period_type}",
        "metric_id": spec["metric_id"],
        "label": spec["label"],
        "group": spec["group"],
        "required": bool(spec["required"]),
        "period": period,
        "period_type": period_type,
        "state": "ready" if fact is not None else "pending",
        "reason": None if fact is not None else "compatible_fact_or_formula_inputs_missing",
        "fact": _public_fact(fact) if fact is not None else None,
        "yoy": None,
    }
    if fact is not None and prior is not None:
        current_value, prior_value = _fact_value(fact), _fact_value(prior)
        if current_value is not None and prior_value not in (None, Decimal("0")):
            entry["yoy"] = {
                "value": str(current_value / prior_value - Decimal("1")),
                "unit": "ratio",
                "prior_period": prior.get("period_end"),
                "current_value": str(current_value),
                "prior_value": str(prior_value),
                "input_fact_ids": [_fact_id(fact), _fact_id(prior)],
                "prior_fact": _public_fact(prior),
                "formula_version": FORMULA_VERSION + ":yoy",
            }
    return entry


def _current_metric(
    lookup: Mapping[tuple[str, str, str], list[dict[str, Any]]],
    metric_id: str,
    as_of: date,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = []
    for (metric, period, period_type), values in lookup.items():
        if metric == metric_id and period <= as_of.isoformat() and period_type in {"market_quote", "instant"}:
            candidates.extend(values)
    if not candidates:
        return None
    latest = max(str(item.get("period_end")) for item in candidates)
    return _choose_candidates([item for item in candidates if str(item.get("period_end")) == latest], conflicts)


def _select_metric_entries(
    facts: Sequence[Mapping[str, Any]],
    periods: Mapping[str, Any],
    profile: Mapping[str, Any],
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lookup = _fact_lookup(facts)
    entries = []
    for spec in profile["core_metrics"]:
        if spec["period_scope"] == "current":
            fact = _current_metric(lookup, spec["metric_id"], date.fromisoformat(periods["current"]), conflicts)
            entries.append(_metric_entry(spec, periods["current"], "current", fact, None))
            continue
        for annual, values in ((True, periods["annual"]), (False, periods["quarters"])):
            period_type = _desired_period_type(spec["metric_id"], annual=annual)
            for period in values:
                fact = _select_metric(lookup, spec["metric_id"], period, period_type, conflicts)
                prior_period = date(int(period[:4]) - 1, int(period[5:7]), int(period[8:10])).isoformat()
                prior = _select_metric(lookup, spec["metric_id"], prior_period, period_type, conflicts)
                entries.append(_metric_entry(spec, period, period_type, fact, prior))
        if profile.get("include_balance_predecessor") and spec["metric_id"] in {"total_assets", "total_liabilities"}:
            period = str(int(periods["annual"][0][:4])-1)+"-12-31"
            fact = _select_metric(lookup, spec["metric_id"], period, "instant", conflicts)
            entries.append(_metric_entry(spec, period, "instant", fact, None))
        if profile.get("include_latest_cumulative_and_ttm") and _desired_period_type(spec["metric_id"], annual=True) == "cumulative":
            period = periods["quarters"][-1]
            prior_period = str(int(period[:4]) - 1) + period[4:]
            for kind in ("cumulative", "ttm"):
                if any(x["metric_id"] == spec["metric_id"] and x["period"] == period and x["period_type"] == kind for x in entries):
                    continue
                fact = _select_metric(lookup, spec["metric_id"], period, kind, conflicts)
                prior = _select_metric(lookup, spec["metric_id"], prior_period, kind, conflicts)
                entries.append(_metric_entry(spec, period, kind, fact, prior))
    return entries


def _latest_records(
    records: Sequence[Mapping[str, Any]], dataset_ids: set[str], *, limit: int
) -> list[dict[str, Any]]:
    candidates = [record for record in records if record.get("dataset_id") in dataset_ids]
    candidates.sort(
        key=lambda item: (
            str(item.get("period") or ""),
            str(item.get("available_at") or ""),
            str(item.get("record_id") or ""),
        ),
        reverse=True,
    )
    candidates.sort(key=lambda item: int(item.get("_projection_priority", 99)))
    result = []
    seen = set()
    for record in candidates:
        key = (record.get("dataset_id"), canonical_sha256(record.get("fields") or {}))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "record_id": record.get("record_id"),
                "dataset_id": record.get("dataset_id"),
                "period": record.get("period"),
                "available_at": record.get("available_at"),
                "snapshot_id": record.get("snapshot_id"),
                "row_key": record.get("row_key"),
                "fields": record.get("fields") or {},
                "source_projection_role": record.get("_projection_role"),
            }
        )
        if len(result) >= limit:
            break
    return result


def _business_payload(
    facts: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    periods: Mapping[str, Any],
    ticker: str,
    profile: Mapping[str, Any],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    company = _latest_records(records, {"company_basic"}, limit=1)
    latest_annual = periods["annual"][-1]
    segment_candidates = [
        fact
        for fact in facts
        if fact.get("metric_id") in {"segment_revenue", "segment_cost", "segment_profit"}
        and fact.get("period_end") == latest_annual
    ]
    segments = sorted(
        (_public_fact(fact) for fact in segment_candidates),
        key=lambda item: (
            item["metric_id"] != "segment_revenue",
            -(abs(_decimal(item["value"]) or Decimal("0"))),
            str(item.get("dimension_name") or ""),
        ),
    )[:12]
    counterparty_records = [
        record
        for record in records
        if record.get("dataset_id") == "customers_peer"
        and record.get("period") in periods["annual"]
        and str((record.get("fields") or {}).get("RANK")) in {"1", "2", "3", "4", "5"}
    ]
    counterparty_records.sort(
        key=lambda item: (
            str(item.get("period")),
            str((item.get("fields") or {}).get("TYPE")),
            -int((item.get("fields") or {}).get("RANK") or 99),
        ),
        reverse=True,
    )
    counterparties = []
    for record in counterparty_records[:30]:
        fields = record.get("fields") or {}
        amount = _decimal(fields.get("AMOUNT"))
        total = _decimal(fields.get("SUM_AMOUNT"))
        reported = _decimal(fields.get("TOI_RATIO"))
        computed = amount / total * 100 if amount is not None and total not in (None, Decimal("0")) else None
        delta = reported - computed if reported is not None and computed is not None else None
        item = {
            "record_id": record.get("record_id"),
            "snapshot_id": record.get("snapshot_id"),
            "row_key": record.get("row_key"),
            "period": record.get("period"),
            "type": fields.get("TYPE"),
            "rank": fields.get("RANK"),
            "amount": str(amount) if amount is not None else None,
            "amount_unit": "CNY",
            "provider_total": str(total) if total is not None else None,
            "reported_ratio_percent": str(reported) if reported is not None else None,
            "recomputed_ratio_percent": str(computed) if computed is not None else None,
            "rounding_delta_percentage_points": str(delta) if delta is not None else None,
            "total_amount_semantics": "provider_denominator_not_confirmed_disclosed",
        }
        counterparties.append(item)
        if delta not in (None, Decimal("0")):
            conflicts.append(
                {
                    "conflict_id": "lite-counterparty-difference-"
                    + canonical_sha256([ticker, record.get("record_id"), str(delta)])[:24],
                    "kind": "reported_ratio_rounding_difference",
                    "record_id": record.get("record_id"),
                    "reported_ratio_percent": str(reported),
                    "recomputed_ratio_percent": str(computed),
                    "delta_percentage_points": str(delta),
                    "resolved": True,
                    "resolution": "retain_reported_ratio_and_independent_recalculation",
                }
            )
    return {
        "company": company[0] if company else None,
        "company_name": profile["industry_profile"]["supported_tickers"].get(ticker),
        "segments": segments,
        "counterparties": counterparties,
        "counterparty_boundary": "仅前五名披露行；供应商总额语义未确认，不反推未披露分母。",
    }


def _load_evidence(
    roots: Sequence[Path], ticker: str, as_of: date
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    evidence: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}
    catalogs = []
    for priority, root in enumerate(roots):
        evidence_path = root / "document-evidence.jsonl"
        for item in _read_jsonl(evidence_path):
            if item.get("company") != ticker or str(item.get("period") or "9999") > as_of.isoformat():
                continue
            value = dict(item)
            value["_source_evidence_path"] = str(evidence_path.resolve())
            value["_projection_priority"] = priority
            previous = evidence.get(str(value["evidence_id"]))
            if previous and _payload_without_internal(previous) != _payload_without_internal(value):
                raise ValueError("conflicting_immutable_evidence_payload")
            evidence[str(value["evidence_id"])] = value
        live = root / "live-documents.json"
        if live.is_file():
            payload = json.loads(live.read_text(encoding="utf-8"))
            catalogs.extend(item for item in payload.get("catalogs", []) if item.get("ticker") == ticker)
            for document in payload.get("documents", []):
                period = str(document.get("period") or "")
                published_at = str(document.get("published_at") or "")[:10]
                if (
                    document.get("ticker") == ticker
                    and document.get("original_sha256")
                    and (not period or period <= as_of.isoformat())
                    and (not published_at or published_at <= as_of.isoformat())
                ):
                    documents[str(document["original_sha256"])] = document
    return list(evidence.values()), documents, catalogs


def _auxiliary_inputs(roots: Sequence[Path]) -> list[dict[str, Any]]:
    values = []
    for priority, root in enumerate(roots):
        files = {}
        source_requests = 0
        uncached_requests = 0
        for name in ("document-evidence.jsonl", "live-documents.json", "official-table-facts.jsonl"):
            path = root / name
            if not path.is_file():
                continue
            files[name] = {"path": str(path.resolve()), "sha256": _hash_file(path)}
            if name == "live-documents.json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                requests = payload.get("requests", [])
                source_requests += len(requests)
                uncached_requests += sum(not item.get("cache_reused", False) for item in requests)
        if files:
            values.append(
                {
                    "root": str(root.resolve()),
                    "role": "primary_auxiliary" if priority == 0 else f"supplement_{priority}_auxiliary",
                    "files": files,
                    "referenced_request_count": source_requests,
                    "referenced_uncached_request_count": uncached_requests,
                }
            )
    return values


def _merge_request_audit(
    existing: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """合并多公司目录请求；同一缓存键保留首次真实 I/O 证据。"""
    merged: dict[str, dict[str, Any]] = {}
    for item in [*existing, *current]:
        request_key = str(item.get("request_key") or "")
        if not request_key:
            raise ValueError("catalog_request_key_missing")
        previous = merged.get(request_key)
        if previous is not None:
            if previous.get("sha256") != item.get("sha256"):
                raise ValueError(f"catalog_request_snapshot_conflict:{request_key}")
            continue
        merged[request_key] = dict(item)
    return [merged[key] for key in sorted(merged)]


def refresh_lite_catalog(
    *, output_root: Path, ticker: str, as_of: date, profile_id: str = LITE_PROFILE_ID
) -> dict[str, Any]:
    """显式联网刷新期后公告目录；不下载正文，不修改事实库。"""
    from datetime import datetime as _datetime

    from analysis.acquisition.research_fetch import ResearchFetch
    from .reading import select_research_document

    profile = load_research_profile(profile_id)
    if ticker not in profile["industry_profile"]["supported_tickers"]:
        raise ValueError(profile["industry_profile"]["unsupported_message"])
    transport = ResearchFetch(output_root / "acquisition")
    stock_path, stock_proof = transport.fetch(
        "https://www.cninfo.com.cn/new/data/szse_stock.json"
    )
    stocks = {
        item["code"]: item
        for item in json.loads(stock_path.read_bytes()).get("stockList", [])
    }
    if ticker not in stocks:
        raise ValueError("company_identity_missing")
    stock = stocks[ticker]
    entries = {}
    pages = []
    expected = None
    start = date(as_of.year, 1, 1)
    for page in range(1, 21):
        params = {
            "stock": ticker + "," + stock["orgId"],
            "tabName": "fulltext",
            "pageSize": 30,
            "pageNum": page,
            "column": "szse",
            "category": "",
            "seDate": f"{start.isoformat()}~{as_of.isoformat()}",
            "searchkey": "",
            "sortName": "time",
            "sortType": "desc",
            "isHLtitle": "true",
        }
        path, proof = transport.fetch(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query", data=params
        )
        payload = json.loads(path.read_bytes())
        rows = payload.get("announcements")
        if not isinstance(rows, list):
            raise ValueError("catalog_shape_invalid")
        expected = payload.get("totalAnnouncement")
        for item in rows:
            if item.get("secCode") != ticker:
                raise ValueError("catalog_company_mismatch")
            identity = "cninfo:" + str(item["announcementId"])
            entry = {
                "canonical_resource_id": identity,
                "ticker": ticker,
                "title": item["announcementTitle"],
                "resource_url": "https://static.cninfo.com.cn/" + item["adjunctUrl"],
                "expected_mime_types": ["application/pdf"],
                "metadata": {
                    "ticker": ticker,
                    "org_id": stock["orgId"],
                    "formal_category": item.get("announcementType"),
                },
                "published_at": _datetime.fromtimestamp(
                    item["announcementTime"] / 1000, timezone.utc
                ).isoformat(),
                "catalog_sha256": proof["sha256"],
            }
            previous = entries.get(identity)
            if previous and previous != entry:
                raise ValueError("catalog_duplicate_payload_conflict")
            entries[identity] = entry
        pages.append(
            {
                "page": page,
                "rows": len(rows),
                "has_more": payload.get("hasMore"),
                "sha256": proof["sha256"],
                "request_key": proof["request_key"],
            }
        )
        if not payload.get("hasMore"):
            break
    else:
        raise ValueError("catalog_page_bound_exceeded")
    if expected is not None and len(entries) != expected:
        raise ValueError("catalog_total_mismatch")
    documents = []
    for entry in entries.values():
        choice = select_research_document(
            entry, as_of=as_of, research_profile_id=profile_id
        )
        documents.append(
            {
                "ticker": ticker,
                "title": entry["title"],
                "resource_id": entry["canonical_resource_id"],
                "url": entry["resource_url"],
                "published_at": entry["published_at"],
                "catalog_sha256": entry["catalog_sha256"],
                **choice,
                "download_status": "not_requested",
                "parse_status": "not_requested",
                "semantic_status": "not_started",
            }
        )
    risk_classes = Counter(
        item["document_class"]
        for item in documents
        if item["document_class"] not in {"D01", "D02", "D03", "D20", "D21"}
    )
    catalog = {
        "ticker": ticker,
        "scope": "post_annual_events",
        "query_start": start.isoformat(),
        "query_end": as_of.isoformat(),
        "count": len(entries),
        "expected": expected,
        "pages": pages,
        "terminal": True,
        "risk_class_counts": dict(sorted(risk_classes.items())),
        "body_downloaded": False,
    }
    existing_path = output_root / "live-documents.json"
    existing = (
        json.loads(existing_path.read_text(encoding="utf-8"))
        if existing_path.is_file()
        else {"documents": [], "catalogs": [], "requests": []}
    )
    existing["documents"] = [
        item for item in existing.get("documents", []) if item.get("ticker") != ticker
    ] + sorted(documents, key=lambda item: item["resource_id"])
    existing["catalogs"] = [
        item
        for item in existing.get("catalogs", [])
        if not (item.get("ticker") == ticker and item.get("scope") == "post_annual_events")
    ] + [catalog]
    current_network_io = any(
        not item.get("cache_reused", False) for item in transport.requests
    )
    existing["requests"] = _merge_request_audit(
        existing.get("requests", []), transport.requests
    )
    existing["profile_id"] = profile_id
    existing["profile_sha256"] = profile["content_sha256"]
    existing["performed_network_io"] = any(
        not item.get("cache_reused", False) for item in existing["requests"]
    )
    existing["manual_acceptance"] = "pending"
    _write_json(existing_path, existing)
    return {
        "ticker": ticker,
        "catalog_count": len(entries),
        "catalog_pages": len(pages),
        "terminal": True,
        "risk_class_counts": dict(sorted(risk_classes.items())),
        "requests": len(transport.requests),
        "uncached_requests": sum(
            not item.get("cache_reused", False) for item in transport.requests
        ),
        "performed_network_io": current_network_io,
        "output": str(existing_path.resolve()),
        "stock_list_request_key": stock_proof["request_key"],
    }


def _evidence_payload(
    all_evidence: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    periods: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = []
    coverage = []
    preferred_periods = [periods["quarters"][-1], periods["annual"][-1]]
    for slot in profile["evidence_slots"]:
        candidates = [item for item in all_evidence if item.get("route_id") in slot["routes"]]
        candidates.sort(
            key=lambda item: (
                str(item.get("period")) in preferred_periods,
                str(item.get("period") or ""),
                -int(item.get("_projection_priority", 99)),
                str(item.get("evidence_id")),
            ),
            reverse=True,
        )
        chosen = []
        seen = set()
        for item in candidates:
            key = (item.get("route_id"), item.get("original_sha256"), item.get("locator"))
            if key in seen:
                continue
            seen.add(key)
            document = documents.get(str(item.get("original_sha256")), {})
            text = str(item.get("text") or "")
            index_item = {
                "evidence_id": item["evidence_id"],
                "slot_id": slot["slot_id"],
                "group": slot["group"],
                "company": item.get("company"),
                "period": item.get("period"),
                "route_id": item.get("route_id"),
                "document_class": item.get("document_class"),
                "original_sha256": item.get("original_sha256"),
                "original_path": document.get("original_path"),
                "source_url": item.get("original_url"),
                "locator": item.get("locator"),
                "parser_version": item.get("parser_version") or document.get("parser_version"),
                "semantic_status": item.get("semantic_status"),
                "question_answer_status": item.get("question_answer_status"),
                "data_nature": item.get("data_nature"),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "excerpt": " ".join(text.split())[:420],
                "source_evidence_path": item.get("_source_evidence_path"),
            }
            selected.append(index_item)
            chosen.append(index_item)
            if len(chosen) >= int(slot["max_items"]):
                break
        coverage.append(
            {
                "requirement_id": "lite.evidence." + slot["slot_id"],
                "slot_id": slot["slot_id"],
                "group": slot["group"],
                "required": bool(slot["required"]),
                "routes": slot["routes"],
                "state": "source_text_available" if chosen else "pending",
                "reason": (
                    "located_source_passages_require_question_review"
                    if chosen
                    else "located_source_passage_missing"
                ),
                "evidence_ids": [item["evidence_id"] for item in chosen],
            }
        )
    selected.sort(key=lambda item: item["evidence_id"])
    return selected, coverage


def _question_state(rows: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, int]]:
    counts = dict(Counter(str(row.get("state") or "pending") for row in rows))
    if not rows or counts.get("pending"):
        return "pending", counts
    if counts.get("source_text_available"):
        return "source_text_available", counts
    if counts.get("ready"):
        return "ready", counts
    return "not_applicable", counts


def _period_relevant(period: str | None, periods: Mapping[str, Any]) -> bool:
    if not period:
        return False
    display = set(periods["annual"]) | set(periods["quarters"]) | {periods["current"]}
    if period in display:
        return True
    if "/" in period:
        start, end = period.split("/", 1)
        return end >= periods["annual"][0] and start <= periods["current"]
    return False


def _question_routes(
    coverage_rows: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    periods: Mapping[str, Any],
) -> list[dict[str, Any]]:
    registry = json.loads(
        (ROOT / "config/structured_data/research_requirements.v1.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = defaultdict(list)
    for requirement in registry["requirements"]:
        requirements[requirement["question_id"]].append(requirement["requirement_id"])
    route_for = {
        question_id: route
        for route, question_ids in profile["question_routing"].items()
        for question_id in question_ids
    }
    by_question = defaultdict(list)
    for row in coverage_rows:
        if _period_relevant(row.get("period"), periods):
            by_question[row.get("question_id")].append(row)
    result = []
    for question in registry["questions"]:
        question_id = question["question_id"]
        state, counts = _question_state(by_question[question_id])
        step = question_id.split(".", 1)[0]
        result.append(
            {
                "question_id": question_id,
                "title": question.get("title"),
                "scope_route": route_for[question_id],
                "quality_state": state,
                "quality_counts": counts,
                "full_requirement_refs": sorted(requirements[question_id]),
                "groups": profile["step_groups"].get(step, []),
                "activation_status": (
                    "not_activated_in_lite_default"
                    if route_for[question_id] == "deferred"
                    else "active"
                ),
            }
        )
    return result


def _catalog_status(
    documents: Mapping[str, Mapping[str, Any]],
    catalogs: Sequence[Mapping[str, Any]],
    periods: Mapping[str, Any],
) -> dict[str, Any]:
    latest_annual = periods["annual"][-1]
    as_of = date.fromisoformat(periods["current"])
    interim_year = as_of.year if as_of >= date(as_of.year, 8, 31) else as_of.year - 1
    latest_interim = date(interim_year, 6, 30).isoformat()
    statuses = {}
    for label, period, category in (
        ("latest_annual", latest_annual, "D01"),
        ("latest_interim", latest_interim, "D02"),
    ):
        matches = [
            item
            for item in documents.values()
            if item.get("period") == period and item.get("document_class") == category
        ]
        statuses[label] = {
            "period": period,
            "state": "parsed" if any(item.get("parse_status") == "parsed" for item in matches) else "pending",
            "resource_ids": [item.get("resource_id") for item in matches],
        }
    post_annual = [
        item
        for item in catalogs
        if item.get("scope") == "post_annual_events"
        and item.get("query_end") == periods["current"]
        and item.get("terminal")
    ]
    return {
        "required_reports": statuses,
        "catalog_pages_terminal": bool(catalogs) and all(item.get("terminal") for item in catalogs),
        "post_annual_event_catalog_state": "ready" if post_annual else "pending",
        "post_annual_event_catalog_reason": (
            "bounded_catalog_refreshed_but_selected_bodies_still_require_triggers_and_review"
            if post_annual
            else "cached_catalog_is_limited_to_annual_and_interim_categories"
        ),
        "post_annual_event_catalog": post_annual[-1] if post_annual else None,
        "risk_absence_conclusion_allowed": False,
    }


def _full_work_items(
    projections: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    periods: Mapping[str, Any],
) -> list[dict[str, Any]]:
    active_questions = set(profile["question_routing"]["core"])
    profile_datasets = profile["datasets"]
    values = []
    seen = set()
    for projection in projections:
        for item in projection["work"]:
            if item.get("question_id") not in active_questions or not _period_relevant(item.get("period"), periods):
                continue
            dataset = item.get("dataset_id")
            raw_name = item.get("raw_name")
            rule = profile_datasets.get(dataset) if dataset else None
            if dataset and (not rule or raw_name and raw_name not in rule["fields"]):
                continue
            value = dict(item)
            value["profile_id"] = profile["profile_id"]
            value["trigger"] = "active_lite_core_requirement"
            value["acquire_allowed"] = bool(
                value.get("stage") == "acquisition"
                and value.get("reason") == "input_not_acquired"
                and rule
            )
            key = (
                value.get("question_id"),
                value.get("requirement_id"),
                value.get("period"),
                value.get("stage"),
                dataset,
                raw_name,
            )
            if key not in seen:
                seen.add(key)
                values.append(value)
    return values


def _next_work(
    metric_entries: Sequence[Mapping[str, Any]],
    evidence_coverage: Sequence[Mapping[str, Any]],
    context_coverage: Sequence[Mapping[str, Any]],
    question_work: Sequence[Mapping[str, Any]],
    catalog: Mapping[str, Any],
    ticker: str,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    items = list(question_work)
    for entry in metric_entries:
        if entry["state"] == "ready" or not entry["required"]:
            continue
        items.append(
            {
                "company": ticker,
                "requirement_id": entry["requirement_id"],
                "period": entry["period"],
                "stage": (
                    "deterministic_calculation"
                    if entry["metric_id"] in RATIO_DEPENDENCIES
                    else "acquisition"
                ),
                "metric_id": entry["metric_id"],
                "reason": entry["reason"],
                "acquire_allowed": entry["metric_id"] not in RATIO_DEPENDENCIES,
                "profile_id": profile["profile_id"],
                "trigger": "missing_required_core_metric",
            }
        )
    for slot in evidence_coverage:
        if slot["state"] == "source_text_available":
            items.append(
                {
                    "company": ticker,
                    "requirement_id": slot["requirement_id"],
                    "stage": "document_reading",
                    "reason": slot["reason"],
                    "evidence_ids": slot["evidence_ids"],
                    "acquire_allowed": False,
                    "profile_id": profile["profile_id"],
                    "trigger": "source_text_requires_question_review",
                }
            )
        elif slot["required"]:
            items.append(
                {
                    "company": ticker,
                    "requirement_id": slot["requirement_id"],
                    "stage": "document_reading",
                    "reason": slot["reason"],
                    "acquire_allowed": False,
                    "profile_id": profile["profile_id"],
                    "trigger": "required_evidence_slot_missing",
                }
            )
    for item in context_coverage:
        if item["state"] in {"ready", "not_applicable"}:
            continue
        items.append(
            {
                "company": ticker,
                "requirement_id": item["requirement_id"],
                "stage": item.get("stage", "semantic_processing"),
                "reason": item.get("reason"),
                "record_ids": item.get("record_ids", []),
                "acquire_allowed": bool(item.get("acquire_allowed", False)),
                "profile_id": profile["profile_id"],
                "trigger": "active_lite_context_requirement",
            }
        )
    if catalog["post_annual_event_catalog_state"] != "ready":
        items.append(
            {
                "company": ticker,
                "requirement_id": "lite.catalog.post_annual_events",
                "period": None,
                "stage": "acquisition",
                "reason": catalog["post_annual_event_catalog_reason"],
                "acquire_allowed": True,
                "profile_id": profile["profile_id"],
                "trigger": "risk_catalog_scope_not_verified",
            }
        )
    unique = {}
    for item in items:
        key = canonical_sha256(item)
        unique[key] = item
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            STAGE_ORDER.get(str(item.get("stage")), 99),
            str(item.get("question_id") or ""),
            str(item.get("requirement_id") or ""),
            str(item.get("period") or ""),
        ),
    )
    return {
        "profile_id": profile["profile_id"],
        "company": ticker,
        "items": ordered,
        "stage_counts": dict(Counter(item.get("stage") for item in ordered)),
        "boundary": "only active lite gaps; observed or parsed inputs are not scheduled for duplicate acquisition",
    }


def _count_tokens(text: str) -> dict[str, Any]:
    try:
        import importlib.metadata
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("o200k_base")
        return {
            "count": len(encoding.encode(text)),
            "kind": "tokenizer",
            "method": "tiktoken:o200k_base",
            "version": importlib.metadata.version("tiktoken"),
            "counted_scope": "core-pack.md UTF-8 text",
        }
    except (ImportError, LookupError):
        return {
            "count": math.ceil(len(text.encode("utf-8")) / 2),
            "kind": "conservative_estimate",
            "method": "ceil_utf8_bytes_div_2",
            "version": "1",
            "counted_scope": "core-pack.md UTF-8 text",
        }


def _format_number(value: str | None, unit: str | None, metric_id: str = "") -> str:
    parsed = _decimal(value)
    if parsed is None:
        return "—"
    if unit == "CNY":
        return f"{parsed / Decimal('100000000'):.2f}亿元"
    if metric_id in {"pe_ttm", "pb", "ps_ttm", "pe", "ps", "ev_ebitda", "eastmoney_pe_ttm", "eastmoney_pb_mrq", "eastmoney_ps_ttm"}:
        return f"{parsed:.2f}倍"
    if unit == "ratio":
        return f"{parsed * 100:.2f}%"
    return f"{parsed.normalize()} {unit or ''}".strip()


def _markdown_table(metric_entries: Sequence[Mapping[str, Any]], periods: Sequence[str], *, annual: bool = True) -> str:
    by_key = {}
    for item in metric_entries:
        key = (item["metric_id"], item["period"], item["period_type"])
        if key in by_key and by_key[key] != item:
            raise ValueError(f"conflicting_display_metric:{key}")
        by_key[key] = item
    metrics = []
    for item in metric_entries:
        if item["period"] in periods and item["metric_id"] not in {value[0] for value in metrics}:
            metrics.append((item["metric_id"], item["label"]))
    lines = [
        "| 指标 | " + " | ".join(periods) + " |",
        "|---|" + "---:|" * len(periods),
    ]
    for metric_id, label in metrics:
        cells = []
        for period in periods:
            item = by_key.get((metric_id, period, _desired_period_type(metric_id, annual=annual)))
            if not item or item["state"] != "ready":
                cells.append("待补")
                continue
            fact = item["fact"]
            if fact["period_type"] != item["period_type"]:
                raise ValueError(f"display_fact_period_mismatch:{metric_id}:{period}")
            value = _format_number(fact["value"], fact["unit"], metric_id)
            cells.append(f"{value} `[{item['fact_ref']}]`")
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _render_markdown(payload: Mapping[str, Any], *, compact: bool = False) -> str:
    ticker, name = payload["ticker"], payload["company_name"] or "名称待核实"
    periods = payload["periods"]
    metrics = payload["metrics"]
    evidence = payload["evidence"]
    by_group = defaultdict(list)
    for item in evidence:
        by_group[item["group"]].append(item)
    company_fields = ((payload["business"].get("company") or {}).get("fields") or {})
    business_text = str(company_fields.get("MAIN_BUSINESS") or "主营业务结构化描述待核实")
    business_text = " ".join(business_text.split())[: (220 if compact else 520)]
    lines = [
        f"# {name}（{ticker}）八步轻量核心研究输入包",
        "",
        "> 研究输入，非投资结论。只能引用包内事实和证据；缺失项按 ID 定向读取或补取，假设与结论必须另列。",
        "",
        f"- 截止日：`{payload['as_of']}`；profile：`{payload['profile_id']}`；pack：`{payload['pack_id']}`",
        f"- 年度窗口：{', '.join(periods['annual'])}；季度窗口：{', '.join(periods['quarters'])}",
        f"- 数据边界：白酒画像已登记；期后事项目录状态为 `{payload['catalog']['post_annual_event_catalog_state']}`，不得据此写“无重大变化”。",
        "",
        "## 使用说明与八步导航",
        "",
        "先读本包；数字必须保留单位、期间和 `[Fnnn]`。完整事实 ID 在 `core-pack.json` 的同名引用中。需要上下文时调用 `evidence --evidence-id`，一次最多约 2,000 token。`source_text_available` 仅表示原文可读，不表示问题已回答。不得自动生成评级、目标价或安全边际。",
        "",
        "| 步骤 | 核心组 | 质量状态 |",
        "|---|---|---|",
    ]
    for step in [f"ES0{i}" for i in range(1, 9)]:
        questions = [item for item in payload["question_routes"] if item["question_id"].startswith(step + ".")]
        counts = Counter(item["quality_state"] for item in questions)
        lines.append(
            f"| {step} | {', '.join(payload['step_groups'][step])} | "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            + " |"
        )
    lines += [
        "",
        "## A 业务",
        "",
        f"- 主体：{company_fields.get('ORG_NAME') or name}；行业：{company_fields.get('CSRC_INDUSTRY_NAME') or company_fields.get('SWINDUSTRY_NAME2') or '待核实'}。",
        f"- 主营业务结构化原文：{business_text}",
        f"- 分部事实：{len(payload['business']['segments'])} 条；客户/供应商前五名记录：{len(payload['business']['counterparties'])} 条。金额/比例独立复算差异保留在冲突清单，不把供应商总额当披露分母。",
    ]
    for item in by_group["A"]:
        excerpt = item["excerpt"][: (120 if compact else 260)]
        lines.append(f"- `[E:{item['evidence_id']}]` {item['period']} {item['locator']}：{excerpt}")
    lines += [
        "",
        "## B 财务",
        "",
        "### 最近三个完整年度",
        "",
        _markdown_table([item for item in metrics if item["group"] == "B"], periods["annual"]),
        "",
        "### 最近八个应需季度（流量为单季、存量为期末）",
        "",
        _markdown_table([item for item in metrics if item["group"] == "B"], periods["quarters"], annual=False),
        "",
        "## C 质量与风险",
        "",
        _markdown_table([item for item in metrics if item["group"] == "C"], periods["annual"]),
        "",
        "季度风险指标详见 `core-pack.json`；差异筛查仅触发阅读，不自动判定造假或重大风险。结构化无记录不证明无事项。",
    ]
    for item in by_group["C"]:
        excerpt = item["excerpt"][: (120 if compact else 260)]
        lines.append(f"- `[E:{item['evidence_id']}]` {item['period']} {item['locator']}：{excerpt}")
    lines += ["", "## D 治理与资本", ""]
    for item in payload["governance"][: (4 if compact else 8)]:
        fields = "; ".join(f"{key}={value}" for key, value in list(item["fields"].items())[:5])
        lines.append(f"- `{item['dataset_id']}` {item.get('period') or '当前'} `[R:{item['record_id']}]`：{fields}")
    for item in by_group["D"]:
        excerpt = item["excerpt"][: (120 if compact else 260)]
        lines.append(f"- `[E:{item['evidence_id']}]` {item['period']} {item['locator']}：{excerpt}")
    lines += ["", "## E 行业与驱动", ""]
    lines.append(
        "同行仅从七家白酒画像中选择，依据为同一行业范围和同一核心口径；品牌带、渠道、产品结构与区域差异会限制可比性。"
    )
    for peer in payload["peers"]:
        values = ", ".join(
            f"{item['label']}={_format_number(item['value'], item['unit'], item['metric_id'])}"
            for item in peer["metrics"]
        )
        lines.append(f"- {peer['name']}（{peer['ticker']}）：{values or '可比指标待补'}")
    for item in by_group["E"]:
        excerpt = item["excerpt"][: (120 if compact else 260)]
        lines.append(f"- `[E:{item['evidence_id']}]` {item['period']} {item['locator']}：{excerpt}")
    current = [item for item in metrics if item["group"] == "F"]
    lines += ["", "## F 估值与约束", ""]
    for item in current:
        if item["state"] == "ready":
            fact = item["fact"]
            lines.append(
                f"- {item['label']}：{_format_number(fact['value'], fact['unit'], item['metric_id'])}，实际数据期 `{fact['period_end']}` `[{item['fact_ref']}]`"
            )
        else:
            lines.append(f"- {item['label']}：待补（{item['reason']}）")
    lines += [
        "- 历史估值分位、DCF、ROIC/WACC、EV/EBITDA 与目标价默认未启用；没有经确认的预测和情景参数。",
        "",
        "## 核心缺口、冲突与限制",
        "",
    ]
    gaps = [item for item in payload["coverage_requirements"] if item["state"] not in {"ready", "not_applicable"}]
    if gaps:
        for item in gaps:
            lines.append(
                f"- `{item['requirement_id']}`：{item['state']} / {item.get('reason') or '未说明'}"
            )
    else:
        lines.append("- 数值核心要求无缺口；原文语义核查和期后事项目录仍按下列状态保留。")
    unresolved = [item for item in payload["conflicts"] if not item.get("resolved")]
    lines.append(f"- 冲突：共 {len(payload['conflicts'])} 条，其中未解决 {len(unresolved)} 条；完整候选值见 `core-pack.json`。")
    lines.append(
        f"- 期后目录：{payload['catalog']['post_annual_event_catalog_state']} / {payload['catalog']['post_annual_event_catalog_reason']}。"
    )
    lines += [
        "- 银行、保险、券商及未知行业：轻量画像未验证，禁止套用普通企业指标。",
        "- 代理现金流只表示“经营现金流减购建长期资产现金支出”，不是 FCFF/FCFE。",
        "",
        "## 54 题范围路由",
        "",
        "| 问题 | 路由 | 质量状态 | 要求数 |",
        "|---|---|---|---:|",
    ]
    for item in payload["question_routes"]:
        lines.append(
            f"| {item['question_id']} | {item['scope_route']} | {item['quality_state']} | {len(item['full_requirement_refs'])} |"
        )
    lines += [
        "",
        "## 引用索引说明",
        "",
        "`[Fnnn]` 在 `core-pack.json` 中映射到完整 fact ID、标准值、单位、期间、公式和 namespace；`[E:...]` 在 `evidence-index.jsonl` 中保留原件哈希、URL、本地路径和页/表定位。默认只读本包，具体问题再按 ID 读取原文。",
        "",
    ]
    return "\n".join(lines)


def _peer_payload(
    input_root: Path,
    ticker: str,
    profile: Mapping[str, Any],
    periods: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = []
    inputs = []
    wanted = ("operating_income", "gross_margin", "eastmoney_pe_ttm", "eastmoney_pb_mrq")
    for peer in profile["peer_sets"].get(ticker, ()):
        company = input_root / peer
        if not company.is_dir():
            continue
        projection = _discover_projection(input_root, peer, "peer", 0)
        descriptor = {"ticker": peer, "files": projection["files"], "manifests": projection["manifests"]}
        inputs.append({**descriptor, "sha256": canonical_sha256(descriptor)})
        facts, _ = _merge_items([projection], "facts")
        lookup = _fact_lookup(facts)
        metrics = []
        conflicts: list[dict[str, Any]] = []
        for metric in wanted:
            if metric.startswith("eastmoney_"):
                fact = _current_metric(lookup, metric, date.fromisoformat(periods["current"]), conflicts)
            else:
                fact = _select_metric(lookup, metric, periods["annual"][-1], "cumulative", conflicts)
            if fact is not None:
                public = _public_fact(fact)
                metrics.append(
                    {
                        "metric_id": metric,
                        "label": next(item["label"] for item in profile["core_metrics"] if item["metric_id"] == metric),
                        "value": public["value"],
                        "unit": public["unit"],
                        "period": public["period_end"],
                        "fact_id": public["fact_id"],
                    }
                )
        result.append(
            {
                "ticker": peer,
                "name": profile["industry_profile"]["supported_tickers"][peer],
                "metrics": metrics,
                "comparability_basis": "same validated baijiu lite profile and frozen period definitions",
                "limitations": "brand tier, region, channel and product-mix differences are not normalized",
            }
        )
    return result, inputs


def _validate_existing_pack(pack_dir: Path, identity_hash: str) -> bool:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("pack_identity_hash") != identity_hash:
        return False
    for name, expected in manifest.get("output_hashes", {}).items():
        path = pack_dir / name
        if not path.is_file() or _hash_file(path) != expected:
            raise ValueError(f"cached_lite_pack_hash_mismatch:{name}")
    return True


def build_lite_pack(
    *,
    input_root: Path,
    ticker: str,
    as_of: date,
    output_root: Path,
    supplements: Sequence[Path] = (),
    profile_id: str = LITE_PROFILE_ID,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    profile = load_research_profile(profile_id)
    if ticker not in profile["industry_profile"]["supported_tickers"]:
        raise ValueError(profile["industry_profile"]["unsupported_message"])
    roots = [input_root, *supplements]
    output_resolved = output_root.resolve()
    if any(output_resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("lite_output_must_be_outside_input_projection")
    projections = [
        _discover_projection(root, ticker, "primary" if index == 0 else f"supplement_{index}", index)
        for index, root in enumerate(roots)
        if (root / ticker).is_dir()
    ]
    if not projections or projections[0]["role"] != "primary":
        raise FileNotFoundError("primary_lite_projection_missing")
    facts, _ = _merge_items(projections, "facts")
    records, _ = _merge_items(projections, "records")
    events, _ = _merge_items(projections, "events")
    periods = lite_periods(as_of, profile)
    conflicts: list[dict[str, Any]] = []
    records = _selected_records_at(records, as_of, conflicts)
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone(timedelta(hours=8)))
    events = [event for event in events
              if datetime.fromisoformat(event["available_at"].replace("Z", "+00:00")) <= cutoff
              and datetime.fromisoformat(event["announced_at"].replace("Z", "+00:00")) <= cutoff]
    metrics = _select_metric_entries(facts, periods, profile, conflicts)
    fact_refs = {
        fact_id: f"F{index:03d}"
        for index, fact_id in enumerate(
            sorted({_fact_id(item["fact"]) for item in metrics if item.get("fact")}),
            start=1,
        )
    }
    for item in metrics:
        if item.get("fact"):
            item["fact_ref"] = fact_refs[_fact_id(item["fact"])]
            item["fact"]["reference_id"] = item["fact_ref"]
        else:
            item["fact_ref"] = None
    business = _business_payload(facts, records, periods, ticker, profile, conflicts)
    all_evidence, documents, catalogs = _load_evidence(roots, ticker, as_of)
    evidence, evidence_coverage = _evidence_payload(all_evidence, documents, profile, periods)
    from .research_coverage import build_question_coverage
    detailed = build_question_coverage(ticker=ticker, facts=facts, records=records,
        evidence=all_evidence, profile=profile, periods=periods, as_of=as_of)
    coverage_rows = detailed["rows"]
    question_routes = _question_routes(coverage_rows, profile, periods)
    catalog = _catalog_status(documents, catalogs, periods)
    governance = _latest_records(
        records,
        {
            "controller",
            "dividend",
            "repurchase",
            "capital_projects",
            "capital_raise",
            "guarantee",
            "litigation",
            "violation",
            "management_roster",
        },
        limit=12,
    )
    peers, peer_inputs = _peer_payload(input_root, ticker, profile, periods)
    metric_coverage = [
        {
            key: item[key]
            for key in (
                "requirement_id",
                "metric_id",
                "label",
                "group",
                "required",
                "period",
                "period_type",
                "state",
                "reason",
            )
        }
        | {"fact_ids": [item["fact"]["fact_id"]] if item["fact"] else []}
        for item in metrics
    ]
    context_coverage = [
        {
            "requirement_id": "lite.context.company_identity_business",
            "group": "A",
            "required": True,
            "state": "source_text_available" if business["company"] else "pending",
            "reason": "structured_source_statement_requires_review" if business["company"] else "company_business_record_missing",
            "record_ids": [business["company"]["record_id"]] if business["company"] else [],
            "stage": "semantic_processing" if business["company"] else "acquisition",
            "acquire_allowed": not bool(business["company"]),
        },
        {
            "requirement_id": "lite.context.segments_latest_annual",
            "group": "A",
            "required": True,
            "state": "ready" if business["segments"] else "pending",
            "reason": None if business["segments"] else "latest_annual_segment_facts_missing",
            "fact_ids": [item["fact_id"] for item in business["segments"]],
            "stage": "acquisition",
            "acquire_allowed": not bool(business["segments"]),
        },
        {
            "requirement_id": "lite.context.counterparty_disclosure",
            "group": "A",
            "required": False,
            "state": "source_text_available" if business["counterparties"] else "pending",
            "reason": "provider_rows_require_disclosure_semantic_review" if business["counterparties"] else "counterparty_disclosure_not_obtained",
            "record_ids": [item["record_id"] for item in business["counterparties"]],
            "stage": "semantic_processing" if business["counterparties"] else "acquisition",
            "acquire_allowed": not bool(business["counterparties"]),
        },
        {
            "requirement_id": "lite.context.governance_capital_records",
            "group": "D",
            "required": True,
            "state": "source_text_available" if governance else "pending",
            "reason": "structured_records_require_event_state_review" if governance else "governance_capital_records_missing",
            "record_ids": [item["record_id"] for item in governance],
            "stage": "semantic_processing" if governance else "acquisition",
            "acquire_allowed": not bool(governance),
        },
        {
            "requirement_id": "lite.context.peer_comparison",
            "group": "E",
            "required": True,
            "state": "ready" if len(peers) >= 2 else "pending",
            "reason": None if len(peers) >= 2 else "two_comparable_baijiu_peers_missing",
            "peer_tickers": [item["ticker"] for item in peers],
            "stage": "research_context",
            "acquire_allowed": False,
        },
        {
            "requirement_id": "lite.context.latest_annual_original",
            "group": "C",
            "required": True,
            "state": "source_text_available" if catalog["required_reports"]["latest_annual"]["state"] == "parsed" else "pending",
            "reason": "parsed_original_requires_question_review" if catalog["required_reports"]["latest_annual"]["state"] == "parsed" else "latest_annual_original_missing_or_unparsed",
            "stage": "document_reading" if catalog["required_reports"]["latest_annual"]["state"] == "parsed" else "acquisition",
            "acquire_allowed": catalog["required_reports"]["latest_annual"]["state"] != "parsed",
        },
        {
            "requirement_id": "lite.context.latest_interim_original",
            "group": "C",
            "required": True,
            "state": "source_text_available" if catalog["required_reports"]["latest_interim"]["state"] == "parsed" else "pending",
            "reason": "parsed_original_requires_question_review" if catalog["required_reports"]["latest_interim"]["state"] == "parsed" else "latest_interim_original_missing_or_unparsed",
            "stage": "document_reading" if catalog["required_reports"]["latest_interim"]["state"] == "parsed" else "acquisition",
            "acquire_allowed": catalog["required_reports"]["latest_interim"]["state"] != "parsed",
        },
    ]
    coverage_requirements = metric_coverage + evidence_coverage + context_coverage
    question_work = _full_work_items([{"work": detailed["work_items"]}], profile, periods)
    next_work = _next_work(metrics, evidence_coverage, context_coverage, question_work, catalog, ticker, profile)
    source_inputs = [
        {
            key: projection[key]
            for key in ("root", "company_path", "role", "priority", "files", "manifests", "namespaces")
        }
        for projection in projections
    ]
    auxiliary_inputs = _auxiliary_inputs(roots)
    identity = {
        "pack_version": PACK_VERSION,
        "profile_id": profile_id,
        "profile_sha256": profile["content_sha256"],
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "source_inputs": source_inputs,
        "auxiliary_inputs": auxiliary_inputs,
        "peer_inputs": peer_inputs,
        "selected_fact_ids": sorted(
            item["fact"]["fact_id"] for item in metrics if item.get("fact")
        ),
        "selected_evidence_ids": sorted(item["evidence_id"] for item in evidence),
        "selection_version": PACK_VERSION,
        "formula_version": FORMULA_VERSION,
        "question_coverage": detailed["summary"],
        "token_budget": int(max_tokens or profile["budget"]["default_max_tokens"]),
    }
    identity_hash = canonical_sha256(identity)
    pack_id = "lite-pack-" + identity_hash[:24]
    pack_dir = output_root / ticker / as_of.isoformat() / pack_id
    audit_path = output_root / ticker / as_of.isoformat() / "last-run-audit.json"
    if _validate_existing_pack(pack_dir, identity_hash):
        manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
        _write_json(
            audit_path,
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "pack_id": pack_id,
                "cache_reused": True,
                "performed_network_io": False,
            },
        )
        return {
            "status": manifest["status"],
            "pack_id": pack_id,
            "pack_dir": str(pack_dir.resolve()),
            "cache_reused": True,
            "token_count": manifest["token_count"],
            "performed_network_io": False,
        }
    payload = {
        "pack_version": PACK_VERSION,
        "pack_id": pack_id,
        "profile_id": profile_id,
        "profile_sha256": profile["content_sha256"],
        "ticker": ticker,
        "company_name": profile["industry_profile"]["supported_tickers"].get(ticker),
        "as_of": as_of.isoformat(),
        "periods": periods,
        "groups": ["A", "B", "C", "D", "E", "F"],
        "business": business,
        "metrics": metrics,
        "governance": governance,
        "events": events,
        "peers": peers,
        "evidence": evidence,
        "coverage_requirements": coverage_requirements,
        "question_routes": question_routes,
        "step_groups": profile["step_groups"],
        "catalog": catalog,
        "conflicts": sorted(conflicts, key=lambda item: item["conflict_id"]),
        "limitations": [
            "research_input_not_investment_conclusion",
            "strict_historical_publication_not_proven_for_current_vendor_series",
            "source_text_available_does_not_mean_question_answered",
            "bank_insurance_brokerage_unknown_industry_profile_not_validated",
        ]
        + [
            (
                "post_annual_event_catalog_not_verified"
                if catalog["post_annual_event_catalog_state"] != "ready"
                else "post_annual_event_catalog_refreshed_bodies_require_triggered_review"
            )
        ],
        "model_reading_instruction": "先读核心包；只能引用已有事实；缺证据按ID查原文；假设和结论另列；不自动评级。",
    }
    markdown = _render_markdown(payload)
    token_count = _count_tokens(markdown)
    budget = int(max_tokens or profile["budget"]["default_max_tokens"])
    omissions = []
    if token_count["count"] > budget:
        markdown = _render_markdown(payload, compact=True)
        token_count = _count_tokens(markdown)
        omissions.append(
            {
                "kind": "optional_detail_compacted",
                "reason": "token_budget",
                "retained_in": "core-pack.json/evidence-index.jsonl",
            }
        )
    status = "ready" if token_count["count"] <= budget else "budget_exceeded"
    if status == "budget_exceeded":
        markdown = (
            "> 状态：`budget_exceeded`。必保数字、风险、冲突、缺口和引用未被静默删除；"
            "请提高预算或先处理缺口，不要把本包视为预算内默认模型输入。\n\n"
            + markdown
        )
        token_count = _count_tokens(markdown)
    payload["status"] = status
    payload["token_count"] = token_count
    payload["token_budget"] = budget
    payload["omissions"] = omissions
    coverage = {
        "profile_id": profile_id,
        "profile_sha256": profile["content_sha256"],
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "periods": periods,
        "requirements": coverage_requirements,
        "question_routes": question_routes,
        "counts": dict(Counter(item["state"] for item in coverage_requirements)),
        "question_quality_counts": dict(Counter(item["quality_state"] for item in question_routes)),
        "full_requirement_count": len(
            {ref for item in question_routes for ref in item["full_requirement_refs"]}
        ),
        "question_count": len(question_routes),
        "route_counts": dict(Counter(item["scope_route"] for item in question_routes)),
        "full_coverage_preserved": bool(detailed["summary"]["full_coverage_preserved"]),
        "full_coverage_summary": detailed["summary"],
    }
    pack_dir.mkdir(parents=True, exist_ok=True)
    _write_text(pack_dir / "core-pack.md", markdown)
    _write_json(pack_dir / "core-pack.json", payload)
    _write_json(pack_dir / "core-coverage.json", coverage)
    _write_jsonl(pack_dir / "question-coverage.jsonl", coverage_rows)
    _write_jsonl(pack_dir / "evidence-index.jsonl", evidence)
    _write_json(pack_dir / "next-work.json", next_work)
    output_names = (
        "core-pack.md",
        "core-pack.json",
        "core-coverage.json",
        "question-coverage.jsonl",
        "evidence-index.jsonl",
        "next-work.json",
    )
    output_hashes = {name: _hash_file(pack_dir / name) for name in output_names}
    manifest = {
        "pack_version": PACK_VERSION,
        "pack_id": pack_id,
        "pack_identity_hash": identity_hash,
        "status": status,
        "profile_id": profile_id,
        "profile_sha256": profile["content_sha256"],
        "selection_version": PACK_VERSION,
        "formula_version": FORMULA_VERSION,
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "periods": periods,
        "source_inputs": source_inputs,
        "auxiliary_inputs": auxiliary_inputs,
        "peer_inputs": peer_inputs,
        "source_namespace_ids": sorted(
            {namespace for source in source_inputs for namespace in source["namespaces"]}
        ),
        "cross_projection_policy": "explicit roles; no namespace rebinding; primary wins only with conflict retained",
        "output_hashes": output_hashes,
        "token_count": token_count,
        "token_budget": budget,
        "omissions": omissions,
        "omission_count": len(omissions),
        "selected_fact_count": sum(item["state"] == "ready" for item in metric_coverage),
        "selected_evidence_count": len(evidence),
        "conflict_count": len(conflicts),
        "unresolved_conflict_count": sum(not item.get("resolved") for item in conflicts),
        "request_statistics": {
            "pack_build_network_requests": 0,
            "cache_replay": True,
            "referenced_source_requests": sum(item["referenced_request_count"] for item in auxiliary_inputs),
            "referenced_uncached_source_requests": sum(item["referenced_uncached_request_count"] for item in auxiliary_inputs),
        },
        "audit_time_location": str(audit_path.resolve()),
        "wall_clock_excluded_from_pack_hash": True,
    }
    _write_json(pack_dir / "manifest.json", manifest)
    _write_json(
        audit_path,
        {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "pack_id": pack_id,
            "cache_reused": False,
            "performed_network_io": False,
        },
    )
    return {
        "status": status,
        "pack_id": pack_id,
        "pack_dir": str(pack_dir.resolve()),
        "cache_reused": False,
        "token_count": token_count,
        "performed_network_io": False,
        "coverage_counts": coverage["counts"],
        "question_quality_counts": coverage["question_quality_counts"],
        "next_work_items": len(next_work["items"]),
    }


def _split_utf8(text: str, byte_limit: int) -> list[str]:
    chunks = []
    current = []
    size = 0
    for character in text:
        width = len(character.encode("utf-8"))
        if current and size + width > byte_limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def read_evidence(
    *,
    pack_dir: Path,
    evidence_id: str,
    page: int = 1,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    budget = int(max_tokens or load_research_profile(manifest["profile_id"])["budget"]["evidence_page_tokens"])
    if budget <= 0 or page <= 0:
        raise ValueError("evidence_page_and_budget_must_be_positive")
    index = next(
        (item for item in _read_jsonl(pack_dir / "evidence-index.jsonl") if item.get("evidence_id") == evidence_id),
        None,
    )
    if index is None:
        raise KeyError(f"unknown_evidence_id:{evidence_id}")
    source_path = Path(index["source_evidence_path"])
    source = next(
        (item for item in _read_jsonl(source_path) if item.get("evidence_id") == evidence_id),
        None,
    )
    if source is None:
        raise ValueError("evidence_source_record_missing")
    text = str(source.get("text") or "")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != index["text_sha256"]:
        raise ValueError("evidence_text_hash_mismatch")
    original_path = Path(index["original_path"]) if index.get("original_path") else None
    original_verified = bool(
        original_path
        and original_path.is_file()
        and _hash_file(original_path) == index.get("original_sha256")
    )
    chunks = _split_utf8(text, budget * 2)
    if page > len(chunks):
        raise ValueError("evidence_page_out_of_range")
    content = chunks[page - 1]
    return {
        "evidence_id": evidence_id,
        "page": page,
        "total_pages": len(chunks),
        "truncated": page < len(chunks),
        "next_page": page + 1 if page < len(chunks) else None,
        "token_count": _count_tokens(content),
        "content": content,
        "source_url": index.get("source_url"),
        "original_path": index.get("original_path"),
        "original_sha256": index.get("original_sha256"),
        "original_hash_verified": original_verified,
        "locator": index.get("locator"),
        "parser_version": index.get("parser_version"),
        "semantic_status": index.get("semantic_status"),
        "question_answer_status": index.get("question_answer_status"),
    }
