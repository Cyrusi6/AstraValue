"""Snapshot-bound Python exploration; all user code runs in the isolated runtime.

An execution is not a financial formula registration. Numerical explorations need
an independent implementation and synthetic boundary checks before report use.
Presentation charts instead require the separate image/data review in Charts.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import re
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import python_sandbox
from .workspace import ResearchError, ResearchWorkspace, digest, encode, read_json, sha


MAX_CODE = 32000
MAX_INPUT_ROWS = 2000
MAX_FAILURES = 3
VALIDATION_SECONDS = 240
PERIOD_TYPES = {"cumulative", "single_quarter", "instant", "current", "ttm", "ratio"}


def _text(value: Any, name: str, maximum: int = 3000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ResearchError(f"python_{name}_required_or_too_long")
    return value.strip()


def _json_value(value: Any, *, max_bytes: int = 1_000_000):
    """Reject non-JSON/nonfinite content before it reaches an artifact or worker."""
    try:
        body = encode(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResearchError("python_finite_json_required") from exc
    if len(body.encode("utf-8")) > max_bytes:
        raise ResearchError("python_json_budget_exceeded")
    def check(item, depth=0):
        if depth > 20:
            raise ResearchError("python_json_too_deep")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ResearchError("python_json_string_keys_required")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif isinstance(item, str) and item.strip().lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            raise ResearchError("python_nonfinite_result")
    check(value)
    return value


def _code_signature(code: str, *, ignore_docstrings: bool = False) -> str:
    _text(code, "code", MAX_CODE)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ResearchError("python_code_syntax_error") from exc
    # AST normalization ignores comments/whitespace. Docstrings can be read by
    # executable Python, so execution identity preserves them; independence does not.
    class RemoveStrings(ast.NodeTransformer):
        def visit_Expr(self, node):
            return None if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else self.generic_visit(node)
    return ast.dump(RemoveStrings().visit(tree) if ignore_docstrings else tree, include_attributes=False)


def _numeric(value):
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        raise ResearchError("python_nonfinite_result")
    return number


def _units(value):
    if isinstance(value, str):
        return _text(value, "output_unit", 200)
    if not isinstance(value, dict) or not 1 <= len(value) <= 100:
        raise ResearchError("python_output_unit_requires_string_or_field_mapping")
    for path, unit in value.items():
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*", path):
            raise ResearchError("python_output_unit_field_path_invalid")
        _text(unit, "output_field_unit", 200)
    return value


def _numeric_paths(value, prefix=""):
    if isinstance(value, dict):
        return [path for key, item in value.items() for path in _numeric_paths(item, prefix + ("." if prefix else "") + key)]
    if isinstance(value, list):
        return [path for index, item in enumerate(value) for path in _numeric_paths(item, prefix + "." + str(index))]
    return [prefix] if _numeric(value) is not None else []


def compare_results(actual, expected, relative_tolerance=1e-9, absolute_tolerance=1e-12):
    """Compare the entire JSON tree, including keys, order, labels, units, and nulls."""
    rel, absolute = Decimal(str(relative_tolerance)), Decimal(str(absolute_tolerance))
    if not rel.is_finite() or not absolute.is_finite() or not 0 <= rel <= Decimal("0.000001") or not 0 <= absolute <= Decimal("0.000001"):
        raise ResearchError("python_validation_tolerance_out_of_range")
    _json_value(actual); _json_value(expected)
    mismatches = []
    def visit(a, b, path):
        if len(mismatches) >= 12:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a) != set(b):
                mismatches.append({"path": path, "reason": "keys_differ"}); return
            for key in sorted(a):
                visit(a[key], b[key], path + "." + key)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                mismatches.append({"path": path, "reason": "length_differ"}); return
            for index, (aa, bb) in enumerate(zip(a, b)):
                visit(aa, bb, f"{path}[{index}]")
        else:
            aa, bb = _numeric(a), _numeric(b)
            if aa is not None and bb is not None:
                equal = abs(aa - bb) <= max(absolute, rel * max(abs(aa), abs(bb)))
            else:
                equal = type(a) is type(b) and a == b
            if not equal:
                mismatches.append({"path": path, "reason": "value_or_type_differ"})
    visit(actual, expected, "result")
    return {"passed": not mismatches, "mismatches": mismatches}


def _find(w, rid, kind, ident):
    value = next((item for item in w.artifacts(rid, kind) if item["artifact_id"] == ident), None)
    if value is None:
        raise ResearchError("unknown_active_python_" + kind)
    if ident != kind + "_" + digest({key: val for key, val in value.items() if key != "artifact_id"})[:24]:
        raise ResearchError("python_artifact_record_integrity_failed")
    return value


def _artifact(w, rid, kind, payload, snapshot_id):
    """Publish only to the snapshot selected before execution, including concurrent updates."""
    state, _, _ = w.pack(rid)
    if state["snapshot_id"] != snapshot_id:
        raise ResearchError("python_snapshot_changed_during_execution")
    value = {**payload, "research_id": rid, "snapshot_id": snapshot_id}
    ident = kind + "_" + digest(value)[:24]
    with w.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        live = con.execute("SELECT state_json FROM research_tasks WHERE id=?", (rid,)).fetchone()
        if json.loads(live[0])["snapshot_id"] != snapshot_id:
            raise ResearchError("python_snapshot_changed_during_artifact_write")
        con.execute("INSERT OR IGNORE INTO research_artifacts VALUES(?,?,?,?,?)", (ident, rid, kind, snapshot_id, encode(value)))
    return {"artifact_id": ident, **value}


def _verify_calculation(w, rid, item):
    if item.get("validation") not in {"deterministic_calculation", "source_hash_and_pdf_cells_and_formula_checks"} or item.get("status") not in {"ready", "ready_with_gaps"}:
        raise ResearchError("python_formal_calculation_required")
    if item.get("source_hashes"):
        from .catalog import Catalog
        _, catalog = Catalog(w)._load(rid)
        originals = {row["material_id"]: row for row in catalog["items"]}
        for ref, expected in item["source_hashes"].items():
            source = originals.get(ref, {}).get("payload", {})
            if source.get("sha256") != expected or not source.get("path") or sha(Path(source["path"])) != expected:
                raise ResearchError("python_calculation_original_source_changed")


def _check_files(files):
    if not isinstance(files, dict) or not files:
        raise ResearchError("python_output_files_missing")
    for name, item in files.items():
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise ResearchError("python_output_file_contract_invalid")
        path = Path(item["path"])
        if not path.is_file() or sha(path) != item["sha256"]:
            raise ResearchError("python_artifact_integrity_failed:" + name)


def _source_state(w, rid, exploration, seen=None):
    state, _, _ = w.pack(rid)
    if state["snapshot_id"] != exploration["snapshot_id"]:
        raise ResearchError("python_snapshot_changed")
    _check_files(exploration["files"])
    if sha(Path(exploration["input_path"])) != exploration["input_sha256"] or sha(Path(exploration["code_path"])) != exploration["code_sha256"]:
        raise ResearchError("python_input_or_code_integrity_failed")
    data = read_json(Path(exploration["input_path"]))
    if digest(data) != exploration["input_digest"]:
        raise ResearchError("python_input_integrity_failed")
    result_name = "result.json" if exploration["mode"] == "calculation" else "chart-data.json"
    result = read_json(Path(exploration["files"][result_name]["path"]))
    if digest(result) != digest(exploration["result"]):
        raise ResearchError("python_result_integrity_failed")
    seen = set(seen or ())
    if exploration["artifact_id"] in seen or len(seen) >= 12:
        raise ResearchError("python_exploration_dependency_cycle_or_depth")
    seen.add(exploration["artifact_id"])
    for ident in exploration["provenance"]["calculation_ids"]:
        calculation = _find(w, rid, "calculation", ident)
        _verify_calculation(w, rid, calculation)
        if digest(calculation) != exploration["provenance"]["calculation_hashes"][ident]:
            raise ResearchError("python_calculation_source_changed")
    for ident in exploration["provenance"]["exploration_ids"]:
        source = get_validated_exploration(w, rid, ident, _seen=seen)
        if source["validation_id"] != exploration["provenance"]["exploration_validation_ids"][ident]:
            raise ResearchError("python_source_validation_changed")
    return {**exploration, "code": Path(exploration["code_path"]).read_text(encoding="utf-8"), "input_data": data}


def get_exploration(w, research_id, exploration_id, *, _seen=None):
    """Read a successful exploration after verifying files and active source bindings."""
    value = _find(w, research_id, "exploration", exploration_id)
    if value.get("status") != "succeeded":
        raise ResearchError("python_exploration_not_succeeded")
    return _source_state(w, research_id, value, _seen)


def get_validated_exploration(w, research_id, exploration_id, *, _seen=None):
    """Only numerical, independently validated exploration is usable as report evidence."""
    value = get_exploration(w, research_id, exploration_id, _seen=_seen)
    if value["mode"] != "calculation":
        raise ResearchError("python_chart_is_not_numeric_evidence")
    validations = [x for x in w.artifacts(research_id, "exploration_validation") if x.get("exploration_id") == exploration_id]
    if not validations or validations[-1].get("status") != "passed":
        raise ResearchError("python_exploration_validation_required")
    validation = _find(w, research_id, "exploration_validation", validations[-1]["artifact_id"])
    for field in ("input_sha256", "code_sha256", "result_sha256"):
        if validation.get(field) != value.get(field):
            raise ResearchError("python_exploration_validation_stale")
    hydrated_executions = []
    for execution in validation["executions"]:
        _check_files(execution["files"])
        hydrated = dict(execution)
        for name, field in (("code.py", "code"), ("input.json", "input_data"), ("result.json", "result")):
            if name in execution["files"]:
                path = Path(execution["files"][name]["path"])
                hydrated[field] = path.read_text(encoding="utf-8") if name.endswith(".py") else read_json(path)
        hydrated_executions.append(hydrated)
    validation = {**validation, "executions": hydrated_executions,
                  "validation_code": next((item.get("code") for item in hydrated_executions if item["name"] == "independent"), None)}
    return {**value, "validation_status": "validated", "validation_id": validation["artifact_id"],
            "validation": validation,
            "validation_scope": "independent_numeric_and_boundary_checks; exploratory_not_registered_formula",
            "promotion_state": "not_promoted"}


class CustomPython:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def _select(self, research_id, inputs, assumptions):
        state, _, pack = self.w.pack(research_id)
        if not isinstance(inputs, dict) or not 1 <= len(inputs) <= 12:
            raise ResearchError("python_named_input_selectors_required")
        assumptions = assumptions or {}
        if not isinstance(assumptions, dict) or len(assumptions) > 30:
            raise ResearchError("python_assumptions_object_required")
        for name, item in assumptions.items():
            if not isinstance(item, dict) or "value" not in item or set(item) - {"value", "reason", "unit"}:
                raise ResearchError("python_assumption_requires_value_reason_optional_unit")
            _text(item.get("reason"), "assumption_reason")
        _json_value(assumptions)
        facts = {item["fact_ref"]: item for item in pack["metrics"] if item.get("fact_ref")}
        tables, fact_refs, calculation_hashes, exploration_validations = {}, set(), {}, {}
        row_count = 0
        for alias, selector in inputs.items():
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", alias):
                raise ResearchError("python_input_alias_invalid")
            if not isinstance(selector, dict):
                raise ResearchError("python_selector_object_required")
            kinds = set(selector) & {"fact_refs", "metric_ids", "calculation_ids", "exploration_ids"}
            if len(kinds) != 1:
                raise ResearchError("python_selector_requires_exactly_one_reference_type")
            kind = next(iter(kinds))
            allowed = {kind, "periods", "period_type"} if kind == "metric_ids" else {kind}
            if set(selector) - allowed:
                raise ResearchError("python_raw_or_unknown_input_selector_forbidden")
            refs = selector[kind]
            if not isinstance(refs, list) or not refs or len(refs) > MAX_INPUT_ROWS or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ResearchError("python_nonempty_reference_list_required")
            if len(set(refs)) != len(refs):
                raise ResearchError("python_duplicate_references")
            if kind in {"fact_refs", "metric_ids"}:
                if kind == "fact_refs":
                    if set(refs) - set(facts):
                        raise ResearchError("python_unknown_current_fact_reference")
                    selected = [facts[ref] for ref in refs]
                else:
                    periods, period_type = selector.get("periods"), selector.get("period_type")
                    if not isinstance(periods, list) or not periods or any(not isinstance(p, str) for p in periods) or len(set(periods)) != len(periods) or period_type not in PERIOD_TYPES:
                        raise ResearchError("python_metric_selector_requires_periods_and_period_type")
                    selected = []
                    for metric in refs:
                        for period in periods:
                            matches = [item for item in facts.values() if item.get("metric_id") == metric and item.get("period") == period and item.get("period_type") == period_type]
                            if len(matches) != 1:
                                raise ResearchError("python_metric_period_missing_or_ambiguous:" + metric + ":" + period)
                            selected.append(matches[0])
                rows = []
                for item in selected:
                    fact = item.get("fact")
                    if item.get("state") != "ready" or not fact or fact.get("value") is None:
                        raise ResearchError("python_selected_fact_not_formal_ready")
                    if _numeric(fact["value"]) is None or not fact.get("unit"):
                        raise ResearchError("python_selected_fact_numeric_value_and_unit_required")
                    ref = item["fact_ref"]
                    rows.append({"fact_ref": ref, "fact_id": fact["fact_id"], "metric_id": item["metric_id"],
                                 "label": item.get("label", item["metric_id"]), "period": item["period"],
                                 "period_type": item["period_type"], "value": fact["value"], "unit": fact["unit"],
                                 "currency": fact.get("currency"), "scope": fact.get("scope"),
                                 "source_hash": digest(item)})
                    fact_refs.add(ref)
                rows.sort(key=lambda item: (item["period"], item["metric_id"], item["fact_ref"]))
                tables[alias] = {"kind": "facts", "rows": rows}
            elif kind == "calculation_ids":
                rows = []
                for ident in refs:
                    item = _find(self.w, research_id, "calculation", ident)
                    _verify_calculation(self.w, research_id, item)
                    calculation_hashes[ident] = digest(item)
                    rows.append({key: item.get(key) for key in ("artifact_id", "method", "formula_version", "result", "input_definitions", "input_fact_ids", "assumptions")})
                tables[alias] = {"kind": "calculations", "rows": rows}
            else:
                rows = []
                for ident in refs:
                    item = get_validated_exploration(self.w, research_id, ident)
                    exploration_validations[ident] = item["validation_id"]
                    rows.append({key: item[key] for key in ("artifact_id", "result", "definition", "applicability", "output_unit", "output_period", "validation_id", "promotion_state")})
                tables[alias] = {"kind": "validated_explorations", "rows": rows}
            row_count += len(tables[alias]["rows"])
        if row_count > MAX_INPUT_ROWS:
            raise ResearchError("python_input_row_budget_exceeded")
        data = {"snapshot_id": state["snapshot_id"], "as_of": state["as_of"], "tables": tables, "assumptions": assumptions}
        _json_value(data, max_bytes=4_000_000)
        provenance = {"snapshot_id": state["snapshot_id"], "manifest_sha256": state["manifest_sha256"],
                      "input_fact_refs": sorted(fact_refs), "calculation_ids": sorted(calculation_hashes),
                      "calculation_hashes": calculation_hashes, "exploration_ids": sorted(exploration_validations),
                      "exploration_validation_ids": exploration_validations}
        return state, data, provenance

    @contextmanager
    def _lock(self, directory: Path, *, retry=False):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "execution.lock"
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            if retry and time.time() - path.stat().st_mtime > VALIDATION_SECONDS + 120:
                path.unlink()
                handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            else:
                raise ResearchError("python_execution_in_progress_or_requires_expired_lock_retry")
        try:
            os.write(handle, str(os.getpid()).encode()); os.close(handle)
            yield
        finally:
            path.unlink(missing_ok=True)

    def _execute(self, code, data, mode, directory, *, timeout_seconds=30):
        outcome = python_sandbox.run_python(code, data, mode=mode, output_dir=directory,
                                            limits={"timeout_seconds": timeout_seconds})
        if outcome.get("status") == "succeeded":
            try:
                files = outcome.get("files", {})
                names = {"code.py", "input.json", "run.json", "result.json" if mode == "calculation" else "chart-data.json"}
                if mode == "chart":
                    names.add("figure.png")
                if names - files.keys():
                    raise ResearchError("python_runtime_output_contract_incomplete")
                # Check paths before reading them; only this attempt's outputs are admissible.
                root = directory.resolve()
                if any(not Path(item["path"]).resolve().is_relative_to(root) for item in files.values()):
                    raise ResearchError("python_runtime_output_outside_attempt")
                _check_files(files)
                _json_value(read_json(Path(files["result.json" if mode == "calculation" else "chart-data.json"]["path"])))
                if read_json(Path(files["input.json"]["path"])) != data:
                    raise ResearchError("python_runtime_changed_input")
                if Path(files["code.py"]["path"]).read_text(encoding="utf-8") != code:
                    raise ResearchError("python_runtime_changed_code")
            except (ResearchError, OSError, KeyError, ValueError) as exc:
                # Rejected exports are durable failed attempts, not an exception loophole in the budget.
                outcome.update(status="failed", retryable=False,
                               error={"code": "output_contract_rejected", "message": str(exc), "exception_type": type(exc).__name__})
                outcome["files"] = {name: item for name, item in outcome.get("files", {}).items()
                                    if isinstance(item, dict) and item.get("path") and Path(item["path"]).resolve().is_relative_to(directory.resolve())}
        return outcome

    def run_python_analysis(self, research_id: str, purpose: str, code: str, inputs: dict,
                            mode: str = "calculation", assumptions: dict | None = None,
                            definition: str | None = None, applicability: str | None = None,
                            output_unit: str | dict[str, str] | None = None, output_period: str | None = None,
                            retry: bool = False):
        """Execute snapshot selectors, with model assumptions separate from historical facts."""
        purpose = _text(purpose, "purpose"); signature = _code_signature(code)
        if mode not in {"chart", "calculation"}:
            raise ResearchError("python_mode_requires_chart_or_calculation")
        if mode == "calculation":
            definition = _text(definition, "definition")
            applicability = _text(applicability, "applicability")
            output_unit = _units(output_unit)
            output_period = _text(output_period, "output_period", 200)
        state, data, provenance = self._select(research_id, inputs, assumptions)
        execution_key = digest([research_id, state["snapshot_id"], signature, data, mode])
        # Definitions are semantic; correcting units creates a new unvalidated analysis.
        # Their failures still share the same implementation/input execution budget.
        # Python may inspect its own text/line numbers. Result caching therefore
        # binds exact source bytes; only failure accounting uses normalized AST.
        key = digest([execution_key, digest(code), definition, applicability, output_unit, output_period])
        analysis_id = "python_analysis_" + key[:24]
        # Cosmetic description changes cannot bypass the failure budget or repeat work.
        directory = self.w.state / "custom-python" / analysis_id
        with self._lock(directory, retry=retry):
            attempts = [item for item in self.w.artifacts(research_id, "python_run") if item.get("analysis_id") == analysis_id]
            if attempts and (attempts[-1]["status"] == "succeeded" or not retry):
                return self.get_python_analysis(research_id, analysis_id)
            related = [item for item in self.w.artifacts(research_id, "python_run") if item.get("execution_key") == execution_key]
            if sum(item["status"] in {"failed", "timed_out", "resource_limited"} for item in related) >= MAX_FAILURES:
                raise ResearchError("python_same_code_failure_budget_exhausted:revise_code_or_inputs")
            attempt = len(attempts) + 1
            attempt_dir = directory / ("attempt-" + str(attempt))
            # Interrupted work has no outcome artifact. Preserve its files and use a new slot.
            while attempt_dir.exists():
                attempt += 1; attempt_dir = directory / ("attempt-" + str(attempt))
            outcome = self._execute(code, data, mode, attempt_dir)
            live, _, _ = self.w.pack(research_id)
            if live["snapshot_id"] != state["snapshot_id"]:
                raise ResearchError("python_snapshot_changed_during_execution")
            exploration_id = chart_id = None
            if outcome["status"] == "succeeded":
                files = outcome["files"]
                result_name = "result.json" if mode == "calculation" else "chart-data.json"
                value = {"analysis_id": analysis_id, "status": "succeeded", "mode": mode, "purpose": purpose,
                         "definition": definition or purpose, "applicability": applicability,
                         "output_unit": output_unit, "output_period": output_period,
                         "inputs": inputs, "assumptions": assumptions or {}, "provenance": provenance,
                         "result": read_json(Path(files[result_name]["path"])), "files": files,
                         "input_path": files["input.json"]["path"], "input_sha256": files["input.json"]["sha256"],
                         "input_digest": digest(data), "code_path": files["code.py"]["path"],
                         "code_sha256": files["code.py"]["sha256"], "result_sha256": files[result_name]["sha256"],
                         "execution": outcome.get("execution", {}), "validation_status": "unverified",
                         "promotion_state": "not_promoted"}
                exploration = _artifact(self.w, research_id, "exploration", value, state["snapshot_id"])
                exploration_id = exploration["artifact_id"]
                if mode == "chart":
                    image = files["figure.png"]
                    chart = _artifact(self.w, research_id, "chart", {"template": "custom_python", "title": definition or purpose,
                        "template_version": "custom-python-v1", "exploration_id": exploration_id,
                        "data": value["result"], "files": files, "provenance": provenance,
                        "image_path": image["path"], "image_sha256": image["sha256"],
                        "input_sha256": value["input_sha256"], "code_sha256": value["code_sha256"],
                        "validation": "data_bound; visual_review_required"}, state["snapshot_id"])
                    chart_id = chart["artifact_id"]
            _artifact(self.w, research_id, "python_run", {"analysis_id": analysis_id, "attempt": attempt,
                "execution_key": execution_key,
                "mode": mode, "purpose": purpose, "status": outcome["status"], "files": outcome.get("files", {}),
                "error": outcome.get("error"), "execution": outcome.get("execution", {}),
                "retryable": outcome.get("retryable", outcome["status"] != "succeeded"),
                "exploration_id": exploration_id, "chart_id": chart_id,
                "input_digest": digest(data), "code_digest": digest(code)}, state["snapshot_id"])
        return self.get_python_analysis(research_id, analysis_id)

    def get_exploration(self, research_id: str, exploration_id: str):
        return get_exploration(self.w, research_id, exploration_id)

    def get_validated_exploration(self, research_id: str, exploration_id: str):
        return get_validated_exploration(self.w, research_id, exploration_id)

    def get_python_analysis(self, research_id: str, analysis_id: str, include_code: bool = False,
                            page: int = 1, page_size: int = 30,
                            result_path: list[str | int] | None = None):
        if page < 1 or not 1 <= page_size <= 100:
            raise ResearchError("python_invalid_result_pagination")
        runs = self.w.artifacts(research_id, "python_run")
        if analysis_id.startswith("exploration_"):
            matching = [item for item in runs if item.get("exploration_id") == analysis_id]
        else:
            matching = [item for item in runs if item.get("analysis_id") == analysis_id]
        if not matching:
            raise ResearchError("unknown_active_python_analysis")
        run = _find(self.w, research_id, "python_run", matching[-1]["artifact_id"])
        result = {key: run.get(key) for key in ("analysis_id", "research_id", "snapshot_id", "attempt", "mode", "purpose", "status", "error", "retryable", "exploration_id", "chart_id")}
        result["execution"] = {key: run.get("execution", {}).get(key) for key in ("runtime", "image_id", "image_digest", "limits", "elapsed_seconds", "exit_code", "input_unchanged")}
        result["failure_count"] = sum(item["status"] in {"failed", "timed_out", "resource_limited"} for item in runs if item.get("execution_key", item.get("analysis_id")) == run.get("execution_key", run["analysis_id"]))
        result["failure_budget"] = MAX_FAILURES
        if run.get("exploration_id"):
            exploration = get_exploration(self.w, research_id, run["exploration_id"])
            validations = [x for x in self.w.artifacts(research_id, "exploration_validation") if x.get("exploration_id") == exploration["artifact_id"]]
            if validations and validations[-1]["status"] == "passed":
                exploration = get_validated_exploration(self.w, research_id, exploration["artifact_id"])
            elif validations:
                latest = _find(self.w, research_id, "exploration_validation", validations[-1]["artifact_id"])
                exploration = {**exploration, "validation_status": "failed", "validation_id": latest["artifact_id"]}
            result.update({key: exploration.get(key) for key in ("definition", "applicability", "output_unit", "output_period", "validation_status", "validation_id", "promotion_state", "provenance")})
            value = exploration["result"]
            path = result_path or []
            if not isinstance(path, list) or len(path) > 20 or any(not isinstance(key, (str, int)) or isinstance(key, bool) for key in path):
                raise ResearchError("python_result_path_invalid")
            try:
                for part in path:
                    if isinstance(part, int) and part < 0:
                        raise IndexError()
                    value = value[part]
            except (KeyError, IndexError, TypeError):
                raise ResearchError("python_result_path_missing") from None
            rows = list(value.items()) if isinstance(value, dict) else list(enumerate(value)) if isinstance(value, list) else [("value", value)]
            result.update(result_entries=[{"key": key, "value": val} for key, val in rows[(page-1)*page_size:page*page_size]],
                          result_path=path, result_count=len(rows), page=page, next_page=page+1 if page*page_size < len(rows) else None,
                          files={name: {key: item[key] for key in ("sha256", "size_bytes") if key in item} for name, item in exploration["files"].items()})
            # Large matrix/list fields remain available via a bounded nested page.
            for entry in result["result_entries"]:
                if len(encode(entry["value"])) > max(240, 20000 // page_size):
                    entry["value"] = {"summary": "large_structured_result", "sha256": digest(entry["value"]),
                                      "item_count": len(entry["value"]) if isinstance(entry["value"], (dict, list)) else None,
                                      "read_path": [*path, entry["key"]]}
            if include_code:
                result["code"] = Path(exploration["code_path"]).read_text(encoding="utf-8")
        else:
            result["next_action"] = "retry=true_after_fixing_runtime" if run["status"] == "unavailable" else "inspect_error_then_retry_or_revise_code"
            if include_code and run.get("files", {}).get("code.py"):
                _check_files(run["files"])
                result["code"] = Path(run["files"]["code.py"]["path"]).read_text(encoding="utf-8")
        return result

    def _boundary_data(self, data, case):
        if not isinstance(case, dict) or set(case) - {"name", "reason", "overrides", "expected_result", "expected_error"}:
            raise ResearchError("python_boundary_case_contract_invalid")
        _text(case.get("name"), "boundary_name", 100); _text(case.get("reason"), "boundary_reason")
        if ("expected_result" in case) == ("expected_error" in case):
            raise ResearchError("python_boundary_requires_result_or_error")
        if "expected_error" in case and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", str(case["expected_error"])):
            raise ResearchError("python_boundary_exact_exception_type_required")
        if "expected_result" in case:
            _json_value(case["expected_result"])
        overrides = case.get("overrides")
        if not isinstance(overrides, list) or not 1 <= len(overrides) <= MAX_INPUT_ROWS:
            raise ResearchError("python_boundary_nonempty_bounded_overrides_required")
        value = copy.deepcopy(data)
        seen = set()
        for override in overrides:
            if not isinstance(override, dict) or set(override) != {"path", "value"}:
                raise ResearchError("python_boundary_override_contract_invalid")
            path = override["path"]
            if not isinstance(path, list) or not path or any(not isinstance(x, (str, int)) or isinstance(x, bool) for x in path):
                raise ResearchError("python_boundary_override_path_invalid")
            key = encode(path)
            if key in seen:
                raise ResearchError("python_duplicate_boundary_override")
            seen.add(key)
            table_value = (len(path) == 5 and path[0] == "tables" and path[2] == "rows" and path[-1] == "value" and isinstance(path[3], int) and path[3] >= 0)
            computed_value = (len(path) >= 6 and path[0] == "tables" and path[2] == "rows" and path[4] == "result" and isinstance(path[3], int) and path[3] >= 0)
            assumption_value = len(path) >= 3 and path[0] == "assumptions" and path[2] == "value"
            if not table_value and not assumption_value and not computed_value:
                raise ResearchError("python_boundary_may_only_override_values_not_units_periods_or_provenance")
            try:
                current = value
                for part in path[:-1]:
                    current = current[part]
                original = current[path[-1]]  # Require an existing value; no invented input fields.
                if computed_value and _numeric(original) is None:
                    raise ResearchError("python_boundary_computed_override_requires_numeric_leaf")
                current[path[-1]] = override["value"]
            except (KeyError, IndexError, TypeError):
                raise ResearchError("python_boundary_override_path_missing") from None
        _json_value(value)
        if value == data:
            raise ResearchError("python_boundary_must_change_inputs")
        # Synthetic values live in this invocation only and are never registered as facts.
        return value

    def validate_python_analysis(self, research_id: str, analysis_id: str, validation_code: str,
                                 validation_reason: str, boundary_cases: list[dict],
                                 relative_tolerance: float = 1e-9, absolute_tolerance: float = 1e-12,
                                 retry: bool = False):
        """Independent reimplementation, reproducibility, and explicit synthetic edge tests."""
        run = self.get_python_analysis(research_id, analysis_id)
        exploration_id = run.get("exploration_id")
        if not exploration_id:
            raise ResearchError("python_successful_exploration_required")
        exploration = get_exploration(self.w, research_id, exploration_id)
        if exploration["mode"] != "calculation":
            raise ResearchError("python_chart_uses_visual_review_not_numeric_promotion")
        _text(validation_reason, "validation_reason")
        original_code = Path(exploration["code_path"]).read_text(encoding="utf-8")
        if _code_signature(validation_code, ignore_docstrings=True) == _code_signature(original_code, ignore_docstrings=True):
            raise ResearchError("python_independent_validation_implementation_required")
        if not any(isinstance(node, ast.Name) and node.id == "data" and isinstance(node.ctx, ast.Load) for node in ast.walk(ast.parse(validation_code))):
            raise ResearchError("python_verifier_must_use_selected_data")
        compare_results({}, {}, relative_tolerance, absolute_tolerance)
        if not isinstance(boundary_cases, list) or not 2 <= len(boundary_cases) <= 6 or any(not isinstance(case, dict) for case in boundary_cases):
            raise ResearchError("python_validation_requires_2_to_6_boundary_cases")
        if not any("expected_error" in case for case in boundary_cases) or not any("expected_result" in case for case in boundary_cases):
            raise ResearchError("python_validation_requires_success_and_failure_boundary")
        if len({case.get("name") for case in boundary_cases}) != len(boundary_cases):
            raise ResearchError("python_boundary_names_must_be_unique")
        data = read_json(Path(exploration["input_path"]))
        if isinstance(exploration["output_unit"], dict):
            missing_units = set(_numeric_paths(exploration["result"])) - exploration["output_unit"].keys()
            if missing_units:
                raise ResearchError("python_validation_output_units_missing:" + ",".join(sorted(missing_units)[:10]))
        input_definitions = []
        for alias, table in data["tables"].items():
            if table["kind"] == "facts":
                for item in table["rows"]:
                    if any(not item.get(field) for field in ("unit", "period", "period_type", "currency", "scope")):
                        raise ResearchError("python_validation_input_unit_period_currency_scope_required")
                    input_definitions.append({"table": alias, **{field: item[field] for field in ("fact_ref", "metric_id", "period", "period_type", "unit", "currency", "scope")}})
            else:
                input_definitions.append({"table": alias, "kind": table["kind"], "registered_or_validated_sources": [row["artifact_id"] for row in table["rows"]]})
        case_data = [self._boundary_data(data, case) for case in boundary_cases]
        boundary_values = [{key: val for key, val in case.items() if key not in {"name", "reason"}} for case in boundary_cases]
        budget_key = digest([exploration_id, _code_signature(validation_code), boundary_values, relative_tolerance, absolute_tolerance])
        key = digest([budget_key, digest(validation_code)])
        directory = self.w.state / "custom-python" / exploration["analysis_id"] / ("validation-" + key[:20])
        with self._lock(directory, retry=retry):
            all_validations = [v for v in self.w.artifacts(research_id, "exploration_validation") if v.get("exploration_id") == exploration_id]
            existing = [v for v in all_validations if v.get("validation_key") == key]
            if existing and (not retry or (existing[-1]["status"] == "passed" and existing[-1]["artifact_id"] == all_validations[-1]["artifact_id"])):
                # A later failed verifier revokes report eligibility. Explicit retry
                # reruns an earlier good verifier instead of being stuck on its cache.
                verified = _find(self.w, research_id, "exploration_validation", all_validations[-1]["artifact_id"])
                if verified["status"] == "passed":
                    get_validated_exploration(self.w, research_id, exploration_id)
                return self._validation_response(verified)
            related = [v for v in all_validations if v.get("validation_budget_key") == budget_key]
            if sum(v["status"] == "failed" for v in related) >= MAX_FAILURES:
                raise ResearchError("python_same_verifier_failure_budget_exhausted")
            attempt = len(existing) + 1
            root = directory / ("attempt-" + str(attempt))
            while root.exists():
                attempt += 1; root = directory / ("attempt-" + str(attempt))
            root.mkdir(parents=True)
            checks = [{"name": "input_and_output_definitions", "passed": True,
                       "input_count": len(input_definitions), "output_unit": exploration["output_unit"],
                       "output_period": exploration["output_period"],
                       "note": "Explicit source definitions preserved; economic interpretation remains the author's responsibility."}]
            executions = []
            deadline = time.monotonic() + VALIDATION_SECONDS
            def execute(label, code, values):
                remaining = int(deadline - time.monotonic())
                if remaining < 1:
                    raise ResearchError("python_validation_time_budget_exceeded")
                outcome = self._execute(code, values, "calculation", root / label, timeout_seconds=min(30, remaining))
                executions.append({"name": label, **outcome})
                return outcome
            def result_for(outcome):
                return read_json(Path(outcome["files"]["result.json"]["path"]))
            try:
                repeated = execute("repeat-original", original_code, data)
                check = {"name": "repeatability", "passed": repeated["status"] == "succeeded"}
                if check["passed"]:
                    check.update(compare_results(result_for(repeated), exploration["result"], relative_tolerance, absolute_tolerance))
                checks.append(check)
                if check["passed"]:
                    independent = execute("independent", validation_code, data)
                    check = {"name": "independent_calculation", "passed": independent["status"] == "succeeded"}
                    if check["passed"]:
                        check.update(compare_results(result_for(independent), exploration["result"], relative_tolerance, absolute_tolerance))
                    checks.append(check)
                if all(check["passed"] for check in checks):
                    for index, (case, synthetic) in enumerate(zip(boundary_cases, case_data)):
                        for variant, code in (("original", original_code), ("independent", validation_code)):
                            outcome = execute(f"boundary-{index}-{variant}", code, synthetic)
                            check = {"name": case["name"], "implementation": variant, "reason": case["reason"], "synthetic": True}
                            if "expected_error" in case:
                                error = outcome.get("error") or {}
                                check.update(passed=outcome["status"] == "failed" and error.get("code") == "python_error" and error.get("exception_type") == case["expected_error"], expected_error=case["expected_error"], actual_error=error.get("exception_type"))
                            else:
                                check["passed"] = outcome["status"] == "succeeded"
                                if check["passed"]:
                                    check.update(compare_results(result_for(outcome), case["expected_result"], relative_tolerance, absolute_tolerance))
                            checks.append(check)
                # Recheck sources and snapshot after all external executions, before publishing validation.
                get_exploration(self.w, research_id, exploration_id)
            except ResearchError as exc:
                checks.append({"name": "execution_or_integrity", "passed": False, "reason": str(exc)})
            passed = bool(checks) and all(check["passed"] for check in checks) and len(executions) == 2 + 2 * len(boundary_cases)
            value = _artifact(self.w, research_id, "exploration_validation", {
                "exploration_id": exploration_id, "analysis_id": exploration["analysis_id"], "validation_key": key,
                "validation_budget_key": budget_key,
                "attempt": attempt, "status": "passed" if passed else "failed", "reason": validation_reason,
                "checks": checks, "executions": executions, "boundary_cases": boundary_cases,
                "definition": exploration["definition"], "applicability": exploration["applicability"],
                "output_unit": exploration["output_unit"], "output_period": exploration["output_period"],
                "input_definitions": input_definitions,
                "input_sha256": exploration["input_sha256"], "code_sha256": exploration["code_sha256"],
                "result_sha256": exploration["result_sha256"], "validation_code_digest": digest(validation_code),
                "relative_tolerance": relative_tolerance, "absolute_tolerance": absolute_tolerance,
                "promotion_state": "not_promoted", "scope": "independent_numeric_and_boundary_checks"}, exploration["snapshot_id"])
        return self._validation_response(value)

    @staticmethod
    def _validation_response(value):
        return {"analysis_id": value["analysis_id"], "exploration_id": value["exploration_id"],
                "validation_id": value["artifact_id"], "validation_status": "validated" if value["status"] == "passed" else "failed",
                "promotion_state": "not_promoted", "checks": value["checks"],
                "execution_count": len(value["executions"]), "attempt": value["attempt"],
                "next_action": "may_cite_as_exploration_not_formal_metric" if value["status"] == "passed" else "inspect_checks_then_revise_or_retry"}
