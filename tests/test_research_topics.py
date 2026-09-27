import json
from copy import deepcopy

import pytest

from analysis.research.topics import matrix, metric_cells, parse_note_table, group_topics, topic_content
from analysis.research.catalog import Catalog
from analysis.research.workspace import ResearchError, sha
from test_research_workspace import workspace


def inventory():
    return [['项目', '期末余额', None, None, '期初余额', None, None],
            [None, '账面余额', '存货跌价准备', '账面价值', '账面余额', '存货跌价准备', '账面价值'],
            ['材料', '10', '', '10', '9', '', '9'],
            ['商品', '20', '2', '18', '20', '1', '19'],
            ['合计', '30', '2', '28', '29', '1', '28']]


def test_inventory_blank_zero_and_failed_reconciliation():
    rows=inventory()
    cells=parse_note_table(rows,'存货构成与减值','2025-12-31','元','source1')
    assert len(cells)==9
    blank=next(c for c in cells if c['label']=='材料' and c['table'].endswith('减值准备'))
    assert blank['value'] is None and blank['state']=='disclosed_blank'
    rows[2][2]='0'
    zero=parse_note_table(rows,'存货构成与减值','2025-12-31','元','source1')[1]
    assert zero['value']=='0' and zero['state']=='ready'
    rows[-1][3]='29'
    assert not parse_note_table(rows,'存货构成与减值','2025-12-31','元','source1')
    rows=inventory(); rows[0][1]='期初余额'
    assert not parse_note_table(rows,'存货构成与减值','2025-12-31','元','source1')


def test_alignment_handles_missing_conflicts_units_and_provenance():
    cells=parse_note_table(inventory(),'存货构成与减值','2024-12-31','元','s1')
    later=parse_note_table(inventory(),'存货构成与减值','2025-12-31','元','s2')
    later=[c for c in later if c['label']!='材料']
    conflict={**cells[0],'value':'11','ref':'s3'}
    separate={**cells[0],'unit':'万元','ref':'s4'}
    result=matrix(cells+later+[conflict,separate])
    gross=next(t for t in result if t['table'].endswith('账面余额') and t['unit']=='元')
    row=next(r for r in gross['rows'] if r['label']=='材料')
    assert gross['periods']==['2024-12-31','2025-12-31']
    assert row['states']==['conflict','missing'] and row['values']==[None,None]
    assert row['refs'][0]==['s1','s3']
    assert len(result)==4


def test_annual_h1_single_quarter_are_separate_panels():
    base={'metric_id':'income','label':'收入','period':'2024-12-31','period_type':'cumulative',
          'fact_ref':'F1','state':'ready','fact':{'value':'100','unit':'CNY','scope':'consolidated'}}
    rows=[base,{**base,'period':'2024-06-30'}, {**base,'period_type':'single_quarter'},deepcopy(base)]
    tables=matrix(metric_cells(rows))
    assert len(tables)==3
    assert all(t['rows'][0]['states']==['ready'] for t in tables)


def test_note_flow_does_not_mix_half_year_with_annual():
    rows=[['项目','本期发生额','上期发生额'],['费用','10','8'],['合计','10','8']]
    cells=parse_note_table(rows,'管理费用','2024-12-31','元','a')
    cells+=parse_note_table(rows,'管理费用','2025-06-30','元','b')
    assert len(matrix(cells))==2
    assert {c['value'] for c in cells}=={'10'}


