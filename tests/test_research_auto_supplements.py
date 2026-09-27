import json
from pathlib import Path
from collections import Counter
from types import SimpleNamespace
import pytest
from test_research_workspace import workspace
from test_research_sw_industry import evidence
from analysis.research.workspace import sha,digest,ResearchError,read_json
from analysis.research.supplement_transport import dump,Checkpoint
from analysis.research.jobs import MaterialJobs
from analysis.research.tools import operations
from analysis.research.catalog import Catalog
from analysis.research.material_status import LABELS,present,period_statuses
from analysis.research.api_discovery import register_pages
from analysis.structured.protocols import EastmoneyRequest,ProtocolFamily


def api_page(tmp_path,rows,pages=1,total=None):
    spec=next(x['request'] for x in read_json(Path(__file__).resolve().parents[1]/'config/structured_data/datasets.v1.json')['datasets'] if x['dataset_id']=='violation')
    request=EastmoneyRequest(ProtocolFamily[spec['protocol'].upper()],spec['endpoint'],
        {**spec['fixed_parameters'],'pageNumber':'1','pageSize':'100','filter':'(SECURITY_CODE="600519")'})
    path=tmp_path/'api.json'
    dump(path,{'code':0,'success':True,'result':{'count':len(rows) if total is None else total,'pages':pages,'data':rows}})
    return request,{'path':str(path),'sha256':sha(path),'http_status':200,'content_type':'application/json',
                    'url':request.endpoint,'observed_at':'2025-01-02T00:00:00+00:00'}


@pytest.mark.parametrize('bad,expected',[('company','company_mismatch'),('partial','partial_history'),('hash','source_changed')])
def test_registration_rejects_wrong_company_partial_or_tampered(workspace,tmp_path,bad,expected):
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    rows=[{'SECURITY_CODE':'000858' if bad=='company' else '600519','NOTICE_DATE':'2024-12-31'}]
    pair=api_page(tmp_path,rows,pages=2 if bad=='partial' else 1,total=2 if bad=='partial' else 1)
    if bad=='hash':Path(pair[1]['path']).write_text('{}')
    with pytest.raises(ResearchError,match=expected):register_pages(workspace,rid,'regulatory_records',[pair])
    assert not workspace.artifacts(rid,'catalog_api_materials')


def test_registration_filters_unknown_invalid_and_future_disclosure_dates(workspace,tmp_path):
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    rows=[{'SECURITY_CODE':'600519','NOTICE_DATE':d} for d in ['2024-12-31',None,'','2024-99-01','2025-01-02']]
    result=register_pages(workspace,rid,'regulatory_records',[api_page(tmp_path,rows)])
    assert result['records']==5 and result['admitted_records']==1


def test_public_list_read_and_route_content_agree_without_mutating_states(workspace,monkeypatch):
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];api=Catalog(workspace)
    item={'material_id':'test-route','title':'担保','category':'governance','purpose':'风险','reader':'missing',
          'payload':{},'period':['2024-12-31'],'status':'readable','status_label':'已有可读',
          'availability':{'source_status':'missing','available_periods':['2024-12-31'],'routes':[
              {'period':'2024-12-31','state':'issuer_disclosed_none_or_not_applicable','scope':'重大担保'}]}}
    data={'items':[item]};monkeypatch.setattr(api,'_load',lambda rid:(workspace.task(rid)[0],data))
    listed=api.list_materials(rid,'governance')['items'][0]
    result=api.read_material(rid,'test-route');body=json.loads(result['content'])
    assert listed['status']==result['status']==body['status']==body['materials'][0]['state']=='verified_none'
    assert item['availability']['routes'][0]['state']=='issuer_disclosed_none_or_not_applicable'
    assert api.read_material(rid,'test-route',period='2023-12-31')['status']=='unavailable'


def test_sw_business_failure_invalidates_cache_and_can_retry(workspace,tmp_path):
    from analysis.research.sw_discovery import discover
    _,data=evidence(tmp_path,'000858','五粮液');calls=[]
    member=data['sources'][1];path=Path(member['path']);valid=path.read_bytes()
    dump(path,{'code':'500','data':None});member['sha256']=sha(path)
    class Fake:
        def fetch(self,url,params=None):
            return {**(member if params else data['sources'][0]),'observed_at':data['observed_at']}
        def invalidate(self,url,params=None):calls.append((url,params))
    rid=workspace.prepare_research('000858','2025-01-01')['research_id'];folder=tmp_path/'scan';folder.mkdir()
    with pytest.raises(ResearchError,match='response_incomplete'):discover(workspace,rid,folder,Fake())
    assert len(calls)==1 and not workspace.artifacts(rid,'sw_industry_classification')
    path.write_bytes(valid);member['sha256']=sha(path)
    assert discover(workspace,rid,folder,Fake())['ticker']=='000858'


