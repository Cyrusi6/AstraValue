import json
from copy import deepcopy
import pytest
from test_research_workspace import workspace
from analysis.research.sw_industry import register, LABEL, enrich
from analysis.research.availability import reconcile,resolved_view
from analysis.research.workspace import sha,ResearchError
from analysis.research.catalog import Catalog


def evidence(tmp_path,ticker='600519',name='贵州茅台'):
    directory=tmp_path/'dir.html';directory.write_text('''<div id="level2Items"><a href="/stockdata/sw-industry-2021?industryCode=801125.SI"><div class="lg-industries-item-chinese-title">801125.SI</div><div class="lg-industries-item-number">白酒Ⅱ(1)<span class="parent-industry-name">[食品饮料]</span></div></a></div>''',encoding='utf8')
    members=tmp_path/'members.json';members.write_text(json.dumps({'code':'200','data':{'count':1,'next':None,'results':[{'stockcode':ticker,'stockname':name}]}}),encoding='utf8')
    data={'ticker':ticker,'company':name,'level':2,'classification':'申万2021版','industry_name':'白酒Ⅱ','parent_industry':'食品饮料','industry_index_code':'801125','display_index_code':'801125.SI',
        'observed_at':'2026-09-16T09:00:00+08:00','temporal_scope':'current constituents only; no historical membership reconstruction',
        'sources':[{'path':str(directory),'sha256':sha(directory),'url':'https://legulegu.com/stockdata/sw-industry-overview','tls_verified':True},
                   {'path':str(members),'sha256':sha(members),'url':'https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/?swindexcode=801125','tls_verified':False}]}
    p=tmp_path/'result.json';p.write_text(json.dumps(data),encoding='utf8');return p,data


def test_register_catalog_read_and_temporal_scope(workspace,tmp_path):
    p,data=evidence(tmp_path);rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    first=register(workspace,rid,p);second=register(workspace,rid,p)
    assert first['artifact_id']==second['artifact_id']
    api=Catalog(workspace)
    item=next(x for x in api.list_materials(rid,'industry')['items'] if x['title']=='申万二级行业替代分类')
    result=json.loads(api.read_material(rid,item['material_id'])['content'])
    assert result['industry_index_code']=='801125' and result['original_provider_value'] is None
    assert result['after_research_cutoff'] and result['effective_date'] is None
    assert result['sources'][1]['tls_verified'] is False
    with pytest.raises(ResearchError,match='period_not_available'):api.read_material(rid,item['material_id'],period='2025-01-01')
    chunks=[];page=1
    while page:
        r=api.read_material(rid,item['material_id'],page=page,max_tokens=40);chunks.append(r['content']);page=r['next_page']
    assert json.loads(''.join(chunks))==result
    other=workspace.prepare_research('000858','2025-01-01')['research_id']
    with pytest.raises(ResearchError,match='current_catalog'):api.read_material(other,item['material_id'])
    assert not workspace.artifacts(other,'sw_industry_classification')
    data['industry_name']='changed';p.write_text(json.dumps(data),encoding='utf8')
    with pytest.raises(ResearchError,match='result_changed'):api.read_material(rid,item['material_id'])


def test_field_status_and_raw_nulls_preserved(workspace,tmp_path):
    p,_=evidence(tmp_path);rid=workspace.prepare_research('600519','2025-01-01')['research_id'];record=register(workspace,rid,p)
    state,_,pack=workspace.pack(rid)
    original={'items':[{'material_id':field,'reader':'field','title':field,'status':'processing','status_label':'已有待处理',
        'period':['2024-12-31'],'payload':{'field':field}} for field in ['SWINDUSTRY_CODE2','SWINDUSTRY_NAME2']]}
    d=enrich(deepcopy(original),[record],state);d=reconcile(d,pack)
    for r in d['items'][:2]:
        assert r['status_label']==LABEL and r['status']=='readable'
        v=resolved_view(r);assert v['status_label']==LABEL and v['materials'][0]['original_provider_value'] is None
        assert resolved_view(r,'2024-12-31')['status']=='processing'
    assert original['items'][0]['status']=='processing'
    assert reconcile(enrich(deepcopy(d),[record],state),pack)==d


@pytest.mark.parametrize('bad',['company','name','code','level','sources','count','tamper'])
def test_reject_bad_evidence(workspace,tmp_path,bad):
    p,data=evidence(tmp_path);rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    if bad=='company':data['ticker']='000858'
    if bad=='name':data['industry_name']='wrong'
    if bad=='code':data['industry_index_code']='801000'
    if bad=='level':data['level']=3
    if bad=='sources':data['sources']=[]
    if bad in {'count','tamper'}:
        from pathlib import Path
        m=Path(data['sources'][1]['path']);body=json.loads(m.read_text());body['data']['count']=2;m.write_text(json.dumps(body))
        if bad=='count':data['sources'][1]['sha256']=sha(m)
    p.write_text(json.dumps(data),encoding='utf8')
    with pytest.raises(ResearchError):register(workspace,rid,p)
