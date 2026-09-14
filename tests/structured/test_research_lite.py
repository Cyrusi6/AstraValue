import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from analysis.structured.research_lite import (
    _merge_request_audit,
    build_lite_pack,
    lite_periods,
    read_evidence,
)
from analysis.structured.scope import LITE_PROFILE_ID, load_research_profile


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n' for row in rows),encoding='utf8')


def _fact(fact_id, metric, value, period, kind='cumulative', *, namespace='ns-primary'):
    return {'fact_id':fact_id,'ticker':'600519','metric_id':metric,'value':value,'unit':'CNY',
        'currency':'CNY','period_start':period[:4]+'-01-01','period_end':period,'period_type':kind,
        'scope':'consolidated','as_of':'2026-09-13T00:00:00Z','source_ids':['source-1'],
        'verification_status':'供应商直采','derived_from_fact_ids':[], 'method_ref':None,
        'metadata':{'decimal_value':str(value),'available_at':'2026-09-13T00:00:00Z','storage_namespace_id':namespace}}


def _fixture(root: Path, *, revenue='100', evidence=True, namespace='ns-primary'):
    company=root/'600519';company.mkdir(parents=True)
    periods=['2022-12-31','2023-12-31','2024-12-31','2025-12-31']
    facts=[]
    for index,period in enumerate(periods):
        value=revenue if period=='2025-12-31' else str(70+index*10)
        facts.extend([_fact(f'{namespace}-revenue-{period}', 'operating_income',value,period,namespace=namespace),
            _fact(f'{namespace}-cost-{period}','operating_cost',str(int(value)//2),period,namespace=namespace)])
    _write_jsonl(company/'coverage-facts.jsonl',facts)
    _write_jsonl(company/'normalized-records.jsonl',[{'company':'600519','dataset_id':'company_basic','record_id':'company-1',
        'snapshot_id':'snapshot-1','row_key':'row-1','period':None,'available_at':'2026-09-13T00:00:00Z',
        'fields':{'ORG_NAME':'贵州茅台酒股份有限公司','MAIN_BUSINESS':'生产并销售白酒','CSRC_INDUSTRY_NAME':'酒、饮料和精制茶制造业'}}])
    _write_jsonl(company/'question-coverage.jsonl',[{'question_id':'ES01.Q01','requirement_id':'REQ.ES01.Q01.001',
        'period':'2026-09-13','state':'source_text_available'}])
    (company/'next-work.json').write_text('{"items":[]}\n',encoding='utf8')
    (company/'manifest.json').write_text(json.dumps({'run_id':'run-1','source_namespace_id':namespace,
        'contract_hash':'contract','materialization_hash':'materialized'}),encoding='utf8')
    if not evidence:return
    original=root/'original.pdf';original.write_bytes(b'%PDF-test-source')
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    routes=['RD01','RD02','RD03','RD06','RD07','RD08','RD09','RD10','RD11','RD12']
    rows=[]
    for index,route in enumerate(routes):
        text=('这是带否定、条件和上下文的原文证据，不代表自动研究结论。'*30)+route
        rows.append({'company':'600519','period':'2025-12-31','route_id':route,'document_class':'D01',
            'evidence_id':f'evidence-{route}','original_sha256':digest,'original_url':'https://example.invalid/report.pdf',
            'locator':f'page:{index+1}','parser_version':'test-v-lite','text':text,'data_nature':'source_text',
            'semantic_status':'organized_literal_source_passage','question_answer_status':'not_evaluated'})
    _write_jsonl(root/'document-evidence.jsonl',rows)
    (root/'live-documents.json').write_text(json.dumps({'catalogs':[{'ticker':'600519','terminal':True}],
        'documents':[{'ticker':'600519','period':'2025-12-31','document_class':'D01','parse_status':'parsed',
            'resource_id':'annual','original_sha256':digest,'original_path':str(original.resolve()),'parser_version':'parser'},
            {'ticker':'600519','period':'2026-06-30','document_class':'D02','parse_status':'parsed',
            'resource_id':'interim','original_sha256':digest,'original_path':str(original.resolve()),'parser_version':'parser'}]}),encoding='utf8')


def test_period_window_matches_three_years_and_eight_required_quarters():
    periods=lite_periods(date(2026,9,13),load_research_profile(LITE_PROFILE_ID))
    assert periods['annual']==['2023-12-31','2024-12-31','2025-12-31']
    assert periods['quarters']==['2024-09-30','2024-12-31','2025-03-31','2025-06-30','2025-09-30','2025-12-31','2026-03-31','2026-06-30']
    assert '2022-12-31' in periods['dependencies']


def test_pack_is_stable_routes_all_questions_and_reads_bounded_evidence(tmp_path):
    source=tmp_path/'source';output=tmp_path/'output';_fixture(source)
    first=build_lite_pack(input_root=source,ticker='600519',as_of=date(2026,9,13),output_root=output)
    second=build_lite_pack(input_root=source,ticker='600519',as_of=date(2026,9,13),output_root=output)
    assert first['status'] in {'ready','budget_exceeded'}
    assert second['cache_reused'] and first['pack_id']==second['pack_id']
    pack=Path(first['pack_dir']);assert {p.name for p in pack.iterdir()} >= {
        'core-pack.md','core-pack.json','core-coverage.json','evidence-index.jsonl','next-work.json','manifest.json'}
    coverage=json.loads((pack/'core-coverage.json').read_text(encoding='utf8'))
    assert coverage['question_count']==54 and coverage['route_counts']=={'conditional':11,'core':38,'deferred':5}
    assert coverage['full_coverage_preserved']
    evidence_id=json.loads((pack/'evidence-index.jsonl').read_text(encoding='utf8').splitlines()[0])['evidence_id']
    page=read_evidence(pack_dir=pack,evidence_id=evidence_id,max_tokens=25)
    assert page['truncated'] and page['next_page']==2 and page['token_count']['count']<=25
    assert page['original_hash_verified']


def test_supplement_conflict_and_low_budget_are_explicit(tmp_path):
    primary=tmp_path/'primary';supplement=tmp_path/'supplement';output=tmp_path/'output'
    _fixture(primary,revenue='100',evidence=False,namespace='ns-primary')
    _fixture(supplement,revenue='101',evidence=False,namespace='ns-supplement')
    result=build_lite_pack(input_root=primary,supplements=(supplement,),ticker='600519',as_of=date(2026,9,13),output_root=output,max_tokens=10)
    assert result['status']=='budget_exceeded'
    pack=Path(result['pack_dir']);payload=json.loads((pack/'core-pack.json').read_text(encoding='utf8'))
    conflicts=[item for item in payload['conflicts'] if item['kind']=='logical_fact_value_conflict']
    assert conflicts and not conflicts[0]['resolved']
    assert payload['token_budget']==10
    markdown=(pack/'core-pack.md').read_text(encoding='utf8')
    assert '质量与风险' in markdown and '期后目录' in markdown and 'budget_exceeded' in markdown


def test_unvalidated_industry_fails_closed(tmp_path):
    try:
        build_lite_pack(input_root=tmp_path,ticker='600000',as_of=date(2026,9,13),output_root=tmp_path/'out')
    except ValueError as exc:
        assert str(exc)=='轻量画像未验证'
    else:
        raise AssertionError('unvalidated financial profile must fail closed')


def test_peer_input_hash_participates_in_pack_identity(tmp_path):
    source=tmp_path/'source';output=tmp_path/'output';_fixture(source)
    peer=source/'000858';peer.mkdir()
    peer_facts=[_fact('peer-revenue','operating_income','80','2025-12-31')]
    _write_jsonl(peer/'coverage-facts.jsonl',peer_facts)
    first=build_lite_pack(input_root=source,ticker='600519',as_of=date(2026,9,13),output_root=output)
    peer_facts[0]['value']='81';peer_facts[0]['metadata']['decimal_value']='81'
    _write_jsonl(peer/'coverage-facts.jsonl',peer_facts)
    second=build_lite_pack(input_root=source,ticker='600519',as_of=date(2026,9,13),output_root=output)
    assert first['pack_id']!=second['pack_id']
    manifest=json.loads((Path(second['pack_dir'])/'manifest.json').read_text(encoding='utf8'))
    assert manifest['peer_inputs'][0]['ticker']=='000858'


def test_historical_as_of_uses_latest_disclosed_interim(tmp_path):
    source=tmp_path/'source';output=tmp_path/'output';_fixture(source)
    live=json.loads((source/'live-documents.json').read_text(encoding='utf8'))
    annual=live['documents'][0]
    prior_interim=dict(live['documents'][1],period='2025-06-30',resource_id='prior-interim')
    future_interim=dict(live['documents'][1],period='2026-06-30',resource_id='future-interim',published_at='2026-08-30')
    live['documents']=[annual,prior_interim,future_interim]
    (source/'live-documents.json').write_text(json.dumps(live),encoding='utf8')
    result=build_lite_pack(input_root=source,ticker='600519',as_of=date(2026,7,15),output_root=output)
    payload=json.loads((Path(result['pack_dir'])/'core-pack.json').read_text(encoding='utf8'))
    interim=payload['catalog']['required_reports']['latest_interim']
    assert interim['period']=='2025-06-30' and interim['resource_ids']==['prior-interim']


def test_catalog_request_audit_accumulates_and_preserves_real_io():
    first={'request_key':'stock-list','sha256':'a','cache_reused':False}
    replay={'request_key':'stock-list','sha256':'a','cache_reused':True}
    company={'request_key':'company-page','sha256':'b','cache_reused':False}
    merged=_merge_request_audit([first],[replay,company])
    assert [item['request_key'] for item in merged]==['company-page','stock-list']
    assert next(item for item in merged if item['request_key']=='stock-list')['cache_reused'] is False
    with pytest.raises(ValueError,match='catalog_request_snapshot_conflict'):
        _merge_request_audit([first],[first|{'sha256':'changed'}])
