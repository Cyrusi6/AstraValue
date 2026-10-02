"""Processing candidates preserve every frozen output, including future artifacts."""
import json
from datetime import date
from pathlib import Path

import pytest

from analysis.research.pack_outputs import read_pack_outputs
from analysis.research.processing import Processing
from analysis.research.statements import Statements
from analysis.research.workspace import ResearchError, ResearchWorkspace, read_json, sha
from analysis.structured.materialization_replay import _export_result
from analysis.structured.research_lite import build_lite_pack
from analysis.structured.research_projection import build_research_projection
from analysis.structured.reporting_bridge import build_report_request
from analysis.structured.scope import STANDARD_PROFILE_ID
from test_materialization import Fixture


class Workspace:
    def __init__(self, root, pack):
        self.state = root / "state"
        self.directory = pack

    def pack(self, research_id):
        manifest = self._verify_pack(self.directory)
        return ({"ticker": "600519", "as_of": "2026-09-13", "snapshot_id": manifest["pack_id"]},
                self.directory, read_json(self.directory / "core-pack.json"))

    def _verify_pack(self, directory):
        return ResearchWorkspace._verify_pack(self, directory)

    def artifact(self, research_id, kind, payload):
        return payload


def _pack(tmp_path, legacy=False):
    source = Fixture()
    source.list_acquisition_coverage = lambda **_: []
    source.add()
    source.add("dividend", "PRETAX_BONUS_RMB", "10", row={"NOTICE_DATE": "2026-08-01", "ASSIGN_PROGRESS": "预案"})
    result = source.run()
    research = build_research_projection(source, source, "run-1", research_profile_id=STANDARD_PROFILE_ID)
    _export_result(result, tmp_path / "source/600519", "ns-1", research_projection=research)
    built = build_lite_pack(input_root=tmp_path / "source", ticker="600519", as_of=date(2026, 9, 13),
        profile_id=STANDARD_PROFILE_ID, output_root=tmp_path / "packs", max_tokens=100000)
    pack = Path(built["pack_dir"])
    manifest = read_json(pack / "manifest.json")
    # A future artifact need not be UTF-8 and may use a nested relative path.
    future = pack / "audit/future-artifact.bin"
    future.parent.mkdir()
    future.write_bytes(b"\xff\x00\r\nkept exactly\r\n")
    manifest["output_hashes"]["audit/future-artifact.bin"] = sha(future)
    if legacy:
        (pack / "question-coverage.jsonl").unlink()
        manifest["output_hashes"].pop("question-coverage.jsonl")
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return pack, result


@pytest.mark.parametrize("operation", ["processing", "statements"])
@pytest.mark.parametrize("legacy", [False, True])
def test_candidates_preserve_full_coverage_events_and_unknown_bytes(tmp_path, monkeypatch, operation, legacy):
    old, materialization = _pack(tmp_path, legacy)
    before = {p: sha(p) for p in old.rglob("*") if p.is_file()}
    workspace = Workspace(tmp_path, old)
    if operation == "processing":
        # Keep all explicit gaps of this sparse fixture; production budget behavior
        # is covered elsewhere. The real builder and standard-profile route run.
        monkeypatch.setattr("analysis.research.processing.build_lite_pack",
            lambda **kwargs: build_lite_pack(**kwargs, max_tokens=100000))
        action = lambda: Processing(workspace).prepare_processing("research")
    else:
        action = lambda: Statements(workspace).prepare_statements("research", [])
    candidate = action()
    path = Path(candidate["candidate_pack_path"])
    manifest = workspace._verify_pack(path)
    assert action()["candidate_pack_path"] == str(path)
    assert (path / "audit/future-artifact.bin").read_bytes() == (old / "audit/future-artifact.bin").read_bytes()
    assert "audit/future-artifact.bin" in manifest["output_hashes"]
    request = build_report_request(path)
    assert request.events == list(materialization.events)
    assert len(request.events) == 1
    if not legacy:
        coverage = (path / "question-coverage.jsonl").read_bytes()
        assert coverage == (old / "question-coverage.jsonl").read_bytes()
        assert "question-coverage.jsonl" in manifest["output_hashes"]
        assert len(request.research_coverage["requirements"]) == len(coverage.splitlines())
        assert len(request.research_coverage["requirements"]) > len(request.research_coverage["core_requirements"])
    elif operation == "statements":
        assert "question-coverage.jsonl" not in manifest["output_hashes"]
    assert before == {p: sha(p) for p in before}


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/absolute", "C:relative", "a/../b", "a\\b", "manifest.json"])
def test_candidate_inheritance_rejects_unbound_output_paths(tmp_path, name):
    with pytest.raises(ResearchError, match="candidate_parent_output_path_invalid"):
        read_pack_outputs(tmp_path, {"output_hashes": {name: "untrusted"}})


@pytest.mark.parametrize("missing", [False, True])
def test_candidate_inheritance_rejects_changed_or_missing_frozen_output(tmp_path, missing):
    file = tmp_path / "question-coverage.jsonl"
    file.write_bytes(b'{"state":"pending"}\r\n')
    manifest = {"output_hashes": {file.name: sha(file)}}
    if missing:
        file.unlink()
    else:
        file.write_bytes(b'{"state":"ready"}\r\n')
    with pytest.raises(ResearchError, match="candidate_parent_output_missing" if missing else "candidate_parent_output_changed"):
        read_pack_outputs(tmp_path, manifest)
