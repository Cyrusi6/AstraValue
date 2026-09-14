from copy import deepcopy
from datetime import date
from decimal import Decimal
import json

import pytest

from analysis.research.processing import extract_evidence, organize_disclosures, audit_metrics
from analysis.research.workspace import ResearchError, sha
from analysis.research.calculations import Calculations
from analysis.structured.scope import load_research_profile, LITE_PROFILE_ID, STANDARD_PROFILE_ID, scope_start
from analysis.structured.research_lite import lite_periods


def test_standard_window_does_not_change_old_profile():
    old = deepcopy(load_research_profile(LITE_PROFILE_ID))
    standard = load_research_profile(STANDARD_PROFILE_ID)
    p=lite_periods(date(2026,9,13),standard)
    assert p['annual']==[f'{y}-12-31' for y in range(2021,2026)]
    assert len(p['quarters'])==12 and p['quarters'][0]=='2023-09-30'
    assert scope_start(date(2026,9,13),STANDARD_PROFILE_ID)==date(2020,1,1)
    assert scope_start(date(2026,2,1),STANDARD_PROFILE_ID)==date(2019,1,1)
    assert old == load_research_profile(LITE_PROFILE_ID)


def test_original_answer_selection_and_integrity(tmp_path):
    p=tmp_path/'source.json';p.write_text(json.dumps({'question':'库存一亿瓶？','answer':'存销比低于1'}),encoding='utf8')
    spec=dict(path=str(p),sha256=sha(p),source_url='https://example.com',ticker='600519',published_at='2026-08-21',title='答复',boundary='公司陈述',group='A',json_path=['answer'])
    row=extract_evidence(spec,'600519','2026-09-13')
    assert row['content']=='存销比低于1' and row['numeric_admission'] is False
    with pytest.raises(ResearchError,match='after_cutoff'):extract_evidence(spec,'600519','2026-08-01')
    with pytest.raises(ResearchError,match='identity'):extract_evidence(spec,'000858','2026-09-13')
    p.write_text('{}')
    with pytest.raises(ResearchError,match='hash'):extract_evidence(spec,'600519','2026-09-13')


def test_concentration_units_and_inventory_reconcile_fail_closed():
    row=dict(evidence_id='e',period='2026-06-30',content='前五名客户销售额1,234.50万元，占年度销售总额10.10%；存货分类 单位：元 账面价值\n原材料\n10.00 10.00 10.00 10.00\n在产品\n22.00 2.00 20.00 22.00 2.00 20.00\n库存商品\n30.00 30.00 30.00 30.00\n自制半成品\n40.00 40.00 40.00 40.00\n合计\n102.00 2.00 100.00 102.00 2.00 100.00\n(2)其他')
    result=organize_disclosures([row])
    assert result[0]['amount_cny']=='12345000.00' and result[0]['share']=='0.101'
    assert result[1]['production_and_semifinished_share']=='0.6'
    row['content']=row['content'].replace('102.00 2.00 100.00','105.00 2.00 100.00')
    assert len(organize_disclosures([row]))==1


def test_public_company_answer_excludes_question_and_platform_notice(tmp_path):
    p=tmp_path/'api.json'
    record=dict(companyId=519,questionType=2,content='公司答复',questionContent='投资者猜测',crtTime='2026-08-21 15:00:00',isAnswerPassed=False)
    p.write_text(json.dumps({'record':record}),encoding='utf8')
    spec=dict(path=str(p),sha256=sha(p),source_url='https://example.com',ticker='600519',published_at='2026-08-21',title='公司问答',boundary='公司陈述',group='A',json_path=['record','content'],source_role='company_statement_on_exchange_public_platform')
    assert extract_evidence(spec,'600519','2026-09-13')['content']=='公司答复'
    spec['json_path'][-1]='questionContent'
    with pytest.raises(ResearchError,match='answer_record'):extract_evidence(spec,'600519','2026-09-13')
    spec['json_path'][-1]='content';record['questionType']=4
    p.write_text(json.dumps({'record':record}),encoding='utf8');spec['sha256']=sha(p)
    with pytest.raises(ResearchError,match='ineligible'):extract_evidence(spec,'600519','2026-09-13')


def test_financial_summary_uses_total_equity_and_actual_span():
    rows=[]
    for year in range(2021,2026):
        for metric,value,kind in [('net_profit',20,'cumulative'),('operating_income',100*(1.1**(year-2021)),'cumulative'),('parent_net_profit',15,'cumulative'),('total_assets',200,'instant'),('total_liabilities',100,'instant')]:
            rows.append(dict(state='ready',fact_ref=f'{metric}{year}',metric_id=metric,period=f'{year}-12-31',period_type=kind,
              fact=dict(fact_id=f'{metric}{year}',value=str(value),unit='CNY',scope='consolidated',currency='CNY')))
    class W:
        def pack(self,r):return {},None,dict(metrics=rows,periods=dict(annual=[f'{y}-12-31' for y in range(2021,2026)]))
        def artifact(self,r,k,p):return p
    result=Calculations(W()).calculate('r','financial_summary',{})['result']
    dup=[x for x in result['rows'] if x['metric']=='consolidated_dupont']
    assert len(dup)==4 and all(Decimal(x['roe'])==Decimal('.2') for x in dup)
    assert result['gaps'][0]['period']=='2021-12-31'
    cagr=result['rows'][0]
    assert cagr['years']==4 and abs(Decimal(cagr['value'])-Decimal('.1'))<Decimal('1e-14')
    rows[-1]['fact']['scope']='parent'
    with pytest.raises(ResearchError,match='scope_or_unit'):Calculations(W()).calculate('r','financial_summary',{})


def test_audit_rejects_wrong_yoy_and_conflicting_keys():
    row=dict(metric_id='operating_income',period='2025-12-31',period_type='cumulative',state='ready',required=True,
      fact=dict(value='120',source_ids=['s'],unit='CNY'),yoy=dict(prior_value='100',value='.3'))
    pack=dict(metrics=[row],periods=dict(annual=[f'{y}-12-31' for y in range(2021,2026)],quarters=['2026-06-30']*12))
    assert audit_metrics(pack)['status']=='failed'
    row['yoy']['value']='.2';assert audit_metrics(pack)['status']=='passed'
    other=deepcopy(row);other['fact']['value']='130';pack['metrics'].append(other)
    assert any('conflicting_metric' in e for e in audit_metrics(pack)['errors'])
