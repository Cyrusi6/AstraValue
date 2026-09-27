"""Real full-release acceptance. Run separately; unmet evidence must fail."""
import json
import os
import subprocess
import sys
from pathlib import Path

from analysis.knowledge import KnowledgeService

ROOT = Path(__file__).resolve().parents[3]


def test_all_54_questions_and_recorded_acceptance_are_ready(tmp_path):
    script = ROOT / "scripts/validate_knowledge_release.py"
    assert script.is_file(), "complete release command has not been implemented"
    store = Path(os.environ.get("KNOWLEDGE_STORE", str(tmp_path / "release-store")))
    bundle_id = os.environ.get("KNOWLEDGE_BUNDLE")
    if not bundle_id:
        service = KnowledgeService.from_catalog(ROOT / "config/methods/knowledge/catalog.v1.json", store)
        bundle_id = service.build_candidate()["bundle_id"]
    args = [sys.executable, "-X", "utf8", str(script), "--store", str(store), "--bundle-id", bundle_id]
    if os.environ.get("KNOWLEDGE_ACCEPTANCE"):
        args.extend(["--acceptance", os.environ["KNOWLEDGE_ACCEPTANCE"]])
    completed = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode != 2, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"], json.dumps(report, ensure_ascii=False, indent=2)
