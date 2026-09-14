from __future__ import annotations

import base64
from pathlib import Path

from .workspace import ResearchWorkspace, ResearchError, digest, sha


class Charts:
    TEMPLATES = {
        "income_profit": ("收入与归母净利润", ["operating_income", "parent_net_profit"], "line"),
        "profit_cashflow": ("净利润与经营现金流", ["net_profit", "operating_cash_flow"], "bar"),
        "cashflow_proxy": ("经营现金流及扣除长期资产购建支出后的代理值", ["operating_cash_flow", "operating_cash_flow_less_asset_purchase_proxy"], "line"),
    }

    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def chart_catalog(self, research_id: str):
        _, _, pack = self.w.pack(research_id)
        available = {x["metric_id"] for x in pack["metrics"] if x["state"] == "ready"}
        return {"items": [{"template": k, "title": title, "status": "available" if set(metrics) <= available else "input_gap"}
                         for k,(title,metrics,_) in self.TEMPLATES.items()],
                "conditional": ["valuation_sensitivity requires calculation_id", "杜邦与现金流瀑布暂未接入，不能以趋势图冒充"]}

    def create_chart(self, research_id: str, template: str, calculation_id: str | None = None):
        """Create a deterministic chart from the active snapshot; missing values are gaps, never zero."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"], "axes.unicode_minus": False})
        state, _, pack = self.w.pack(research_id)
        data = []
        if template == "valuation_sensitivity":
            calculation = next((x for x in self.w.artifacts(research_id, "calculation") if x["artifact_id"] == calculation_id), None)
            if not calculation or calculation["method"] != "pe_scenarios":
                raise ResearchError("active_pe_calculation_required")
            data = calculation["result"]["sensitivity"]
            title = "估值对盈利增长与PE假设的敏感性（元/股）"
            fig, ax = plt.subplots(figsize=(9.5, 4.2))
            growths = sorted({float(x["growth"]) for x in data})
            multiples = sorted({float(x["multiple"]) for x in data})
            values = {(float(x["growth"]), float(x["multiple"])): float(x["fair_value"]) for x in data}
            matrix = [[values[g,m] for m in multiples] for g in growths]
            ax.imshow(matrix, cmap="Blues", aspect="auto")
            ax.set_xticks(range(len(multiples)), [f"{x:g}倍" for x in multiples])
            ax.set_yticks(range(len(growths)), [f"{x:.0%}" for x in growths])
            ax.set_xlabel("预测市盈率"); ax.set_ylabel("一年盈利增长假设")
            for i,g in enumerate(growths):
                for j,m in enumerate(multiples):
                    ax.text(j,i,f"{values[g,m]:.0f}",ha="center",va="center",color="#121e29")
        elif template in self.TEMPLATES:
            title, metrics, style = self.TEMPLATES[template]
            periods = pack["periods"]["annual"]
            fig, ax = plt.subplots(figsize=(9.5, 4.2))
            for mi, metric in enumerate(metrics):
                rows = {x["period"]: x for x in pack["metrics"] if x["metric_id"] == metric and x["period_type"] == "cumulative"}
                vals = []
                for period in periods:
                    row = rows.get(period)
                    good = row is not None and row["state"] == "ready"
                    vals.append(float(row["fact"]["value"])/1e8 if good else float("nan"))
                    data.append({"metric_id": metric, "period": period, "value": row["fact"]["value"] if good else None,
                                 "fact_ref": row.get("fact_ref") if good else None, "unit": "CNY", "display_unit": "亿元"})
                label = next((x["label"] for x in rows.values()), metric)
                if style == "bar":
                    ax.bar([i+(mi-.5)*.32 for i in range(len(periods))], vals, width=.32, label=label)
                else:
                    ax.plot(range(len(periods)), vals, marker="o", linewidth=2.5, label=label)
            ax.set_xticks(range(len(periods)), [p[:4] for p in periods]);ax.set_ylabel("亿元")
            ax.legend(frameon=False);ax.grid(axis="y",alpha=.2)
        else:
            raise ResearchError("unknown_chart_template")
        ax.set_title(title,loc="left",pad=18,fontweight="bold")
        fig.tight_layout()
        ident = "chart_" + digest([state["snapshot_id"], template, data])[:24]
        path = self.w.state / "charts" / (ident + ".png")
        path.parent.mkdir(parents=True,exist_ok=True)
        fig.savefig(path,dpi=160,bbox_inches="tight");plt.close(fig)
        return self.w.artifact(research_id, "chart", {"template": template, "title": title, "data": data,
            "image_path": str(path), "image_sha256": sha(path), "template_version": "research-charts-v1",
            "calculation_id": calculation_id, "validation": "data_bound; visual_review_required"})

    def view_chart(self, research_id: str, chart_id: str):
        """Return a verified chart image; MCP exposes it as an image content block."""
        chart = next((x for x in self.w.artifacts(research_id,"chart") if x["artifact_id"] == chart_id),None)
        if not chart or sha(Path(chart["image_path"])) != chart["image_sha256"]:
            raise ResearchError("chart_missing_or_modified")
        return {"path": chart["image_path"], "sha256": chart["image_sha256"], "mime_type": "image/png"}
