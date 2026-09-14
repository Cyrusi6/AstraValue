import json
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal

import fitz
import pytest
from openpyxl import Workbook

from analysis.documents import parse_research_original
from analysis.structured.interpretation import load_interpretation, numeric_semantic_failure
from analysis.structured.materialization import _dimension, _validate_rule
from analysis.structured.registry import StructuredRegistryLoader, PROJECT_ROOT
from analysis.structured.runtime import _build_sources
from analysis.structured.research import periods
from datetime import date


def test_all_new_interpretations_have_verified_evidence_and_correct_capital_unit():
    bundle=StructuredRegistryLoader().load()
    context=SimpleNamespace(field_registry_id=bundle.fields.registry_id,field_registry_version=bundle.fields.version,
        field_registry_hash=bundle.content_hashes['fields'],frozen_config={'source_definitions':[s.to_mapping() for s in _build_sources(bundle).values()]})
    value=load_interpretation('eastmoney-financial-interpretation-v1.0.0',context)
    assert len(value['rules'])==77
    for key,rule in value['rules'].items():_validate_rule(key,rule)
    assert value['rules']['balance_fields.SHARE_CAPITAL']['unit']=='CNY'
    assert value['rules']['market_cap.TOTAL_SHARES']['unit']=='shares'
    assert 'SUM_AMOUNT' not in {r['raw_field'] for r in value['rules'].values()}


def test_counterparty_remainder_never_enters_disclosed_amount():
    rule={'dataset_id':'customers_peer','raw_field':'AMOUNT'}
    assert numeric_semantic_failure(rule,{'RANK':6},Decimal('1'))=='counterparty_remainder_is_not_disclosed_amount'
    assert numeric_semantic_failure(rule,{'RANK':1},Decimal('0')) is None
    first=_dimension('customers_peer',{'TYPE_CODE':'1','RANK':1,'ITEM_NAME':'第一名'})
    supplier=_dimension('customers_peer',{'TYPE_CODE':'2','RANK':1,'ITEM_NAME':'第一名'})
    assert first['dimension_type'] != supplier['dimension_type']
    assert first['dimension_code']=='rank:1'


def test_pdf_page_locations_cache_hash_and_mime(tmp_path):
    path=tmp_path/'original.pdf'
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((60,60),'Audit report and financial notes')
        doc.save(path)
    result=parse_research_original(path,mime_type='application/pdf',output_dir=tmp_path/'parsed')
    assert any(u['locator']=='page:1' and 'Audit report' in u['text'] for u in result['units'])
    assert result['consumption_status']=='not_yet_consumable'
    again=parse_research_original(path,mime_type='application/pdf',output_dir=tmp_path/'parsed')
    assert again['cache_reused'] and again['content_sha256']==result['content_sha256']
    with pytest.raises(ValueError,match='mime_content'):parse_research_original(path,mime_type='text/html',output_dir=tmp_path/'parsed')
    parsed=Path(result['parsed_path']);v=json.loads(parsed.read_text(encoding='utf8'));v['units'][0]['text']='changed';parsed.write_text(json.dumps(v),encoding='utf8')
    with pytest.raises(ValueError,match='cache_identity'):parse_research_original(path,mime_type='application/pdf',output_dir=tmp_path/'parsed')


