from datetime import date, datetime, timezone

from analysis.adapters.official_adapter import (
    FilingCandidate,
    OfficialDisclosureAdapter,
    _CninfoClient,
    _SseClient,
    _select_filings,
)
from analysis.filing_parser import FilingPeriod
from analysis.models import SyncRequest

from test_dimensional_parser import _complete_annual_text, _document


AS_OF = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _filing(year: int, kind: str, *, provider: str = "cninfo") -> FilingCandidate:
    period_end = {
        "q1": date(year, 3, 31),
        "h1": date(year, 6, 30),
        "q3": date(year, 9, 30),
        "annual": date(year, 12, 31),
    }[kind]
    title_kind = {
        "q1": "第一季度报告",
        "h1": "半年度报告",
        "q3": "第三季度报告",
        "annual": "年度报告",
    }[kind]
    return FilingCandidate(
        ticker="600000",
        company_name="测试公司",
        title=f"测试公司{year}年{title_kind}",
        published_at=datetime(year + (kind == "annual"), 4, 1, tzinfo=timezone.utc),
        url=f"https://example.com/{year}-{kind}.pdf",
        provider=provider,
        announcement_id=f"{year}-{kind}-{provider}",
        period=FilingPeriod(year=year, kind=kind, period_end=period_end),
        metadata={},
    )


def test_select_filings_with_zero_quarters_returns_only_requested_annuals():
    candidates = [
        _filing(2024, "annual"),
        _filing(2025, "annual"),
        _filing(2026, "q1"),
        _filing(2026, "h1"),
    ]

    selected = _select_filings(candidates, annual_years=1, single_quarters=0)

    assert [(item.period.year, item.period.kind) for item in selected] == [
        (2025, "annual")
    ]


def test_dimensions_only_sync_downloads_annual_and_emits_dimension_facts(
    monkeypatch,
    tmp_path,
):
    annual = _filing(2025, "annual")
    quarterly = _filing(2026, "h1")
    monkeypatch.setattr(
        _CninfoClient,
        "list_filings",
        lambda self, ticker, start, end, as_of: [annual, quarterly],
    )
    monkeypatch.setattr(
        _SseClient,
        "list_filings",
        lambda self, ticker, start, end, as_of: [],
    )
    monkeypatch.setattr(
        "analysis.adapters.official_adapter._download_pdf",
        lambda client, url, referer=None: b"fixture-pdf",
    )
    downloaded_titles = []

    def fake_ingest(**kwargs):
        downloaded_titles.append(kwargs["title"])
        return _document(
            tmp_path,
            _complete_annual_text(),
            title=kwargs["title"],
        )

    monkeypatch.setattr(
        "analysis.adapters.official_adapter.ingest_downloaded_document",
        fake_ingest,
    )

    result = OfficialDisclosureAdapter(tmp_path / "raw").sync(
        "600000",
        SyncRequest(
            providers=["official"],
            scopes=["dimensions"],
            as_of=AS_OF,
            annual_years=1,
        ),
    )

    assert downloaded_titles == [annual.title]
    assert result.facts == []
    assert len(result.dimensional_facts) == 30
    assert {item.data_snapshot_id for item in result.dimensional_facts} == {
        result.sync_result_id
    }
    assert "经营维度事实30条" in result.provider_results["official"]
    assert "单季目标0期" in result.provider_results["official"]
