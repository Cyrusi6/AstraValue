"""Protocol acceptance is distinct from actual Codex/Claude host acceptance."""
import asyncio
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")


def test_stdio_initialize_list_and_invalid_research_is_structured_error():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check():
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
        async with stdio_client(StdioServerParameters(command=sys.executable,
            args=["-m", "analysis.research.mcp_server"], env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                schemas = {tool.name: tool.inputSchema for tool in listing.tools}
                assert {'material_types', 'refresh', 'execute'} <= set(schemas['request_materials']['properties'])
                assert schemas['prepare_research']['properties']['offline']['default'] is False
                assert {'run_python_analysis','get_python_analysis','validate_python_analysis','review_custom_chart'} <= names
                assert {'research_id','purpose','code','inputs','mode'} <= set(schemas['run_python_analysis']['properties'])
                assert 'validation_code' in schemas['validate_python_analysis']['properties']
                assert {'visual_review','data_review'} <= set(schemas['review_custom_chart']['properties'])
                assert {"prepare_research", "calculate", "save_section", "build_report", "view_report", "query_valuation"} <= names
                response = await session.call_tool("get_task", {"research_id": "nonexistent-test"})
                assert response.isError
                assert "unknown_research_id" in str(response.content)

    asyncio.run(check())
