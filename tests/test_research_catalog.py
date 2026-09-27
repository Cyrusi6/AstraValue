import json
from pathlib import Path
import pytest
from test_research_workspace import workspace
from analysis.research.catalog import Catalog, classify_filing
from analysis.research.tools import operations
from analysis.research.workspace import ResearchError, sha


def test_first_pack_is_directory_only_and_exclusions_are_not_model_tools(workspace):
    s=workspace.prepare_research('贵州茅台','2025-01-01');ops=operations(workspace)
    first=ops['get_research_brief'](s['research_id'])
    assert first['categories']
    assert 'F1' not in json.dumps(first) and 'exclusion' not in json.dumps(first)
    assert 'catalog_audit' not in ops and 'refresh_catalog' not in ops
    rows=ops['list_materials'](s['research_id'],'metrics')['items']
    r=next(x for x in rows if x['title']=='营收')
    content=ops['read_material'](s['research_id'],r['material_id'])
    assert 'F1' in content['content'] and '100' in content['content']
    other=workspace.prepare_research('五粮液','2025-01-01')
    with pytest.raises(ResearchError,match='current_catalog'):
        ops['read_material'](other['research_id'],r['material_id'])


def test_scope_filter_and_field_drilldown_account_for_every_cached_row(workspace):
    root=workspace.root;p=root/'packs/600519/2025-01-01/lite-pack-test'
    records=root/'rows.jsonl'
    rows=[{'record_id':'r1','company':'600519','dataset_id':'balance_fields','period':'2024-12-31','snapshot_id':'raw1',
        'fields':{'ACCOUNTS_PAYABLE':10,'GOODWILL':None,'UNUSED_SECRET_FIELD':'hidden'}},
        {'record_id':'r2','company':'600519','dataset_id':'margin','period':'2024-12-31','snapshot_id':'raw2','fields':{'value':1}},
        {'record_id':'r3','company':'000858','dataset_id':'balance_fields','period':'2024-12-31','snapshot_id':'raw3','fields':{}}]
    records.write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf8')
    m=json.loads((p/'manifest.json').read_text('utf8'));m['source_inputs']=[{'files':{'normalized-records.jsonl':{'path':str(records),'sha256':sha(records)}}}]
    (p/'manifest.json').write_text(json.dumps(m),encoding='utf8')
    s=workspace.prepare_research('贵州茅台','2025-01-01');api=Catalog(workspace);rid=s['research_id']
    parent=next(x for x in api.list_materials(rid,'metrics',page_size=40)['items'] if x['title']=='balance_fields 原始字段')
    fields=api.list_materials(rid,'metrics',parent_id=parent['material_id'],page_size=40)['items']
    assert all('UNUSED_SECRET_FIELD' not in x['title'] for x in fields)
    amount=next(x for x in fields if x['title'].endswith('.ACCOUNTS_PAYABLE'))
    assert '10' in api.read_material(rid,amount['material_id'],period='2024-12-31')['content']
    blank=next(x for x in fields if x['title'].endswith('.GOODWILL'))
    assert blank['status']=='processing'
    _,d=api._load(rid)
    actual={str(records)+':'+r['record_id'] for r in rows}
    assert actual <= {x['source_key'] for x in d['ledger']}
    assert 'outside_research_scope' in str(api.catalog_audit(rid))
    assert 'outside_research_scope' not in str(api.catalog_overview(rid))


def test_filing_use_filter_is_not_everything():
    assert classify_filing('贵州茅台权益分派实施公告')[0]=='capital'
    assert classify_filing('贵州茅台高级管理人员被实施留置公告')[0]=='governance'
    assert classify_filing('年度报告摘要')[0] is None
    assert classify_filing('股东大会通知')[0] is None