def test_refresh_ignores_inline_cross_reference_and_stops_at_next_note(workspace,monkeypatch):
    import fitz
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    pdf=workspace.root/'notes.pdf'
    with fitz.open() as doc:
        page=doc.new_page()
        for y,text in [(60,'详见七、合并财务报表项目注释'),(90,'1、存货'),(120,'政策文字'),
                       (240,'七、合并财务报表项目注释'),(270,'1、存货'),(300,'库存数据'),
                       (400,'2、金流量表补充资料'),(430,'(1). 现金流量表补充资料'),(450,'其他数据'),(500,'八、合并范围的变更')]:
            page.insert_text((40,y),text,fontname='china-s',fontsize=11)
        doc.save(pdf)
    original_pack=workspace.pack
    def pack(task):
        s,p,data=original_pack(task)
        data={**data,'statements':[{'path':str(pdf),'sha256':sha(pdf),'source_url':'https://example.test',
               'period':'2024-12-31','evidence_id':'statement1','statement':'balance_sheet'}]}
        return s,p,data
    monkeypatch.setattr(workspace,'pack',pack)
    api=Catalog(workspace);api.refresh_catalog(rid);_,data=api._load(rid)
    note=next(r for r in data['items'] if r['category']=='notes' and r['reader']=='pdf' and r['title']=='存货')
    text=api.read_material(rid,note['material_id'])['content']
    assert '库存数据' in text and '政策文字' not in text and '其他数据' not in text
    assert any(r['reader']=='pdf' and r['title']=='现金流量表补充资料' for r in data['items'])


def test_topic_grouping_keeps_sources_readable_and_period_filter(monkeypatch):
    items={}
    for year in (2024,2025):
        ident=str(year)
        items[ident]={'material_id':ident,'category':'notes','reader':'pdf','title':'存货',
                      'period':f'{year}-12-31','payload':{}}
    monkeypatch.setattr('analysis.research.topics.extract_note',lambda r,t:
                        parse_note_table(inventory(),t,r['period'],'元',r['material_id']))
    monkeypatch.setattr('analysis.research.note_tables.extract_tables',lambda r,t:([],
        {'period':r['period'],'state':'narrative_only','processed_tables':0,'detected_tables':0}))
    group_topics(items,'snapshot')
    root=next(r for r in items.values() if r['reader']=='topic')
    assert root['title']=='存货构成与减值'
    assert all(items[str(y)]['parent_id']==root['material_id'] for y in (2024,2025))
    result=topic_content(root,{'items':list(items.values())},'2025-12-31')
    assert len(result['sources'])==1 and result['tables'][0]['periods']==['2025-12-31']


def test_topic_read_pagination_integrity_and_old_source_entry(workspace):
    import fitz
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    api=Catalog(workspace);s,data=api._load(rid)
    pdf=workspace.root/'test.pdf'
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((40,60),'TOPIC SOURCE');page.insert_text((40,160),'UNRELATED')
        doc.save(pdf)
    child={'material_id':'original','category':'notes','reader':'pdf','title':'存货','period':'2024-12-31',
           'status':'readable','status_label':'已有可读','purpose':'原文',
           'payload':{'path':str(pdf),'sha256':sha(pdf),'source_url':'https://example.test/report',
                      'page':1,'end_page':1,'start_y':0,'end_y':100}}
    topic={'material_id':'topic','category':'notes','reader':'topic','title':'存货构成与减值',
           'period':['2024-12-31'],'status':'readable','status_label':'已有可读','purpose':'跨期表',
           'payload':{'children':['original'],'cells':parse_note_table(inventory(),'存货构成与减值','2024-12-31','元','original')}}
    child['parent_id']='topic';data['items']=[child,topic]
    path=workspace.root/'catalog-test.json';path.write_text(json.dumps(data),encoding='utf8')
    workspace.artifact(rid,'material_catalog',{'path':str(path),'sha256':sha(path)})
    assert api.list_materials(rid,'notes',query='存货')['total']==1
    assert api.list_materials(rid,'notes',parent_id='topic')['items'][0]['material_id']=='original'
    content='';page=1
    while page:
        response=api.read_material(rid,'topic',page=page,max_tokens=100)
        content+=response['content'];page=response['next_page']
    assert json.loads(content)['tables'][0]['periods']==['2024-12-31']
    raw=api.read_material(rid,'original')['content']
    assert 'TOPIC SOURCE' in raw and 'UNRELATED' not in raw
    pdf.write_bytes(b'changed')
    with pytest.raises(ResearchError,match='material_original_changed'):
        api.read_material(rid,'topic')
