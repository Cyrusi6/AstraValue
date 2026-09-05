from dataclasses import replace
from datetime import timedelta
import json

import pytest

from analysis.acquisition.models import AcquisitionRunKind, SnapshotIntegrityEvent, SnapshotIntegrityStatus
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH, SourceRegistryLoader
from analysis.acquisition.validators import content_contract_compatible, validate_observation_anchor
from orchestrator_support import NOW, ScenarioAdapter, discovery_result, envelope, make_runtime, resource, targeted_plan


@pytest.mark.parametrize("failure", [None, "same_200", "changed_200", "undeclared", "url", "hash", "quarantine", "inflight_quarantine", "unsent"])
def test_new_production_observation_may_conditionally_reuse_adhoc_content(tmp_path, failure):
    phase = 0
    validators = []
    snapshot = None
    current = None
    chosen = resource()
    def fetch(work):
        nonlocal phase
        phase += 1
        validators.append(work.validators)
        if phase == 1:
            return envelope(url=work.resource.url, body=b"%PDF-1.4\nfixture\n%%EOF",
                content_type="application/pdf", headers={"last-modified":"Wed, 02 Sep 2026 08:00:00 GMT"})
        if failure == "inflight_quarantine":
            current.repository.append_snapshot_integrity_event(SnapshotIntegrityEvent(snapshot_id=snapshot.snapshot_id,
                status=SnapshotIntegrityStatus.QUARANTINED, reason_code="fixture"))
        if failure in {"same_200", "changed_200"}:
            body = b"%PDF-1.4\nfixture\n%%EOF" if failure == "same_200" else b"%PDF-1.4\nchanged fixture\n%%EOF"
            return envelope(url=work.resource.url, body=body, content_type="application/pdf")
        return envelope(url=work.resource.url, status=304, body=b"", content_type="application/pdf")
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _,work: discovery_result(work, resources=(chosen,), declared_total=1), fetch)
    root = tmp_path / "runtime"
    old_payload = json.loads(DEFAULT_REGISTRY_PATH.with_name("business_model_sources.v1.7.json").read_text("utf-8"))
    old_payload["definitions"][0]["retention_policy"]["discovery_body"] = "minimal_proof"
    for query in old_payload["definitions"][0]["queries"]:
        query["parameter_bindings"] = {}
        query["discovery_body_policy"] = "minimal_proof"
    old_path = tmp_path / "old-registry.json"
    old_path.write_text(json.dumps(old_payload, ensure_ascii=False), "utf-8")
    old = make_runtime(root, adapter, registry_path=old_path)
    first = targeted_plan(old, run_kind=AcquisitionRunKind.AD_HOC)
    old.orchestrator.execute_run(first.run.run_id)
    snapshot = old.repository.find_raw_resource_snapshot(resource_role="content", canonical_resource_id=chosen.canonical_resource_id)
    old_run = old.repository.get_run(first.run.run_id)
    with old.repository._connect(readonly=True) as connection:
        assert connection.execute("SELECT count(*) FROM source_checkpoints").fetchone()[0] == 0
    old.close()
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text("utf-8"))
    payload["definitions"][0]["retention_policy"]["discovery_body"] = "minimal_proof"
    for query in payload["definitions"][0]["queries"]:
        query["parameter_bindings"] = {}
        query["discovery_body_policy"] = "minimal_proof"
    policy = payload["definitions"][0]["incremental_policy"]
    if failure == "undeclared":
        policy.pop("content_validator_compatible_from_versions")
    elif failure == "unsent":
        policy.update(use_etag=False, use_last_modified=False)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload,ensure_ascii=False), "utf-8")
    current = make_runtime(root, adapter, registry_path=path)
    if failure == "url":
        chosen = replace(chosen, url="https://static.cninfo.com.cn/finalpage/other.pdf")
    elif failure == "hash":
        (current.data_root / snapshot.archive_relative_path).write_bytes(b"damaged")
    elif failure == "quarantine":
        current.repository.append_snapshot_integrity_event(SnapshotIntegrityEvent(snapshot_id=snapshot.snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED, reason_code="fixture"))
    second = targeted_plan(current, mode="baseline", as_of=NOW+timedelta(hours=1))
    result = current.orchestrator.execute_run(second.run.run_id)
    assert current.repository.get_run(first.run.run_id) == old_run
    assert current.repository.get_raw_resource_snapshot(snapshot.snapshot_id) == snapshot
    if failure in {None, "same_200", "changed_200"}:
        expected = {"success":2} if failure == "changed_200" else {"success":1,"unchanged":1}
        assert result.outcome_counts == expected
        obs = next(o for a in result.attempt_ids for o in current.repository.list_resource_observations(attempt_id=a))
        if failure == "changed_200":
            assert obs.snapshot_id != snapshot.snapshot_id
            assert obs.disposition.value == "changed"
            assert obs.validator_source_snapshot_id == snapshot.snapshot_id
            replacement = current.repository.get_raw_resource_snapshot(obs.snapshot_id)
            assert replacement.available_at == obs.retrieved_at
        else:
            assert obs.snapshot_id == snapshot.snapshot_id
        assert obs.source_definition_version == "1.8.0"
        assert obs.request_summary["validator_source_definition_version"] == "1.6.0"
        assert validators[1] == {"last_modified":"Wed, 02 Sep 2026 08:00:00 GMT"}
        with current.repository._connect(readonly=True) as connection:
            validate_observation_anchor(connection, obs.model_dump(mode="json"))
            tampered = obs.model_dump(mode="json")
            tampered["request_summary"]["validator_source_definition_version"] = "0.0.0"
            with pytest.raises(ValueError, match="incompatible_content"):
                validate_observation_anchor(connection, tampered)
    else:
        assert "unchanged" not in result.outcome_counts
        assert result.material_gap_count > 0
        if failure != "inflight_quarantine":
            assert validators[1] == {}
    current.close()


def test_content_compatibility_checks_identity_time_and_permission_separately():
    loader=SourceRegistryLoader()
    current=loader.load_registry().definition("cninfo.disclosures")
    old=loader.load_registry(DEFAULT_REGISTRY_PATH.with_name("business_model_sources.v1.7.json")).definition("cninfo.disclosures")
    assert content_contract_compatible(current,old)
    assert not current.incremental_policy.checkpoint_compatible_from_versions
    for field,value in [("source_definition_id","other.source"),("upstream_identity","other"),
                        ("source_timezone","UTC")]:
        assert not content_contract_compatible(current,old.model_copy(update={field:value}))
    denied=old.model_copy(update={"license_policy":old.license_policy.model_copy(update={"archive_original":"denied"})})
    assert not content_contract_compatible(current,denied)


def test_all_published_registry_hashes_remain_frozen():
    expected={"1.4":"46d267440e5151226bda9b83a4284fd7ff2572935c8968cf751dc06b5262f500",
              "1.5":"8bf43179d583de4536e6c10aa2f30f11e6695d5c7cb18aadef60b96a43f79c14",
              "1.6":"a3eb504749e4307a5cdc37fb3c34cdf98e10c86e9847e1a678a6a08b3ceea64b",
              "1.7":"497038f52dc9b92faed3945cc99221e816fcaec4659ca417abb37f6c01bf2ee7",
              "1.8":"bf6743b55880dd6dd26ff3d538412d0e0e5ffd226869b87a258b9fb1db6ad8ba"}
    loader=SourceRegistryLoader()
    for version,sha in expected.items():
        assert loader.load_registry(DEFAULT_REGISTRY_PATH.with_name(f"business_model_sources.v{version}.json")).content_hash==sha
