"""User-only completeness inventory and independent PDF cell checks for all notes."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re

import fitz

from analysis.research.catalog import Catalog
from analysis.research.workspace import ResearchWorkspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--research-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--material-id',action='append',default=[],help='Reprocess only these note original IDs')
    args=parser.parse_args()
    api=Catalog(ResearchWorkspace(Path(__file__).resolve().parents[1]))
    if args.refresh:api.refresh_catalog(args.research_id)
    if args.material_id:api.refresh_topic_tables(args.research_id,args.material_id)
    state,data=api._load(args.research_id)
    originals={r['material_id']:r for r in data['items']}
    topics=[r for r in data['items'] if r['reader']=='topic' and r['category']=='notes']
    docs={};textpages={};failures=[];checked=0;rows=[];pending=[];collisions=[]
    try:
        for topic in topics:
            cells=topic['payload'].get('table_cells',[])
            if not topic['payload'].get('processing'):raise AssertionError('refresh_topic_v2_required')
            observations={}
            for cell in cells:
                key=tuple(cell.get(k) for k in ('table','label','period','period_type','unit','currency'))
                observations.setdefault(key,set()).add((cell['state'],cell['value']))
                if cell['state']!='ready':continue
                source=originals[cell['ref']]['payload'];path=source['path']
                if path not in docs:docs[path]=fitz.open(path)
                loc=cell['locator'];bbox=loc['bbox']
                page_key=(path,loc['page'])
                if page_key not in textpages:textpages[page_key]=docs[path][loc['page']-1].get_textpage()
                text=textpages[page_key].extractTextbox(fitz.Rect(bbox)) if bbox else ''
                normalized=re.sub(r'\s+','',text).replace(',','').replace('，','')
                try:okay=Decimal(normalized)==Decimal(cell['value'])
                except Exception:okay=False
                if not okay:failures.append({'topic':topic['title'],'period':cell['period'],'column':cell['column'],
                    'row':cell['label'],'expected':cell['value'],'read_from_bbox':text,'locator':loc})
                checked+=1
            for key,values in observations.items():
                if len(values)>1:collisions.append({'key':key,'values':list(values)})
            summary=topic['payload']['processing']
            count=Counter(s['state'] for s in summary)
            table_count=sum(s['detected_tables'] for s in summary)
            processed=sum(s['processed_tables'] for s in summary)
            rows.append({'topic':topic['title'],'original_periods':len(summary),
                         'periods_with_data':len({c['period'] for c in cells}),
                         'complete_detected_table_periods':count['detected_tables_processed'],
                         'processed_tables':processed,'detected_tables':table_count,'states':dict(count)})
            pending.extend({'topic':topic['title'],**s} for s in summary if s['state']!='detected_tables_processed')
    finally:
        for doc in docs.values():doc.close()
    result={'snapshot_id':state['snapshot_id'],'topics':len(rows),
            'original_topic_periods':sum(r['original_periods'] for r in rows),
            'periods_with_data':sum(r['periods_with_data'] for r in rows),
            'fully_processed_detected_table_periods':sum(r['complete_detected_table_periods'] for r in rows),
            'detected_tables':sum(r['detected_tables'] for r in rows),
            'processed_tables':sum(r['processed_tables'] for r in rows),
            'numeric_cells_independently_checked':checked,'numeric_cell_failures':len(failures),
            'conflicting_cells':len(collisions)}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'子表核验结果.json').write_text(json.dumps({'summary':result,'topics':rows,'pending':pending,
                                                         'cell_failures':failures,'conflicts':collisions},ensure_ascii=False,indent=2),encoding='utf8')
    lines=['# 附注自动整理结果','',
           '“已整理期数”表示该期检测到的所有表格均已处理；另列子表数量，避免用一张表代表整个附注。期初、期末、本期和上期列保留各自含义，空白不补零。','',
           '| 主题 | 有数据期数 / 原文可读期数 | 已整理期数 | 已处理子表 / 检测到的子表 |',
           '| --- | --- | --- | --- |']
    for row in rows:
        lines.append(f"| {row['topic']} | {row['periods_with_data']} / {row['original_periods']} | {row['complete_detected_table_periods']} | {row['processed_tables']} / {row['detected_tables']} |")
    lines+=['',f'独立核对数值单元格：{checked}；不一致：{len(failures)}。同口径同期间冲突：{len(collisions)}。',
            '', '本页统计当前缓存中识别出的表格；附注叙述仍可按原文读取，不把表格整理等同于全部研究完成。']
    (args.output/'附注自动整理结果.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False))
    if failures:raise AssertionError('independent_numeric_verification_failed')
    if collisions:raise AssertionError('ambiguous_table_alignment')


if __name__=='__main__':main()
