"""Self-contained report attachments for hash-verified custom Python executions."""
from copy import deepcopy
from pathlib import Path

from .workspace import ResearchError, read_json, sha


def _read(path, expected_hash, *, json=False):
    path = Path(path)
    try:
        if sha(path) != expected_hash:
            raise ResearchError("custom_archive_integrity_failed")
        return read_json(path) if json else path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResearchError("custom_archive_file_missing") from exc


def archive_exploration(workspace, research_id, exploration):
    """Called only after the execution service has checked snapshot, files, sources and validation."""
    archived = deepcopy(exploration)
    archived["code"] = _read(exploration["code_path"], exploration["code_sha256"])
    archived["input_data"] = _read(exploration["input_path"], exploration["input_sha256"], json=True)
    if exploration.get("validation_status") != "validated":
        return archived
    validation_id = exploration.get("validation_id")
    validation = next((x for x in workspace.artifacts(research_id, "exploration_validation")
                       if x["artifact_id"] == validation_id), None)
    if not validation or validation.get("status") != "passed" or validation.get("exploration_id") != exploration["artifact_id"]:
        raise ResearchError("custom_archive_validation_missing")
    validation = deepcopy(validation)
    for execution in validation.get("executions", []):
        files = execution.get("files", {})
        if execution.get("name") == "independent":
            descriptor = files.get("code.py", {})
            validation["validation_code"] = _read(descriptor["path"], descriptor["sha256"])
        if "result.json" in files:
            descriptor = files["result.json"]
            execution["result"] = _read(descriptor["path"], descriptor["sha256"], json=True)
    archived["validation"] = validation
    return archived
