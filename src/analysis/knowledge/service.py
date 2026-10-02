from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _bundle_hash(value: dict) -> str:
    """Hash content using the rule recorded in the immutable bundle.

    Unversioned bundles retain their original digest, including created_at.
    Version 2 separates stable content identity from whole-payload integrity.
    Never try a weaker hash as a fallback for a damaged legacy bundle.
    """
    normalized = copy.deepcopy(value)
    normalized.pop("content_sha256", None)
    version = normalized.get("bundle_hash_version")
    if version == 2:
        normalized.pop("created_at", None)
        normalized.pop("integrity_sha256", None)
    elif version is not None:
        raise ValueError(f"unsupported bundle hash version: {version}")
    return _hash(normalized)


def _bundle_integrity_hash(value: dict) -> str:
    normalized = copy.deepcopy(value)
    normalized.pop("integrity_sha256", None)
    return _hash(normalized)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".knowledge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}", value):
        raise ValueError("invalid knowledge identifier")
    return value


def _index(items: list[dict], key: str) -> dict:
    return {item[key]: item for item in items}


def _source_alias_index(catalog: dict) -> dict[tuple[str, str], tuple[str, str]]:
    """Return versioned historical-source aliases for a catalog.

    Aliases are intentionally scoped to a frozen catalog/bundle.  They make
    old manifests readable after duplicate source identities are collapsed,
    while new methods and bundles continue to carry the canonical identity.
    Older bundles that used ``original_source_id`` remain supported below.
    """
    return {
        (item["alias_source_id"], item["alias_version"]):
        (item["canonical_source_id"], item["canonical_version"])
        for item in catalog.get("source_aliases", [])
        if item.get("status") == "historical_alias"
        and item.get("scope") == "historical bundle/manifest only"
    }


def _canonical_source_key(catalog: dict, source_id: str, version: str) -> tuple[str, str]:
    return _source_alias_index(catalog).get((source_id, version), (source_id, version))


def _source_refs(method: dict, cases: dict) -> list[dict]:
    refs = [ref for rule in method.get("rules", []) for ref in rule.get("source_refs", [])]
    refs.extend(ref for cid in method.get("case_ids", []) for ref in cases.get(cid, {}).get("source_refs", []))
    return refs


