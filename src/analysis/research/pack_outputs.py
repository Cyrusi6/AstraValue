"""Byte-preserving inheritance of all explicitly frozen pack artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from .workspace import ResearchError


def read_pack_outputs(directory: Path, manifest: dict) -> dict[str, bytes]:
    """Read only manifest-bound files, rejecting changed bytes or escaping paths."""
    root = directory.resolve()
    hashes = manifest.get("output_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise ResearchError("candidate_parent_output_manifest_missing")
    outputs = {}
    for name, expected in hashes.items():
        if (not isinstance(name, str) or not name or "\\" in name
                or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or name == "manifest.json"):
            raise ResearchError("candidate_parent_output_path_invalid")
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ResearchError("candidate_parent_output_path_invalid")
        if not path.is_file():
            raise ResearchError("candidate_parent_output_missing:" + name)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ResearchError("candidate_parent_output_changed:" + name)
        outputs[name] = content
    return outputs


def output_hashes(outputs: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in outputs.items()}
