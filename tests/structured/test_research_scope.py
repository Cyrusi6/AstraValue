from datetime import date
from types import SimpleNamespace

import pytest

from analysis.structured.scope import load_scope, selected_datasets, request_fields, assert_network_scope
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
