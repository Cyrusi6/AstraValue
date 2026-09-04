from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from analysis.governance.golden import validate_governance_golden


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "golden" / "governance_management_v1.json"


def test_pending_governance_golden_manifest_is_structurally_valid():
    result = validate_governance_golden(MANIFEST)
    assert result["passed"]
    assert result["structurally_valid"]
    assert result["status"] == "pending_manual_validation"
    assert result["pending_count"] == result["check_count"] == 8


def test_pending_governance_golden_manifest_fails_strict_mode():
    result = validate_governance_golden(MANIFEST, strict=True)
    assert not result["passed"]
    assert result["structurally_valid"]


def test_governance_golden_cli_reports_pending_and_strict_nonzero():
    script = ROOT / "scripts" / "validate_governance_golden.py"
    ordinary = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    strict = subprocess.run(
        [sys.executable, str(script), "--strict"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert ordinary.returncode == 0
    assert ordinary.stdout.startswith("pending_manual_validation\n")
    assert strict.returncode != 0
    assert strict.stdout.startswith("pending_manual_validation\n")


def test_pending_manifest_cannot_fake_a_signature(tmp_path: Path):
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["checks"][0]["reviewer"] = "未签署"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = validate_governance_golden(path)
    assert not result["passed"]
    assert any("不得伪填" in error for error in result["errors"])
