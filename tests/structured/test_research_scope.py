from datetime import date
from types import SimpleNamespace

import pytest

from analysis.structured.scope import (
    LITE_PROFILE_ID,
    assert_network_scope,
    load_research_profile,
    load_scope,
    request_fields,
    selected_datasets,
)
from analysis.structured.reading import select_research_document


def entry(title, published='2026-04-20', ticker='600519'):
    return {'title':title,'resource_url':'https://static.cninfo.com.cn/test.PDF',
            'expected_mime_types':['application/pdf'],'ticker':ticker,'published_at':published}


def test_default_scope_and_old_network_gate():
    scope=load_scope()
    assert len(scope['datasets'])==55 and len(scope['documents'])==21
    assert {'balance_fields','income_quarter','customers_peer','capital_raise'} <= set(selected_datasets())
    for dataset in ('baostock_daily','margin','billboard','fund_holds','tags','baostock_adjust'):
        assert dataset not in selected_datasets()
        with pytest.raises(ValueError,match='excluded'):selected_datasets([dataset])
    assert selected_datasets(['baostock_profit'])==('baostock_profit',)
    with pytest.raises(ValueError,match='legacy_scope_network_blocked'):
        assert_network_scope(SimpleNamespace(frozen_config={}), 'balance_fields')
    assert_network_scope(SimpleNamespace(frozen_config={'research_scope':scope}),'balance_fields')


def test_optional_columns_are_narrowed_including_em_m():
    params=request_fields('market_cap',{'sty':'ALL','filter':'company'})
    assert 'CLOSE_PRICE' in params['sty'] and 'TOTAL_SHARES' in params['sty']
    assert 'CHANGE_RATE' not in params['sty'] and 'ALL' not in params['sty']
    params=request_fields('segments',{'columns':'ALL'})
    assert 'MAIN_BUSINESS_INCOME' in params['columns'] and 'ITEM_PARENT_CODE' in params['columns']
    assert 'ALL' not in params['columns']


def test_retrieval_timestamp_is_provenance_only():
    scope = load_scope()
    for dataset_id, rule in scope['datasets'].items():
        assert '__retrieved_at' not in rule.get('request_fields', [])
    profile = load_research_profile(LITE_PROFILE_ID)
    for rule in profile['datasets'].values():
        assert '__retrieved_at' not in rule.get('fields', [])
    params = request_fields('company_basic', {'columns': 'ALL'})
    assert '__retrieved_at' not in params['columns'].split(',')


def test_request_fields_excludes_local_provenance_from_legacy_profile(monkeypatch):
    from analysis.structured import scope as scope_module

    rule = dict(load_scope()['datasets']['company_basic'])
    rule['request_fields'] = [*rule['request_fields'], '__retrieved_at']
    monkeypatch.setattr(scope_module, 'load_scope', lambda: {'datasets': {'company_basic': rule}})
    for parameter in ('columns', 'fields', 'sty'):
        params = request_fields('company_basic', {parameter: 'ALL'})
        assert '__retrieved_at' not in params[parameter].split(',')
        assert 'SECUCODE' in params[parameter].split(',')


def test_lite_profile_narrows_plan_request_and_network_binding():
    profile=load_research_profile(LITE_PROFILE_ID)
    assert sum(map(len,profile['question_routing'].values()))==54
    assert len(selected_datasets(research_profile_id=LITE_PROFILE_ID)) < len(selected_datasets())
    assert 'market_cap' in selected_datasets(research_profile_id=LITE_PROFILE_ID)
    with pytest.raises(ValueError,match='research_profile_excluded'):
        selected_datasets(['block_trade'],LITE_PROFILE_ID)
    fields=request_fields('market_cap',{'sty':'ALL'},research_profile_id=LITE_PROFILE_ID)['sty'].split(',')
    assert {'CLOSE_PRICE','TOTAL_MARKET_CAP','PE_TTM','PB_MRQ','PS_TTM'} <= set(fields)
    assert {'CHANGE_RATE','OPEN_PRICE','VOLUME'} & set(fields) == set()
    frozen={'research_scope':load_scope(),'research_profile_id':LITE_PROFILE_ID,'research_profile':profile}
    assert_network_scope(SimpleNamespace(frozen_config=frozen),'market_cap')
    with pytest.raises(ValueError,match='research_profile_network_binding_invalid'):
        assert_network_scope(SimpleNamespace(frozen_config=frozen|{'research_profile':{}}),'market_cap')


@pytest.mark.parametrize('title,category,selected',[
    ('2025年年度报告','D01',True),
    ('2025年年度报告摘要','D04',False),
    ('2025年年度报告（英文版）','D21',False),
    ('2025年度审计报告','D21',False),
    ('2025年度内部控制自我评价报告','D17',False),
    ('关于募集资金使用情况的专项报告','D11',False),
    ('2026年第一季度报告','D03',False),
    ('2019年年度报告','D01',False),
])
def test_document_baseline_and_precise_exclusions(title,category,selected):
    result=select_research_document(entry(title),as_of=date(2026,9,13))
    assert (result['document_class'],result['selected'])==(category,selected)


def test_triggered_quarter_and_no_guessed_identity_or_future_publication():
    result=select_research_document(entry('2026年第一季度报告'),as_of=date(2026,9,13),question_ids=['ES02.Q01'],trigger_reason='statement_scope_missing')
    assert result['selected'] and result['question_ids']==['ES02.Q01']
    assert not select_research_document(entry('2025年年度报告',ticker=None),as_of=date(2026,9,13))['selected']
    assert not select_research_document(entry('2025年年度报告',published='2026-09-20'),as_of=date(2026,9,13))['selected']


def test_english_interim_is_not_permanently_excluded():
    result=select_research_document(entry('2026年半年度报告（英文版）',published='2026-08-30'),as_of=date(2026,9,13),question_ids=['ES01.Q01'],trigger_reason='explicit_language_evidence')
    assert result['document_class']=='D02' and result['selected']


def test_lite_document_baseline_is_latest_annual_and_interim_only():
    chosen=[]
    for title,published in (
        ('2024年年度报告','2025-04-20'),
        ('2025年年度报告','2026-04-20'),
        ('2025年半年度报告','2025-08-30'),
        ('2026年半年度报告','2026-08-30'),
    ):
        result=select_research_document(entry(title,published=published),as_of=date(2026,9,13),research_profile_id=LITE_PROFILE_ID)
        if result['selected']:chosen.append((title,result['document_class']))
    assert chosen==[('2025年年度报告','D01'),('2026年半年度报告','D02')]
