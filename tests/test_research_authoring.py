import asyncio
from copy import deepcopy
import inspect
import pytest

from test_research_workspace import workspace
from analysis.research.authoring import Authoring, prompt_document
from analysis.research.briefing import Briefing, aligned_peers
from analysis.research.drafts import Drafts
from analysis.research.tools import operations
from analysis.research.workspace import ResearchError


def test_public_schema_removes_chapter_rebuttal_and_hosts_share_prompt(workspace):
    ops = operations(workspace)
    assert set(inspect.signature(ops['save_section']).parameters) == {
        'research_id','snapshot_id','number','markdown','judgment','evidence_refs'}
    a = ops['get_research_prompt']()
    assert a == prompt_document()
    import hashlib
    assert a['prompt'] and a['sha256'] == hashlib.sha256(a['prompt'].encode()).hexdigest()
    review = ops['get_research_prompt'](stage='review')
    assert review == prompt_document('review')
    assert a['version'] == review['version'] == 'buy-side-v5'
    assert a['sha256'] != review['sha256']


def test_chapter_needs_no_rebuttal_and_global_risk_is_bounded(workspace):
    s=workspace.prepare_research('贵州茅台','2025-01-01')
    a=Authoring(workspace);rid=s['research_id'];sid=s['snapshot_id']
    a.save_section(rid,sid,1,'商业模式以现金销售为主。','现金销售形成经营优势',['F1'])
    assert Drafts(workspace).get_draft(rid)['sections'][0]['invalidation']==[]
    assert Drafts(workspace).get_draft(rid)['sections'][0]['contract_version']=='buy-side-v2'
    with pytest.raises(ResearchError,match='risk_summary_requires'):
        a.save_conclusion(rid,sid,'谨慎','观点',['理由'],'风'*301)
    with pytest.raises(ResearchError,match='defensive_boilerplate'):
        a.save_section(rid,sid,2,'本报告不作断言','观点',['F1'])
    with pytest.raises(ResearchError,match='single_global'):
        a.save_section(rid,sid,1,'### 反证\n另外一面','观点',['F1'])
    assert 'markdown' not in a.get_draft(rid)['sections'][0]
    assert a.get_draft(rid,1)['section']['markdown']


def test_unaligned_peers_excluded_without_mutating_input():
    metric=dict(metric_id='revenue',period='2025-12-31',period_type='cumulative',unit='CNY',currency='CNY',scope='consolidated',value='100')
    rows=[dict(ticker='A',metrics=[metric]),dict(ticker='B',metrics=[dict(metric,period_type='single_quarter')])]
    original=deepcopy(rows)
    assert aligned_peers(rows)[0]==[]
    assert rows==original
    rows[1]['metrics'][0]=dict(metric,value='200')
    assert len(aligned_peers(rows)[0])==2
    del rows[1]['metrics'][0]['scope']
    admitted, excluded=aligned_peers(rows)
    assert len(admitted)==1 and excluded[0]['ticker']=='B'


def test_brief_is_clean_without_rewriting_frozen_files(workspace):
    s=workspace.prepare_research('贵州茅台','2025-01-01')
    result=Briefing(workspace).get_research_brief(s['research_id'])
    assert result['categories'] and 'F1' not in str(result)
    assert result['peer_comparison']['status'] == 'pending'
    assert result['peer_comparison']['included_tickers'] == []
    assert 'exclusion_reasons' not in str(result) and 'audit_entry' not in result
    public = operations(workspace)['prepare_research']('贵州茅台','2025-01-01')
    assert public['content'] == result['content']
    assert '不自动评级' not in public['content'] and 'core_gaps' not in public


def test_typed_calculation_rejects_wrong_structure_before_arithmetic():
    from analysis.research.calculation_schema import PEAssumptions
    from pydantic import ValidationError
    with pytest.raises(ValidationError,match='valid list'):
        PEAssumptions.model_validate({'scenarios':{'value':{'base':{}},'reason':'scenario'},
            'sensitivity_growth':{'value':[0,.1],'reason':'growth'},
            'sensitivity_multiples':{'value':[15,20],'reason':'multiple'}})
    from test_research_calculations import FrozenInputs, assumptions
    from analysis.research.calculation_schema import public_calculator
    result=public_calculator(FrozenInputs())('r','pe_scenarios',{'earnings':'E','shares':'S','price':'P'},assumptions())
    from decimal import Decimal
    assert Decimal(result['result']['scenarios'][1]['fair_value']) == Decimal('165')
