from __future__ import annotations

import inspect

from analysis.governance import models, ports
from analysis.governance.ports import GovernanceAcquisitionPort
from tests.governance.fakes import FakeGovernanceAcquisitionPort


def test_fake_drives_governance_acquisition_port_without_control_plane_copy() -> None:
    run = {"run_id": "shared-run:1"}
    coverage = (
        {"coverage_id": "shared-coverage:1", "question_id": "GOV.Q01.OWNERSHIP_CONTROL"},
        {"coverage_id": "shared-coverage:2", "question_id": "GOV.Q02.PLEDGE_FREEZE"},
    )
    manifest = {"manifest_id": "shared-manifest:1"}
    artifact = {"raw_snapshot_id": "shared-raw:1", "content_hash": "a" * 64}
    fake = FakeGovernanceAcquisitionPort(
        runs={"shared-run:1": run},
        coverage={"shared-run:1": coverage},
        manifests={"shared-manifest:1": manifest},
        artifacts={("shared-manifest:1", "shared-raw:1"): artifact},
        acquisition_handler=lambda request: {
            "request": request,
            "manifest_id": "shared-manifest:2",
        },
    )

    assert isinstance(fake, GovernanceAcquisitionPort)
    assert fake.get_run("shared-run:1") is run
    assert fake.get_coverage(
        "shared-run:1", question_ids=("GOV.Q02.PLEDGE_FREEZE",)
    ) == (coverage[1],)
    assert fake.get_manifest("shared-manifest:1") is manifest
    assert fake.load_artifact(
        "shared-manifest:1", "shared-raw:1", for_llm=False
    ) is artifact
    result = fake.request_acquisition({"gap_id": "govgap:1"})
    assert result == {
        "request": {"gap_id": "govgap:1"},
        "manifest_id": "shared-manifest:2",
    }
    assert fake.artifact_reads == [("shared-manifest:1", "shared-raw:1", False)]


def test_governance_package_does_not_define_second_shared_control_plane() -> None:
    forbidden = {
        "GovernanceRun",
        "GovernanceCheckpoint",
        "ContentBlob",
        "EvidenceSnapshotManifest",
    }
    assert forbidden.isdisjoint(vars(models))
    assert forbidden.isdisjoint(vars(ports))
    assert not any(
        name in inspect.getsource(ports)
        for name in ("class GovernanceRun", "class GovernanceCheckpoint", "class ContentBlob")
    )
