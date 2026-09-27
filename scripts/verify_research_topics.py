"""Verify real topic views and export a readable user preview, using existing caches."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re

import fitz

from analysis.research.catalog import Catalog
from analysis.research.topics import topic_content
from analysis.research.workspace import ResearchWorkspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--research-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    w=ResearchWorkspace(Path(__file__).resolve().parents[1]);api=Catalog(w)
    state,data=api._load(args.research_id)
    topics=[r for r in data['items'] if r['reader']=='topic']
    notes=[r for r in topics if r['category']=='notes']
    inventory=next(r for r in notes if r['title']=='存货构成与减值')
    inventory_view=topic_content(inventory,data)
    checked=0
    # Independent text-stream comparison: no reuse of the table extraction algorithm.
    for source in inventory_view['sources']:
        child=next(r for r in data['items'] if r['material_id']==source['material_id'])
        p=child['payload'];period=child['period']
        with fitz.open(p['path']) as doc:
            text='\n'.join(doc[i-1].get_text() for i in range(p['page'],p['end_page']+1))
        if '存货分类' not in text:
            raise AssertionError('inventory_classification_not_located:'+period)
        block=re.split(r'\(2\)|（2）',text.split('存货分类',1)[1])[0]
        cells=[c for c in inventory['payload']['cells'] if c['ref']==source['material_id']]
        assert cells, period
        labels=list(dict.fromkeys(c['label'] for c in cells))
        for label in labels:
            hit=re.search(re.escape(label)+r'\s*(.*?)(?='+'|'.join(re.escape(x) for x in labels)+r'|\Z)',block,re.S)
            assert hit, (period,label)
            numbers=re.findall(r'\d[\d,]*\.\d{2}',hit[1])
            assert len(numbers) in {4,6}, (period,label,numbers)
            expected=[numbers[0],numbers[1] if len(numbers)==6 else None,numbers[2] if len(numbers)==6 else numbers[1]]
            for measure,amount in zip(('账面余额','减值准备','账面价值'),expected):
                cell=next(c for c in cells if c['label']==label and c['table'].endswith('/ '+measure))
                assert (Decimal(cell['value']) if cell['value'] is not None else None)==(Decimal(amount.replace(',','')) if amount else None)
                checked+=1
    # Read through the actual bounded tool path, including sources and continuation.
    content='';page=1;reads=0
    while page:
        result=api.read_material(args.research_id,inventory['material_id'],page=page,max_tokens=2000)
        content+=result['content'];page=result['next_page'];reads+=1
    assert json.loads(content)==inventory_view
    lines=['# 主题目录与跨期数据预览','',
           '首包仍只显示分类摘要。二级同主题资料合并，跨期表与逐期原文从同一入口读取。','',
           '| 附注主题 | 覆盖期间 | 有数据期数 / 原文可读期数 | 全部检测子表已整理期数 |',
           '| --- | --- | --- | --- |']
    for r in notes:
        count=len({c['period'] for c in r['payload']['cells']+r['payload'].get('table_cells',[])})
        complete=sum(s['state']=='detected_tables_processed' for s in r['payload'].get('processing',[]))
        lines.append(f"| {r['title']} | {r['period'][0]}—{r['period'][-1]} | {count} / {len(r['period'])} | {complete} |")
    lines+=['','## 存货构成与减值','',
            '单位：元。年末与半年末均为对应时点余额；以下数值保留原始精度。原表留空显示“空白”，不补零。','']
    order={'账面价值':0,'账面余额':1,'减值准备':2}
    for table in sorted(inventory_view['tables'],key=lambda t:order[t['table'].split(' / ')[-1]]):
        lines+=['### '+table['table'].split(' / ')[-1],'',
                '| 期间 | '+' | '.join(r['label'] for r in table['rows'])+' |',
                '| --- | '+' | '.join('---:' for _ in table['rows'])+' |']
        for index,period in enumerate(table['periods']):
            values=[]
            for row in table['rows']:
                v,cell_state=row['values'][index],row['states'][index]
                values.append(format(Decimal(v),',.2f') if v is not None else '空白' if cell_state=='disclosed_blank' else cell_state)
            lines.append('| '+period+' | '+' | '.join(values)+' |')
        lines.append('')
    lines+=['## 原文定位','']
    for source in inventory_view['sources']:
        child=next(r for r in data['items'] if r['material_id']==source['material_id']);p=child['payload']
        lines.append(f"- {child['period']}：[原件]({p['source_url']})，第{p['page']}—{p['end_page']}页。")
    summary={'snapshot_id':state['snapshot_id'],'note_topics':len(notes),
             'topics_with_tables':sum(bool(r['payload']['cells'] or r['payload'].get('table_cells')) for r in notes),
             'inventory_periods':len(inventory_view['sources']),'inventory_cells_independently_checked':checked,
             'inventory_tool_pages':reads,'boundary':'支持列头的表已跨期对齐；其他格式保留逐期原文，未冒充全部结构化。'}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'主题目录与存货跨期表.md').write_text('\n'.join(lines),encoding='utf8')
    (args.output/'存货跨期数据.json').write_text(json.dumps(inventory_view,ensure_ascii=False,indent=2),encoding='utf8')
    (args.output/'跨期核验结果.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
