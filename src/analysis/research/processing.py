"""Frozen research processing: existing projections, selected originals and auditable checks."""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
import json
import re

from analysis.structured.research_lite import build_lite_pack, _count_tokens
from analysis.structured.scope import STANDARD_PROFILE_ID
from .workspace import ResearchError, ResearchWorkspace, digest, read_json, sha
from .pack_outputs import output_hashes, read_pack_outputs


def extract_evidence(spec: dict, ticker: str, cutoff: str) -> dict:
    """Select original PDF pages or a JSON record; never promote statements to numeric facts."""
    required = {"path", "sha256", "source_url", "ticker", "published_at", "title", "boundary", "group"}
    if required - spec.keys() or spec["ticker"] != ticker:
        raise ResearchError("supplement_identity_or_metadata_missing")
    if date.fromisoformat(spec["published_at"][:10]) > date.fromisoformat(cutoff):
        raise ResearchError("supplement_after_cutoff")
    path = Path(spec["path"]).resolve()
    if sha(path) != spec["sha256"]:
        raise ResearchError("supplement_hash_mismatch")
    if "pages" in spec:
        import fitz
        with fitz.open(path) as doc:
            if not spec["pages"] or any(not 1 <= p <= len(doc) for p in spec["pages"]):
                raise ResearchError("supplement_page_out_of_range")
            content = "\n".join(doc[p-1].get_text() for p in spec["pages"])
        locator = "pages:" + ",".join(map(str, spec["pages"]))
    elif "json_path" in spec:
        value = read_json(path)
        parent = None
        for key in spec["json_path"]:
            parent = value
            value = value[key]
        if spec.get("source_role") == "company_statement_on_exchange_public_platform":
            if spec["json_path"][-1] != "content" or not isinstance(parent,dict) or not parent.get("companyId"):
                raise ResearchError("company_answer_record_required")
            if parent.get("questionType") not in (2,3) or any(parent.get(k) for k in ("isDel","isTestData","isCancelInfo")):
                raise ResearchError("public_company_answer_ineligible")
            if not parent.get("crtTime") or parent["crtTime"][:10] > cutoff:
                raise ResearchError("answer_after_cutoff")
        # For API records, the caller selects the answer field, never question text.
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        locator = "json:" + json.dumps(spec["json_path"], ensure_ascii=False)
    else:
        raise ResearchError("supplement_locator_required")
    if not content.strip():
        raise ResearchError("supplement_requires_text_processing")
    row = {"title": spec["title"], "group": spec["group"], "period": spec.get("period"),
           "published_at": spec["published_at"], "source_url": spec["source_url"],
           "original_path": str(path), "original_sha256": spec["sha256"], "locator": locator,
           "content": content, "excerpt": content[:350], "boundary": spec["boundary"],
           "source_role": spec.get("source_role", "issuer_disclosure"),
           "numeric_admission": False, "selection": spec}
    return {"evidence_id": "supplement-" + digest(row)[:24], **row}


