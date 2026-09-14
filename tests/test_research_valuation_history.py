from copy import deepcopy
from decimal import Decimal

import pytest

from analysis.research.valuation_history import METRICS, compare, merge_series, quantile, summarize
from analysis.research.workspace import ResearchError


def row(value):
    return {"value": str(value), "unit": "ratio", "definition": "test", "fact_ids": [str(value)]}


def test_quantiles_and_midrank_do_not_treat_losses_as_cheap_pe():
    series = {str(i): row(v) for i, v in enumerate([-5, 0, 10, 20, 20, 30])}
    result = summarize(series, Decimal(20))
    assert result["excluded_nonpositive_pe"] == 2
    assert result["median"] == '20.0'
    assert result["reference_midrank_percent"] == '50.0'
    assert quantile([Decimal(10), Decimal(20)], Decimal('.1')) == Decimal(11)
    assert summarize({}, Decimal(20))["status"] == "no_positive_pe"


def test_peer_comparison_intersects_complete_positive_dates_without_fill():
    a = {m: {"2026-09-09": row(20), "2026-09-11": row(21)} for m in METRICS}
    b = {m: {"2026-09-09": row(18)} for m in METRICS}
    result = compare({"a": a, "b": b}, Decimal(20))
    assert result["comparison_date"] == "2026-09-09"
    del b['market_price']['2026-09-09']
    assert compare({"a": a, "b": b}, Decimal(20))["status"] == "no_common_complete_date"


def test_recent_window_does_not_inherit_old_high_valuations():
    dates = {'2021-09-09': row(100), '2025-09-09': row(18), '2026-09-09': row(22)}
    result = compare({'a': {m: deepcopy(dates) for m in METRICS}}, Decimal(20))
    assert result['history']['a']['median'] == '22.0'
    assert result['window_sensitivity']['1y']['a']['median'] == '20.0'
    assert result['window_sensitivity']['1y']['a']['reference_midrank_percent'] == '50.0'


def test_conflicting_values_and_definitions_are_not_silently_overwritten():
    original = {"pe": {"2026-09-09": row(20)}}
    merge_series(original, deepcopy(original))
    assert len(original['pe']['2026-09-09']['fact_ids']) == 1
    with pytest.raises(ResearchError, match="conflicting"):
        merge_series(original, {"pe": {"2026-09-09": row(21)}})
    incoming = deepcopy(original)
    incoming['pe']['2026-09-09']['definition'] = 'forecast'
    with pytest.raises(ResearchError, match="conflicting"):
        merge_series(original, incoming)
