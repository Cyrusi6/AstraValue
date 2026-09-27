"""Read-only live probes using registered contracts; preserve empty versus failed."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import requests
from analysis.structured.protocols import EastmoneyRequest,ProtocolFamily,parse_eastmoney_response,parse_em_f_company_type_response


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--company',default='600519.SH');ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    code,exchange=args.company.split('.')
    out=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');out.mkdir(parents=True)
    root=Path(__file__).resolve().parents[1]
    datasets=json.loads((root/'config/structured_data/datasets.v1.json').read_text(encoding='utf8'))['datasets']
    ids=['customers_peer','guarantee','litigation','violation','seo','allotment','bond_issuance','pledge','unlock_peer','company_basic']
    def fetch(req,name):
        errors=[]
        for route in ['direct','proxy']:
            try:
                with requests.Session() as s:
                    s.trust_env=False
                    if route=='proxy':s.proxies={'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897'}
                    r=s.get(req.endpoint,params=req.params,timeout=(8,20),headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
                (out/(name+'-'+route+'.body')).write_bytes(r.content)
                (out/(name+'-'+route+'.request.json')).write_text(json.dumps({'url':r.url,'status':r.status_code,'time':datetime.now(timezone.utc).isoformat()},ensure_ascii=False),encoding='utf8')
                if r.status_code==200:return r
                errors.append(f'{route}:HTTP {r.status_code}')
            except requests.RequestException as e:errors.append(route+':'+type(e).__name__)
        raise RuntimeError(';'.join(errors))
    def run(ds):
        spec=ds['request'];rows=[];proofs=[];result={'dataset':ds['dataset_id'],'status':'failed'}
        try:
            for page in range(1,21):
                params=dict(spec['fixed_parameters']);params.update(pageNumber=str(page),pageSize='100',filter=f'({spec["company_filter_field"]}="{args.company if spec["company_filter_field"]=="SECUCODE" else code}")')
                req=EastmoneyRequest(ProtocolFamily[spec['protocol'].upper()],spec['endpoint'],params)
                response=fetch(req,ds['dataset_id']+'-'+str(page))
                parsed=parse_eastmoney_response(req,status_code=response.status_code,body=response.content,content_type=response.headers.get('Content-Type'))
                proofs.append(asdict(parsed.proof));rows.extend(parsed.rows)
                result.update(status=parsed.status.value,business_code=parsed.business_code,message=parsed.message,declared_total=parsed.declared_total)
                if parsed.status.value=='failed' or parsed.terminal:break
            else:result['status']='partial'
            if rows and result.get('declared_total') is not None and len(rows)!=result['declared_total']:result['status']='partial'
            result.update(rows=len(rows),pages=len(proofs),proofs=proofs)
            (out/(ds['dataset_id']+'-rows.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
            if ds['dataset_id']=='company_basic':result['fields']=[{k:r.get(k) for k in ['SECURITY_CODE','SWINDUSTRY_CODE2','SWINDUSTRY_NAME2']} for r in rows]
        except Exception as e:result['error']=str(e)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,[d for d in datasets if d['dataset_id'] in ids]))
    try:
        endpoint='https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/'
        req=EastmoneyRequest(ProtocolFamily.EM_F,endpoint+'Index',{'type':'web','code':exchange+code})
        response=fetch(req,'company-type')
        evidence=parse_em_f_company_type_response(req,status_code=response.status_code,body=response.content,content_type=response.headers.get('Content-Type'))
        if not evidence.company_type:raise RuntimeError('company_type_unresolved')
        req=EastmoneyRequest(ProtocolFamily.EM_F,endpoint+'zcfzbAjaxNew',{'companyType':evidence.company_type,'reportDateType':'0','reportType':'1','dates':'2025-12-31,2024-12-31,2023-12-31','code':exchange+code})
        response=fetch(req,'balance-fields');parsed=parse_eastmoney_response(req,status_code=response.status_code,body=response.content,content_type=response.headers.get('Content-Type'))
        result={'dataset':'balance_fields','status':parsed.status.value,'proof':asdict(parsed.proof),'fields':[{k:r.get(k) for k in ['REPORT_DATE','OSOPINION_TYPE','OPINION_TYPE']} for r in parsed.rows]}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    except Exception as e:results.append({'dataset':'balance_fields','status':'failed','error':str(e)})
    (out/'summary.json').write_text(json.dumps({'company':args.company,'queried_at':datetime.now(timezone.utc).isoformat(),'results':results,'policy':'API empty is source-scoped no-record evidence, not issuer-wide absence; live results do not mutate frozen research.'},ensure_ascii=False,indent=2),encoding='utf8')
    print('OUTPUT='+str(out),flush=True)

if __name__=='__main__':main()