def configured(workspace,monkeypatch):
    workspace.config.update(max_attempts=3,max_tool_seconds=120)
    path=workspace.root/'config/structured_data/datasets.v1.json';path.parent.mkdir(parents=True)
    path.write_bytes((Path(__file__).resolve().parents[1]/'config/structured_data/datasets.v1.json').read_bytes())
    original=workspace.pack
    def pack(rid):
        s,p,data=original(rid);return s,p,{**data,'periods':{'annual':['2024-12-31']}}
    monkeypatch.setattr(workspace,'pack',pack)


def fake_transport(monkeypatch,tmp_path,scenario='success'):
    calls=[]
    class Fake:
        def __init__(self,root,seconds=70):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.network_requests=0;self.cache_hits=0
        def invalidate(self,url,params=None):pass
        def fetch(self,url,params=None):
            calls.append((url,dict(params or {})));self.network_requests+=1
            typ='application/json';page=int((params or {}).get('pageNumber',1))
            if url.endswith('/Index'):
                body=b'<input id="hidctype" value="4">';typ='text/html'
            elif url.endswith('zcfzbAjaxNew'):
                body=json.dumps({'pages':1,'count':1,'data':[{'SECURITY_CODE':'600519','SECUCODE':'600519.SH','REPORT_DATE':'2024-12-31','NOTICE_DATE':'2024-12-31','OPINION_TYPE':'标准无保留意见'}]}).encode()
            elif scenario=='empty':body=b'{"code":9201,"success":false,"result":null}'
            elif scenario=='error':body=b'{"code":500,"success":false,"result":null}'
            else:
                body=json.dumps({'code':0,'success':True,'result':{'count':2,'pages':2,'data':[{'SECURITY_CODE':'600519','NOTICE_DATE':'2024-12-31','PUNISH_OBJECT':str(page)}]}}).encode()
                if scenario=='checkpoint' and page==2 and sum(1 for u,p in calls if p.get('pageNumber')=='2')==1:raise Checkpoint()
            path=self.root/(digest([url,params])+'.body');path.write_bytes(body)
            return {'path':str(path),'sha256':sha(path),'url':url,'http_status':200,'content_type':typ,'tls_verified':True,'observed_at':'2025-01-02T00:00:00+00:00'}
    monkeypatch.setattr('analysis.research.supplements.Transport',Fake);return calls


def test_api_request_fetch_register_then_directory_without_manual_registration(workspace,monkeypatch,tmp_path):
    configured(workspace,monkeypatch);calls=fake_transport(monkeypatch,tmp_path)
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];jobs=MaterialJobs(workspace)
    job=jobs.request_materials(rid,'审计及监管','判断依据',material_types=['audit_opinion','regulatory_records'],execute=False)
    before=sha(Path(workspace.task(rid)[0]['pack_path'])/'manifest.json')
    jobs.execute_round(job['task_id']);done=jobs.get(job['task_id']);assert done['state']=='completed'
    assert done['result']['materials']['audit_opinion']['admitted_records']==1
    assert done['result']['materials']['regulatory_records']['admitted_records']==2
    api=Catalog(workspace);_,data=api._load(rid)
    row=next(r for r in data['items'] if r['reader']=='api_record' and r['payload']['dataset_id']=='audit_opinion')
    assert api.read_material(rid,row['material_id'])['status']=='original_readable'
    assert sha(Path(workspace.task(rid)[0]['pack_path'])/'manifest.json')==before
    count=len(calls);again=jobs.request_materials(rid,'换句话问','同一影响',material_types=['regulatory_records','audit_opinion'])
    assert again['state']=='completed' and len(calls)==count


def test_checkpoint_keeps_first_page_and_resumes_same_job(workspace,monkeypatch,tmp_path):
    configured(workspace,monkeypatch);calls=fake_transport(monkeypatch,tmp_path,'checkpoint')
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];jobs=MaterialJobs(workspace)
    job=jobs.request_materials(rid,'监管','风险',material_types=['regulatory_records'],execute=False)
    jobs.execute_round(job['task_id']);assert jobs.get(job['task_id'])['state']=='checkpointed'
    assert not workspace.artifacts(rid,'catalog_api_materials')
    jobs.execute_round(job['task_id']);assert jobs.get(job['task_id'])['state']=='completed'
    assert Counter(p.get('pageNumber') for u,p in calls)=={'1':1,'2':2}


