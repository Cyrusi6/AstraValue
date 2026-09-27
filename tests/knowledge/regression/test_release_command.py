"""The runnable gate reports the actual candidate and never publishes it."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from analysis.knowledge import KnowledgeService

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/validate_knowledge_release.py"


def run_gate(*args):
    assert SCRIPT.is_file(), "runnable release gate is not implemented"
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def test_real_catalog_blocks_candidate_until_current_reviews_are_rebound(tmp_path):
    service = KnowledgeService.from_catalog(ROOT / "config/methods/knowledge/catalog.v1.json", tmp_path / "store")
    with pytest.raises(ValueError, match=r"knowledge\.es04_q05: missing passed"):
        service.build_candidate(bundle_id="command-test")
    assert not (service.store_root / "default.json").exists()


def test_command_does_not_create_or_substitute_unknown_versions(tmp_path):
    completed = run_gate("--store", tmp_path / "empty", "--bundle-id", "missing")
    assert completed.returncode == 2
    assert "unknown version" in json.loads(completed.stdout)["error"]
    assert not (tmp_path / "empty" / "bundles").exists()


def test_external_records_cannot_satisfy_missing_content_reviews(tmp_path):
    service = KnowledgeService.from_catalog(ROOT / "config/methods/knowledge/catalog.v1.json", tmp_path / "store")
    with pytest.raises(ValueError, match=r"knowledge\.es04_q05: missing passed"):
        service.build_candidate(bundle_id="command-evidence")
    assert not (service.store_root / "bundles" / "command-evidence.json").exists()