def test_official_table_cells_formulas_and_empty_values_remain_distinct(tmp_path):
    path=tmp_path/'table.xlsx';book=Workbook();sheet=book.active;sheet.title='单位：元'
    sheet['A1']='金额';sheet['A2']=0;sheet['B2']='=A2+1';book.save(path)
    result=parse_research_original(path,mime_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',output_dir=tmp_path/'parsed')
    cells={u['locator']:u for u in result['units']}
    assert cells['sheet:单位：元/cell:A2']['text']=='0'
    assert cells['sheet:单位：元/cell:B2']['kind']=='formula'
    assert 'sheet:单位：元/cell:B1' not in cells


def test_json_html_and_unsupported_format_states(tmp_path):
    path=tmp_path/'input';path.write_bytes(b'{"amount":null,"zero":0}')
    result=parse_research_original(path,mime_type='application/json',output_dir=tmp_path/'parsed')
    assert {u['locator']:u['value'] for u in result['units']}=={'$.amount':None,'$.zero':0}
    path.write_text('<html><h1>access denied</h1></html>',encoding='utf8')
    with pytest.raises(ValueError,match='interception'):parse_research_original(path,mime_type='text/html',output_dir=tmp_path/'parsed')
    path.write_bytes(b'old binary office document')
    result=parse_research_original(path,mime_type='application/msword',output_dir=tmp_path/'parsed')
    assert result['parse_status']=='adapter_missing' and result['consumption_status']=='not_yet_consumable'


def test_period_windows_include_five_years_and_twelve_published_quarters():
    values=periods('FIN',date(2026,9,13),date(2026,6,30))
    assert '2021-12-31' in values and '2023-09-30' in values and '2026-06-30' in values
    assert '2026-09-30' not in values


def test_negative_numeric_strings_are_not_classified_as_source_text():
    from analysis.structured.research import is_source_text
    assert not any(is_source_text(v) for v in ('-2.5','1e3','NaN','0',''))
    assert is_source_text('公司自主披露的经营模式')


def test_html_table_cells_keep_negative_zero_and_multilevel_headers(tmp_path):
    from analysis.structured.research import normalize_official_tables,write_json
    path=tmp_path/'original.html'
    path.write_text('<table><tr><th>指标</th><th>环比涨跌幅（%）</th><th>同比涨跌幅（%）</th><th>1—8月同比涨跌幅（%）</th></tr><tr><td>工业价格</td><td>-2</td><td>0</td><td>…</td></tr></table>',encoding='utf8')
    parsed=parse_research_original(path,mime_type='text/html',output_dir=tmp_path/'parsed')
    write_json(tmp_path/'selected-documents'/'one.json',{'documents':[{'url':'https://www.stats.gov.cn/example','parse_status':'parsed','metadata':{'table_schema':'nbs-ppi-growth-v1','period':'2026-08'},'parsed_path':parsed['parsed_path'],'original_sha256':parsed['original_sha256'],'published_at':'2026-09-09'}]})
    facts=normalize_official_tables(tmp_path)
    assert {f['value'] for f in facts}=={'-0.02','0'}
    assert {f['locator'] for f in facts}=={'table:1/row:2/column:2','table:1/row:2/column:3'}
    assert all(not f['company_financial_formula_eligible'] for f in facts)


def test_coverage_merges_periods_but_does_not_hide_revision_conflicts(tmp_path):
    from test_materialization import Fixture
    from analysis.structured.materialization_replay import _export_result
    from analysis.structured.research import build_coverage,read_jsonl
    folders=[]
    for i,(period,value) in enumerate([('2025-12-31','100'),('2026-06-30','110'),('2026-06-30','120')]):
        fixture=Fixture();record,field,snapshot=fixture.add(period=period,value=value)
        snapshot.snapshot_id=f'snapshot-{i}';snapshot.sha256=f'{i+1:064x}'
        record['snapshot_id']=snapshot.snapshot_id;record['record_version_id']=f'record-{i}'
        field['record_version_id']=record['record_version_id']
        fixture.snapshots={snapshot.snapshot_id:snapshot}
        result=fixture.run();folder=tmp_path/str(i);_export_result(result,folder,'ns-1');folders.append(folder)
    connection=SimpleNamespace(execute=lambda *_:[])
    summary=build_coverage(connection,'run-1','600519',tmp_path,date(2026,9,13),folders[:2])
    assert summary['selected_coverage_facts']>0
    # The cumulative H1 field cannot be fulfilled by a derived TTM/quarter value.
    rows=list(read_jsonl(tmp_path/'question-coverage.jsonl'))
    ids={f['fact_id']:f for f in read_jsonl(tmp_path/'coverage-facts.jsonl')}
    assert all(not ids[f].get('derived_from_fact_ids') for r in rows for f in r['fact_ids'])
    summary=build_coverage(connection,'run-1','600519',tmp_path,date(2026,9,13),folders)
    assert any('source_or_revision_conflict' in g for g in summary['selection_conflicts'])


def test_financial_calculation_preserves_inputs_and_excludes_financial_company():
    from test_materialization import Fixture
    from analysis.structured.research import compute_financial_indicators
    fixture=Fixture()
    fixture.add(raw='OPERATE_INCOME',value='200')
    facts=[f.model_copy(update={'metadata':f.metadata|{'statement_org_type':'通用'}}) for f in fixture.run().facts if not f.derived_from_fact_ids]
    facts.append(facts[0].model_copy(update={'fact_id':'expense','metric_id':'selling_expense','value':30,
        'metadata':facts[0].metadata|{'decimal_value':'30'}}))
    computed=compute_financial_indicators(facts)
    ratio=next(f for f in computed if f.metric_id=='selling_expense_ratio')
    assert ratio.metadata['decimal_value']=='0.15' and len(ratio.derived_from_fact_ids)==2
    assert compute_financial_indicators([f.model_copy(update={'metadata':f.metadata|{'statement_org_type':'银行'}}) for f in facts])==[]
    mismatched=[facts[0],facts[1].model_copy(update={'period_end':date(2025,12,31)})]
    assert compute_financial_indicators(mismatched)==[]


def test_original_download_recovers_corrupt_partial_blob_without_losing_bytes(tmp_path,monkeypatch):
    import httpx
    from analysis.acquisition.research_fetch import ResearchFetch
    import analysis.acquisition.research_fetch as module
    original_client=httpx.Client;calls=[]
    def respond(request):
        calls.append(str(request.url));return httpx.Response(200,content=b'%PDF-original-bytes',headers={'content-type':'application/pdf'})
    monkeypatch.setattr(module.httpx,'Client',lambda **_:original_client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    fetch=ResearchFetch(tmp_path)
    path,proof=fetch.fetch('https://static.cninfo.com.cn/test.pdf')
    path.write_bytes(b'partial-download')
    restored,_=fetch.fetch('https://static.cninfo.com.cn/test.pdf')
    assert restored.read_bytes()==b'%PDF-original-bytes'
    assert any(p.read_bytes()==b'partial-download' for p in (tmp_path/'raw'/'quarantine').rglob('*') if p.is_file())
    _,cached=fetch.fetch('https://static.cninfo.com.cn/test.pdf')
    assert cached['cache_reused'] and len(calls)==2


def test_reviewed_issuer_and_media_hosts_preserve_source_role(tmp_path, monkeypatch):
    import httpx
    from analysis.acquisition.research_fetch import ResearchFetch
    import analysis.acquisition.research_fetch as module
    original_client = httpx.Client
    monkeypatch.setattr(module.httpx, 'Client', lambda **_: original_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'<html>statement</html>'))))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    fetch = ResearchFetch(tmp_path)
    for host, role in [('www.moutaichina.com','issuer_statement'), ('m.thepaper.cn','media_report')]:
        _, proof = fetch.fetch('https://' + host + '/article')
        assert proof['source_role'] == role
    with pytest.raises(ValueError, match='unregistered'):
        fetch.fetch('https://www.moutaichina.com.example.test/article')


