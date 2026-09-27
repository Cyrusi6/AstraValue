"""Explicit import of verified API probes as additive, research-scoped reading material."""
from pathlib import Path
from collections import defaultdict
from .workspace import ResearchError,read_json,sha,digest


def register(w,rid,run):
    state,_,_=w.pack(rid);run=Path(run)
    summary=read_json(run/'summary.json')
    if summary['company'].split('.')[0]!=state['ticker']:raise ResearchError('api_company_mismatch')
    items=[]
    for dataset,filename in [('audit_opinion','balance-fields-direct.body'),('violation','violation-1-direct.body')]:
        source=run/filename;body=read_json(source);source_hash=sha(source)
        proof=next(r for r in summary['results'] if r['dataset']==('balance_fields' if dataset=='audit_opinion' else dataset))
        hashes=[r['response_sha256'] for r in proof.get('proofs',[proof.get('proof',{})])]
        if proof['status']!='success' or source_hash not in hashes:raise ResearchError('api_source_proof_mismatch')
        rows=body['data'] if dataset=='audit_opinion' else body['result']['data']
        if dataset=='violation' and len(rows)!=proof['declared_total']:raise ResearchError('api_partial_history')
        by_period=defaultdict(list)
        for row in rows:
            if row.get('SECURITY_CODE')!=state['ticker']:raise ResearchError('api_company_mismatch')
            disclosed=str(row.get('NOTICE_DATE',''))[:10]
            if not disclosed or disclosed>state['as_of']:continue
            if dataset=='audit_opinion':
                period=row['REPORT_DATE'][:10]
                if period>state['as_of'] or not row.get('OPINION_TYPE'):continue
                value={k:row.get(k) for k in ('REPORT_DATE','NOTICE_DATE','OPINION_TYPE','SECUCODE')}
            else:
                period=disclosed;value=row
            by_period[period].append(value)
        request=read_json(source.with_name(filename.replace('.body','.request.json')))
        for period,values in sorted(by_period.items()):
            items.append({'dataset_id':dataset,'period':period,'state':'api_records_readable',
                'finding':'审计意见' if dataset=='audit_opinion' else '历史监管记录（未按事件去重）',
                'scope':'来源字段OPINION_TYPE；不认定与OSOPINION_TYPE同义' if dataset=='audit_opinion' else '接口返回的历史记录，记录条数不等于独立事件数，也不证明后来无事项',
                'rows':values,'path':str(source.resolve()),'sha256':source_hash,'source_url':request['url'],
                'retrieved_at':summary['queried_at']})
    return w.artifact(rid,'catalog_api_materials',{'items':items,'registration_version':'api-materials-v1'})


def enrich(data,records):
    items=data['items'];known={r['material_id'] for r in items}
    if not records:return data
    parent='material-'+digest(['audit_opinion_catalog'])[:24]
    if parent not in known and any(x['dataset_id']=='audit_opinion' for r in records for x in r['items']):
        items.append({'material_id':parent,'title':'审计意见','category':'governance','reader':'missing',
            'status':'missing','status_label':'尚未取得','period':[],'purpose':'按年度读取已披露审计意见',
            'payload':{'dataset_id':'audit_opinion'}});known.add(parent)
    parents={r['payload']['dataset_id']:r['material_id'] for r in items if r['reader']=='missing' and r['payload'].get('dataset_id')}
    for record in records:
        for entry in record['items']:
            if sha(Path(entry['path']))!=entry['sha256']:raise ResearchError('api_original_changed')
            ident='material-'+digest(entry)[:24]
            if ident in known:continue
            known.add(ident)
            items.append({'material_id':ident,'category':'governance','title':entry['finding'],
                'parent_id':parents.get(entry['dataset_id']),'period':entry['period'],'reader':'api_record',
                'status':'readable','status_label':'已有可读','purpose':'已登记API原始记录','payload':entry})
    return data
