from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from analysis.governance.canonical import canonical_sha256
from analysis.governance.models import SourceRole
from analysis.governance.registry import (
    AcquisitionScopeIdentity,
    DEFERRED_SOURCE_IDS,
    EXACT_QUESTION_IDS,
    GovernanceQueryPack,
    GovernanceRegistryError,
    HistoryPolicy,
    QueryStage,
    QueryTimeSemantics,
    load_query_pack,
    load_question_registry,
    load_registry_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = (
    ROOT / "config" / "data_sources" / "governance_management_questions.v1.json"
)
QUERY_PACK_PATH = (
    ROOT / "config" / "data_sources" / "governance_management_query_pack.v1.json"
)


def _raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_query_pack_has_exact_question_traceability_and_many_to_many_mapping():
    bundle = load_registry_bundle(QUESTIONS_PATH, QUERY_PACK_PATH)
    assert tuple(item.question_id for item in bundle.questions.questions) == EXACT_QUESTION_IDS
    assert all(bundle.query_pack.queries_for_question(item) for item in EXACT_QUESTION_IDS)
    assert any(len(query.question_ids) > 1 for query in bundle.query_pack.queries)
    assert bundle.query_pack.physical_query_count == len(bundle.query_pack.queries)


def test_history_horizon_is_question_specific_and_not_a_global_fixed_limit():
    questions = load_question_registry(QUESTIONS_PATH)
    board = questions.question("GOV.Q03.BOARD_COMMITTEES")
    pay = questions.question("GOV.Q05.REMUNERATION_INCENTIVES")
    pledge = questions.question("GOV.Q02.PLEDGE_FREEZE")
    assert (
        board.history_policy.default
        == HistoryPolicy.LAST_FIVE_COMPLETE_FISCAL_YEARS
    )
    assert pay.history_policy.default == HistoryPolicy.LAST_FIVE_COMPLETE_FISCAL_YEARS
    assert (
        pay.history_policy.record_overrides["incentive_plan"]
        == HistoryPolicy.OVERLAPPING_LIFECYCLE
    )
    assert pledge.history_policy.default == HistoryPolicy.OVERLAPPING_LIFECYCLE


def test_loaded_registry_is_deeply_immutable_and_hash_stable() -> None:
    bundle = load_registry_bundle(QUESTIONS_PATH, QUERY_PACK_PATH)
    before_bundle_hash = bundle.canonical_hash
    before_questions_hash = bundle.questions.canonical_hash
    pay = bundle.questions.question("GOV.Q05.REMUNERATION_INCENTIVES")
    overrides = pay.history_policy.record_overrides

    with pytest.raises(TypeError, match="does not support item assignment"):
        overrides["incentive_plan"] = HistoryPolicy.SINCE_LISTING
    with pytest.raises(AttributeError):
        overrides.update(
            {"compensation_record": HistoryPolicy.OVERLAPPING_LIFECYCLE}
        )

    assert bundle.canonical_hash == before_bundle_hash
    assert bundle.questions.canonical_hash == before_questions_hash
    assert (
        pay.history_policy.record_overrides["incentive_plan"]
        == HistoryPolicy.OVERLAPPING_LIFECYCLE
    )
    assert json.loads(bundle.model_dump_json())["questions"]["questions"]


def test_mutable_page_is_current_observation_only_and_uses_retrieval_boundary():
    pack = load_query_pack(QUERY_PACK_PATH)
    current = [
        query
        for query in pack.queries
        if query.time_semantics == QueryTimeSemantics.CURRENT_OBSERVATION_ONLY
    ]
    assert current
    assert all(
        query.physical_query.date_semantics == "retrieved_at_only"
        for query in current
    )
    historical = [
        query
        for query in pack.queries
        if query.time_semantics == QueryTimeSemantics.HISTORICAL_EVENT_STREAM
    ]
    assert historical
    assert all(
        query.physical_query.date_semantics == "half_open_interval"
        for query in historical
    )


def test_source_role_is_fixed_and_authoritative_queries_are_formal():
    pack = load_query_pack(QUERY_PACK_PATH)
    role_by_source = {
        item.source_definition_id: item.source_role for item in pack.source_references
    }
    assert role_by_source["cninfo.disclosures"] == SourceRole.OFFICIAL_DISCLOSURE
    assert role_by_source["sse.disclosures"] == SourceRole.OFFICIAL_DISCLOSURE
    assert role_by_source["szse.disclosures"] == SourceRole.OFFICIAL_DISCLOSURE
    for query in pack.queries:
        if query.stage == QueryStage.FETCH:
            assert role_by_source[query.source_definition_id] in {
                SourceRole.OFFICIAL_DISCLOSURE,
                SourceRole.REGULATOR_EXCHANGE,
            }


def test_akshare_is_discovery_only_and_cannot_require_an_attachment():
    pack = load_query_pack(QUERY_PACK_PATH)
    reference = next(
        item for item in pack.source_references if "akshare" in item.source_definition_id
    )
    query = next(
        item for item in pack.queries if item.source_definition_id == reference.source_definition_id
    )
    assert reference.source_role == SourceRole.DISCOVERY_ONLY
    assert reference.stage == QueryStage.DISCOVERY
    assert query.stage == QueryStage.DISCOVERY
    assert not query.required_attachment
    assert query.physical_query.canonical_semantics == "discovery_lead_only"


def test_deferred_sources_are_disabled_and_absent_from_queries():
    pack = load_query_pack(QUERY_PACK_PATH)
    references = {
        item.source_definition_id: item for item in pack.source_references
    }
    assert DEFERRED_SOURCE_IDS <= references.keys()
    for source_id in DEFERRED_SOURCE_IDS:
        reference = references[source_id]
        assert reference.source_role == SourceRole.DEFERRED
        assert not reference.enabled
        assert reference.stage == QueryStage.DEFERRED
    assert not (
        DEFERRED_SOURCE_IDS
        & {query.source_definition_id for query in pack.queries}
    )


def test_query_pack_rejects_unknown_question_and_wrong_physical_dedup():
    raw = _raw(QUERY_PACK_PATH)
    raw["queries"][0]["question_ids"].append("GOV.Q99.UNKNOWN")
    pack = GovernanceQueryPack.model_validate_json(
        json.dumps(raw, ensure_ascii=False), strict=True
    )
    with pytest.raises(ValueError, match="未知问题"):
        from analysis.governance.registry import GovernanceRegistryBundle

        GovernanceRegistryBundle(
            questions=load_question_registry(QUESTIONS_PATH), query_pack=pack
        )

    duplicate = _raw(QUERY_PACK_PATH)
    clone = copy.deepcopy(duplicate["queries"][0])
    clone["query_id"] = "GOV.QRY.CNINFO.PERIODIC_REPORTS.DUPLICATE"
    clone["execution_key"] = "cninfo:duplicate-key:{ticker}:{start}:{end}:{page}"
    duplicate["queries"].append(clone)
    with pytest.raises(ValueError, match="相同物理查询使用不同execution_key"):
        GovernanceQueryPack.model_validate_json(
            json.dumps(duplicate, ensure_ascii=False), strict=True
        )


def test_scope_identity_is_part_of_checkpoint_manifest_and_latest_selector_keys():
    governance = load_registry_bundle(QUESTIONS_PATH, QUERY_PACK_PATH).scope_identity
    business = AcquisitionScopeIdentity(
        acquisition_scope="business_model",
        question_set_id="business_model_questions",
        question_set_version="1.0.0",
        query_pack_version="1.0.0",
        source_registry_version=governance.source_registry_version,
    )
    common = {
        "ticker": "600519",
        "source_definition_id": "cninfo.disclosures",
        "source_definition_version": "1.0.0",
        "partition_key": "announcements",
        "checkpoint_version": 1,
    }
    assert governance.checkpoint_key(**common) != business.checkpoint_key(**common)
    assert governance.manifest_selector_key(ticker="600519") != business.manifest_selector_key(
        ticker="600519"
    )
    assert governance.latest_selector_key(ticker="600519") != business.latest_selector_key(
        ticker="600519"
    )


def test_governance_query_pack_hash_does_not_change_an_independent_business_pack_hash():
    business_pack = {
        "scope": "business_model",
        "question_set_id": "business_model_questions",
        "query_pack_version": "1.0.0",
        "queries": ["business-overview"],
    }
    before = canonical_sha256(
        business_pack,
        schema_name="business-query-pack",
        schema_version="1",
    )
    load_registry_bundle(QUESTIONS_PATH, QUERY_PACK_PATH)
    after = canonical_sha256(
        business_pack,
        schema_name="business-query-pack",
        schema_version="1",
    )
    assert before == after


def test_registry_validator_contract_and_nonzero_failure(tmp_path: Path):
    script = ROOT / "scripts" / "validate_governance_registry.py"
    success = subprocess.run(
        [
            sys.executable,
            str(script),
            "--questions",
            str(QUESTIONS_PATH),
            "--query-pack",
            str(QUERY_PACK_PATH),
            "--require-plan-traceability",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert success.returncode == 0, success.stderr
    assert success.stdout.startswith("GOVERNANCE_REGISTRY_OK ")

    broken = _raw(QUESTIONS_PATH)
    broken["questions"].pop()
    broken_path = tmp_path / "broken-questions.json"
    broken_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    failure = subprocess.run(
        [sys.executable, str(script), "--questions", str(broken_path)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert failure.returncode != 0
    assert "GOVERNANCE_REGISTRY_INVALID" in failure.stderr
