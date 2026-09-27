import json
from pathlib import Path
import pytest
from analysis.research.api_materials import register,enrich
from analysis.research.workspace import sha,ResearchError

class Workspace:
    def pack(self,rid):return {'ticker':'600519','as_of':'2026-09-13'},None,{}
    def artifact(self,rid,kind,payload):return payload


def prepare(tmp_path):
    audit={'SECURITY_CODE':'600519','SECUCODE':'600519.SH','REPORT_DATE':'2025-12-31','NOTICE_DATE':'2026-04-17','OPINION_TYPE':'标准无保留意见'}
    records=[{'SECURITY_CODE':'600519','NOTICE_DATE':'2020-12-31','PUNISH_OBJECT':'A'},
             {'SECURITY_CODE':'600519','NOTICE_DATE':'2020-12-31','PUNISH_OBJECT':'A'},
             {'SECURITY_CODE':'600519','NOTICE_DATE':'2026-10-01','PUNISH_OBJECT':'B'}]
    summary={'company':'600519.SH','queried_at':'2026-09-15','results':[]}
    for name,body,ds in [('balance-fields-direct',{'data':[audit]},'balance_fields'),('violation-1-direct',{'result':{'data':records}},'violation')]:
        p=tmp_path/(name+'.body');p.write_text(json.dumps(body),encoding='utf8')
        p.with_name(name+'.request.json').write_text(json.dumps({'url':'https://example.com'}),encoding='utf8')
        summary['results'].append({'dataset':ds,'status':'success','proofs':[{'response_sha256':sha(p)}],'declared_total':len(records)})
    (tmp_path/'summary.json').write_text(json.dumps(summary),encoding='utf8')


def test_import_is_scoped_and_keeps_duplicate_observations(tmp_path):
    prepare(tmp_path);record=register(Workspace(),'r',tmp_path)
    assert len(record['items'])==2
    history=next(r for r in record['items'] if r['dataset_id']=='violation')
    assert len(history['rows'])==2 and history['period']=='2020-12-31'
    data={'items':[]};enrich(data,[record]);count=len(data['items']);enrich(data,[record]);assert len(data['items'])==count
    (tmp_path/'balance-fields-direct.body').write_text('{}')
    with pytest.raises(ResearchError,match='api_original_changed'):enrich(data,[record])


def test_wrong_company_and_hash_rejected(tmp_path):
    prepare(tmp_path)
    class Other(Workspace):
        def pack(self,rid):return {'ticker':'000858','as_of':'2026-09-13'},None,{}
    with pytest.raises(ResearchError,match='company_mismatch'):register(Other(),'r',tmp_path)
    (tmp_path/'violation-1-direct.body').write_text('{}')
    with pytest.raises(ResearchError,match='proof_mismatch'):register(Workspace(),'r',tmp_path)
