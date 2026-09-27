"""Fetch and register API reading material with explicit source/period validation."""
from dataclasses import asdict
from pathlib import Path
from collections import defaultdict
from datetime import date
from analysis.structured.protocols import EastmoneyRequest,ProtocolFamily,parse_eastmoney_response,parse_em_f_company_type_response
from .workspace import read_json,ResearchError,sha
from .supplement_transport import dump

API_TYPES={'audit_opinion':'balance_fields','regulatory_records':'violation','customers_peer':'customers_peer',
    'guarantee':'guarantee','litigation':'litigation','seo':'seo','allotment':'allotment',
    'bond_issuance':'bond_issuance','pledge':'pledge','unlock_peer':'unlock_peer'}


def register_pages(w,rid,kind,request_pages):
    state,_,_=w.pack(rid);rows=[];proofs=[];total=None
    for number,(request,source) in enumerate(request_pages,1):
        if sha(Path(source['path']))!=source['sha256']:raise ResearchError('api_source_changed')
        parsed=parse_eastmoney_response(request,status_code=source['http_status'],body=Path(source['path']).read_bytes(),content_type=source['content_type'])
        if parsed.status.value not in {'success','empty'}:raise ResearchError('api_protocol_failed:'+str(parsed.proof.diagnostic))
        if parsed.page_number!=number:raise ResearchError('api_page_sequence_mismatch')
        if number<len(request_pages) and parsed.terminal:raise ResearchError('api_unexpected_terminal_page')
        if total is None:total=parsed.declared_total
        elif total!=parsed.declared_total:raise ResearchError('api_total_changed')
        for index,row in enumerate(parsed.rows):rows.append((row,source,index))
        proofs.append(asdict(parsed.proof))
    if not request_pages or not parsed.terminal or total is not None and len(rows)!=total:
        raise ResearchError('api_partial_history')
    items=[];grouped=defaultdict(list)
    for row,source,index in rows:
        code=row.get('SECURITY_CODE') or str(row.get('SECUCODE','')).split('.')[0]
        if code!=state['ticker']:raise ResearchError('api_company_mismatch')
        published=str(row.get('NOTICE_DATE') or '')[:10]
        # Unknown release date cannot be admitted to a historical report.
        try:date.fromisoformat(published)
        except ValueError:continue
        if published>state['as_of']:continue
        period=str(row.get('REPORT_DATE') if kind=='audit_opinion' else published)[:10]
        try:date.fromisoformat(period)
        except ValueError:continue
        if period>state['as_of']:continue
        if kind=='audit_opinion' and not row.get('OPINION_TYPE'):continue
        value={k:row.get(k) for k in ('REPORT_DATE','NOTICE_DATE','OPINION_TYPE','SECUCODE')} if kind=='audit_opinion' else row
        grouped[(period,source['path'])].append((value,source,index))
    dataset='audit_opinion' if kind=='audit_opinion' else API_TYPES[kind]
    for (period,_),values in sorted(grouped.items()):
        source=values[0][1]
        items.append({'dataset_id':dataset,'period':period,'state':'api_records_readable',
            'finding':'审计意见' if kind=='audit_opinion' else dataset+' 已返回记录',
            'scope':'OPINION_TYPE明确披露值；不映射OSOPINION_TYPE编码' if kind=='audit_opinion' else '仅接口返回记录，未将重复记录计为独立事件，不证明其他期间无事项',
            'rows':[v[0] for v in values],'row_indices':[v[2] for v in values],
            'path':source['path'],'sha256':source['sha256'],'source_url':source['url'],'retrieved_at':source['observed_at']})
    artifact=w.artifact(rid,'catalog_api_materials',{'items':items,'registration_version':'api-materials-auto-v1',
        'material_type':kind,'source_records':len(rows),'admitted_records':sum(len(x['rows']) for x in items),
        'source_state':'empty' if not rows else 'records_returned','protocol_proofs':proofs,
        'sources':[s for _,s in request_pages]})
    return {'status':'original_readable' if items else 'unavailable','artifact_id':artifact['artifact_id'],
        'source_state':'empty' if not rows else 'records_returned','records':len(rows),
        'admitted_records':sum(len(x['rows']) for x in items),
        'reason':None if items else '接口无记录' if not rows else '返回记录未满足截止日或披露字段要求'}


def fetch_register(w,rid,kind,folder,transport):
    state,_,pack=w.pack(rid);folder=Path(folder)
    exchange=state['canonical_ticker'].split('.')[-1]
    if exchange not in {'SH','SZ','BJ'}:raise ResearchError('api_exchange_unknown')
    dataset=API_TYPES[kind]
    spec=next(r['request'] for r in read_json(w.root/'config/structured_data/datasets.v1.json')['datasets'] if r['dataset_id']==dataset)
    params=dict(spec['fixed_parameters']);protocol=ProtocolFamily[spec['protocol'].upper()]
    if kind=='audit_opinion':
        prefix='https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/'
        request=EastmoneyRequest(ProtocolFamily.EM_F,prefix+'Index',{'type':'web','code':exchange+state['ticker']})
        source=transport.fetch(request.endpoint,request.params)
        proof=parse_em_f_company_type_response(request,status_code=source['http_status'],body=Path(source['path']).read_bytes(),content_type=source['content_type'])
        if not proof.company_type:
            transport.invalidate(request.endpoint,request.params)
            raise ResearchError('company_type_unresolved')
        periods=[p for p in pack['periods']['annual'] if p<=state['as_of']]
        if not periods:raise ResearchError('audit_report_periods_missing')
        params.update(companyType=proof.company_type,dates=','.join(periods),code=exchange+state['ticker'])
    pages=[]
    checkpoint=folder/(kind+'-pages.json');cached=read_json(checkpoint) if checkpoint.exists() else []
    for stored in cached:
        request=EastmoneyRequest(protocol,spec['endpoint'],stored['params']);pages.append((request,stored['source']))
    start=len(pages)+1
    if pages:
        request,source=pages[-1]
        last=parse_eastmoney_response(request,status_code=source['http_status'],body=Path(source['path']).read_bytes(),content_type=source['content_type'])
        if last.terminal:return register_pages(w,rid,kind,pages)
    for page in range(start,201):
        if kind!='audit_opinion':
            field=spec['company_filter_field'];company=state['canonical_ticker'] if field=='SECUCODE' else state['ticker']
            params.update(pageNumber=str(page),pageSize='100',filter=f'({field}="{company}")')
        request=EastmoneyRequest(protocol,spec['endpoint'],dict(params));source=transport.fetch(request.endpoint,request.params)
        parsed=parse_eastmoney_response(request,status_code=source['http_status'],body=Path(source['path']).read_bytes(),content_type=source['content_type'])
        if parsed.status.value not in {'success','empty'}:
            # Invalid response is retained but not cached as successful evidence for retries.
            transport.invalidate(request.endpoint,request.params)
            raise ResearchError('api_protocol_failed:'+str(parsed.proof.diagnostic))
        pages.append((request,source));cached.append({'params':dict(params),'source':source});dump(checkpoint,cached)
        if parsed.terminal:return register_pages(w,rid,kind,pages)
    raise ResearchError('api_page_limit_exceeded')