def test_missing_financials_do_not_shorten_the_required_coverage_window():
    from analysis.structured.research import required_latest_period
    assert required_latest_period(date(2026,9,13))==date(2026,6,30)
    assert required_latest_period(date(2026,10,30))==date(2026,6,30)
    assert required_latest_period(date(2026,10,31))==date(2026,9,30)


def test_observed_field_without_period_is_processing_work_not_refetch(tmp_path):
    from analysis.structured.research import build_coverage,write_json,read_jsonl
    write_json(tmp_path/'manifest.json',{'run_id':'r','contract_hash':'h','source_namespace_id':'n','selected_fact_ids':[],'selected_dimensional_fact_ids':[]})
    record={'record_version_id':'observed','snapshot_id':'snapshot','row_key':'row','available_at':'2026-09-12','raw_row':{'OPERATE_INCOME':100}}
    connection=SimpleNamespace(execute=lambda *_:[{'dataset_id':'income_fields','purpose':'data','payload':json.dumps(record)}])
    build_coverage(connection,'r','600519',tmp_path,date(2026,9,13))
    rows=[r for r in read_jsonl(tmp_path/'question-coverage.jsonl') if r['input'].get('dataset_id')=='income_fields' and r['input'].get('raw_name')=='OPERATE_INCOME']
    assert rows and all(r['reason']=='observed_field_period_unconfirmed' for r in rows)
    work=json.loads((tmp_path/'next-work.json').read_text(encoding='utf8'))['items']
    assert all(not r['acquire_allowed'] and r['stage']=='semantic_processing' for r in work if r['raw_name']=='OPERATE_INCOME' and r['dataset_id']=='income_fields')
