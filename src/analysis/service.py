from __future__ import annotations

from copy import deepcopy

from .models import (
    AssumptionPatchRequest,
    ReportCreateRequest,
    ReportStatus,
    ReportVersion,
    ResearchRating,
    ReviewRequest,
    VerificationStatus,
    new_id,
    utc_now,
)
from .registry import MethodRegistry
from .reporting import ReportBuilder
from .storage import ReportStorage, report_diff
from .timeseries import TimeSeriesStore
from .structured.consumption import is_fact_consumable


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
        request = self._with_synced_facts(request)
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

    def _with_synced_facts(self, request: ReportCreateRequest) -> ReportCreateRequest:
        resolved = request.model_copy(deep=True)
        if not resolved.use_synced_facts or resolved.facts:
            return resolved
        sync = (
            self.storage.get_sync_result(resolved.sync_result_id)
            if resolved.sync_result_id
            else self.storage.latest_sync_result(
                resolved.ticker,
                resolved.as_of,
                required_scope="financials",
            )
        )
        if sync is None:
            return resolved
        request_ticker = "".join(char for char in resolved.ticker if char.isdigit())
        sync_ticker = "".join(char for char in sync.ticker if char.isdigit())
        if request_ticker != sync_ticker:
            raise ValueError(
                f"同步批次股票代码不匹配: 报告{resolved.ticker}，同步批次{sync.ticker}"
            )

        def aware(value):
            from datetime import timezone

            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

        if aware(sync.as_of) > aware(resolved.as_of):
            raise ValueError("同步批次截止时间晚于报告截止时间，禁止未来数据进入报告")
        resolved.sync_result_id = sync.sync_result_id
        if sync.company_name and not resolved.company_name.strip():
            resolved.company_name = sync.company_name
        resolved.sources = list(
            {item.source_id: item for item in [*resolved.sources, *sync.sources]}.values()
        )
        resolved.facts = list(sync.facts)
        dimensional_sync = sync
        if resolved.dimensional_sync_result_id:
            dimensional_sync = self.storage.get_sync_result(
                resolved.dimensional_sync_result_id
            )
        elif not sync.dimensional_facts:
            dimensional_sync = self.storage.latest_sync_result(
                resolved.ticker,
                resolved.as_of,
                required_scope="dimensions",
            )
        if dimensional_sync is not None:
            dimensional_ticker = "".join(
                char for char in dimensional_sync.ticker if char.isdigit()
            )
            if request_ticker != dimensional_ticker:
                raise ValueError(
                    "维度同步批次股票代码不匹配: "
                    f"报告{resolved.ticker}，同步批次{dimensional_sync.ticker}"
                )
            if aware(dimensional_sync.as_of) > aware(resolved.as_of):
                raise ValueError("维度同步批次截止时间晚于报告截止时间，禁止未来数据进入报告")
            resolved.dimensional_sync_result_id = dimensional_sync.sync_result_id
            resolved.dimensional_facts = list(dimensional_sync.dimensional_facts)
            resolved.sources = list(
                {
                    item.source_id: item
                    for item in [*resolved.sources, *dimensional_sync.sources]
                }.values()
            )
        event_sync = sync if sync.events else None
        if resolved.event_sync_result_id:
            event_sync = self.storage.get_sync_result(resolved.event_sync_result_id)
        elif event_sync is None:
            event_sync = self.storage.latest_sync_result(
                resolved.ticker,
                resolved.as_of,
                required_records="events",
            )
        if event_sync is not None:
            event_ticker = "".join(
                char for char in event_sync.ticker if char.isdigit()
            )
            if request_ticker != event_ticker:
                raise ValueError(
                    "事件同步批次股票代码不匹配: "
                    f"报告{resolved.ticker}，同步批次{event_sync.ticker}"
                )
            if aware(event_sync.as_of) > aware(resolved.as_of):
                raise ValueError("事件同步批次截止时间晚于报告截止时间，禁止未来数据进入报告")
            resolved.event_sync_result_id = event_sync.sync_result_id
            resolved.events = list(event_sync.events)
            resolved.sources = list(
                {
                    item.source_id: item
                    for item in [*resolved.sources, *event_sync.sources]
                }.values()
            )
        resolved.verification_records = [
            *resolved.verification_records,
            *sync.verification_records,
        ]
        if resolved.current_price is None:
            usable_prices = [
                item
                for item in sync.facts
                if item.metric_id == "market_price"
                and item.value is not None
                and item.value > 0
                and is_fact_consumable(item, as_of=resolved.as_of)
                and aware(item.as_of) <= aware(resolved.as_of)
            ]
            if usable_prices:
                price = max(
                    usable_prices,
                    key=lambda item: (item.period_end or item.as_of.date(), item.as_of),
                )
                resolved.current_price = price.value
                resolved.price_as_of = price.as_of
        return ReportCreateRequest.model_validate(resolved.model_dump(mode="python"))

    def patch_assumptions(self, report_id: str, patch: AssumptionPatchRequest) -> ReportVersion:
        previous = self.storage.get_report(report_id)
        request = self._request_from_report(previous)
        request.assumptions = patch.assumptions
        if not patch.recalculate:
            report = previous.model_copy(deep=True)
            report.report_id = new_id()
            report.parent_report_id = previous.report_id
            report.version = self.storage.next_version(previous.ticker)
            report.created_at = utc_now()
            report.status = ReportStatus.DRAFT
            report.conclusion.rating_confirmed = False
            report.assumptions = deepcopy(patch.assumptions)
            report.audit.assumptions = deepcopy(patch.assumptions)
            report.audit.version_changes = ["更新情景假设，计算结果已失效，等待重算"]
            report.request_metadata["calculations_stale"] = True
            scenario_section = report.sections[7]
            scenario_section.tables = []
            scenario_section.warnings = ["假设已修改，旧情景结果已移除；请调用recalculate"]
            self.storage.save_report(report)
            return report
        return self._derive(previous, request, use_pinned_bundle=True, change="更新情景假设并确定性重算")

    def recalculate(self, report_id: str) -> ReportVersion:
        previous = self.storage.get_report(report_id)
        return self._derive(previous, self._request_from_report(previous), use_pinned_bundle=True, change="使用冻结方法集合重新计算")

    def reanalyze(self, report_id: str) -> ReportVersion:
        previous = self.storage.get_report(report_id)
        return self._derive(previous, self._request_from_report(previous), use_pinned_bundle=False, change="使用当前方法集合重新分析")

    def review(self, report_id: str, review: ReviewRequest) -> ReportVersion:
        previous = self.storage.get_report(report_id)
        request = self._request_from_report(previous)
        request.requested_rating = review.rating
        if review.confirm_assumptions:
            for item in request.assumptions:
                item.confirmed = True
        report = self.builder.build(
            request,
            version=self.storage.next_version(previous.ticker),
            parent_report_id=previous.report_id,
            bundle_override=previous.audit.method_bundle,
            version_changes=["人工复核并确认研究评级"],
        )
        if review.rating != ResearchRating.UNRATED and report.audit.conflicts:
            raise ValueError("存在未解决数据冲突，只能确认暂不评级")
        if review.rating != ResearchRating.UNRATED and report.audit.unreferenced_numbers:
            raise ValueError("估值输入仍有无血缘数字，只能确认暂不评级")
        scenario_rows = report.sections[7].tables[0]["rows"] if report.sections[7].tables else []
        scenario_complete = len(scenario_rows) == 3 and all(
            item.get("status") == "已计算" and item.get("confirmed") for item in scenario_rows
        )
        if review.rating != ResearchRating.UNRATED and not scenario_complete:
            raise ValueError("确认非暂不评级前必须完成三套情景测算")
        report.status = ReportStatus.REVIEWED
        report.conclusion.rating = review.rating
        report.conclusion.rating_confirmed = True
        report.audit.manual_edits.append(review.note or "用户完成评级与假设复核")
        self.storage.save_report(report)
        return report

    def changes(self, report_id: str) -> dict:
        current = self.storage.get_report(report_id)
        previous = self.storage.get_report(current.parent_report_id) if current.parent_report_id else None
        return report_diff(current, previous)

    def _derive(
        self,
        previous: ReportVersion,
        request: ReportCreateRequest,
        *,
        use_pinned_bundle: bool,
        change: str,
    ) -> ReportVersion:
        bundle = previous.audit.method_bundle if use_pinned_bundle else None
        report = self.builder.build(
            request,
            version=self.storage.next_version(previous.ticker),
            parent_report_id=previous.report_id,
            bundle_override=bundle,
            version_changes=[change],
        )
        self.storage.save_sources(request.sources)
        self.storage.save_facts(request.facts)
        self.storage.save_research_records(
            dimensional_facts=request.dimensional_facts,
            events=request.events,
        )
        self.timeseries.append_facts(request.facts)
        self.timeseries.append_research_records(
            dimensional_facts=request.dimensional_facts,
            events=request.events,
        )
        self.storage.save_report(report)
        self.timeseries.export_snapshot(report.ticker, report.data_snapshot_id, report.facts)
        return report

    @staticmethod
    def _request_from_report(report: ReportVersion) -> ReportCreateRequest:
        return ReportCreateRequest(
            ticker=report.ticker,
            company_name=report.company_name,
            industry=report.industry,
            as_of=report.as_of,
            current_price=report.conclusion.current_price,
            price_as_of=report.conclusion.price_as_of,
            sources=deepcopy(report.audit.sources),
            facts=deepcopy(report.facts),
            dimensional_facts=deepcopy(report.dimensional_facts),
            events=deepcopy(report.events),
            industry_facts=deepcopy(report.industry_facts),
            peer_sets=deepcopy(report.peer_sets),
            claims=deepcopy(report.claims),
            assumptions=deepcopy(report.assumptions),
            verification_records=deepcopy(report.audit.verification_records),
            model_inputs=deepcopy(report.model_inputs),
            requested_rating=report.conclusion.rating,
            report_notes=report.request_metadata.get("report_notes"),
            use_synced_facts=False,
            sync_result_id=report.request_metadata.get("sync_result_id"),
            dimensional_sync_result_id=report.request_metadata.get(
                "dimensional_sync_result_id"
            ),
            event_sync_result_id=report.request_metadata.get(
                "event_sync_result_id"
            ),
            research_coverage_snapshot_id=report.research_coverage_snapshot_id,
            research_coverage=deepcopy(report.research_coverage),
            materialization_selected_fact_ids=deepcopy(
                report.materialization_selected_fact_ids
            ),
            input_metadata={
                key: deepcopy(value)
                for key, value in report.request_metadata.items()
                if key
                not in {
                    "industry_route",
                    "report_notes",
                    "sync_result_id",
                    "dimensional_sync_result_id",
                    "event_sync_result_id",
                }
            },
        )