def audit_metrics(pack: dict) -> dict:
    """Reconcile existing formal arithmetic and missingness without replacing missing data."""
    errors, seen, checks = [], {}, Counter()
    for row in pack["metrics"]:
        key = (row["metric_id"], row["period"], row["period_type"])
        if key in seen and seen[key] != row:
            errors.append("conflicting_metric:" + str(key))
        seen[key] = row
        fact = row.get("fact")
        if row["state"] != "ready":
            if fact is not None:
                errors.append("unready_fact_has_value:" + str(key))
            continue
        if not fact or not fact.get("source_ids") or not fact.get("unit"):
            errors.append("missing_fact_lineage:" + str(key)); continue
        v = Decimal(fact["value"])
        if not v.is_finite(): errors.append("nonfinite:" + str(key))
        checks["ready_values"] += 1
        y = row.get("yoy")
        if y:
            prior = Decimal(y["prior_value"])
            if prior == 0 or abs((v-prior)/prior - Decimal(y["value"])) > Decimal("1e-24"):
                errors.append("yoy_mismatch:" + str(key))
            checks["yoy_reconciliations"] += 1
    annual = pack["periods"]["annual"]
    quarters = pack["periods"]["quarters"]
    if len(annual) != 5 or len(quarters) != 12:
        errors.append("standard_window_requires_5_years_12_quarters")
    # Independent aggregation from displayed single quarters to annual/latest TTM.
    for metric in ("operating_income", "parent_net_profit", "net_profit", "operating_cash_flow", "long_asset_cash_purchase"):
        for kind in ("cumulative", "ttm"):
            for end in (annual[-1], quarters[-1]):
                target = seen.get((metric, end, kind))
                if not target or target["state"] != "ready": continue
                end_serial = int(end[:4])*4 + (int(end[5:7])-1)//3
                required = [q for q in quarters if (int(q[:4])*4+(int(q[5:7])-1)//3 <= end_serial)
                            and ((q[:4] == end[:4]) if kind == "cumulative" else (int(q[:4])*4+(int(q[5:7])-1)//3 > end_serial-4))]
                count = int(end[5:7])//3 if kind == "cumulative" else 4
                inputs = [seen.get((metric,q,"single_quarter")) for q in required]
                if len(inputs) != count or any(not x or x["state"] != "ready" for x in inputs): continue
                value = sum(Decimal(x["fact"]["value"]) for x in inputs)
                if abs(value-Decimal(target["fact"]["value"])) > Decimal("0.02"):
                    errors.append("quarter_sum_mismatch:"+str((metric,end,kind)))
                checks["quarter_aggregations"] += 1
    gaps = [{k:x.get(k) for k in ("metric_id","period","period_type","required","reason")}
            for x in seen.values() if x["state"] not in {"ready","disclosed_blank"}]
    return {"status":"failed" if errors else "checked_with_explicit_gaps" if gaps else "passed",
            "errors":errors,"checks":dict(checks),"gaps":gaps,
            "annual_periods":annual,"quarter_periods":quarters,
            "boundary":"processing integrity is separate from source completeness and research completion"}


def organize_disclosures(evidence: list[dict]) -> list[dict]:
    """Conservative Chinese filing extraction; unsupported layouts remain original text."""
    result = []
    for row in evidence:
        text = re.sub(r"\s+", "", row["content"])
        for name, verb, total in (("客户","销售","销售"),("供应商","采购","采购")):
            matches = list(re.finditer(r"前五名"+name+verb+r"额([\d,.]+)万元，占年度"+total+r"总额([\d.]+)%",text))
            if len(matches) == 1:
                m = matches[0]
                result.append({"type":"counterparty_concentration","counterparty":name,"period":row["period"],
                  "amount_cny":str(Decimal(m[1].replace(",",""))*10000),"share":str(Decimal(m[2])/100),
                  "source_text":m[0],"evidence_id":row["evidence_id"],"rule_version":"filing-concentration-v1",
                  "boundary":"披露的前五名汇总；不代表具体名单，不能反推未披露客户"})
        raw = row["content"]
        if "存货分类" not in raw or "账面价值" not in raw or not re.search(r"单位：?元", text): continue
        block = raw.split("存货分类",1)[1].split("(2)",1)[0]
        names = ["原材料","在产品","库存商品","自制半成品","合计"]
        values = {}
        for name in names:
            match = re.search(name+r"\s*([\d, .\n\r]+)", block)
            if not match: break
            nums = re.findall(r"\d[\d,]*\.\d{2}", match[1])
            if len(nums) not in (4,6): break
            # Four values: balance=carrying value, no impairment value displayed.
            # Six values: balance, impairment, carrying value for each endpoint.
            parsed = [Decimal(x.replace(",","")) for x in nums]
            index = 1 if len(nums)==4 else 2
            if len(nums)==4 and (parsed[0]!=parsed[1] or parsed[2]!=parsed[3]): break
            if len(nums)==6 and (parsed[0]-parsed[1]!=parsed[2] or parsed[3]-parsed[4]!=parsed[5]): break
            values[name]=parsed[index]
        if len(values)==5 and sum(values[n] for n in names[:-1])==values["合计"] and values["合计"]>0:
            result.append({"type":"inventory_composition","period":row["period"],"unit":"CNY",
               "carrying_values":{k:str(v) for k,v in values.items()},
               "production_and_semifinished_share":str((values["在产品"]+values["自制半成品"])/values["合计"]),
               "formula":"(在产品账面价值+自制半成品账面价值)/存货合计账面价值",
               "rule_version":"filing-inventory-layout-v1","evidence_id":row["evidence_id"],
               "boundary":"公司账面存货，非经销商库存；仅支持四值或六值且减值、总额勾稽一致的原文表"})
    return result


class Processing:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def prepare_processing(self, research_id: str, evidence_sources: list[dict] | None = None,
                           attachments: list[dict] | None = None):
        """Build a five-year/twelve-quarter candidate from existing inputs; explicit adoption is separate."""
        state, old_path, _ = self.w.pack(research_id)
        manifest = read_json(old_path / "manifest.json")
        inherited = read_pack_outputs(old_path, manifest)
        roots = list(dict.fromkeys(x["root"] for x in manifest["source_inputs"] + manifest.get("auxiliary_inputs",[]) if x.get("root")))
        # Verify every frozen descriptor before reusing inputs.
        for group in ("source_inputs", "auxiliary_inputs"):
            for row in manifest.get(group, []):
                for d in list(row.get("files",{}).values()) + row.get("manifests",[]):
                    if sha(Path(d["path"])) != d["sha256"]:
                        raise ResearchError("processing_source_changed")
        built = build_lite_pack(input_root=Path(roots[0]), ticker=state["ticker"], as_of=date.fromisoformat(state["as_of"]),
                                output_root=self.w.state/"processing-base", supplements=[Path(r) for r in roots[1:]],
                                profile_id=STANDARD_PROFILE_ID)
        base = Path(built["pack_dir"])
        base_manifest = read_json(base/"manifest.json")
        # Standard-profile rebuilding intentionally replaces matching outputs;
        # other parent artifacts remain frozen, including future output types.
        inherited.update(read_pack_outputs(base, base_manifest))
        pack = read_json(base/"core-pack.json")
        evidence = [extract_evidence(s,state["ticker"],state["as_of"]) for s in evidence_sources or []]
        attached = []
        for a in attachments or []:
            if not all(a.get(k) for k in ("path","sha256","title","boundary")) or sha(Path(a["path"])) != a["sha256"]:
                raise ResearchError("processing_attachment_invalid")
            attached.append(a)
        audit = audit_metrics(pack)
        if audit["errors"]:
            raise ResearchError("processing_validation_failed:"+str(audit["errors"]))
        disclosures = organize_disclosures(evidence)
        identity = {"version":"research-processing-v1.0.2", "base":built["pack_id"],
                    "parent_manifest_sha256":sha(old_path/"manifest.json"),
                    "evidence":evidence,"attachments":attached,"audit":audit,"disclosures":disclosures}
        ident = "lite-pack-" + digest(identity)[:24]
        target = self.w.state/"processing-packs"/state["ticker"]/state["as_of"]/ident
        pack.update(pack_id=ident, supplemental_evidence=evidence, processing_attachments=attached, processing_audit=audit, organized_disclosures=disclosures)
        content = (base/"core-pack.md").read_text(encoding="utf-8")
        content = content.replace(built["pack_id"], ident)
        content = content.replace("不得自动生成评级、目标价或安全边际。", "宿主模型自主提出评级与假设，代码执行估值。")
        content = content.replace("不自动评级", "宿主模型负责评级")
        content = content.replace("历史估值分位、DCF、ROIC/WACC、EV/EBITDA 与目标价默认未启用；没有经确认的预测和情景参数。",
                                  "历史估值见采用附件；预测由宿主模型提出后交代码计算。DCF、ROIC/WACC与EV/EBITDA仍需匹配口径，不默认启用。")
        content += "\n\n## 已采用补充材料索引\n\n正文按需读取；公司陈述不自动成为标准指标。\n"
        content += "\n".join(f"- {x['title']}：{x['evidence_id']}；{x['boundary']}" for x in evidence)
        content += "\n" + "\n".join(f"- {x['title']}：{x['boundary']}" for x in attached)
        content += "\n\n已整理的披露表（有界原文规则，非供应商标准指标）：\n" + json.dumps(disclosures,ensure_ascii=False)
        count = _count_tokens(content)
        if count["count"] > pack["token_budget"]:
            raise ResearchError("processing_pack_budget_exceeded")
        pack["token_count"] = count
        coverage = read_json(base/"core-coverage.json")
        if any(x["type"] == "counterparty_concentration" for x in disclosures):
            for rows in (pack["coverage_requirements"], coverage["requirements"]):
                for row in rows:
                    if row["requirement_id"] == "lite.context.counterparty_disclosure":
                        row.update(state="source_text_available",reason="reported_top_five_aggregates_available_names_not_inferred")
            content = content.replace("pending / counterparty_disclosure_not_obtained", "source_text_available / 前五名汇总已整理，具体名单不据此补齐")
            coverage["counts"] = dict(Counter(x["state"] for x in coverage["requirements"]))
            count = _count_tokens(content)
            pack["token_count"] = count
        if count["count"] > pack["token_budget"]:
            raise ResearchError("processing_pack_budget_exceeded")
        outputs = {"core-pack.json": json.dumps(pack,ensure_ascii=False,indent=2), "core-pack.md":content,
                   "core-coverage.json":json.dumps(coverage,ensure_ascii=False,indent=2),
                   "processing-audit.json":json.dumps(audit,ensure_ascii=False,indent=2)}
        outputs = inherited | {name:text.encode("utf-8") for name,text in outputs.items()}
        new_manifest = base_manifest
        new_manifest.update(pack_id=ident, pack_identity_hash=digest(identity), token_count=count,
                            processing_identity=identity, parent_snapshot_id=state["snapshot_id"],
                            output_hashes=output_hashes(outputs))
        if target.exists():
            self.w._verify_pack(target)
            if (read_json(target/"core-pack.json") != pack
                    or read_json(target/"manifest.json")["output_hashes"] != new_manifest["output_hashes"]):
                raise ResearchError("immutable_processing_pack_conflict")
        else:
            target.mkdir(parents=True)
            for name, content in outputs.items():
                destination = target/name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            (target/"manifest.json").write_text(json.dumps(new_manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        return self.w.artifact(research_id,"snapshot_candidate",{"candidate_pack_path":str(target),"candidate_snapshot_id":ident,
             "reason":"扩展五年十二季度及最新累计/TTM，采用有定位补充原文和独立计算附件；原始快照不变",
             "build_status":"ready","audit":audit,"token_count":count})
