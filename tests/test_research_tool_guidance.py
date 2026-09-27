"""Recover from observed research-tool mistakes without reading implementation code."""
import asyncio
import json

import pytest

from test_research_workspace import workspace, dump
from analysis.research.tools import operations
from analysis.research.workspace import ResearchError, sha


@pytest.fixture
def research(workspace):
    pack = next((workspace.root / "packs/600519").glob("*/lite-pack-test"))
    payload = json.loads((pack / "core-pack.json").read_text(encoding="utf-8"))
    payload["metrics"].extend([
        {"metric_id": "operating_cost", "label": "营业成本", "group": "B", "state": "ready",
         "period_type": "cumulative", "period": "2024-12-31", "fact_ref": "F2",
         "fact": {"fact_id": "cost", "value": "20", "unit": "CNY", "period_end": "2024-12-31"}},
        {"metric_id": "market_price", "label": "股价", "group": "F", "state": "ready",
         "period_type": "current", "period": "2025-01-01", "fact_ref": "F3",
         "fact": {"fact_id": "price", "value": "12", "unit": "CNY_per_share", "period_end": "2024-12-31"}},
    ])
    dump(pack / "core-pack.json", payload)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["output_hashes"]["core-pack.json"] = sha(pack / "core-pack.json")
    dump(pack / "manifest.json", manifest)
    rid = workspace.prepare_research("贵州茅台", "2025-01-01")["research_id"]
    return workspace, rid, operations(workspace)


def test_mcp_discovery_publishes_reading_bounds(research):
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError
    from analysis.research.mcp_server import create_server

    async def check():
        server = create_server(research[0])
        schemas = {tool.name: tool.inputSchema for tool in await server.list_tools()}
        for name in ("list_materials", "query_research", "read_material", "read_evidence", "read_document_page"):
            properties = schemas[name]["properties"]
            assert properties["page"]["minimum"] == 1
            field, maximum = ("page_size", 40) if name in {"list_materials", "query_research"} else ("max_tokens", 4000)
            assert properties[field]["minimum"] == 1
            assert properties[field]["maximum"] == maximum
        assert schemas["read_document_page"]["properties"]["document_page"]["minimum"] == 1
        assert "current" in schemas["query_research"]["properties"]["period_type"]["enum"]
        assert "metrics" in schemas["list_materials"]["properties"]["category"]["enum"]
        # MCP rejects the same oversized request before any dataset read.
        with pytest.raises(ToolError, match="page_size") as caught:
            await server.call_tool("query_research", {"research_id": "missing", "page_size": 100})
        assert "40" in str(caught.value)

    asyncio.run(check())


@pytest.mark.parametrize("name,args,field,limit", [
    ("list_materials", {"category": "metrics", "page_size": 100}, "page_size", "1..40"),
    ("query_research", {"page_size": 100}, "page_size", "1..40"),
    ("query_research", {"page_size": 50}, "page_size", "1..40"),
    ("query_research", {"page": 0}, "page", ">=1"),
    ("query_research", {"page_size": 1.5}, "page_size", "1..40"),
    ("read_material", {"material_id": "missing", "max_tokens": 4001}, "max_tokens", "1..4000"),
    ("read_evidence", {"evidence_id": "missing", "page": 0}, "page", ">=1"),
    ("read_document_page", {"evidence_id": "missing", "document_page": 0}, "document_page", ">=1"),
    ("read_document_page", {"evidence_id": "missing", "document_page": 1, "max_tokens": 4001}, "max_tokens", "1..4000"),
])
def test_direct_calls_explain_invalid_field_before_data_access(workspace, name, args, field, limit):
    with pytest.raises(ResearchError) as caught:
        operations(workspace)[name](research_id="missing", **args)
    message = str(caught.value)
    assert field in message and limit in message
    assert "unknown_research_id" not in message


def test_cli_reports_same_pagination_error(workspace, monkeypatch, capsys):
    from analysis.research import cli
    monkeypatch.setattr(cli, "ResearchWorkspace", lambda root: workspace)
    assert cli.main(["query_research", "--arguments", '{"research_id":"missing","page_size":50}']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert "page_size" in result["reason"] and "40" in result["reason"]


def test_unknown_metric_guidance_uses_only_registered_topic_ids(research):
    _, rid, ops = research
    result = ops["query_research"](rid, metric_ids=["operating_income", "operating_cots"])
    assert result["unknown_metric_ids"] == ["operating_cots"]
    assert result["available_metric_ids"] == ["operating_cost", "operating_income"]
    assert result["similar_metric_ids"]["operating_cots"][0] == "operating_cost"
    assert "market_price" not in result["available_metric_ids"]
    assert result["next_action"] == "list_materials"
    assert result["read_entry"] == {"tool": "list_materials", "research_id": rid, "category": "metrics"}
    assert "request_materials" not in json.dumps(result)
    assert "rows" not in result  # No silent substitution of a guessed metric.


def test_wrong_period_gives_actual_options_without_widening_query(research):
    _, rid, ops = research
    result = ops["query_research"](rid, topic="valuation", period_type="instant")
    assert result["total"] == 0 and result["rows"] == [] and result["next_page"] is None
    hint = result["query_hint"]
    assert hint["available_period_types"] == ["current"]
    assert hint["fact_date_examples"] == [{"metric_id": "market_price", "period_type": "current",
                                          "period": "2025-01-01", "fact_period_end": "2024-12-31"}]
    corrected = ops["query_research"](rid, topic="valuation", period_type="current", page_size=40)
    assert corrected["rows"][0]["period"] == "2025-01-01"
    assert corrected["rows"][0]["fact"]["period_end"] == "2024-12-31"
    assert "query_hint" not in corrected


def test_empty_topic_or_page_does_not_invent_period_hints(research):
    _, rid, ops = research
    # The ID exists in another topic, so there is no matching metric to describe.
    wrong_topic = ops["query_research"](rid, topic="valuation", metric_ids=["operating_income"], period_type="instant")
    assert wrong_topic["total"] == 0 and "query_hint" not in wrong_topic
    page_after_end = ops["query_research"](rid, topic="valuation", period_type="current", page=2, page_size=40)
    assert page_after_end["total"] == 1 and page_after_end["rows"] == []
    assert "query_hint" not in page_after_end
    no_fact_date = ops["query_research"](rid, metric_ids=["operating_income"], period_type="instant")
    assert no_fact_date["query_hint"]["available_period_types"] == ["cumulative"]
    assert no_fact_date["query_hint"]["fact_date_examples"] == []
