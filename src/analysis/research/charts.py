from __future__ import annotations

from pathlib import Path

from .workspace import ResearchWorkspace, ResearchError, digest, sha, read_json


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
                "conditional": ["valuation_sensitivity requires PE calculation_id", "dupont requires financial_summary calculation_id",
                                "history_pe/peer_pe require adopted verified valuation history", "现金流瀑布尚未接入，不能以趋势图冒充"],
                "custom": {"tool": "run_python_analysis", "mode": "chart",
                           "review": "view_chart → review_custom_chart → {{chart:chart_id}}",
                           "inputs": "当前快照的只读数据、正式计算或经验证的探索计算"}}

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
            heatmap = ax.imshow(matrix, cmap="Blues", aspect="auto")
            ax.set_xticks(range(len(multiples)), [f"{x:g}倍" for x in multiples])
            ax.set_yticks(range(len(growths)), [f"{x:.0%}" for x in growths])
            ax.set_xlabel("预测市盈率"); ax.set_ylabel("相对基期的盈利增长假设")
            for i,g in enumerate(growths):
                for j,m in enumerate(multiples):
                    color = "white" if heatmap.norm(values[g,m]) > .55 else "#121e29"
                    ax.text(j,i,f"{values[g,m]:.0f}",ha="center",va="center",color=color)
        elif template in {"history_pe", "peer_pe"}:
            bound = [x for x in pack.get("supplemental_evidence",[]) if x.get("source_role")=="verified_deterministic_calculation"]
            if len(bound)!=1:
                raise ResearchError("bound_valuation_history_required")
            history = read_json(Path(bound[0]["original_path"]))
            fig,ax=plt.subplots(figsize=(9.5,4.2))
            if template == "history_pe":
                from datetime import date
                series=history["series"][state["ticker"]]["eastmoney_pe_ttm"]
                data=[{"date":d,**v} for d,v in sorted(series.items())]
                title="历史市盈率（TTM）"
                ax.plot([date.fromisoformat(x["date"]) for x in data],[float(x["value"]) for x in data],color="#225b72",linewidth=1.8,label="历史PE（TTM）")
                reference=float(history["summary"]["history"][state["ticker"]]["reference_multiple"])
                ax.axhline(reference,color="#bd8549",linestyle="--",label=f"{reference:g}倍参考线")
                ax.set_ylabel("倍");ax.legend(frameon=False)
            else:
                from analysis.structured.scope import load_research_profile
                names=load_research_profile(pack["profile_id"])["industry_profile"]["supported_tickers"]
                data=[{"ticker":t,"date":history["summary"]["comparison_date"],**v["eastmoney_pe_ttm"]} for t,v in history["summary"]["rows"].items()]
                data.sort(key=lambda x:(x["ticker"]!=state["ticker"],x["ticker"]))
                title="同日同行市盈率（TTM）｜"+history["summary"]["comparison_date"]
                ax.bar([names.get(x["ticker"],x["ticker"]) for x in data],[float(x["value"]) for x in data],color=["#225b72"]+["#9eb9c4"]*(len(data)-1))
                for i,x in enumerate(data):ax.text(i,float(x["value"])+.4,f"{float(x['value']):.2f}",ha="center")
                ax.set_ylabel("倍");ax.set_ylim(0,max(float(x["value"]) for x in data)*1.2)
            ax.grid(axis="y",alpha=.2)
        elif template == "dupont":
            calculation=next((x for x in self.w.artifacts(research_id,"calculation") if x["artifact_id"]==calculation_id),None)
            if not calculation or calculation["method"]!="financial_summary":raise ResearchError("financial_summary_required")
            data=[x for x in calculation["result"]["rows"] if x["metric"]=="consolidated_dupont"]
            if not data:raise ResearchError("dupont_inputs_missing")
            title="合并口径杜邦分解（含少数股东，非加权归母ROE）"
            fig,axs=plt.subplots(1,4,figsize=(11,3.8))
            for ax,key,label,scale in zip(axs,["net_margin","asset_turnover","equity_multiplier","roe"],["净利率（%）","资产周转率（次）","权益乘数（倍）","总权益收益率（%）"],[100,1,1,100]):
                ax.plot([x["period"][:4] for x in data],[float(x[key])*scale for x in data],marker="o",color="#225b72")
                ax.set_title(label,fontsize=11);ax.tick_params(axis="x",rotation=45);ax.grid(axis="y",alpha=.2)
            fig.suptitle(title,fontsize=13);ax=axs[-1]
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
        if template != "dupont":ax.set_title(title,loc="left",pad=18,fontweight="bold")
        fig.tight_layout()
        version = "research-charts-v3" if template == "valuation_sensitivity" else "research-charts-v1"
        identity = [state["snapshot_id"], template, data]
        if version != "research-charts-v1":
            identity.append(version)
        ident = "chart_" + digest(identity)[:24]
        path = self.w.state / "charts" / (ident + ".png")
        path.parent.mkdir(parents=True,exist_ok=True)
        fig.savefig(path,dpi=160,bbox_inches="tight");plt.close(fig)
        return self.w.artifact(research_id, "chart", {"template": template, "title": title, "data": data,
            "image_path": str(path), "image_sha256": sha(path), "template_version": version,
            "calculation_id": calculation_id, "validation": "data_bound; visual_review_required"})

    def view_chart(self, research_id: str, chart_id: str):
        """Return a verified chart image; MCP exposes it as an image content block."""
        chart, binding, _ = self._verified_chart(research_id, chart_id)
        if binding:
            self.w.artifact(research_id, "chart_view", binding)
        return {"path": chart["image_path"], "sha256": chart["image_sha256"], "mime_type": "image/png"}

    def _verified_chart(self, research_id: str, chart_id: str):
        chart = next((x for x in self.w.artifacts(research_id, "chart") if x["artifact_id"] == chart_id), None)
        if not chart:
            raise ResearchError("chart_not_in_active_snapshot:" + chart_id)
        payload = {k: v for k, v in chart.items() if k != "artifact_id"}
        if chart_id != "chart_" + digest(payload)[:24]:
            raise ResearchError("chart_record_modified")
        try:
            if sha(Path(chart["image_path"])) != chart["image_sha256"]:
                raise ResearchError("chart_integrity_failed")
        except (OSError, KeyError) as exc:
            raise ResearchError("chart_missing_or_modified") from exc
        if chart.get("template") != "custom_python":
            return chart, None, None
        from .custom_python import get_exploration
        run = get_exploration(self.w, research_id, chart["exploration_id"])
        image = run.get("files", {}).get("figure.png", {})
        if (run.get("mode") != "chart" or image.get("sha256") != chart["image_sha256"]
                or Path(image.get("path", "")).resolve() != Path(chart["image_path"]).resolve()
                or digest(chart.get("data")) != digest(run.get("result"))):
            raise ResearchError("custom_chart_execution_mismatch")
        binding = {"chart_id": chart_id, "exploration_id": run["artifact_id"],
                   "image_sha256": chart["image_sha256"], "code_sha256": run["code_sha256"],
                   "input_sha256": run["input_sha256"], "files_sha256": digest(run["files"]),
                   "result_sha256": digest(run["result"]), "provenance_sha256": digest(run["provenance"])}
        return chart, binding, run

    def review_custom_chart(self, research_id: str, chart_id: str, visual_review: str,
                            data_review: str, approved: bool = True):
        """Record the model's visual and data review after view_chart; no human confirmation is required."""
        _, binding, _ = self._verified_chart(research_id, chart_id)
        if binding is None:
            raise ResearchError("custom_chart_required")
        if (not isinstance(visual_review, str) or not isinstance(data_review, str)
                or not visual_review.strip() or not data_review.strip()
                or len(visual_review) > 3000 or len(data_review) > 3000):
            raise ResearchError("custom_chart_visual_and_data_review_required")
        if not isinstance(approved, bool):
            raise ResearchError("custom_chart_approval_must_be_boolean")
        views = self.w.artifacts(research_id, "chart_view")
        if not any(all(row.get(k) == v for k, v in binding.items()) for row in views):
            raise ResearchError("custom_chart_must_be_viewed_before_review")
        # A revision makes a later rejection authoritative, even if an earlier approval is identical.
        reviews = self.w.artifacts(research_id, "chart_review")
        return self.w.artifact(research_id, "chart_review", {
            **binding, "approved": bool(approved), "visual_review": visual_review.strip(),
            "data_review": data_review.strip(), "reviewer_role": "host_model",
            "revision": len(reviews) + 1,
        })

    def report_chart(self, research_id: str, chart_id: str):
        """Verify one referenced image and retain its custom execution and review with the report."""
        chart, binding, run = self._verified_chart(research_id, chart_id)
        if binding is None:
            return chart
        reviews = [x for x in self.w.artifacts(research_id, "chart_review") if x.get("chart_id") == chart_id]
        if not reviews or not reviews[-1].get("approved"):
            raise ResearchError("custom_chart_review_required:" + chart_id)
        review = reviews[-1]
        if not all(review.get(k) == v for k, v in binding.items()):
            raise ResearchError("custom_chart_review_stale:" + chart_id)
        views = self.w.artifacts(research_id, "chart_view")
        if not any(all(row.get(k) == v for k, v in binding.items()) for row in views):
            raise ResearchError("custom_chart_view_receipt_missing")
        from .custom_python import get_validated_exploration
        from .custom_archive import archive_exploration
        sources = [archive_exploration(self.w, research_id, get_validated_exploration(self.w, research_id, key))
                   for key in run.get("provenance", {}).get("exploration_ids", [])]
        return {**chart, "custom_execution": archive_exploration(self.w, research_id, run), "custom_review": review,
                "validated_exploration_sources": sources}
