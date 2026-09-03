from __future__ import annotations

import pytest

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

