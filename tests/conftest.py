from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.bootstrap import StorageBootstrapper
from analysis.acquisition.models import (
    AcquisitionRun,
    CoverageEntry,
    CoveragePlanDisposition,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
    SourceDefinitionRef,
)
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH, SourceRegistryLoader
from analysis.acquisition.repository import AcquisitionRepository
from analysis.demo import build_demo_request
from analysis.service import AnalysisService
from analysis.storage import ReportStorage
from analysis.timeseries import TimeSeriesStore


@pytest.fixture
def service(tmp_path):
    storage = ReportStorage(tmp_path / "analysis.db")
    timeseries = TimeSeriesStore(tmp_path / "timeseries.duckdb", tmp_path / "parquet")
    return AnalysisService(storage=storage, timeseries=timeseries)


@pytest.fixture
def demo_request():
    return build_demo_request()


@pytest.fixture
def demo_report(service, demo_request):
    return service.create_report(demo_request)


@pytest.fixture
def acquisition_store(tmp_path):
    """Small, real v6 repository graph shared by acquisition storage tests."""

    now = datetime.now(timezone.utc)
    db_path = tmp_path / "analysis.db"
    data_root = tmp_path / "data"
    bootstrap = StorageBootstrapper(db_path, data_root).bootstrap()
    repository = AcquisitionRepository(db_path)

    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    loaded_registry = loader.load_registry(
        INITIAL_REGISTRY_PATH,
        question_set=questions,
    )
    registry = loaded_registry.registry
    repository.save_source_registry_version(registry)
    definition = registry.definition("cninfo.disclosures")
    definition_hash = loaded_registry.source_definition_hashes[
        (definition.source_definition_id, definition.version)
    ]
    query = definition.queries[0]
    run = AcquisitionRun(
        run_id="run-storage-1",
        ticker="600519",
        company_name="贵州茅台",
        mode="baseline",
        as_of=now,
        created_at=now,
        registry_id=registry.registry_id,
        registry_version=registry.registry_version,
        registry_content_hash=loaded_registry.content_hash,
        question_set_id=questions.question_set.question_set_id,
        question_set_version=questions.question_set.version,
        question_set_content_hash=questions.content_hash,
        source_definition_refs=(
            SourceDefinitionRef(
                source_definition_id=definition.source_definition_id,
                version=definition.version,
                content_hash=definition_hash,
            ),
        ),
        company_anchor_date=now.date(),
        company_anchor_quality="fixture",
        storage_namespace_id=bootstrap.namespace.namespace_id,
    )
    plan_item = PhysicalQueryPlanItem(
        plan_item_id="plan-storage-1",
        run_id=run.run_id,
        source_definition_id=definition.source_definition_id,
        source_definition_version=definition.version,
        query_id=query.query_id,
        query_family=query.query_family,
        execution_key="execution-storage-1",
        request_method=query.request_method,
        endpoint=query.endpoint,
        normalized_parameters={"ticker": "600519"},
        partition_key="periodic-reports",
        pagination_fingerprint="pagination-v1",
        ordinal=0,
        time_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        time_end=now,
    )
    coverage_entries = tuple(
        CoverageEntry(
            coverage_entry_id=f"coverage-storage-{index}",
            run_id=run.run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            question_id=questions.question_set.topics[index - 1].question_id,
            query_id=query.query_id,
            plan_disposition=CoveragePlanDisposition.REQUIRED,
            time_start=plan_item.time_start,
            time_end=plan_item.time_end,
        )
        for index in (1, 2)
    )
    links = tuple(
        PhysicalQueryCoverageLink(
            plan_item_id=plan_item.plan_item_id,
            coverage_entry_id=entry.coverage_entry_id,
        )
        for entry in coverage_entries
    )
    repository.save_plan_bundle(run, (plan_item,), coverage_entries, links)
    return SimpleNamespace(
        repository=repository,
        db_path=db_path,
        data_root=data_root,
        namespace=bootstrap.namespace,
        now=now,
        registry=registry,
        loaded_registry=loaded_registry,
        questions=questions,
        definition=definition,
        run=run,
        plan_item=plan_item,
        coverage_entries=coverage_entries,
        links=links,
    )
