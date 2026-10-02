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


def register_projection_files(workspace, files):
    pack=workspace.root/'packs/600519/2025-01-01/lite-pack-test'
    path=pack/'manifest.json'
    manifest=json.loads(path.read_text('utf8'))
    manifest['source_inputs']=[{'files':{
        name:{'path':str(source),'sha256':sha(source)} for name,source in files.items()}}]
    path.write_text(json.dumps(manifest),encoding='utf8')
    return pack


def test_current_run_records_are_indexed_when_root_is_empty_and_null_rows_stay_processing(workspace):
    empty=workspace.root/'empty.jsonl';empty.write_text('',encoding='utf8')
    records=workspace.root/'run.jsonl'
    rows=[{'record_id':'income','company':'600519','dataset_id':'income_fields','period':'2024-12-31',
           'snapshot_id':'income-snapshot','fields':{'OPERATE_INCOME':100}},
          {'record_id':'blank','company':'600519','dataset_id':'balance_fields','period':'2024-12-31',
           'snapshot_id':'balance-snapshot','fields':{'GOODWILL':None}},
          {'record_id':'future','company':'600519','dataset_id':'cashflow_fields','period':'2025-12-31',
           'snapshot_id':'future-snapshot','fields':{'NETCASH_OPERATE':100}}]
    records.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf8')
    register_projection_files(workspace, {'normalized-records.jsonl':empty,
        'runs/current/normalized-records.jsonl':records})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    catalog=Catalog(workspace);_,data=catalog._load(rid)
    income=next(r for r in data['items'] if r['title']=='income_fields 原始字段')
    balance=next(r for r in data['items'] if r['title']=='balance_fields 原始字段')
    assert not any(r['reader']=='missing' and r['payload'].get('dataset_id') in {'income_fields','balance_fields'}
                   for r in data['items'])
    assert catalog.read_material(rid,income['material_id'])['status']=='original_readable'
    assert catalog.read_material(rid,balance['material_id'])['status']=='processing'
    cashflow=next(r for r in data['items'] if r['reader']=='missing' and r['payload']['dataset_id']=='cashflow_fields')
    assert catalog.read_material(rid,cashflow['material_id'])['status']=='unavailable'
    assert set(data['audit_scope']['projection_files'])=={str(empty),str(records)}
    assert any(r.get('reason')=='period_after_cutoff' for r in data['ledger'])


def test_frozen_formal_facts_supply_exact_dataset_routes_without_claiming_raw_fields(workspace):
    facts=workspace.root/'facts.jsonl'
    facts.write_text('\n'.join(json.dumps(row) for row in [
        {'fact_id':'600519-revenue','ticker':'600519','metadata':{'structured_dataset_id':'income_fields'}},
        {'fact_id':'unselected-assets','ticker':'600519','metadata':{'structured_dataset_id':'balance_fields'}}]),encoding='utf8')
    register_projection_files(workspace, {'runs/current/facts.jsonl':facts})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    catalog=Catalog(workspace);_,data=catalog._load(rid)
    income=next(r for r in data['items'] if r['reader']=='missing' and r['payload']['dataset_id']=='income_fields')
    result=catalog.read_material(rid,income['material_id'],period='2024-12-31')
    assert result['status']=='formal_numeric' and result['source_status']=='missing'
    view=json.loads(result['content'])
    assert view['available_periods']==['2024-12-31']
    assert view['materials'][0]['fact_ref']=='F1'
    assert view['materials'][0]['read_entry']['tool']=='read_material'
    assert 'request_materials' not in result['content']
    assert catalog.read_material(rid,income['material_id'],period='2023-12-31')['status']=='unavailable'
    balance=next(r for r in data['items'] if r['reader']=='missing' and r['payload']['dataset_id']=='balance_fields')
    assert catalog.read_material(rid,balance['material_id'])['status']=='unavailable'
    before=facts.read_bytes();facts.write_text('changed',encoding='utf8')
    with pytest.raises(ResearchError,match='catalog_projection_changed'):
        catalog.read_material(rid,income['material_id'])
    facts.write_bytes(before)


