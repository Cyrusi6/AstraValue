from .workspace import ResearchWorkspace
from threading import Lock

_chart_lock = Lock()


def operations(workspace: ResearchWorkspace) -> dict:
    from .calculations import Calculations
    from .charts import Charts
    from .drafts import Drafts
    from .jobs import MaterialJobs
    from .knowledge import Knowledge
    from .reports import Reports
    from .valuation_history import ValuationHistory
    from .processing import Processing
    registry = {name: getattr(workspace, name) for name in (
        "prepare_research", "get_task", "query_research", "read_evidence", "read_document_page",
    )}
    for instance, names in (
        (Calculations(workspace), ("calculate",)),
        (Charts(workspace), ("chart_catalog", "create_chart", "view_chart")),
        (Drafts(workspace), ("save_section", "save_conclusion", "get_draft")),
        (MaterialJobs(workspace), ("request_materials", "query_material_requirements", "resume_task", "adopt_snapshot")),
        (Knowledge(workspace), ("search_knowledge",)),
        (Reports(workspace), ("build_report", "view_report")),
        (ValuationHistory(workspace), ("query_valuation",)),
        (Processing(workspace), ("prepare_processing",)),
    ):
        registry.update({name: getattr(instance, name) for name in names})

    def get_task(research_id: str, include_pack: bool = False):
        """Read a research r_ ID or background j_ task ID. Status reads never execute network jobs."""
        if research_id.startswith("j_"):
            return MaterialJobs(workspace).get(research_id)
        result = workspace.get_task(research_id, False)
        if include_pack and result.get("stages", {}).get("processing") == "ready":
            result.update(Briefing(workspace).get_research_brief(research_id))
            result.pop("core_gaps", None)
        return result

    registry["get_task"] = get_task
    from .authoring import Authoring
    writing = Authoring(workspace)
    registry.update({name: getattr(writing, name) for name in (
        "get_research_prompt", "save_section", "save_conclusion", "get_draft")})
    from .briefing import Briefing
    registry["get_research_brief"] = Briefing(workspace).get_research_brief
    def prepare_research(company: str, as_of: str = "latest", scope: str = "eight_step", offline: bool = False):
        """Prepare a company task and return the shared clean writing view when data is ready."""
        result = workspace.prepare_research(company, as_of, scope)
        if result.get("research_id") and result.get("stages", {}).get("processing") == "ready":
            rid=result['research_id']
            view=get_task(rid,True)
            if not offline and not workspace.config.get('offline',False):
                from .supplements import existing
                cached=existing(workspace,rid,'sw_industry')
                view['supplement_preparation']=cached or MaterialJobs(workspace).request_materials(rid,
                    '准备申万二级替代分类','用于选取同行',material_types=['sw_industry'])
            return view
        return result
    registry["prepare_research"] = prepare_research
    def create_chart(research_id: str, template: str, calculation_id: str | None = None):
        """Render a chart serially; return compact metadata, preserving all plotting data in storage."""
        with _chart_lock:
            result = Charts(workspace).create_chart(research_id, template, calculation_id)
        return {k:v for k,v in result.items() if k != "data"} | {"data_rows":len(result["data"])}
    registry["create_chart"] = create_chart
    from .calculation_schema import public_calculator
    registry["calculate"] = public_calculator(workspace)
    from .statements import Statements
    statement_tools=Statements(workspace)
    registry.update({name:getattr(statement_tools,name) for name in ('statement_catalog','read_statement','prepare_statements')})
    from .catalog import Catalog
    catalog=Catalog(workspace)
    registry.update({name:getattr(catalog,name) for name in ('list_materials','read_material')})
    from .custom_python import CustomPython
    python_tools=CustomPython(workspace)
    registry.update({name:getattr(python_tools,name) for name in (
        'run_python_analysis','get_python_analysis','validate_python_analysis')})
    registry['review_custom_chart']=Charts(workspace).review_custom_chart
    return registry
