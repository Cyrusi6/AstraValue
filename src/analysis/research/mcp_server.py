"""Run with python -m analysis.research.mcp_server (stdio, no stdout logs)."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP, Image

from .tools import operations
from .workspace import POLICY, ResearchWorkspace


def create_server(workspace: ResearchWorkspace | None = None):
    server = FastMCP("AstraValue Research", instructions=POLICY)
    registry = operations(workspace or ResearchWorkspace())
    for name, function in registry.items():
        if name in {"view_chart", "view_report"}:
            continue
        server.tool(name=name)(function)

    @server.tool()
    def view_chart(research_id: str, chart_id: str) -> Image:
        """View a verified chart, returned directly as a PNG image."""
        return Image(path=registry["view_chart"](research_id, chart_id)["path"])

    @server.tool()
    def view_report(research_id: str, report_id: str, page: int = 1) -> Image:
        """View a rendered PDF page, returned directly as a PNG image."""
        return Image(path=registry["view_report"](research_id, report_id, page)["path"])
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
