"""Bind verified reading materials to an adopted snapshot in its state transaction."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .workspace import ResearchError, digest, encode, sha


MATERIAL_KINDS = ("catalog_api_materials", "sw_industry_classification")


def inherit_material_artifacts(connection: sqlite3.Connection, research_id: str,
                               previous_snapshot: str, snapshot_id: str) -> None:
    """The caller owns the transaction; existing registrations are never changed."""
    rows = connection.execute(
        "SELECT id,kind,payload FROM research_artifacts "
        "WHERE research_id=? AND snapshot_id=? AND kind IN (?,?) ORDER BY rowid",
        (research_id, previous_snapshot, *MATERIAL_KINDS),
    ).fetchall()
    for row in rows:
        record = json.loads(row["payload"])
        if record.get("research_id") != research_id or record.get("snapshot_id") != previous_snapshot:
            raise ResearchError("material_inheritance_binding_mismatch")
        try:
            if row["kind"] == "catalog_api_materials":
                # Auto-registration also retains query evidence when no rows are admitted.
                for source in record["items"] + record.get("sources", []):
                    if sha(Path(source["path"])) != source["sha256"]:
                        raise ResearchError("api_original_changed")
            else:
                from .sw_industry import verify
                verify(record)
        except OSError as exc:
            raise ResearchError("material_inheritance_source_unavailable:" + row["id"]) from exc
        if snapshot_id == previous_snapshot:
            continue
        value = {**record, "snapshot_id": snapshot_id,
                 "inherited_from_artifact_id": row["id"],
                 "inherited_from_snapshot_id": previous_snapshot}
        ident = row["kind"] + "_" + digest(value)[:24]
        connection.execute(
            "INSERT OR IGNORE INTO research_artifacts(id,research_id,kind,snapshot_id,payload) VALUES(?,?,?,?,?)",
            (ident, research_id, row["kind"], snapshot_id, encode(value)),
        )