def test_existing_catalog_is_extended_for_unindexed_current_run_without_overwriting_it(workspace):
    records=workspace.root/'run.jsonl'
    records.write_text(json.dumps({'record_id':'income','company':'600519','dataset_id':'income_fields',
        'period':'2024-12-31','snapshot_id':'snapshot','fields':{'OPERATE_INCOME':100}}),encoding='utf8')
    register_projection_files(workspace, {'runs/current/normalized-records.jsonl':records})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    previous=workspace.root/'old-catalog.json'
    previous.write_text(json.dumps({'items':[],'audit_scope':{'projection_files':[]}}),encoding='utf8')
    original=previous.read_bytes()
    workspace.artifact(rid,'material_catalog',{'path':str(previous),'sha256':sha(previous)})
    catalog=Catalog(workspace);_,data=catalog._load(rid)
    assert any(r['title']=='income_fields 原始字段' for r in data['items'])
    assert previous.read_bytes()==original
    assert len(workspace.artifacts(rid,'material_catalog'))==2
    catalog._load(rid)
    assert len(workspace.artifacts(rid,'material_catalog'))==2


def test_registered_relative_archive_paths_resolve_from_workspace_root(workspace):
    import fitz
    import sqlite3
    archive=workspace.root/'archive';archive.mkdir()
    source=archive/'filing.pdf'
    with fitz.open() as document:
        page=document.new_page();page.insert_text((40,50),'600519 年度报告',fontname='china-s')
        document.save(source)
    database=archive/'archive.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE raw_resource_snapshots (resource_role TEXT,payload TEXT)')
        connection.execute('INSERT INTO raw_resource_snapshots VALUES (?,?)',('content',json.dumps({
            'snapshot_id':'filing','published_at':'2025-01-01','sha256':sha(source),
            'archive_relative_path':'filing.pdf','canonical_url':'https://example.com/filing.pdf'})))
    workspace.config['valuation_caches']=[{'db':'archive/archive.sqlite','data_root':'archive'}]
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    _,data=Catalog(workspace)._load(rid)
    assert data['audit_scope']['registered_archive_databases']==[str(database)]
    assert data['audit_scope']['cached_originals_examined']==1
    assert any(r['reader']=='pdf' and r['payload']['path']==str(source) for r in data['items'])
    assert not any(r.get('reason')=='configured_cache_unavailable' for r in data['ledger'])


