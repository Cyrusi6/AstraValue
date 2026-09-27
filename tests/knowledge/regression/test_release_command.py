"""The runnable gate reports the actual candidate and never publishes it."""
import json
import subprocess
import sys
from pathlib import Path

from analysis.knowledge import KnowledgeService

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/validate_knowledge_release.py"


def run_gate(*args):
    assert SCRIPT.is_file(), "runnable release gate is not implemented"
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def test_real_catalog_reports_missing_acceptance_and_preserves_default(tmp_path):
    service = KnowledgeService.from_catalog(ROOT / "config/methods/knowledge/catalog.v1.json", tmp_path / "store")
    bundle = service.build_candidate(bundle_id="command-test")
    output = tmp_path / "report.json"
    completed = run_gate("--store", service.store_root, "--bundle-id", bundle["bundle_id"], "--output", output)
    assert completed.returncode == 1, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["bundle_id"] == "command-test"
    assert report["total"] == 54
    assert report["ready"] == service.coverage("command-test")["available_count"]
    assert report["checks"]["human_review"] is False
    assert not report["passed"]
    assert report["candidate_content_sha256"] == service.get_bundle("command-test")["content_sha256"]
    assert not (service.store_root / "default.json").exists()


def test_command_does_not_create_or_substitute_unknown_versions(tmp_path):
    completed = run_gate("--store", tmp_path / "empty", "--bundle-id", "missing")
    assert completed.returncode == 2
    assert "unknown version" in json.loads(completed.stdout)["error"]
    assert not (tmp_path / "empty" / "bundles").exists()


def test_external_records_cannot_satisfy_missing_content_reviews(tmp_path):
    service = KnowledgeService.from_catalog(ROOT / "config/methods/knowledge/catalog.v1.json", tmp_path / "store")
    service.build_candidate(bundle_id="command-evidence")
    records = tmp_path / "records.json"
    records.write_text(json.dumps({"bundle_id": "older", "source_checks": [{"method_id": "invented", "outcome": "passed"}]}), encoding="utf-8")
    completed = run_gate("--store", service.store_root, "--bundle-id", "command-evidence", "--acceptance", records)
    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["checks"]["bundle_identity"] is False
    assert report["passed"] is False
