"""从逐题缺口选择一个公司/数据集，复用正式 runtime 执行有界精确补采。"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.identity import SecurityIdentity
from analysis.structured.runtime import StructuredDataRuntime
from analysis.structured.scope import load_research_profile, selected_datasets
from analysis.structured.research_lite import _write_json as write_json


def select_acquisition_gaps(gaps, ticker, dataset):
    if not isinstance(gaps,dict) or not gaps.get('profile_id') or not isinstance(gaps.get('items'),list):
        raise ValueError('current_lite_next_work_required:profile_id_and_items')
    if gaps.get('company')!=ticker:raise ValueError('gap_company_mismatch')
    profile=load_research_profile(gaps['profile_id'])
    selected_datasets([dataset],research_profile_id=profile['profile_id'])
    selected=[];skipped=[]
    for item in gaps['items']:
        if not isinstance(item,dict):raise ValueError('gap_item_must_be_object')
        reason=None
        if item.get('acquire_allowed') is not True or item.get('stage')!='acquisition':
            reason='not_acquisition_task'
        elif not item.get('dataset_id'):
            reason='dataset_identity_required:use_workspace_request_materials'
        elif item['dataset_id']!=dataset:
            reason='other_dataset'
        if reason:
            skipped.append({'requirement_id':item.get('requirement_id'),'reason':reason})
            continue
        if item.get('company',ticker)!=ticker:raise ValueError('gap_item_company_mismatch')
        if item.get('profile_id',profile['profile_id'])!=profile['profile_id']:
            raise ValueError('gap_item_profile_mismatch')
        selected.append(item)
    return profile,selected,skipped


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gap-file',type=Path,required=True)
    parser.add_argument('--stock-list',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ticker',required=True)
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--as-of',type=date.fromisoformat,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--proxy')
    args=parser.parse_args(argv)
    gaps=json.loads(args.gap_file.read_text(encoding='utf8'))
    profile,items,skipped=select_acquisition_gaps(gaps,args.ticker,args.dataset)
    dates=sorted({i['period'] for i in items if isinstance(i.get('period'),str)
        and len(i['period'])==10 and i['period'].endswith(('03-31','06-30','09-30','12-31'))})
    result={'company':args.ticker,'dataset_id':args.dataset,'requirements':items,'report_periods':dates,
        'profile_id':profile['profile_id'],'profile_sha256':profile['content_sha256'],
        'skipped_items':skipped,'executed':False,'requests':[]}
    target=args.output/'gap-runs'/f'{args.ticker}-{args.dataset}-{args.as_of}.json'
    if not items:
        result['state']='no_resolved_acquisition_gap';write_json(target,result);print(json.dumps(result,ensure_ascii=False));return 0
    stocks=json.loads(args.stock_list.read_text(encoding='utf8'))['stockList']
    matching=[s for s in stocks if s['code']==args.ticker and s.get('category')=='A股']
    if len(matching)!=1:raise ValueError('unique_official_a_share_identity_required')
    stock=matching[0]
    market='SSE' if args.ticker.startswith('6') else 'SZSE' if args.ticker.startswith(('0','3')) else None
    if market is None:raise ValueError('market_requires_explicit_identity_adapter')
    suffix='SH' if market=='SSE' else 'SZ'
    identity=SecurityIdentity(company_id='company:'+args.ticker,security_id=args.ticker+'.'+suffix,
        canonical_ticker=args.ticker+'.'+suffix,security_code=args.ticker,market=market,current_name=stock['zwjc'],
        security_type='A',source_record_ids=('cninfo-stock-list:'+stock['orgId'],))
    exit_code=0
    if args.execute:
        def capture(request):
            record={'method':request.method,'url':str(request.url),'params':dict(request.url.params),'run_id':result.get('run_id')}
            result['requests'].append(record)
            args.output.mkdir(parents=True,exist_ok=True)
            with (args.output/'gap-requests.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            write_json(target,result)
        try:
            with httpx.Client(timeout=40,trust_env=False,proxy=args.proxy,event_hooks={'request':[capture]}) as client:
                with AcquisitionRuntime.create(args.output/'analysis.db',args.output/'data',http_client=client) as acquisition:
                    runtime=StructuredDataRuntime(acquisition)
                    planned=runtime.plan([identity],mode='incremental',company_scope='company-only',dataset_ids=[args.dataset],
                        research_profile_id=profile['profile_id'],report_periods=dates,
                        as_of=datetime.combine(args.as_of,time.min,tzinfo=timezone.utc))
                    result['run_id']=planned.run_ids[0]
                    result['executed']=True
                    for _ in range(30):
                        execution=runtime.execute(result['run_id'],max_jobs_per_round=10)
                        if not execution['attempted_job_ids']:break
                    result['state']=runtime.status(result['run_id'])
                    if result['state']['summary']['unfinished']:exit_code=1
        except Exception as exc:
            result.update(error={'type':type(exc).__name__,'reason':str(exc)},state='execution_failed')
            exit_code=1
    write_json(target,result)
    print(json.dumps({'output':str(target.resolve()),'new_requests':len(result['requests']),'executed':result['executed']},ensure_ascii=False))
    return exit_code


if __name__=='__main__':raise SystemExit(main())