def method_identity(catalog: dict, method_id: str, project_root: Path | str | None = None, metrics: dict | None = None, _seen: frozenset[str] = frozenset()) -> str:
    """Hash financial content and its actual dependencies, excluding review/status metadata."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    if method_id in _seen:
        raise ValueError("cyclic method content dependency")
    method = copy.deepcopy(_index(catalog.get("methods", []), "method_id")[method_id])
    method.pop("content_status", None)
    if "body" not in method:
        body_path = (root / method.get("body_path", "")).resolve()
        if not body_path.is_relative_to(root.resolve()):
            raise ValueError("body_path must remain in project root")
        method["body"] = body_path.read_text(encoding="utf-8") if body_path.is_file() else None
    metrics = metrics if metrics is not None else _load(root / "config/methods/metrics.json")
    cases = _index(catalog.get("cases", []), "case_id")
    selected_cases = {cid: cases.get(cid) for cid in method.get("case_ids", [])}
    refs = _source_refs(method, cases)
    source_keys = {_canonical_source_key(catalog, r.get("source_id"), r.get("version")) for r in refs}
    selected_sources = sorted((s for s in catalog.get("sources", []) if (s.get("source_id"), s.get("version")) in source_keys), key=lambda s: (s["source_id"], s["version"]))
    selected_metrics = {i["metric_id"]: metrics.get("metrics", {}).get(i["metric_id"]) for i in method.get("required_inputs", []) if i.get("metric_id")}
    payload = {"method": method, "sources": selected_sources, "cases": selected_cases, "metrics_version": metrics.get("schema_version"), "metrics": selected_metrics}
    if method.get("dependencies"):
        payload["dependency_identities"] = {dep: method_identity(catalog, dep, root, metrics, _seen | {method_id}) for dep in method["dependencies"]}
    return _hash(payload)


class KnowledgeService:
    """Local deterministic guidance service. No company facts or network IO."""

    def __init__(self, catalog_path: Path | str, store_root: Path | str):
        self.catalog_path = Path(catalog_path).resolve()
        self.project_root = self.catalog_path.parents[3]
        self.store_root = Path(store_root).resolve()
        self.catalog = _load(self.catalog_path)
        self.questions = _load(self.project_root / "config/structured_data/research_requirements.v1.json")
        if len(self.questions.get("questions", [])) != 54 or self.questions.get("question_set_version") != "1.0.0":
            raise ValueError("knowledge baseline must be the 54-question set 1.0.0")
        self.question_index = _index(self.questions["questions"], "question_id")

    @classmethod
    def from_catalog(cls, path: Path | str, store_root: Path | str) -> KnowledgeService:
        return cls(path, store_root)

    def _metrics(self) -> dict:
        return _load(self.project_root / "config/methods/metrics.json")

    def method_identity(self, method_id: str, catalog: dict | None = None) -> str:
        return method_identity(catalog if catalog is not None else self.catalog, method_id, self.project_root, self._metrics())

    def _freeze(self, catalog: dict) -> dict:
        catalog = copy.deepcopy(catalog)
        for method in catalog.get("methods", []):
            path = (self.project_root / method.get("body_path", "")).resolve()
            if not path.is_relative_to(self.project_root):
                raise ValueError("body_path must remain in project root")
            method["body"] = path.read_text(encoding="utf-8") if path.is_file() else None
        return catalog

    def validate_candidate(self, catalog: dict | None = None) -> dict:
        """Structural/identity checks only; never claims semantic truth from pass flags."""
        catalog = self.catalog if catalog is None else catalog
        errors: list[str] = []
        warnings: list[str] = []
        for field, key in [("methods", "method_id"), ("sources", "source_id"), ("cases", "case_id"), ("reviews", "review_id")]:
            items = catalog.get(field, [])
            if not isinstance(items, list) or any(not isinstance(i, dict) or not i.get(key) for i in items):
                return {"valid": False, "errors": [f"invalid {field} collection"], "warnings": []}
            identities = [(i[key], i.get("version")) if field == "sources" else i[key] for i in items]
            if len(set(identities)) != len(identities):
                errors.append(f"duplicate {field} identity")
        methods = _index(catalog.get("methods", []), "method_id")
        cases = _index(catalog.get("cases", []), "case_id")
        sources = {(s["source_id"], s.get("version")): s for s in catalog.get("sources", [])}
        for mid, method in methods.items():
            status = method.get("content_status")
            if status not in {"skeleton", "draft", "reviewed", "published"}:
                errors.append(f"{mid}: invalid content_status")
            for qid in method.get("question_ids", []):
                if qid not in self.question_index:
                    errors.append(f"{mid}: unknown question {qid}")
            for dependency in method.get("dependencies", []) + method.get("alternatives", []):
                if dependency not in methods:
                    errors.append(f"{mid}: unresolved method {dependency}")
            if status != "published":
                warnings.append(f"{mid}: {status}, not available coverage")
                continue
            for field in ["version", "title", "question_ids", "steps", "evidence_requirements", "rules", "counterexamples", "limitations", "case_ids", "applicability"]:
                if not method.get(field):
                    errors.append(f"{mid}: missing {field}")
            path = (self.project_root / method.get("body_path", "")).resolve()
            if not path.is_relative_to(self.project_root) or not path.is_file() or not path.read_text(encoding="utf-8").strip():
                errors.append(f"{mid}: missing method body")
            elif re.search(r"content_status:\s*skeleton", path.read_text(encoding="utf-8")):
                errors.append(f"{mid}: skeleton body cannot publish")
            if method.get("unresolved_conflicts"):
                errors.append(f"{mid}: unresolved methodology conflicts")
            for inp in method.get("required_inputs", []):
                if not inp.get("input_id") or not inp.get("concept") or inp.get("kind") not in {"metric", "text"} or "requirements" not in inp:
                    errors.append(f"{mid}: input lacks concept, kind or required basis")
                if inp.get("kind") == "metric" and not inp.get("requirements"):
                    errors.append(f"{mid}: quantitative input needs period/unit/scope requirements")
                if inp.get("kind") == "text" and not inp.get("locator_requirement"):
                    errors.append(f"{mid}: text evidence requires a disclosure locator")
            for rule in method.get("rules", []):
                if not rule.get("rule_id") or not rule.get("statement") or not rule.get("source_refs"):
                    errors.append(f"{mid}: rule missing identity, statement or source")
            for ref in _source_refs(method, cases):
                source = sources.get(_canonical_source_key(catalog, ref.get("source_id"), ref.get("version")))
                if not source or source.get("verification_status") != "verified":
                    errors.append(f"{mid}: unverified source {ref.get('source_id')}")
                if not ref.get("locator") or not ref.get("support"):
                    errors.append(f"{mid}: source locator/support missing")
                elif re.fullmatch(r"(?:home\s?page|首页|网站首页|title\s*only|https?://[^/]+/?)", str(ref["locator"]).strip(), re.I):
                    errors.append(f"{mid}: homepage/title alone is not a rule locator")
                if source and any(not source.get(k) for k in ["title", "author", "url", "acquired_at", "content_sha256"]):
                    errors.append(f"{mid}: source provenance incomplete")
            case_kinds = set()
            for cid in method.get("case_ids", []):
                case = cases.get(cid)
                if not case:
                    errors.append(f"{mid}: missing case {cid}")
                    continue
                if mid not in case.get("method_ids", []):
                    errors.append(f"{mid}: case {cid} not linked to method")
                case_kinds.add(case.get("kind"))
                if any(not case.get(k) for k in ["expected_factors", "supported_conclusions", "forbidden_conclusions", "reason", "source_refs", "evaluation_mode"]):
                    errors.append(f"{mid}: incomplete case standard {cid}")
            if not {"normal", "counterexample"}.issubset(case_kinds) or not case_kinds.intersection({"missing", "boundary"}):
                errors.append(f"{mid}: needs normal, counterexample and missing/boundary cases")
            try:
                identity = self.method_identity(mid, catalog)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"{mid}: cannot resolve content identity: {exc}")
                continue
            reviews = [r for r in catalog.get("reviews", []) if r.get("method_id") == mid and r.get("method_version") == method.get("version") and r.get("content_sha256") == identity]
            for kind in ["source", "case"]:
                if not any(r.get("kind") == kind and r.get("outcome") == "passed" and r.get("reasons") and r.get("reviewer") and not r.get("unresolved_issues") for r in reviews):
                    errors.append(f"{mid}: missing passed {kind} review for current content identity")
            if any(r.get("outcome") != "passed" or r.get("unresolved_issues") for r in reviews):
                errors.append(f"{mid}: failed/unresolved current content review")
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(mid: str) -> None:
            if mid in visiting:
                errors.append(f"{mid}: cyclic execution dependency")
                return
            if mid in done or mid not in methods:
                return
            visiting.add(mid)
            for dep in methods[mid].get("dependencies", []):
                visit(dep)
            visiting.remove(mid)
            done.add(mid)
        for mid in methods:
            visit(mid)
        return {"valid": not errors, "errors": sorted(set(errors)), "warnings": warnings}

    def build_candidate(self, catalog: dict | None = None, bundle_id: str | None = None) -> dict:
        if bundle_id is not None and (self.store_root / "bundles" / f"{_id(bundle_id)}.json").exists():
            raise ValueError(f"immutable bundle already exists: {bundle_id}")
        catalog = self.catalog if catalog is None else catalog
        check = self.validate_candidate(catalog)
        if not check["valid"]:
            raise ValueError("invalid candidate: " + "; ".join(check["errors"]))
        frozen = self._freeze(catalog)
        metrics = self._metrics()
        payload = {"catalog": frozen, "metrics": metrics, "questions": copy.deepcopy(self.questions), "kind": "candidate", "bundle_hash_version": 2}
        identities = {m["method_id"]: method_identity(frozen, m["method_id"], self.project_root, metrics) for m in frozen.get("methods", [])}
        for method in frozen.get("methods", []):
            if method.get("content_status") != "published":
                continue
            reviews = [r for r in frozen.get("reviews", []) if r.get("method_id") == method["method_id"] and r.get("method_version") == method["version"] and r.get("content_sha256") == identities[method["method_id"]]]
            if any(not any(r.get("kind") == kind and r.get("outcome") == "passed" and r.get("reasons") and r.get("reviewer") and not r.get("unresolved_issues") for r in reviews) for kind in ["source", "case"]):
                raise ValueError(f"frozen content identity changed after validation: {method['method_id']}")
        payload["method_identities"] = identities
        bundle_id = _id(bundle_id or "kb-" + _hash(payload)[:24])
        payload.update(bundle_id=bundle_id, created_at=datetime.now(timezone.utc).isoformat())
        payload["content_sha256"] = _bundle_hash(payload)
        payload["integrity_sha256"] = _bundle_integrity_hash(payload)
        path = self.store_root / "bundles" / f"{bundle_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        except FileExistsError as exc:
            raise ValueError(f"immutable bundle already exists: {bundle_id}") from exc
        return {"bundle_id": bundle_id, "bundle_kind": "candidate", "content_sha256": payload["content_sha256"], "validation": check}

    def _default_id(self) -> str | None:
        path = self.store_root / "default.json"
        return _load(path).get("bundle_id") if path.exists() else None

    def get_bundle(self, bundle_id: str) -> dict:
        path = self.store_root / "bundles" / f"{_id(bundle_id)}.json"
        if not path.exists():
            raise KeyError(f"unknown version: {bundle_id}")
        payload = _load(path)
        digest = payload.get("content_sha256")
        if (payload.get("bundle_id") != bundle_id or digest != _bundle_hash(payload)
                or (payload.get("bundle_hash_version") == 2
                    and payload.get("integrity_sha256") != _bundle_integrity_hash(payload))):
            raise ValueError(f"bundle integrity prevents complete reproduction: {bundle_id}")
        return payload

    def _changes(self) -> list[dict]:
        path = self.store_root / "changes.json"
        return _load(path).get("changes", []) if path.exists() else []

    def _notices(self, bundle: dict, mid: str) -> list[dict]:
        identity = bundle["method_identities"].get(mid)
        return [copy.deepcopy(c) for c in self._changes() if identity in c.get("affected_identities", {}).get(mid, [])]

    def _valid_methods(self, bundle: dict, historical: bool = False) -> set[str]:
        methods = _index(bundle["catalog"].get("methods", []), "method_id")
        valid = {mid for mid, method in methods.items() if method.get("content_status") == "published" and (historical or not self._notices(bundle, mid))}
        changed = True
        while changed:
            changed = False
            for mid in list(valid):
                if any(dep not in valid for dep in methods[mid].get("dependencies", [])):
                    valid.remove(mid)
                    changed = True
        return valid

    def _question_ready(self, bundle: dict, qid: str, valid: set[str]) -> bool:
        mapped = [m for m in bundle["catalog"].get("methods", []) if qid in m.get("question_ids", [])]
        return bool(mapped) and all(m["method_id"] in valid or any(alt in valid for alt in m.get("alternatives", [])) for m in mapped)

    def coverage(self, bundle_id: str | None = None) -> dict:
        default_id = self._default_id()
        bundle_id = bundle_id or default_id
        base = {"bundle_id": bundle_id, "bundle_kind": "default" if bundle_id and bundle_id == default_id else "candidate" if bundle_id else "none", "total": 54, "mapped_count": 0, "available_count": 0, "historical_published_count": 0, "available_question_ids": [], "pending_question_ids": sorted(self.question_index), "industry_gaps": {}, "default_published": bool(default_id), "methods": []}
        if not bundle_id:
            return base
        bundle = self.get_bundle(bundle_id)
        valid = self._valid_methods(bundle)
        historical = self._valid_methods(bundle, historical=True)
        questions = [q["question_id"] for q in bundle["questions"]["questions"]]
        mapped = {qid for m in bundle["catalog"].get("methods", []) for qid in m.get("question_ids", [])}
        ready = sorted(qid for qid in questions if self._question_ready(bundle, qid, valid))
        base.update(mapped_count=len(mapped), available_count=len(ready), available_question_ids=ready, pending_question_ids=sorted(set(questions) - set(ready)), historical_published_count=sum(self._question_ready(bundle, qid, historical) for qid in questions))
        base["methods"] = [{"method_id": mid, "content_sha256": bundle["method_identities"][mid]} for mid in sorted(valid)]
        for qid in mapped:
            base["industry_gaps"][qid] = sorted({gap for m in bundle["catalog"]["methods"] if qid in m.get("question_ids", []) for gap in m.get("industry_gaps", [])})
        return base

    @staticmethod
    def _binding(inp: dict, metrics: dict) -> dict:
        result = copy.deepcopy(inp)
        if inp.get("kind") == "text":
            result["binding_status"] = "text_evidence" if inp.get("locator_requirement") else "pending"
            return result
        definition = metrics.get("metrics", {}).get(inp.get("metric_id"))
        result["definition"] = copy.deepcopy(definition)
        result["definition_version"] = metrics.get("schema_version")
        result["definition_sha256"] = _hash(definition) if definition is not None else None
        reasons = []
        mismatch = False
        if definition is None:
            reasons.append("metric definition unavailable; mapping pending")
        else:
            expected = inp.get("expected_definition", {})
            for key, value in expected.items():
                if definition.get(key) != value:
                    mismatch = True
                    reasons.append(f"definition {key} mismatch")
            if inp.get("definition_version") and inp["definition_version"] != metrics.get("schema_version"):
                mismatch = True
                reasons.append("definition version mismatch")
            if inp.get("definition_sha256") and inp["definition_sha256"] != _hash(definition):
                mismatch = True
                reasons.append("definition identity mismatch")
            for key, value in inp.get("requirements", {}).items():
                if not value:
                    continue
                if key not in definition:
                    reasons.append(f"{key} not established by metric definition")
                elif definition[key] != value:
                    mismatch = True
                    reasons.append(f"required {key} mismatch")
            if not inp.get("definition_version") or not (inp.get("definition_sha256") or expected):
                reasons.append("definition identity not pinned")
            if inp.get("binding_status") != "matched":
                reasons.append("mapping not approved")
        result["binding_status"] = "mismatch" if mismatch else "pending" if reasons else "matched"
        result["binding_reasons"] = reasons
        return result

    @staticmethod
    def _applicability(method: dict, context: dict) -> dict:
        app = copy.deepcopy(method.get("applicability", {}))
        missing = [key for key in app.get("requires_context", []) if not context.get(key)]
        industry = str(context.get("industry", "")).casefold()
        exclude = [str(i).casefold() for i in app.get("exclude_industries", [])]
        include = [str(i).casefold() for i in app.get("include_industries", [])]
        if industry and (industry in exclude or (include and industry not in include)):
            app.update(status="not_applicable", reason="Industry outside this method's documented conditions", missing_context=[])
        elif missing:
            app.update(status="needs_context", missing_context=missing, reason="Selection context must be confirmed")
        else:
            app.update(status="applicable", missing_context=[])
        return app

    def _method_result(self, bundle: dict, method: dict, context: dict, detail: bool) -> dict:
        mid = method["method_id"]
        result = copy.deepcopy(method)
        if not detail:
            result.pop("body", None)
        result["content_sha256"] = bundle["method_identities"][mid]
        result["notices"] = self._notices(bundle, mid)
        result["available"] = mid in self._valid_methods(bundle)
        result["applicability"] = self._applicability(method, context)
        result["required_inputs"] = [self._binding(i, bundle["metrics"]) for i in method.get("required_inputs", [])]
        result["detail_ref"] = {"kind": "method", "bundle_id": bundle["bundle_id"], "method_id": mid, "method_version": method.get("version"), "context": context}
        cases = _index(bundle["catalog"].get("cases", []), "case_id")
        refs = _source_refs(method, cases)
        source_keys = {_canonical_source_key(bundle["catalog"], r.get("source_id"), r.get("version")) for r in refs}
        sources = [s for s in bundle["catalog"].get("sources", []) if (s.get("source_id"), s.get("version")) in source_keys]
        by_id = _index(bundle["catalog"].get("sources", []), "source_id")
        def original(sid: str) -> str:
            visited = set()
            while sid in by_id and by_id[sid].get("original_source_id") and sid not in visited:
                visited.add(sid)
                sid = by_id[sid]["original_source_id"]
            return sid
        result["independent_source_count"] = len({original(s["source_id"]) for s in sources})
        result["source_refs"] = [{"kind": "source", "bundle_id": bundle["bundle_id"], "source_id": s["source_id"], "source_version": s["version"]} for s in sources]
        if detail:
            result["sources"] = copy.deepcopy(sources)
            result["cases"] = [copy.deepcopy(cases[cid]) for cid in method.get("case_ids", []) if cid in cases]
        return result

    def read(self, question_id: str, bundle_id: str | None = None, context: dict | None = None, detail: bool = False, include_drafts: bool = False, max_chars: int | None = None) -> dict:
        context = copy.deepcopy(context or {})
        if set(context) - {"industry", "business_type", "period_type", "model_purpose"} or any(not isinstance(v, str) for v in context.values()):
            raise ValueError("context accepts only qualitative selection fields, not company facts")
        if question_id not in self.question_index:
            return {"status": "unknown_question", "question_id": question_id, "complete": True}
        bundle_id = bundle_id or self._default_id()
        if not bundle_id:
            return {"status": "no_default_release", "question": copy.deepcopy(self.question_index[question_id]), "complete": True, "gap": "No complete default knowledge release has passed acceptance."}
        try:
            bundle = self.get_bundle(bundle_id)
        except KeyError:
            return {"status": "unknown_version", "bundle_id": bundle_id, "question_id": question_id, "complete": True}
        methods = _index(bundle["catalog"].get("methods", []), "method_id")
        roots = [mid for mid, m in methods.items() if question_id in m.get("question_ids", [])]
        valid = self._valid_methods(bundle)
        order = []
        seen = set()
        def visit(mid: str) -> None:
            if mid in seen:
                return
            seen.add(mid)
            for dep in methods[mid].get("dependencies", []):
                visit(dep)
            order.append(mid)
        for mid in roots:
            visit(mid)
            for alt in methods[mid].get("alternatives", []):
                visit(alt)
        selected = [mid for mid in order if methods[mid].get("content_status") == "published" or include_drafts]
        question = next(q for q in bundle["questions"]["questions"] if q["question_id"] == question_id)
        replacements = [{"method_id": mid, "replacement_method_ids": [alt for alt in methods[mid].get("alternatives", []) if alt in valid]} for mid in roots if mid not in valid and any(alt in valid for alt in methods[mid].get("alternatives", []))]
        result = {"status": "available" if any(mid in valid for mid in roots) or replacements else "preview" if include_drafts and selected else "content_pending", "question": copy.deepcopy(question), "question_set_version": bundle["questions"]["question_set_version"], "mapping_version": bundle["catalog"].get("mapping_version"), "bundle_id": bundle_id, "bundle_kind": "default" if bundle_id == self._default_id() else "candidate", "complete": True, "question_complete": self._question_ready(bundle, question_id, valid), "methods": [self._method_result(bundle, methods[mid], context, detail) for mid in selected], "replacement_paths": replacements, "question_gaps": [{"method_id": mid, "content_status": methods[mid].get("content_status"), "reason": "Required generic path not currently available"} for mid in roots if mid not in valid and not any(alt in valid for alt in methods[mid].get("alternatives", []))]}
        method_contexts = {m["method_id"]: m["applicability"]["status"] for m in result["methods"] if m["method_id"] in valid}
        for mid in order:
            if mid not in method_contexts:
                continue
            dependency_contexts = [method_contexts.get(dep, "needs_context") for dep in methods[mid].get("dependencies", [])]
            if "not_applicable" in dependency_contexts:
                method_contexts[mid] = "not_applicable"
            elif "needs_context" in dependency_contexts and method_contexts[mid] != "not_applicable":
                method_contexts[mid] = "needs_context"
        for method in result["methods"]:
            effective = method_contexts.get(method["method_id"])
            if effective and effective != method["applicability"]["status"]:
                method["applicability"].update(status=effective, reason="Required dependency is not applicable or lacks selection context")
        path_contexts = []
        for mid in roots:
            options = [method_contexts[candidate] for candidate in [mid] + methods[mid].get("alternatives", []) if candidate in method_contexts]
            if options:
                path_contexts.append("applicable" if "applicable" in options else "needs_context" if "needs_context" in options else "not_applicable")
        context_status = "not_applicable" if "not_applicable" in path_contexts else "needs_context" if not path_contexts or "needs_context" in path_contexts else "applicable"
        result["context_status"] = context_status
        result["executable"] = result["question_complete"] and context_status == "applicable"
        result["context_explanation"] = {"not_applicable": "已发布通用内容存在，但当前上下文不适用；查看方法条件和行业补充缺口。", "needs_context": "需先确认返回方法列出的选择条件；内容可用不代表当前适用性已确定。", "applicable": "已知选择条件符合返回方法；仍须按各项输入、证据和题内缺口完成研究。"}[context_status]
        result["continue_ref"] = {"kind": "question", "bundle_id": bundle_id, "question_id": question_id, "context": context, "include_drafts": include_drafts}
        if max_chars is not None:
            if max_chars < 1:
                raise ValueError("max_chars must be positive")
            if len(json.dumps(result, ensure_ascii=False)) > max_chars:
                envelope = {"status": result["status"], "bundle_id": bundle_id, "question_id": question_id, "complete": False, "context_status": context_status, "executable": False, "omitted_sections": ["methods", "question_gaps"], "continue_ref": result["continue_ref"]}
                envelope["minimum_envelope_exceeds_limit"] = len(json.dumps(envelope, ensure_ascii=False)) > max_chars
                return envelope
        return result

    def expand(self, reference: dict) -> dict:
        bundle = self.get_bundle(reference["bundle_id"])
        kind = reference.get("kind")
        if kind == "question":
            return self.read(reference["question_id"], bundle_id=bundle["bundle_id"], context=reference.get("context"), detail=True, include_drafts=reference.get("include_drafts", False))
        if kind == "method":
            method = _index(bundle["catalog"]["methods"], "method_id").get(reference["method_id"])
            if not method or method.get("version") != reference.get("method_version"):
                raise KeyError("unknown method version")
            return self._method_result(bundle, method, reference.get("context", {}), True)
        if kind == "source":
            source_key = _canonical_source_key(bundle["catalog"], reference["source_id"], reference["source_version"])
            for source in bundle["catalog"]["sources"]:
                if (source["source_id"], source["version"]) == source_key:
                    return copy.deepcopy(source)
            raise KeyError("unknown source version")
        raise ValueError("unknown expansion reference")

    def record_change(self, kind: str, object_id: str, reason: str, change_id: str | None = None) -> dict:
        if kind not in {"source", "rule", "input", "method"} or not object_id or not reason.strip():
            raise ValueError("change requires kind, object_id and reason")
        changes = self._changes()
        change_id = _id(change_id or "change-" + _hash([kind, object_id, reason, len(changes)])[:20])
        if any(c["change_id"] == change_id for c in changes):
            raise ValueError("change_id already exists")
        identities: dict[str, set[str]] = {}
        qids: set[str] = set()
        cids: set[str] = set()
        for path in sorted((self.store_root / "bundles").glob("*.json")):
            bundle = self.get_bundle(path.stem)
            catalog = bundle["catalog"]
            methods = _index(catalog.get("methods", []), "method_id")
            cases = _index(catalog.get("cases", []), "case_id")
            affected = set()
            sources = _index(catalog.get("sources", []), "source_id")
            source_target = object_id
            for alias in catalog.get("source_aliases", []):
                if alias.get("alias_source_id") == object_id and alias.get("status") == "historical_alias":
                    source_target = alias.get("canonical_source_id")
                    break
            for mid, method in methods.items():
                refs = _source_refs(method, cases)
                hit = (kind == "method" and mid == object_id) or (kind == "rule" and any(r.get("rule_id") == object_id for r in method.get("rules", []))) or (kind == "input" and any(i.get("metric_id") == object_id or i.get("input_id") == object_id for i in method.get("required_inputs", []))) or (kind == "source" and any(r.get("source_id") in {object_id, source_target} or sources.get(r.get("source_id"), {}).get("original_source_id") in {object_id, source_target} for r in refs))
                if hit:
                    affected.add(mid)
            while True:
                parents = {mid for mid, m in methods.items() if set(m.get("dependencies", [])) & affected}
                if parents <= affected:
                    break
                affected |= parents
            for mid in affected:
                identities.setdefault(mid, set()).add(bundle["method_identities"][mid])
                qids.update(methods[mid].get("question_ids", []))
                cids.update(methods[mid].get("case_ids", []))
        if not identities:
            raise ValueError("change target has no registered snapshot dependencies")
        change = {"change_id": change_id, "kind": kind, "object_id": object_id, "reason": reason, "recorded_at": datetime.now(timezone.utc).isoformat(), "method_ids": sorted(identities), "question_ids": sorted(qids), "case_ids": sorted(cids), "affected_identities": {mid: sorted(ids) for mid, ids in identities.items()}}
        _write(self.store_root / "changes.json", {"changes": changes + [change]})
        return change

    def publish_default(self, bundle_id: str, release_evidence: dict) -> dict:
        from analysis.knowledge_release import evaluate_release, prepare_evidence
        coverage = self.coverage(bundle_id)
        bundle = self.get_bundle(bundle_id)
        evidence = prepare_evidence(bundle_id, coverage["methods"], bundle["catalog"].get("reviews", []), release_evidence)
        gate = evaluate_release({**coverage, "ready_question_ids": coverage["available_question_ids"],
                                 "content_sha256": bundle["content_sha256"]}, evidence, set(self.question_index))
        if not gate["passed"]:
            raise ValueError("complete 54-question release rejected: " + json.dumps(gate, ensure_ascii=False))
        _write(self.store_root / "default.json", {"bundle_id": bundle_id, "release_evidence": evidence, "gate": gate})
        return {"bundle_id": bundle_id, "bundle_kind": "default", "gate": gate}
