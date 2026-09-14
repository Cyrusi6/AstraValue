from .workspace import ResearchWorkspace


def operations(workspace: ResearchWorkspace) -> dict:
    from .calculations import Calculations
    from .charts import Charts
    from .drafts import Drafts
    from .jobs import MaterialJobs
    from .knowledge import Knowledge
    from .reports import Reports
    registry = {name: getattr(workspace, name) for name in (
        "prepare_research", "get_task", "query_research", "read_evidence",
    )}
    for instance, names in (
        (Calculations(workspace), ("calculate",)),
        (Charts(workspace), ("chart_catalog", "create_chart", "view_chart")),
        (Drafts(workspace), ("save_section", "save_conclusion", "get_draft")),
        (MaterialJobs(workspace), ("request_materials", "query_material_requirements", "resume_task", "adopt_snapshot")),
        (Knowledge(workspace), ("search_knowledge",)),
        (Reports(workspace), ("build_report", "view_report")),
    ):
        registry.update({name: getattr(instance, name) for name in names})

    def get_task(research_id: str, include_pack: bool = False):
        """Read a research r_ ID or background j_ task ID. Status reads never execute network jobs."""
        return MaterialJobs(workspace).get(research_id) if research_id.startswith("j_") else workspace.get_task(research_id, include_pack)

    registry["get_task"] = get_task
    return registry