@pytest.mark.parametrize('scenario,state,source',[('empty','completed','empty'),('error','failed',None)])
def test_empty_differs_from_failure_and_never_verified_none(workspace,monkeypatch,tmp_path,scenario,state,source):
    configured(workspace,monkeypatch);fake_transport(monkeypatch,tmp_path,scenario)
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];jobs=MaterialJobs(workspace)
    job=jobs.request_materials(rid,'担保','风险',material_types=['guarantee'],execute=False);jobs.execute_round(job['task_id'])
    got=jobs.get(job['task_id']);assert got['state']==state
    if source:
        assert got['result']['materials']['guarantee']['status']=='unavailable'
        assert got['result']['materials']['guarantee']['source_state']==source
    else:assert not workspace.artifacts(rid,'catalog_api_materials')


def test_prepare_queues_sw_and_offline_does_not(workspace,monkeypatch):
    calls=[]
    monkeypatch.setattr(MaterialJobs,'resume_task',lambda self,ident:calls.append(ident) or {'task_id':ident,'state':'running'})
    ops=operations(workspace);result=ops['prepare_research']('000858','2025-01-01')
    assert result['supplement_preparation']['task_id']==calls[0]
    job=MaterialJobs(workspace).get(calls[0]);assert job['material_types']==['sw_industry']
    ops['prepare_research']('600519','2025-01-01',offline=True);assert len(calls)==1


def test_supplement_failure_budget_and_offline_requests_do_not_start_worker(workspace,monkeypatch):
    rid=workspace.prepare_research('600519','2025-01-01')['research_id'];jobs=MaterialJobs(workspace)
    job=jobs.request_materials(rid,'监管事项','风险',material_types=['regulatory_records'],execute=False)
    job.update(state='failed',failures=3);jobs.save(job)
    result=jobs.request_materials(rid,'换个问题说法','估值',material_types=['regulatory_records'])
    assert result['state']=='exhausted' and result['task_id']==job['task_id']
    monkeypatch.setattr(MaterialJobs,'resume_task',lambda *args:pytest.fail('offline must not start a worker'))
    workspace.config['offline']=True
    assert jobs.request_materials(rid,'审计','财务',material_types=['audit_opinion'])['status']=='offline'


def test_sw_discovery_uses_company_membership_not_ticker_mapping(workspace,monkeypatch,tmp_path):
    from analysis.research.sw_discovery import discover
    p,data=evidence(tmp_path,'000858','五粮液')
    source_by_url={s['url'].split('?')[0]:s for s in data['sources']}
    class Fake:
        def fetch(self,url,params=None):
            s=source_by_url[url];return {**s,'observed_at':data['observed_at']}
    rid=workspace.prepare_research('000858','2025-01-01')['research_id']
    folder=tmp_path/'discovery';folder.mkdir()
    result=discover(workspace,rid,folder,Fake());assert result['ticker']=='000858' and result['industry_name']=='白酒Ⅱ'
    assert operations(workspace)['get_research_brief'](rid)['categories']
    item=operations(workspace)['list_materials'](rid,'industry')['items'][0]
    assert item['status']=='alternative'


def test_six_states_and_mixed_periods_keep_blank_and_unknown():
    base={'status':'readable','period':['2024-12-31'],'reader':'field','payload':{}}
    assert present(base)['status']=='original_readable'
    assert present({**base,'reader':'metrics','payload':[{'state':'ready','fact':{'value':'0'},'period':'2024-12-31'}]})['status']=='formal_numeric'
    assert present({**base,'status':'missing'})['status']=='unavailable'
    assert present({**base,'status':'processing'})['status']=='processing'
    assert present({**base,'reader':'sw_classification'})['status']=='alternative'
    item={**base,'period':['2024-12-31','2025-06-30'],'availability':{'source_status':'missing','routes':[
        {'period':'2024-12-31','state':'issuer_disclosed_none_or_not_applicable'},
        {'period':'2025-06-30','state':'original_readable'}]}}
    assert present(item)['status']=='original_readable'
    assert present(item,'2024-12-31')['status']=='verified_none'
    assert present(item,'2023-12-31')['status']=='unavailable'
    assert len(period_statuses(item))==2
    item['availability']['routes'][0]['state']='disclosed_blank'
    assert present(item,'2024-12-31')['status']=='original_readable'


def test_resolved_blank_metrics_are_readable_and_numeric_periods_stay_formal():
    item={'reader':'metrics','status':'readable','period':['2023-12-31','2024-12-31'],
          'payload':[{'period':'2023-12-31','state':'disclosed_blank','fact':None,
                      'resolution':{'evidence_id':'original-statement'}},
                     {'period':'2024-12-31','state':'ready','fact':{'value':'1'}}]}
    assert present(item)['status']=='original_readable'
    assert present(item,'2023-12-31')['status']=='original_readable'
    assert present(item,'2024-12-31')['status']=='formal_numeric'
    assert len(period_statuses(item))==2
    item['payload'][0].pop('resolution')
    assert present(item,'2023-12-31')['status']=='processing'
