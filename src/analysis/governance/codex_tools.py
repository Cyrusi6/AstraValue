from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlsplit

from .canonical import (
    canonical_json_text,
    canonical_sha256,
    ensure_aware_utc,
    load_canonical_json,
)
from .models import (
    CodexInputPack,
    CodexSessionManifest,
    CodexToolRead,
    GovernanceSnapshot,
    MODEL_SCHEMA_VERSION,
    ObjectReference,
    QuestionSummary,
    ReportGenerationStatus,
    ResearchBudget,
    SnapshotAdoption,
    ToolReadStatus,
    ToolSchemaReference,
)
from .registry import EXACT_QUESTION_IDS


READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "get_evidence_excerpt",
    "get_governance_overview",
    "get_record_lineage",
    "query_audit_and_internal_control",
    "query_compensation_and_incentives",
    "query_coverage_gaps_conflicts",
    "query_governance_events",
    "query_ownership_and_control",
    "query_pledges",
    "query_regulatory_litigation_commitments",
    "query_related_parties",
    "query_roles_and_people",
)

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "browser_profile",
    "chain_of_thought",
    "cookie",
    "owner_token",
    "password",
    "secret",
    "token",
}

_SUBSTANTIVE_PAYLOAD_KEYS = {
    "body",
    "content",
    "excerpt",
    "full_text",
    "fulltext",
    "summary",
    "text",
    "title",
}

_JSON_SCHEMA_KEYWORDS = {
    "$defs",
    "$id",
    "$ref",
    "$schema",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "default",
    "description",
    "enum",
    "examples",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "not",
    "oneOf",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
    "uniqueItems",
}


