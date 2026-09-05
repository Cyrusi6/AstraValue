"""Content-validator compatibility is independent of production checkpoints."""
from __future__ import annotations

from typing import Any

from .models import PolicyDecision, SourceDefinition


def content_versions(definition: SourceDefinition) -> tuple[str, ...]:
    return tuple(dict.fromkeys((definition.version, *(
        definition.incremental_policy.content_validator_compatible_from_versions or ()
    ))))


def content_contract_compatible(current: SourceDefinition, previous: SourceDefinition) -> bool:
    if previous.version not in content_versions(current):
        return False
    for field in ("source_definition_id", "upstream_identity", "adapter_key",
                  "source_timezone", "published_at_precision"):
        if getattr(current, field) != getattr(previous, field):
            return False
    for definition in (current, previous):
        if (definition.license_policy.archive_original != PolicyDecision.ALLOWED
                or definition.retention_policy.content_body != PolicyDecision.ALLOWED):
            return False
    # Discovery categories may change; the interpretation of the content
    # identity and its publication time must not change with them.
    def rules(definition: SourceDefinition) -> set[tuple[str, str]]:
        return {(query.query_id, query.canonical_id_rule) for query in definition.queries
                if query.fetch_policy.value == "required_attachment"}
    return rules(current) == rules(previous)


def snapshot_matches_resource(snapshot: Any, resource: Any, namespace_id: str) -> bool:
    return (
        snapshot.storage_namespace_id == namespace_id
        and snapshot.canonical_resource_id == resource.canonical_resource_id
        and snapshot.canonical_url == resource.resource_url
        and snapshot.upstream_material_id == (resource.upstream_material_id or resource.canonical_resource_id)
        and snapshot.published_at_precision == resource.published_at_precision
        and snapshot.source_timezone == resource.source_timezone
        and snapshot.published_at == resource.published_at
        and str(snapshot.policy_decision).lower().startswith("allowed")
    )


def validate_observation_anchor(connection: Any, data: dict[str, Any]) -> None:
    """Audit cross-version reuse and changed-content predecessor lineage."""
    import json
    from .models import DiscoveredResource, RawResourceSnapshot

    snapshot_id = data.get("snapshot_id")
    if not snapshot_id:
        return
    row = connection.execute("SELECT payload FROM raw_resource_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError("observation_snapshot_missing")
    snapshot = RawResourceSnapshot.model_validate_json(row[0])
    if snapshot.source_definition_id != data["source_definition_id"]:
        raise ValueError("observation_snapshot_source_mismatch")
    validator_id = data.get("validator_source_snapshot_id", data.get("validator_snapshot_id"))
    anchor = snapshot
    if snapshot.source_definition_version == data["source_definition_version"]:
        if not validator_id or validator_id == snapshot_id:
            return
        row = connection.execute("SELECT payload FROM raw_resource_snapshots WHERE snapshot_id=?", (validator_id,)).fetchone()
        if row is None:
            raise ValueError("validator_snapshot_missing")
        anchor = RawResourceSnapshot.model_validate_json(row[0])
        if anchor.source_definition_version == data["source_definition_version"]:
            return
    source_id = snapshot.source_definition_id
    def definition(version: str) -> SourceDefinition:
        result = connection.execute(
            "SELECT payload FROM source_definition_versions WHERE source_definition_id=? AND source_definition_version=?",
            (source_id, version),
        ).fetchone()
        if result is None:
            raise ValueError("validator_definition_missing")
        return SourceDefinition.model_validate_json(result[0])
    current = definition(data["source_definition_version"])
    previous = definition(anchor.source_definition_version)
    resource_row = connection.execute("SELECT payload FROM discovered_resources WHERE discovered_resource_id=?",
                                      (data.get("discovered_resource_id"),)).fetchone()
    run_row = connection.execute(
        "SELECT r.payload FROM acquisition_runs r JOIN acquisition_attempts a ON a.run_id=r.run_id WHERE a.attempt_id=?",
        (data["attempt_id"],),
    ).fetchone()
    if resource_row is None or run_row is None:
        raise ValueError("validator_lineage_missing")
    resource = DiscoveredResource.model_validate_json(resource_row[0])
    namespace_id = json.loads(run_row[0])["storage_namespace_id"]
    full_body_matches = (data.get("http_status") == 200
        and data.get("response_summary", {}).get("body_sha256") == snapshot.sha256
        and data.get("response_summary", {}).get("body_byte_length") == snapshot.byte_length)
    conditional_matches = (data.get("http_status") == 304
        and snapshot_id == anchor.snapshot_id
        and bool(data.get("etag") or data.get("last_modified")))
    changed = snapshot.sha256 != anchor.sha256
    if (not content_contract_compatible(current, previous)
            or anchor.source_definition_id != snapshot.source_definition_id
            or not snapshot_matches_resource(anchor, resource, namespace_id)
            or not snapshot_matches_resource(snapshot, resource, namespace_id)
            or not (current.allows_url(resource.resource_url)
                    or current.allows_url(resource.resource_url, redirect=True))
            or not (full_body_matches or conditional_matches)
            or data.get("disposition") != ("changed" if changed else "unchanged")
            or data.get("attempt_outcome") != ("success" if changed else "unchanged")
            or validator_id != anchor.snapshot_id
            or data.get("original_url") != anchor.canonical_url
            or data.get("final_url") != anchor.canonical_url
            or data.get("request_summary", {}).get("conditional") is not True
            or data.get("request_summary", {}).get("validator_source_definition_version") != previous.version):
        raise ValueError("incompatible_content_validator_anchor")