def test_query_receipts_distinguish_completed_empty_unfinished_and_partial_records(workspace):
    def receipt(ident,dataset,status,**updates):
        return {'coverage_record_id':ident,'run_id':'current','company_id':'company:600519',
            'dataset_id':dataset,'scope_key':dataset,'storage_namespace_id':'namespace','version':1,
            'status':status,'recorded_at':'2025-01-01T01:00:00+00:00',
            'safe_through':'2025-01-01T00:00:00+00:00' if status=='no_data' else None,
            'query_complete':status=='no_data',**updates}
    rows=[receipt('empty','guarantee','no_data'),receipt('failed','litigation','failed'),
          receipt('partial','cashflow_fields','partial'),
          receipt('old','staff_pay','failed',version=8,recorded_at='2024-12-31T23:00:00+00:00'),
          receipt('recovered','staff_pay','no_data'),
          receipt('unconfirmed','goodwill','no_data',query_complete=False,safe_through=None),
          receipt('other-company','seo','no_data',company_id='company:000858'),
          receipt('future','bond_issuance','no_data',recorded_at='2025-01-02T00:00:00+00:00')]
    coverage=workspace.root/'query-coverage.jsonl'
    coverage.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf8')
    records=workspace.root/'records.jsonl'
    records.write_text(json.dumps({'record_id':'cashflow','company':'600519','dataset_id':'cashflow_fields',
        'period':'2024-12-31','snapshot_id':'snapshot','fields':{'NETCASH_OPERATE':10}}),encoding='utf8')
    register_projection_files(workspace, {'runs/current/acquisition-coverage.jsonl':coverage,
        'runs/current/normalized-records.jsonl':records})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];catalog=Catalog(workspace)
    _,data=catalog._load(rid)
    indexed={r['title']:r for r in data['items']}
    empty=catalog.read_material(rid,indexed['guarantee']['material_id'])
    assert empty['status']=='unavailable' and empty['source_query_status']=='source_returned_empty'
    proof=json.loads(empty['content'])['acquisition_coverage']
    assert proof['empty_query_record_ids']==['empty'] and proof['unfinished_record_ids']==[]
    assert proof['receipts'][0]['coverage']==rows[0]
    assert proof['receipts'][0]['source_sha256']==sha(coverage)
    failed=catalog.read_material(rid,indexed['litigation']['material_id'])
    assert failed['source_query_status']=='acquisition_incomplete'
    partial=catalog.read_material(rid,indexed['cashflow_fields 原始字段']['material_id'])
    assert partial['status']=='original_readable' and partial['source_query_status']=='acquisition_incomplete'
    recovered=catalog.read_material(rid,indexed['staff_pay']['material_id'])
    assert recovered['source_query_status']=='source_returned_empty'
    assert json.loads(recovered['content'])['acquisition_coverage']['effective_record_ids']==['recovered']
    assert catalog.read_material(rid,indexed['goodwill']['material_id'])['source_query_status']=='acquisition_incomplete'
    for name in ('seo','bond_issuance'):
        assert 'source_query_status' not in catalog.read_material(rid,indexed[name]['material_id'])
    coverage.write_text('changed',encoding='utf8')
    with pytest.raises(ResearchError,match='material_projection_changed'):
        catalog.read_material(rid,indexed['guarantee']['material_id'])


def test_record_disclosed_after_cutoff_is_not_exposed_as_available(workspace):
    records=workspace.root/'later-record.jsonl'
    records.write_text(json.dumps({'record_id':'late','company':'600519','dataset_id':'income_fields',
        'period':'2024-12-31','available_at':'2025-01-02T00:00:00+00:00','snapshot_id':'snapshot',
        'fields':{'OPERATE_INCOME':200}}),encoding='utf8')
    register_projection_files(workspace,{'runs/current/normalized-records.jsonl':records})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    _,data=Catalog(workspace)._load(rid)
    assert not any(r['reader']=='records' for r in data['items'])
    assert any(r.get('reason')=='record_available_after_cutoff' for r in data['ledger'])


@pytest.mark.parametrize('query_complete,safe_through',[(None,'2025-01-01T00:00:00Z'),(False,'2025-01-01T00:00:00Z'),(True,None)])
def test_old_empty_query_without_completion_proof_stays_unfinished(workspace,query_complete,safe_through):
    row={'coverage_record_id':'legacy-empty','run_id':'legacy','company_id':'company:600519',
        'dataset_id':'guarantee','scope_key':'window','status':'no_data','version':1,
        'recorded_at':'2025-01-01T00:00:00Z','safe_through':safe_through}
    if query_complete is not None:row['query_complete']=query_complete
    source=workspace.root/'old-coverage.jsonl';source.write_text(json.dumps(row),encoding='utf8')
    register_projection_files(workspace,{'runs/legacy/acquisition-coverage.jsonl':source})
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    catalog=Catalog(workspace);_,data=catalog._load(rid)
    item=next(item for item in data['items'] if item['title']=='guarantee')
    result=catalog.read_material(rid,item['material_id'])
    assert result['status']=='unavailable' and result['source_query_status']=='acquisition_incomplete'
    proof=json.loads(result['content'])['acquisition_coverage']
    assert proof['empty_query_record_ids']==[] and proof['unfinished_record_ids']==['legacy-empty']
