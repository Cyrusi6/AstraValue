from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_acquisition_golden.py"
MANIFEST = ROOT / "data" / "golden" / "acquisition_business_model_v1.json"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_pending_manifest_is_structurally_valid_but_not_strictly_signed() -> None:
    normal = _run()
    strict = _run("--strict")
    assert normal.returncode == 0
    assert "ACQUISITION_GOLDEN_PENDING" in normal.stdout
    assert strict.returncode != 0
    assert "ACQUISITION_GOLDEN_PENDING" in strict.stdout


def test_signed_manifest_requires_reviewer_time_and_evidence(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["status"] = "passed"
    for section in payload["review_sections"]:
        section["status"] = "passed"
        section["reviewer"] = "human-reviewer"
        section["reviewed_at"] = "2026-09-03T12:00:00+08:00"
        section["evidence_refs"] = [f"metadata:{section['section_id']}"]
    signed = tmp_path / "signed.json"
    signed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = _run("--manifest", str(signed), "--strict")
    assert result.returncode == 0
    assert "ACQUISITION_GOLDEN_OK" in result.stdout
