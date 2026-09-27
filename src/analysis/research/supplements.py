"""Research-level supplement orchestration shared by preparation and material requests."""
from pathlib import Path
from datetime import datetime
from .workspace import ResearchError,sha
from .supplement_transport import Transport,Checkpoint
from .api_discovery import API_TYPES,fetch_register
from .sw_discovery import discover

MATERIAL_TYPES=('sw_industry',*API_TYPES)


def existing(w,rid,kind):
    if kind=='sw_industry':
        from .sw_industry import verify
        records=w.artifacts(rid,'sw_industry_classification')
        if not records:return None
        record=max(records,key=lambda r:datetime.fromisoformat(r['observed_at']));verify(record)
        return {'status':'alternative','artifact_id':record['artifact_id'],'reused':True}
    dataset='audit_opinion' if kind=='audit_opinion' else API_TYPES[kind]
    records=w.artifacts(rid,'catalog_api_materials')
    for record in reversed(records):
        items=[x for x in record['items'] if x['dataset_id']==dataset]
        if not items and record.get('material_type')!=kind:continue
        for source in items+record.get('sources',[]):
            if sha(Path(source['path']))!=source['sha256']:raise ResearchError('api_original_changed')
        return {'status':'original_readable' if items else 'unavailable','artifact_id':record['artifact_id'],
            'source_state':record.get('source_state'),'reason':None if items else '已查询但无可准入记录','reused':True}
    return None


def execute(jobs,job):
    w=jobs.w;state,_,_=w.pack(job['research_id'])
    if state['snapshot_id']!=job['snapshot_id']:raise ResearchError('material_task_snapshot_no_longer_active')
    folder=w.state/'jobs'/job['task_id'];folder.mkdir(parents=True,exist_ok=True)
    # Company/task-specific caches prevent accidental cross-company source reuse.
    transport=Transport(folder/'http',seconds=max(5,min(70,int(w.config.get('max_tool_seconds',120))-10)))
    job.setdefault('supplement_results',{})
    try:
        for kind in job['material_types']:
            if kind in job['supplement_results']:continue
            value=None if job.get('refresh') else existing(w,job['research_id'],kind)
            if value is None:
                value=discover(w,job['research_id'],folder,transport) if kind=='sw_industry' else fetch_register(w,job['research_id'],kind,folder,transport)
                if kind=='sw_industry' and value.get('artifact_id'):value={'status':'alternative','artifact_id':value['artifact_id']}
            job['supplement_results'][kind]=value
            jobs.save(job)
        job.update(state='completed',result={'materials':job['supplement_results'],
            'read_entry':{'tool':'list_materials','category':'industry' if job['material_types']==['sw_industry'] else 'governance'},
            'meaning':'资料查询处理已结束；不表示全部资料已取得或研究已完成'},reason=None,failures=0)
    except Checkpoint:
        job.update(state='checkpointed',reason='已保存处理进度；resume_task继续，不重复已完成请求',failures=0)
    except (ResearchError,OSError,ValueError,KeyError) as exc:
        job.update(state='failed',reason=str(exc),failures=job.get('failures',0)+1)
    job['network_requests']=job.get('network_requests',0)+transport.network_requests
    job['cache_hits']=job.get('cache_hits',0)+transport.cache_hits
    jobs.save(job)
