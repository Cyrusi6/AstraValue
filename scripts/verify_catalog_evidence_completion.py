"""Register prior API evidence and verify current filing/API catalog reading routes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import fitz
from analysis.research.workspace import ResearchWorkspace,sha
from analysis.research.catalog import Catalog
from analysis.research.api_materials import register
from analysis.research.material_status import present


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--research-id',required=True);ap.add_argument('--api-run',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    w=ResearchWorkspace();api=Catalog(w);rid=args.research_id
    state,path,_=w.pack(rid);before=sha(path/'manifest.json');register(w,rid,args.api_run)
    _,data=api._load(rid);docs={};checked=0
    try:
        for r in data['items']:
            if r['reader']!='filing_check':continue
            p=r['payload']
            if p['path'] not in docs:docs[p['path']]=fitz.open(p['path'])
            text=docs[p['path']][p['page']-1].get_text()
            assert p['excerpt'] in text,(r['period'],p['dataset_id'])
            checked+=1
    finally:
        for doc in docs.values():doc.close()
    groups=[];probes=0
    for r in data['items']:
        if r['reader']!='missing' or not r.get('availability'):continue
        ds=r['payload'].get('dataset_id')
        if ds not in {'audit_opinion','customers_peer','guarantee','litigation','violation','seo','allotment','bond_issuance','pledge','unlock_peer'}:continue
        a=r['availability'];periods=a['available_periods']
        result=api.read_material(rid,r['material_id'],period=periods[-1]);assert result['status'] in {'original_readable','verified_none'};probes+=1
        route=a['routes'][-1]['read_entry'];result=api.read_material(rid,route['material_id']);assert result['status'] in {'original_readable','verified_none'};probes+=1
        groups.append({'dataset':ds,'periods':periods,'states':dict(Counter(x['state'] for x in a['routes']))})
    for ds in ['guarantee','litigation']:
        assert '2026-06-30' in next(g for g in groups if g['dataset']==ds)['periods']
    assert sha(path/'manifest.json')==before
    checks=[r for r in data['items'] if r['reader'] in {'filing_check','api_record'}]
    remaining=[{'title':r['title'],**present(r)} for r in data['items'] if present(r)['status'] in {'processing','unavailable'}]
    summary={'research_id':rid,'snapshot_id':state['snapshot_id'],'groups':groups,'original_excerpts_verified':checked,'read_probes':probes,
             'remaining':remaining,'checks':checks,'frozen_pack_unchanged':True}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'目录接入与核查结果.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    labels={'audit_opinion':'审计意见','customers_peer':'客户供应商','guarantee':'担保','litigation':'重大诉讼仲裁','violation':'监管与治理事项','seo':'增发／证券发行','allotment':'配股／证券发行','bond_issuance':'债券','pledge':'股东质押、标记或冻结','unlock_peer':'限售股份变动'}
    lines=['# API与原文核查接入结果','','本轮补充已接入当前研究目录；旧冻结财务包未改写。','', '| 资料 | 可读期间数 | 起止期间 |','|---|---:|---|']
    for g in groups:lines.append(f'| {labels[g["dataset"]]} | {len(g["periods"])} | {g["periods"][0]}—{g["periods"][-1]} |')
    lines += ['', '审计意见：API的OPINION_TYPE提供2023—2025年标准无保留意见。原OSOPINION_TYPE仍为空，目录给出替代读取路线，不声称两个字段同义。',
      '', '历史监管：6条API记录按5个日期组织；不将疑似重复记录计为独立事件。目录另保留年报/中报最新违法违规及整改原文，包括2026年留置公告引用。',
      '', '2026中报：第16页明确无重大诉讼、仲裁；第21页重大担保栏目不适用。债券、限售股份也有相应披露。',
      '', '客户供应商可读集中度，不宣称全部实名明细齐全。股东质押表只覆盖披露股东，未知仍是未知。增发、配股按年度证券发行栏目核查，不外推到未披露期间。',
      '', f'独立复读原件核验{checked}段原文，{probes}次目录和来源入口调用通过。',
      '', f'当前待处理或尚未取得条目：{len(remaining)}项，具体范围见JSON的remaining；不以旧供应商空字段重复报缺。']
    (args.output/'核查说明.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({'groups':groups,'excerpts':checked,'probes':probes,'remaining':remaining},ensure_ascii=False))

if __name__=='__main__':main()