class CodexToolError(RuntimeError):
    """Stable public failure raised before any unrecorded content is returned."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _semantic_hash(schema_name: str, payload: Any, version: str = "1.0.0") -> str:
    return canonical_sha256(
        payload,
        schema_name=schema_name,
        schema_version=version,
    )


def _model_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("canonical governance object must be a model or mapping")


def canonical_input_pack_hash(value: CodexInputPack | Mapping[str, Any]) -> str:
    """Recompute the semantic hash used by ``CodexInputPackBuilder``."""

    data = _model_mapping(value)
    excluded = {"kind", "schema_version", "input_pack_id", "canonical_hash", "created_at"}
    payload = {
        name: data[name]
        for name in CodexInputPack.model_fields
        if name not in excluded
    }
    return _semantic_hash("governance-codex-input-pack", payload)


def canonical_tool_read_hash(value: CodexToolRead | Mapping[str, Any]) -> str:
    data = _model_mapping(value)
    data.setdefault("kind", "codex_tool_read")
    data.setdefault("schema_version", MODEL_SCHEMA_VERSION)
    payload: dict[str, Any] = {}
    for name, model_field in CodexToolRead.model_fields.items():
        if name in {"canonical_hash", "tool_read_id"}:
            continue
        if name in data:
            payload[name] = data[name]
        elif not model_field.is_required():
            payload[name] = model_field.default
        else:
            raise ValueError(f"missing required tool-read hash field: {name}")
    return _semantic_hash(
        "governance-codex-tool-read",
        payload,
        str(payload["schema_version"]),
    )


def canonical_session_manifest_hash(
    value: CodexSessionManifest | Mapping[str, Any],
) -> str:
    """Hash all immutable session content except the report back-reference.

    ``report_hash`` is deliberately excluded to break the report/session cycle.
    Every other manifest field, including timestamps and execution profile, is
    covered by the digest.
    """

    data = _model_mapping(value)
    data.setdefault("kind", "codex_session_manifest")
    data.setdefault("schema_version", MODEL_SCHEMA_VERSION)
    payload = {
        name: data[name]
        for name in CodexSessionManifest.model_fields
        if name not in {"canonical_hash", "report_hash"}
    }
    return _semantic_hash(
        "governance-codex-session-manifest",
        payload,
        str(payload["schema_version"]),
    )


class _JsonSchemaViolation(ValueError):
    pass


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise _JsonSchemaViolation(f"unsupported JSON Schema type: {expected}")


def _resolve_local_schema_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise _JsonSchemaViolation("only local JSON Schema references are supported")
    current: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise _JsonSchemaViolation(f"unresolved JSON Schema reference: {reference}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise _JsonSchemaViolation("JSON Schema reference must resolve to an object")
    return current


def _validate_json_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate the bounded JSON Schema subset accepted by governance tools.

    Unsupported assertion keywords fail closed instead of silently weakening a
    tool contract. Validation runs over canonical JSON values, so Python-only
    representations cannot bypass the declared wire schema.
    """

    if not isinstance(schema, Mapping):
        raise _JsonSchemaViolation(f"schema at {path} is not an object")
    root = schema if root is None else root
    unsupported = {
        str(key)
        for key in schema
        if key not in _JSON_SCHEMA_KEYWORDS and not str(key).startswith("x-")
    }
    if unsupported:
        raise _JsonSchemaViolation(
            f"unsupported JSON Schema keywords at {path}: {sorted(unsupported)}"
        )
    if "$ref" in schema:
        resolved = _resolve_local_schema_ref(root, str(schema["$ref"]))
        _validate_json_schema(value, resolved, root=root, path=path)
    for member in schema.get("allOf", ()):
        _validate_json_schema(value, member, root=root, path=path)
    if "anyOf" in schema:
        if not any(
            _schema_accepts(value, member, root=root, path=path)
            for member in schema["anyOf"]
        ):
            raise _JsonSchemaViolation(f"value at {path} does not satisfy anyOf")
    if "oneOf" in schema:
        matches = sum(
            _schema_accepts(value, member, root=root, path=path)
            for member in schema["oneOf"]
        )
        if matches != 1:
            raise _JsonSchemaViolation(f"value at {path} must satisfy exactly one oneOf branch")
    if "not" in schema and _schema_accepts(value, schema["not"], root=root, path=path):
        raise _JsonSchemaViolation(f"value at {path} matches a forbidden schema")
    if "const" in schema and value != schema["const"]:
        raise _JsonSchemaViolation(f"value at {path} does not match const")
    if "enum" in schema and value not in schema["enum"]:
        raise _JsonSchemaViolation(f"value at {path} is outside enum")
    if "type" in schema:
        expected_types = schema["type"]
        if isinstance(expected_types, str):
            expected_types = (expected_types,)
        if not isinstance(expected_types, (list, tuple)) or not expected_types:
            raise _JsonSchemaViolation(f"invalid type declaration at {path}")
        if not any(_schema_type_matches(value, item) for item in expected_types):
            raise _JsonSchemaViolation(f"value at {path} has the wrong JSON type")

    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
            raise _JsonSchemaViolation(f"invalid object schema at {path}")
        missing = [name for name in required if name not in value]
        if missing:
            raise _JsonSchemaViolation(f"missing required properties at {path}: {missing}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                _validate_json_schema(item, properties[key], root=root, path=child_path)
            elif additional is False:
                raise _JsonSchemaViolation(f"additional property is forbidden at {child_path}")
            elif isinstance(additional, Mapping):
                _validate_json_schema(item, additional, root=root, path=child_path)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise _JsonSchemaViolation(f"array at {path} is shorter than minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise _JsonSchemaViolation(f"array at {path} is longer than maxItems")
        if schema.get("uniqueItems"):
            rendered = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(rendered) != len(set(rendered)):
                raise _JsonSchemaViolation(f"array at {path} violates uniqueItems")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_json_schema(item, schema["items"], root=root, path=f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise _JsonSchemaViolation(f"string at {path} is shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise _JsonSchemaViolation(f"string at {path} is longer than maxLength")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            raise _JsonSchemaViolation(f"string at {path} does not match pattern")
        if schema.get("format") == "date-time":
            _parse_available_at(value, path=path)
        elif "format" in schema and schema["format"] not in {None, "date-time"}:
            raise _JsonSchemaViolation(f"unsupported JSON Schema format at {path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise _JsonSchemaViolation(f"number at {path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise _JsonSchemaViolation(f"number at {path} is above maximum")


def _schema_accepts(
    value: Any,
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    path: str,
) -> bool:
    try:
        _validate_json_schema(value, schema, root=root, path=path)
    except _JsonSchemaViolation:
        return False
    return True


def _canonical_json_value(value: Any) -> Any:
    return json.loads(canonical_json_text(value))


def _parse_available_at(value: Any, *, path: str) -> datetime:
    if isinstance(value, datetime):
        return ensure_aware_utc(value)
    if not isinstance(value, str):
        raise CodexToolError("invalid_time", f"available_at at {path} is not RFC3339")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
        return ensure_aware_utc(parsed)
    except (TypeError, ValueError) as exc:
        raise CodexToolError(
            "invalid_time", f"available_at at {path} is not an aware RFC3339 time"
        ) from exc


def _policy_allows_llm(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"allowed", "allow", "enabled", "permitted"}
    if isinstance(value, Mapping):
        for key in ("allowed", "allow", "enabled", "for_llm"):
            if key in value:
                return _policy_allows_llm(value[key])
    return False


def _declared_reference_sets(payload: ToolPayload) -> dict[str, set[str]]:
    references = {
        "record": set(payload.actual_record_ids),
        "claim": set(payload.actual_claim_ids),
        "span": set(payload.actual_evidence_span_ids),
        "raw": set(payload.actual_raw_snapshot_ids),
        "citation": set(payload.citation_ids),
    }
    prefixes = {
        "record": "govrec:",
        "claim": "govclaim:",
        "span": "govspan:",
    }
    for kind, values in references.items():
        if len(values) != len(getattr(payload, {
            "record": "actual_record_ids",
            "claim": "actual_claim_ids",
            "span": "actual_evidence_span_ids",
            "raw": "actual_raw_snapshot_ids",
            "citation": "citation_ids",
        }[kind])):
            raise CodexToolError("broken_lineage", f"duplicate {kind} references")
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise CodexToolError("broken_lineage", f"invalid {kind} reference")
        prefix = prefixes.get(kind)
        if prefix is not None and any(not item.startswith(prefix) for item in values):
            raise CodexToolError("cross_namespace", f"invalid {kind} namespace")
    return references


def _validate_payload_security(
    payload: ToolPayload,
    *,
    snapshot_id: str,
    snapshot_hash: str,
    known_at: datetime,
) -> None:
    references = _declared_reference_sets(payload)

    def walk(node: Any, inherited: Mapping[str, Any], path: str) -> None:
        if isinstance(node, Mapping):
            context = dict(inherited)
            if node.get("kind") == "governance_snapshot":
                try:
                    embedded_snapshot = GovernanceSnapshot.model_validate_json(
                        canonical_json_text(node), strict=True
                    )
                except ValueError as exc:
                    raise CodexToolError(
                        "tool_output_schema_invalid",
                        f"embedded governance snapshot is invalid at {path}",
                    ) from exc
                if (
                    embedded_snapshot.governance_snapshot_id != snapshot_id
                    or embedded_snapshot.canonical_snapshot_hash != snapshot_hash
                ):
                    raise CodexToolError(
                        "snapshot_binding_mismatch",
                        f"embedded governance snapshot differs at {path}",
                    )
            for key in (
                "available_at",
                "llm_allowed",
                "allow_llm",
                "llm_policy",
                "integrity_verified",
                "lineage_complete",
                "evidence_span_id",
                "evidence_span_ids",
            ):
                if key in node:
                    context[key] = node[key]
            if "governance_snapshot_id" in node and node["governance_snapshot_id"] != snapshot_id:
                raise CodexToolError(
                    "snapshot_binding_mismatch",
                    f"payload snapshot ID differs at {path}",
                )
            if (
                "governance_snapshot_hash" in node
                and node["governance_snapshot_hash"] != snapshot_hash
            ):
                raise CodexToolError(
                    "snapshot_hash_mismatch",
                    f"payload snapshot hash differs at {path}",
                )
            if "available_at" in node:
                available_at = _parse_available_at(
                    node["available_at"], path=f"{path}.available_at"
                )
                if available_at > known_at:
                    raise CodexToolError(
                        "future_leakage",
                        "tool payload contains evidence unavailable at session known_at",
                    )

            id_fields = {
                "record_id": "record",
                "record_ids": "record",
                "claim_id": "claim",
                "claim_ids": "claim",
                "evidence_span_id": "span",
                "evidence_span_ids": "span",
                "raw_snapshot_id": "raw",
                "raw_snapshot_ids": "raw",
                "citation_id": "citation",
                "citation_ids": "citation",
            }
            for key, kind in id_fields.items():
                if key not in node:
                    continue
                values = node[key]
                values = (values,) if isinstance(values, str) else tuple(values)
                if not set(values).issubset(references[kind]):
                    raise CodexToolError(
                        "broken_lineage",
                        f"payload {kind} identity is absent from recorded references",
                    )
            if isinstance(node.get("object_id"), str):
                object_id = node["object_id"]
                for prefix, kind in (
                    ("govrec:", "record"),
                    ("govclaim:", "claim"),
                    ("govspan:", "span"),
                ):
                    if object_id.startswith(prefix) and object_id not in references[kind]:
                        raise CodexToolError(
                            "broken_lineage",
                            "payload object identity is absent from recorded references",
                        )

            substantive = any(
                str(key).strip().lower() in _SUBSTANTIVE_PAYLOAD_KEYS
                and isinstance(value, str)
                and bool(value.strip())
                for key, value in node.items()
            )
            if substantive:
                if "available_at" not in context:
                    raise CodexToolError(
                        "available_at_unproven",
                        "substantive tool payload lacks an observed available_at",
                    )
                available_at = _parse_available_at(context["available_at"], path=path)
                if available_at > known_at:
                    raise CodexToolError("future_leakage", "future evidence is not readable")
                policy = context.get(
                    "llm_allowed",
                    context.get("allow_llm", context.get("llm_policy")),
                )
                if not _policy_allows_llm(policy):
                    raise CodexToolError(
                        "llm_policy_denied", "source policy does not allow Codex access"
                    )
                if context.get("integrity_verified") is not True:
                    raise CodexToolError(
                        "hash_mismatch", "substantive payload lacks verified content integrity"
                    )
                if context.get("lineage_complete") is not True:
                    raise CodexToolError(
                        "broken_lineage", "substantive payload lacks complete lineage"
                    )
                span_values = context.get(
                    "evidence_span_ids", context.get("evidence_span_id", ())
                )
                span_values = (span_values,) if isinstance(span_values, str) else tuple(span_values)
                if not span_values or not set(span_values).issubset(references["span"]):
                    raise CodexToolError(
                        "span_only_violation",
                        "substantive payload is not bound to recorded evidence spans",
                    )
            for key, item in node.items():
                if key == "requested":
                    continue
                walk(item, context, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, inherited, f"{path}[{index}]")

    walk(payload.payload, {}, "$.payload")


def _reject_sensitive_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_token"):
                raise CodexToolError(
                    "sensitive_parameter",
                    f"sensitive parameter is not accepted at {path}.{key}",
                )
            _reject_sensitive_keys(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if (
            re.search(r"(?i)\bbearer\s+[a-z0-9._~-]{8,}", value)
            or re.search(r"(?i)\bsk-[a-z0-9_-]{8,}", value)
            or re.search(r"(?i)\bcookie\s*[:=]", value)
            or re.match(r"^[a-zA-Z]:[\\/]", value)
            or value.startswith("\\\\")
            or lowered.startswith(("/home/", "/users/"))
        ):
            raise CodexToolError(
                "unsafe_payload", f"private or credential-like content at {path}"
            )
        if lowered.startswith(("http://", "https://")) and urlsplit(value).query:
            raise CodexToolError(
                "unsafe_payload", f"URL query parameters are not permitted at {path}"
            )


@dataclass(frozen=True, slots=True)
class ToolPayload:
    """A tool result plus the exact immutable objects it exposed."""

    payload: Mapping[str, Any]
    actual_record_ids: tuple[str, ...] = ()
    actual_claim_ids: tuple[str, ...] = ()
    actual_evidence_span_ids: tuple[str, ...] = ()
    actual_raw_snapshot_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()


ToolHandler = Callable[[str, Mapping[str, Any]], ToolPayload]
ParameterValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class GovernanceTool:
    name: str
    version: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    handler: ToolHandler
    parameter_validator: ParameterValidator | None = None
    read_only: bool = True

    def __post_init__(self) -> None:
        if self.name not in READ_ONLY_TOOL_NAMES:
            raise ValueError(f"unapproved governance tool: {self.name}")
        if not self.read_only:
            raise ValueError("governance evidence tools are read-only")

    def schema_reference(self) -> ToolSchemaReference:
        return ToolSchemaReference(
            tool_name=self.name,
            tool_version=self.version,
            input_schema_hash=_semantic_hash(
                "governance-tool-input-schema", self.input_schema, self.version
            ),
            output_schema_hash=_semantic_hash(
                "governance-tool-output-schema", self.output_schema, self.version
            ),
        )


class GovernanceToolRegistry:
    def __init__(self, tools: Sequence[GovernanceTool] = ()) -> None:
        self._tools: dict[str, GovernanceTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: GovernanceTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate governance tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> GovernanceTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise CodexToolError("tool_not_found", "unknown governance tool") from exc

    def schema_references(self) -> tuple[ToolSchemaReference, ...]:
        return tuple(self._tools[name].schema_reference() for name in sorted(self._tools))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


@dataclass(slots=True)
class _SessionLedger:
    session_id: str
    parent_report_run_id: str
    model_profile: str
    runner_protocol_version: str
    tool_protocol_version: str
    input_pack: CodexInputPack
    started_at: datetime
    current_snapshot_id: str
    current_snapshot_hash: str
    revision: int = 0
    tool_reads: list[CodexToolRead] = field(default_factory=list)
    research_task_ids: list[str] = field(default_factory=list)
    research_result_bundle_ids: list[str] = field(default_factory=list)
    adoptions: list[SnapshotAdoption] = field(default_factory=list)
    finalized_manifest: CodexSessionManifest | None = None


class SessionRecorder(Protocol):
    def current_binding(self, session_id: str) -> tuple[str, str, int]: ...

    def next_tool_sequence(self, session_id: str) -> int: ...

    def append_tool_read(self, read: CodexToolRead) -> None: ...

    def input_pack(self, session_id: str) -> CodexInputPack: ...


class InMemoryCodexSessionRecorder:
    """Thread-safe reference recorder used by pure services and offline tests.

    Production persistence implements the same append-before-disclose contract.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionLedger] = {}
        self._lock = RLock()

    def start(
        self,
        *,
        session_id: str,
        parent_report_run_id: str,
        model_profile: str,
        runner_protocol_version: str,
        tool_protocol_version: str,
        input_pack: CodexInputPack,
        started_at: datetime,
    ) -> None:
        if not session_id.startswith("govsession:"):
            raise ValueError("session_id must use the govsession namespace")
        input_hash = canonical_input_pack_hash(input_pack)
        if (
            input_pack.canonical_hash != input_hash
            or input_pack.input_pack_id != f"govinput:{input_hash}"
        ):
            raise CodexToolError(
                "input_pack_hash_mismatch",
                "session cannot start from a modified input pack",
            )
        ensure_aware_utc(started_at)
        with self._lock:
            if session_id in self._sessions:
                raise ValueError("session identity is immutable")
            self._sessions[session_id] = _SessionLedger(
                session_id=session_id,
                parent_report_run_id=parent_report_run_id,
                model_profile=model_profile,
                runner_protocol_version=runner_protocol_version,
                tool_protocol_version=tool_protocol_version,
                input_pack=input_pack,
                started_at=started_at,
                current_snapshot_id=input_pack.governance_snapshot_id,
                current_snapshot_hash=input_pack.governance_snapshot_hash,
            )

    def _ledger(self, session_id: str) -> _SessionLedger:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise CodexToolError("session_not_found", "unknown Codex session") from exc

    @staticmethod
    def _ensure_open(ledger: _SessionLedger) -> None:
        if ledger.finalized_manifest is not None:
            raise CodexToolError(
                "session_finalized",
                "the finalized session manifest is immutable",
            )

    def current_binding(self, session_id: str) -> tuple[str, str, int]:
        with self._lock:
            ledger = self._ledger(session_id)
            return (
                ledger.current_snapshot_id,
                ledger.current_snapshot_hash,
                ledger.revision,
            )

    def append_tool_read(self, read: CodexToolRead) -> None:
        with self._lock:
            ledger = self._ledger(read.session_id)
            self._ensure_open(ledger)
            if read.governance_snapshot_id != ledger.current_snapshot_id:
                raise CodexToolError(
                    "snapshot_binding_mismatch",
                    "tool read does not match the session's fixed snapshot",
                )
            expected = len(ledger.tool_reads) + 1
            if read.sequence != expected:
                raise CodexToolError(
                    "tool_sequence_conflict",
                    f"tool sequence must be {expected}",
                )
            ledger.tool_reads.append(read)

    def next_tool_sequence(self, session_id: str) -> int:
        with self._lock:
            ledger = self._ledger(session_id)
            self._ensure_open(ledger)
            return len(ledger.tool_reads) + 1

    def append_research_result(
        self,
        session_id: str,
        task_id: str,
        bundle_id: str,
    ) -> None:
        with self._lock:
            ledger = self._ledger(session_id)
            self._ensure_open(ledger)
            if task_id in ledger.research_task_ids:
                raise ValueError("research task identities are append-only")
            ledger.research_task_ids.append(task_id)
            ledger.research_result_bundle_ids.append(bundle_id)

    def append_adoption(self, adoption: SnapshotAdoption) -> None:
        with self._lock:
            ledger = self._ledger(adoption.session_id)
            self._ensure_open(ledger)
            if adoption.expected_session_revision != ledger.revision:
                raise CodexToolError(
                    "revision_conflict", "session revision changed before adoption"
                )
            if (
                adoption.old_snapshot_id != ledger.current_snapshot_id
                or adoption.old_snapshot_hash != ledger.current_snapshot_hash
            ):
                raise CodexToolError(
                    "snapshot_binding_mismatch",
                    "adoption old snapshot does not match the session",
                )
            ledger.adoptions.append(adoption)
            ledger.current_snapshot_id = adoption.new_snapshot_id
            ledger.current_snapshot_hash = adoption.new_snapshot_hash
            ledger.revision = adoption.resulting_session_revision

    def input_pack(self, session_id: str) -> CodexInputPack:
        with self._lock:
            return self._ledger(session_id).input_pack

    def tool_reads(self, session_id: str) -> tuple[CodexToolRead, ...]:
        with self._lock:
            return tuple(self._ledger(session_id).tool_reads)

    @staticmethod
    def _manifest_payload(
        ledger: _SessionLedger,
        *,
        generation_status: ReportGenerationStatus,
        final_citation_ids: Sequence[str],
        report_hash: str | None,
        output_schema_validated: bool,
        failure_code: str | None,
        completed_at: datetime | None,
    ) -> dict[str, Any]:
        return {
            "kind": "codex_session_manifest",
            "schema_version": MODEL_SCHEMA_VERSION,
            "session_manifest_id": ledger.session_id,
            "parent_report_run_id": ledger.parent_report_run_id,
            "model_profile": ledger.model_profile,
            "runner_protocol_version": ledger.runner_protocol_version,
            "tool_protocol_version": ledger.tool_protocol_version,
            "input_pack_id": ledger.input_pack.input_pack_id,
            "input_pack_hash": ledger.input_pack.canonical_hash,
            "initial_snapshot_id": ledger.input_pack.governance_snapshot_id,
            "final_snapshot_id": ledger.current_snapshot_id,
            "tool_read_ids": tuple(item.tool_read_id for item in ledger.tool_reads),
            "research_task_ids": tuple(ledger.research_task_ids),
            "research_result_bundle_ids": tuple(ledger.research_result_bundle_ids),
            "snapshot_adoption_ids": tuple(
                item.snapshot_adoption_id for item in ledger.adoptions
            ),
            "final_citation_ids": tuple(sorted(set(final_citation_ids))),
            "report_hash": report_hash,
            "generation_status": generation_status,
            "output_schema_validated": output_schema_validated,
            "failure_code": failure_code,
            "started_at": ledger.started_at,
            "completed_at": completed_at,
        }

    def preview_manifest_hash(
        self,
        session_id: str,
        *,
        generation_status: ReportGenerationStatus,
        final_citation_ids: Sequence[str],
        output_schema_validated: bool,
        failure_code: str | None,
        completed_at: datetime,
    ) -> str:
        with self._lock:
            ledger = self._ledger(session_id)
            self._ensure_open(ledger)
            payload = self._manifest_payload(
                ledger,
                generation_status=generation_status,
                final_citation_ids=final_citation_ids,
                report_hash=None,
                output_schema_validated=output_schema_validated,
                failure_code=failure_code,
                completed_at=completed_at,
            )
            return canonical_session_manifest_hash(payload)

    def finalize_session(
        self,
        session_id: str,
        *,
        generation_status: ReportGenerationStatus,
        final_citation_ids: Sequence[str],
        report_hash: str,
        output_schema_validated: bool,
        failure_code: str | None,
        completed_at: datetime,
    ) -> CodexSessionManifest:
        if generation_status not in {
            ReportGenerationStatus.COMPLETED,
            ReportGenerationStatus.FAILED,
        }:
            raise ValueError("only terminal sessions can be finalized")
        with self._lock:
            ledger = self._ledger(session_id)
            payload = self._manifest_payload(
                ledger,
                generation_status=generation_status,
                final_citation_ids=final_citation_ids,
                report_hash=report_hash,
                output_schema_validated=output_schema_validated,
                failure_code=failure_code,
                completed_at=completed_at,
            )
            digest = canonical_session_manifest_hash(payload)
            candidate = CodexSessionManifest(**payload, canonical_hash=digest)
            if ledger.finalized_manifest is not None:
                if ledger.finalized_manifest != candidate:
                    raise CodexToolError(
                        "session_finalized",
                        "the finalized session manifest cannot be replaced",
                    )
                return ledger.finalized_manifest
            ledger.finalized_manifest = candidate
            return candidate

    def manifest(
        self,
        session_id: str,
        *,
        generation_status: ReportGenerationStatus = ReportGenerationStatus.RUNNING,
        final_citation_ids: Sequence[str] = (),
        report_hash: str | None = None,
        output_schema_validated: bool = False,
        failure_code: str | None = None,
        completed_at: datetime | None = None,
    ) -> CodexSessionManifest:
        if generation_status in {
            ReportGenerationStatus.COMPLETED,
            ReportGenerationStatus.FAILED,
        }:
            if report_hash is None or completed_at is None:
                raise ValueError("terminal session manifests require report hash and completed_at")
            return self.finalize_session(
                session_id,
                generation_status=generation_status,
                final_citation_ids=final_citation_ids,
                report_hash=report_hash,
                output_schema_validated=output_schema_validated,
                failure_code=failure_code,
                completed_at=completed_at,
            )
        with self._lock:
            ledger = self._ledger(session_id)
            if ledger.finalized_manifest is not None:
                return ledger.finalized_manifest
            payload = self._manifest_payload(
                ledger,
                generation_status=generation_status,
                final_citation_ids=final_citation_ids,
                report_hash=report_hash,
                output_schema_validated=output_schema_validated,
                failure_code=failure_code,
                completed_at=completed_at,
            )
            digest = canonical_session_manifest_hash(payload)
            return CodexSessionManifest(**payload, canonical_hash=digest)


class CodexInputPackBuilder:
    def __init__(self, tool_registry: GovernanceToolRegistry) -> None:
        self._tool_registry = tool_registry

    def build(
        self,
        *,
        snapshot: GovernanceSnapshot,
        question_descriptions: Mapping[str, str],
        important_records: Sequence[ObjectReference],
        important_event_ids: Sequence[str],
        research_budget: ResearchBudget,
        report_output_schema_hash: str,
        created_at: datetime,
    ) -> CodexInputPack:
        coverage = {item.question_id: item for item in snapshot.question_level_coverage}
        if set(coverage) != set(EXACT_QUESTION_IDS):
            raise ValueError("Codex input pack requires all eleven governance questions")
        if set(question_descriptions) != set(EXACT_QUESTION_IDS):
            raise ValueError("question descriptions must match the exact v1 question set")
        anchors: dict[str, list[str]] = {question_id: [] for question_id in EXACT_QUESTION_IDS}
        for link in snapshot.anchor_links:
            anchors[link.question_id].append(link.anchor_record_id)
        summaries = tuple(
            QuestionSummary(
                question_id=question_id,
                completeness_status=coverage[question_id].completeness_status,
                coverage_entry_ids=coverage[question_id].coverage_entry_ids,
                anchor_record_ids=tuple(sorted(anchors[question_id])),
                summary=question_descriptions[question_id],
            )
            for question_id in EXACT_QUESTION_IDS
        )
        sorted_records = tuple(sorted(important_records, key=lambda item: item.object_id))
        payload = {
            "company_id": snapshot.company_id,
            "state_at": snapshot.state_at,
            "known_at": snapshot.known_at,
            "perspective": snapshot.perspective,
            "governance_snapshot_id": snapshot.governance_snapshot_id,
            "governance_snapshot_hash": snapshot.canonical_snapshot_hash,
            "evidence_manifest_id": snapshot.evidence_manifest_id,
            "evidence_manifest_hash": snapshot.evidence_manifest_hash,
            "question_set_id": "governance_management_questions",
            "question_set_version": snapshot.question_set_version,
            "question_summaries": summaries,
            "important_records": sorted_records,
            "important_event_ids": tuple(sorted(set(important_event_ids))),
            "active_gap_ids": snapshot.active_gap_ids,
            "active_conflict_ids": snapshot.active_conflict_ids,
            "pending_candidate_ids": snapshot.pending_candidate_ids,
            "tool_schemas": self._tool_registry.schema_references(),
            "research_budget": research_budget,
            "temporal_rules": (
                "available_at_lte_known_at",
                "explicit_snapshot_adoption",
                "strict_parent_network_via_research_gate",
            ),
            "report_output_schema_hash": report_output_schema_hash,
        }
        digest = canonical_input_pack_hash(payload)
        return CodexInputPack(
            input_pack_id=f"govinput:{digest}",
            **payload,
            canonical_hash=digest,
            created_at=created_at,
        )


class CodexToolService:
    def __init__(
        self,
        registry: GovernanceToolRegistry,
        recorder: SessionRecorder,
    ) -> None:
        self._registry = registry
        self._recorder = recorder

    def invoke(
        self,
        *,
        session_id: str,
        tool_name: str,
        parameters: Mapping[str, Any],
        occurred_at: datetime,
    ) -> Mapping[str, Any]:
        snapshot_id, snapshot_hash, _revision = self._recorder.current_binding(session_id)
        sequence = self._recorder.next_tool_sequence(session_id)
        input_pack = self._recorder.input_pack(session_id)
        tool_version = "unknown"
        try:
            _reject_sensitive_keys(parameters)
            requested_snapshot = parameters.get("governance_snapshot_id")
            if requested_snapshot is not None and requested_snapshot != snapshot_id:
                raise CodexToolError(
                    "snapshot_binding_mismatch", "tool requests cannot switch snapshots"
                )
            tool = self._registry.get(tool_name)
            tool_version = tool.version
            self._validate_schema_binding(tool, input_pack)
            try:
                _validate_json_schema(
                    _canonical_json_value(parameters),
                    tool.input_schema,
                )
            except _JsonSchemaViolation as exc:
                raise CodexToolError(
                    "invalid_parameters", "tool parameters do not match the fixed schema"
                ) from exc
            normalized = dict(parameters)
            if tool.parameter_validator is not None:
                normalized = dict(tool.parameter_validator(normalized))
            _reject_sensitive_keys(normalized)
            if tool.name == "get_evidence_excerpt":
                allowed_keys = {"evidence_span_id", "governance_snapshot_id"}
                if set(normalized) - allowed_keys:
                    raise CodexToolError(
                        "span_only_violation",
                        "evidence excerpts can only be requested by evidence_span_id",
                    )
                evidence_span_id = normalized.get("evidence_span_id")
                if not isinstance(evidence_span_id, str) or not evidence_span_id.startswith(
                    "govspan:"
                ):
                    raise CodexToolError(
                        "span_only_violation",
                        "evidence excerpts require a governance evidence span ID",
                    )
            try:
                _validate_json_schema(
                    _canonical_json_value(normalized),
                    tool.input_schema,
                )
            except _JsonSchemaViolation as exc:
                raise CodexToolError(
                    "invalid_parameters", "normalized parameters violate the fixed schema"
                ) from exc
            canonical_parameters = canonical_json_text(normalized)
            parameters_hash = _semantic_hash(
                "governance-tool-parameters", normalized, tool.version
            )
        except CodexToolError as exc:
            self._record_failure(
                session_id=session_id,
                snapshot_id=snapshot_id,
                sequence=sequence,
                tool_name=tool_name,
                tool_version=tool_version,
                occurred_at=occurred_at,
                error_code=exc.code,
                status=ToolReadStatus.REJECTED,
            )
            raise
        except (TypeError, ValueError) as exc:
            self._record_failure(
                session_id=session_id,
                snapshot_id=snapshot_id,
                sequence=sequence,
                tool_name=tool_name,
                tool_version=tool_version,
                occurred_at=occurred_at,
                error_code="invalid_parameters",
                status=ToolReadStatus.REJECTED,
            )
            raise CodexToolError("invalid_parameters", "tool parameters are invalid") from exc
        try:
            payload = tool.handler(snapshot_id, normalized)
            if not isinstance(payload, ToolPayload):
                raise CodexToolError(
                    "tool_output_schema_invalid",
                    "governance tool did not return a ToolPayload",
                )
            _reject_sensitive_keys(payload.payload)
            try:
                _validate_json_schema(
                    _canonical_json_value(payload.payload),
                    tool.output_schema,
                )
            except _JsonSchemaViolation as exc:
                raise CodexToolError(
                    "tool_output_schema_invalid",
                    "tool payload does not match the fixed output schema",
                ) from exc
            _validate_payload_security(
                payload,
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                known_at=input_pack.known_at,
            )
        except CodexToolError as exc:
            self._record_failure(
                session_id=session_id,
                snapshot_id=snapshot_id,
                sequence=sequence,
                tool_name=tool.name,
                tool_version=tool.version,
                occurred_at=occurred_at,
                error_code=exc.code,
                status=ToolReadStatus.FAILED,
            )
            raise
        except Exception as exc:
            self._record_failure(
                session_id=session_id,
                snapshot_id=snapshot_id,
                sequence=sequence,
                tool_name=tool.name,
                tool_version=tool.version,
                occurred_at=occurred_at,
                error_code="tool_execution_failed",
                status=ToolReadStatus.FAILED,
            )
            raise CodexToolError(
                "tool_execution_failed", "governance tool execution failed"
            ) from exc
        response = {
            "kind": "governance_tool_response",
            "tool_name": tool.name,
            "tool_version": tool.version,
            "governance_snapshot_id": snapshot_id,
            "governance_snapshot_hash": snapshot_hash,
            "payload": payload.payload,
            "references": {
                "record_ids": tuple(sorted(set(payload.actual_record_ids))),
                "claim_ids": tuple(sorted(set(payload.actual_claim_ids))),
                "evidence_span_ids": tuple(
                    sorted(set(payload.actual_evidence_span_ids))
                ),
                "raw_snapshot_ids": tuple(
                    sorted(set(payload.actual_raw_snapshot_ids))
                ),
                "citation_ids": tuple(sorted(set(payload.citation_ids))),
            },
        }
        response_json = canonical_json_text(response)
        response_hash = _semantic_hash(
            "governance-tool-response", response, tool.version
        )
        read_payload = {
            "kind": "codex_tool_read",
            "schema_version": MODEL_SCHEMA_VERSION,
            "session_id": session_id,
            "governance_snapshot_id": snapshot_id,
            "sequence": sequence,
            "tool_name": tool.name,
            "tool_version": tool.version,
            "canonical_parameters_json": canonical_parameters,
            "parameters_hash": parameters_hash,
            "response_payload_json": response_json,
            "response_hash": response_hash,
            "actual_record_ids": tuple(sorted(set(payload.actual_record_ids))),
            "actual_claim_ids": tuple(sorted(set(payload.actual_claim_ids))),
            "actual_evidence_span_ids": tuple(
                sorted(set(payload.actual_evidence_span_ids))
            ),
            "actual_raw_snapshot_ids": tuple(
                sorted(set(payload.actual_raw_snapshot_ids))
            ),
            "citation_ids": tuple(sorted(set(payload.citation_ids))),
            "status": ToolReadStatus.SUCCEEDED,
            "occurred_at": occurred_at,
        }
        read_hash = canonical_tool_read_hash(read_payload)
        read = CodexToolRead(
            tool_read_id=f"govtoolread:{read_hash}",
            **read_payload,
            canonical_hash=read_hash,
        )
        # The append is deliberately before returning any substantive payload.
        self._recorder.append_tool_read(read)
        return response

    @staticmethod
    def _validate_schema_binding(tool: GovernanceTool, input_pack: CodexInputPack) -> None:
        expected = {
            item.tool_name: item
            for item in input_pack.tool_schemas
        }.get(tool.name)
        if expected is None or expected != tool.schema_reference():
            raise CodexToolError(
                "tool_schema_mismatch",
                "runtime tool schema differs from the immutable input pack",
            )

    def validate_persisted_read(
        self,
        read: CodexToolRead,
        *,
        input_pack: CodexInputPack,
        expected_snapshot_hash: str,
    ) -> None:
        """Re-validate a persisted read before a report can be published."""

        expected_read_hash = canonical_tool_read_hash(read)
        if (
            read.canonical_hash != expected_read_hash
            or read.tool_read_id != f"govtoolread:{expected_read_hash}"
        ):
            raise CodexToolError("tool_read_hash_mismatch", "tool read hash is invalid")
        tool = self._registry.get(read.tool_name)
        self._validate_schema_binding(tool, input_pack)
        if read.tool_version != tool.version:
            raise CodexToolError("tool_schema_mismatch", "tool version changed during session")
        parameters = load_canonical_json(read.canonical_parameters_json)
        if read.parameters_hash != _semantic_hash(
            "governance-tool-parameters", parameters, tool.version
        ):
            raise CodexToolError(
                "tool_parameters_hash_mismatch", "persisted parameter hash is invalid"
            )
        try:
            _validate_json_schema(parameters, tool.input_schema)
        except _JsonSchemaViolation as exc:
            raise CodexToolError(
                "tool_input_schema_invalid", "persisted parameters violate tool schema"
            ) from exc
        if read.status != ToolReadStatus.SUCCEEDED:
            return
        if read.response_payload_json is None or read.response_hash is None:
            raise CodexToolError(
                "tool_response_missing", "successful read lacks an inline response"
            )
        response = load_canonical_json(read.response_payload_json)
        if not isinstance(response, Mapping):
            raise CodexToolError(
                "tool_output_schema_invalid", "persisted response is not an object"
            )
        if read.response_hash != _semantic_hash(
            "governance-tool-response", response, tool.version
        ):
            raise CodexToolError("tool_response_hash_mismatch", "tool response hash is invalid")
        if (
            response.get("tool_name") != read.tool_name
            or response.get("tool_version") != read.tool_version
            or response.get("governance_snapshot_id") != read.governance_snapshot_id
            or response.get("governance_snapshot_hash") != expected_snapshot_hash
        ):
            raise CodexToolError(
                "snapshot_binding_mismatch",
                "persisted response does not match its tool/snapshot binding",
            )
        expected_references = {
            "record_ids": list(read.actual_record_ids),
            "claim_ids": list(read.actual_claim_ids),
            "evidence_span_ids": list(read.actual_evidence_span_ids),
            "raw_snapshot_ids": list(read.actual_raw_snapshot_ids),
            "citation_ids": list(read.citation_ids),
        }
        if response.get("references") != expected_references:
            raise CodexToolError(
                "broken_lineage", "persisted response references differ from the read manifest"
            )
        raw_payload = response.get("payload")
        if not isinstance(raw_payload, Mapping):
            raise CodexToolError(
                "tool_output_schema_invalid", "persisted tool payload is not an object"
            )
        try:
            _validate_json_schema(raw_payload, tool.output_schema)
        except _JsonSchemaViolation as exc:
            raise CodexToolError(
                "tool_output_schema_invalid", "persisted payload violates tool schema"
            ) from exc
        _validate_payload_security(
            ToolPayload(
                payload=raw_payload,
                actual_record_ids=read.actual_record_ids,
                actual_claim_ids=read.actual_claim_ids,
                actual_evidence_span_ids=read.actual_evidence_span_ids,
                actual_raw_snapshot_ids=read.actual_raw_snapshot_ids,
                citation_ids=read.citation_ids,
            ),
            snapshot_id=read.governance_snapshot_id,
            snapshot_hash=expected_snapshot_hash,
            known_at=input_pack.known_at,
        )

    def _record_failure(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        sequence: int,
        tool_name: str,
        tool_version: str,
        occurred_at: datetime,
        error_code: str,
        status: ToolReadStatus,
    ) -> None:
        safe_parameters = {"redacted": True}
        payload = {
            "kind": "codex_tool_read",
            "schema_version": MODEL_SCHEMA_VERSION,
            "session_id": session_id,
            "governance_snapshot_id": snapshot_id,
            "sequence": sequence,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "canonical_parameters_json": canonical_json_text(safe_parameters),
            "parameters_hash": _semantic_hash(
                "governance-tool-parameters", safe_parameters, tool_version
            ),
            "status": status,
            "error_code": error_code,
            "occurred_at": occurred_at,
        }
        digest = canonical_tool_read_hash(payload)
        self._recorder.append_tool_read(
            CodexToolRead(
                tool_read_id=f"govtoolread:{digest}",
                **payload,
                canonical_hash=digest,
            )
        )


def make_mapping_tool(
    name: str,
    handler: ToolHandler,
    *,
    version: str = "1.0.0",
) -> GovernanceTool:
    input_schema = {
        "type": "object",
        "additionalProperties": True,
    }
    output_schema = {
        "type": "object",
        "required": ["kind", "object_id", "schema_version", "canonical_hash"],
        "properties": {
            "kind": {"type": "string", "minLength": 1},
            "object_id": {"type": "string", "minLength": 1},
            "schema_version": {"type": "string", "minLength": 1},
            "canonical_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "additionalProperties": True,
    }
    return GovernanceTool(
        name=name,
        version=version,
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler,
    )


__all__ = [
    "CodexInputPackBuilder",
    "CodexToolError",
    "CodexToolService",
    "GovernanceTool",
    "GovernanceToolRegistry",
    "InMemoryCodexSessionRecorder",
    "READ_ONLY_TOOL_NAMES",
    "SessionRecorder",
    "ToolPayload",
    "canonical_input_pack_hash",
    "canonical_session_manifest_hash",
    "canonical_tool_read_hash",
    "make_mapping_tool",
]
