"""通过正式 structured runtime 运行有界真实行业样本并捕获最终请求。"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.identity import SecurityIdentity
from analysis.structured.runtime import StructuredDataRuntime
from analysis.structured.materialization import StructuredFactMaterializer
from analysis.structured.materialization_replay import _export_result
from analysis.structured.research import write_json
from analysis.structured.storage import canonical_json

SAMPLES = [('600519','consumer'),('600031','manufacturing'),('688981','technology'),('600036','bank'),
           ('601628','insurance'),('600030','securities'),('000002','real_estate'),('601088','resources'),
           ('600900','utility'),('688256','preprofit')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stock-list',type=Path,required=True)
    parser.add_argument('--as-of',default='2026-09-13T00:00:00+00:00')
    parser.add_argument('--ticker',action='append')
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    stocks={r['code']:r for r in json.loads(args.stock_list.read_bytes())['stockList']}
    requests=[]
    def capture(request):
        value={'method':request.method,'url':str(request.url),'params':dict(request.url.params)}
        requests.append(value)
        with (args.output/'requests.jsonl').open('a',encoding='utf8') as f:f.write(canonical_json(value)+'\n')
    summaries=[]
    with httpx.Client(timeout=40,trust_env=False,event_hooks={'request':[capture]}) as client:
        with AcquisitionRuntime.create(args.output/'analysis.db',args.output/'data',http_client=client) as acquisition:
            runtime=StructuredDataRuntime(acquisition)
            for ticker,profile in SAMPLES:
                if args.ticker and ticker not in args.ticker:continue
                if ticker not in stocks:
                    summaries.append({'ticker':ticker,'state':'identity_missing'});continue
                stock=stocks[ticker]; market='SSE' if ticker.startswith('6') else 'SZSE'; suffix='SH' if market=='SSE' else 'SZ'
                identity=SecurityIdentity(company_id='company:'+ticker,security_id=ticker+'.'+suffix,canonical_ticker=ticker+'.'+suffix,
                    security_code=ticker,market=market,current_name=stock['zwjc'],security_type='A',source_record_ids=('cninfo-stock-list:'+stock['orgId'],))
                datasets=['balance_fields','income_fields','cashflow_fields']
                if ticker=='600519':datasets+=['segments','market_cap','customers_peer']
                planned=runtime.plan([identity],mode='incremental',company_scope='company-only',dataset_ids=datasets,
                    report_periods=['2025-12-31','2026-06-30'],industry_profile_id=profile,as_of=datetime.fromisoformat(args.as_of))
                run_id=planned.run_ids[0]; before=len(requests)
                for _ in range(30):
                    executed=runtime.execute(run_id,max_jobs_per_round=10)
                    if not executed['attempted_job_ids']:break
                status=runtime.status(run_id)
                folder=args.output/ticker;folder.mkdir(exist_ok=True)
                try:
                    result=StructuredFactMaterializer(runtime.storage,runtime.repository).materialize(run_id,interpretation_contract='eastmoney-financial-interpretation-v1.0.0',research_scope=True)
                    _export_result(result,folder,acquisition.namespace_id)
                    facts=len(result.facts); dimensions=len(result.dimensional_facts)
                except ValueError as exc:
                    facts=dimensions=0;status['materialization_error']=str(exc)
                summary={'ticker':ticker,'requested_profile':profile,'profile_semantics_validated':False,'run_id':run_id,'new_requests':len(requests)-before,
                    'facts':facts,'dimensions':dimensions,'status':status,'sample_periods':['2025-12-31','2026-06-30']}
                summaries.append(summary);write_json(args.output/'summary.json',{'companies':summaries,'manual_acceptance':'pending','full_industry_semantics':False})
                print(canonical_json({'ticker':ticker,'facts':facts,'requests':len(requests)-before}),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
