from __future__ import annotations

from .models import (
    ReportCreateRequest,
    ReportVersion,
)
from .registry import MethodRegistry
from .reporting import ReportBuilder
from .storage import ReportStorage, report_diff
from .timeseries import TimeSeriesStore


class AnalysisService:
    def __init__(
        self,
        storage: ReportStorage | None = None,
        registry: MethodRegistry | None = None,
        timeseries: TimeSeriesStore | None = None,
    ) -> None:
        self.storage = storage or ReportStorage()
        self.registry = registry or MethodRegistry()
        self.builder = ReportBuilder(self.registry)
        self.timeseries = timeseries or TimeSeriesStore(
            self.storage.db_path.with_suffix(".duckdb"),
            self.storage.db_path.parent / "parquet",
        )

    def create_report(self, request: ReportCreateRequest) -> ReportVersion:
        version = self.storage.next_version(request.ticker)
        report = self.builder.build(request, version=version)
        self.storage.save_sources(request.sources)
        self.storage.save_facts(request.facts)
        self.storage.save_research_records(
            dimensional_facts=request.dimensional_facts,
            events=request.events,
            industry_facts=request.industry_facts,
            peer_sets=request.peer_sets,
        )
        self.timeseries.append_facts(request.facts)
        self.timeseries.append_research_records(
            dimensional_facts=request.dimensional_facts,
            events=request.events,
            industry_facts=request.industry_facts,
            peer_sets=request.peer_sets,
        )
        self.storage.save_report(report)
        self.timeseries.export_snapshot(report.ticker, report.data_snapshot_id, report.facts)
        return report

    def changes(self, report_id: str) -> dict:
        current = self.storage.get_report(report_id)
        previous = self.storage.get_report(current.parent_report_id) if current.parent_report_id else None
        return report_diff(current, previous)
