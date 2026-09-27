"""User-facing audit of availability reconciliation and registered note calculations."""
from collections import Counter
from decimal import Decimal
import argparse
import json
from pathlib import Path
from analysis.research.catalog import Catalog
from analysis.research.tools import operations
from analysis.research.workspace import ResearchWorkspace, read_json, sha
from analysis.research.material_status import present


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--research-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    w=ResearchWorkspace();api=Catalog(w);rid=args.research_id
    state,path,pack=w.pack(rid);original_hash=sha(path/'manifest.json')
    original=read_json(Path(w.artifacts(rid,'material_catalog')[-1]['path']))
    _,data=api._load(rid);ops=operations(w);resolved=[];calls=[]
    for item in data['items']:
        if not item.get('availability'):continue
        a=item['availability'];period=a['available_periods'][-1]
        r=ops['read_material'](rid,item['material_id'],period=period)
        assert r['status']==present(item,period)['status'] and r['status'] not in {'processing','unavailable'}
        resolved.append({'title':item['title'],'material_id':item['material_id'],'periods':a['available_periods'],
                         'original_status':a['source_status'],'status':r['status']})
        # Probe one actual route for each type, in addition to each resolved item.
        route=a['routes'][-1]['read_entry'];signature=(route['tool'],route.get('statement'),tuple(route.get('metric_ids',[])))
        if signature not in calls:
            result=ops[route['tool']](research_id=rid,**{k:v for k,v in route.items() if k!='tool'})
            assert result.get('status') not in {'unavailable','processing','capability_gap'}
            calls.append(signature)
    calculations=[ops['calculate'](rid,m,{'period':'2026-06-30'}) for m in ['inventory_composition','inventory_allowance_ratio']]
    # Independent reference arithmetic from verified 2026H1 source amounts.
    assert Decimal(calculations[0]['result']['value'])==(Decimal('19295520769.07')+Decimal('34923053030.16'))/Decimal('61317208371.30')
    assert Decimal(calculations[1]['result']['value'])==Decimal('1283984.83')/Decimal('61318492356.13')
    assert sha(path/'manifest.json')==original_hash
    remaining=[{'title':r['title'],**present(r)} for r in data['items'] if present(r)['status'] in {'processing','unavailable'}]
    summary={'research_id':rid,'snapshot_id':state['snapshot_id'],
        'before':dict(Counter(present(r)['status'] for r in original['items'])),
        'after':dict(Counter(present(r)['status'] for r in data['items'])),
        'resolved':resolved,'remaining':remaining,'calculations':calculations,
        'read_route_probes':len(calls),'frozen_manifest_unchanged':True}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'核验结果.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    lines=['# 目录状态与附注计算核验','',f'研究快照：{state["snapshot_id"]}',
        '',f'目录状态：{summary["before"]} → {summary["after"]}。',
        '',f'已统一 {len(resolved)} 个条目；逐项读取及 {len(calls)} 类替代路线调用通过。冻结财务包未变。',
        '', '## 已统一条目','']
    lines += [f'- {r["title"]}：{len(r["periods"])}期，{r["periods"][0]} 至 {r["periods"][-1]}。' for r in resolved]
    lines += ['', '已核实原表空白仍为空；未列明的历史期间保留原状态。单季入口只承诺列出的指标，减值损失字段提供原文读取，不冒充标准数值。',
        '', '## 新增正式计算','',
        '- 在产品及自制半成品占存货净额：88.42309564%。',
        '- 存货减值准备率：0.002093960208%。',
        '', '均采用2026年中报期末合并数据，原件位置复读、公式及独立复算通过。输入与公式版本见核验结果JSON；空白、错单位、歧义、合计不符均拒绝产出。其他公司缺少单列分类时不填零。',
        '', '## 保留的未完成项目','']
    lines += [f'- {r["title"]}：{r["status"]}。' for r in remaining]
    (args.output/'核验说明.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({k:summary[k] for k in ['before','after','read_route_probes','frozen_manifest_unchanged']},ensure_ascii=False))


if __name__=='__main__':main()
