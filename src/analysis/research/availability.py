"""Research availability resolves across providers without changing raw-source states."""
import re
from copy import deepcopy
from pathlib import Path
from .workspace import sha, ResearchError
from .material_status import LABELS as PUBLIC_STATUS

FIELD_METRICS={'CONTRACT_ASSET':'contract_assets','GOODWILL':'goodwill'}
STATEMENT_FIELDS={'CREDIT_IMPAIRMENT_LOSS':'信用减值损失','ASSET_IMPAIRMENT_LOSS':'资产减值损失'}


def reconcile(data,pack,dataset_fact_ids=None):
    data=deepcopy(data)
    items=data['items']
    for r in items:
        old=r.pop('availability',None)
        if old:
            r['status']=old['source_status'];r['period']=old['source_periods']
            r['status_label']=old.get('source_label',{'processing':'已有待处理','missing':'尚未取得','readable':'已有可读'}[r['status']])
    metric_items={r['payload'][0]['metric_id']:r for r in items if r['reader']=='metrics' and r['payload']}
    statements={r['evidence_id']:r for r in pack.get('statements',[])}
    statement_items={(r['payload']['statement'],r['period']):r for r in items if r['reader']=='statement'}
    verified=set()
    def verify(original):
        path=original['path']
        if path not in verified:
            if sha(Path(path))!=original['sha256']:raise ResearchError('material_original_changed')
            verified.add(path)
    resolutions={}
    for cell in pack.get('financial_cell_resolutions',[]):
        original=statements.get(cell['evidence_id'])
        if cell['state']!='disclosed_blank' or not original or original['period']!=cell['period']:continue
        verify(original)
        resolutions.setdefault(cell['metric_id'],[]).append({'period':cell['period'],'state':'disclosed_blank',
            'value':None,'evidence_id':cell['evidence_id'],'page':cell['page'],
            'read_entry':{'tool':'read_statement','statement':original['statement'],'period':cell['period']}})
    for r in items:
        routes=[]
        if r['reader']=='field' and r['status']=='processing':
            field=r['payload']['field']
            if field in {'SWINDUSTRY_CODE2','SWINDUSTRY_NAME2'}:
                from .sw_industry import field_routes
                routes.extend(field_routes(items))
            elif field=='OSOPINION_TYPE':
                for check in items:
                    if check['reader']=='api_record' and check['payload']['dataset_id']=='audit_opinion':
                        routes.append({'period':check['period'],'state':'api_records_readable',
                            'scope':check['payload']['scope'],'read_entry':{'tool':'read_material','material_id':check['material_id']}})
            elif field in FIELD_METRICS:
                metric=FIELD_METRICS[field]
                routes.extend(resolutions.get(metric,[]))
                for m in pack['metrics']:
                    if m['metric_id']==metric and m.get('fact') and m['state']=='ready':
                        routes.append({'period':m['period'],'state':'ready','value':m['fact']['value'],
                            'fact_ref':m['fact_ref'],'unit':m['fact']['unit'],
                            'read_entry':{'tool':'read_material','material_id':metric_items[metric]['material_id'],'period':m['period']}})
            elif field in STATEMENT_FIELDS:
                label=STATEMENT_FIELDS[field]
                for s in statements.values():
                    # A matching row proves a readable source, not a numeric value or zero.
                    if s['statement']!='income_statement' or not re.search(re.escape(label),s.get('content','')):continue
                    target=statement_items.get(('income_statement',s['period']))
                    if target:
                        verify(s)
                        routes.append({'period':s['period'],'state':'original_readable',
                            'read_entry':{'tool':'read_material','material_id':target['material_id']}})
        elif r['reader']=='missing':
            ds=r['payload'].get('dataset_id')
            admitted=set((dataset_fact_ids or {}).get(ds,()))
            for m in pack['metrics']:
                fact=m.get('fact') or {}
                if m.get('state')!='ready' or fact.get('fact_id') not in admitted:continue
                target=metric_items.get(m['metric_id'])
                if not target:continue
                routes.append({'metric_id':m['metric_id'],'period':m['period'],'period_type':m['period_type'],
                    'state':'ready','value':fact['value'],'unit':fact['unit'],'fact_ref':m['fact_ref'],
                    'read_entry':{'tool':'read_material','material_id':target['material_id'],'period':m['period']}})
            if ds=='goodwill':
                routes.extend(resolutions.get('goodwill',[]))
            elif ds in {'guarantee','litigation','seo','allotment','bond_issuance','unlock_peer','customers_peer','pledge','violation','audit_opinion'}:
                for check in items:
                    if check['reader'] not in {'filing_check','api_record'} or check['payload']['dataset_id']!=ds:continue
                    routes.append({'period':check['period'],'state':check['payload']['state'],
                        'finding':check['payload']['finding'],'scope':check['payload']['scope'],
                        'read_entry':{'tool':'read_material','material_id':check['material_id']}})
            elif ds in {'income_quarter','cashflow_quarter'}:
                metrics=({'operating_income','operating_cost','net_profit','parent_net_profit'} if ds=='income_quarter' else
                         {'operating_cash_flow','investing_cash_flow','financing_cash_flow','long_asset_cash_purchase'})
                for name in sorted(metrics):
                    ready=[m for m in pack['metrics'] if m['metric_id']==name and m['period_type']=='single_quarter' and m.get('fact') and m['state']=='ready']
                    if ready and name in metric_items:
                        routes.append({'metric_id':name,'periods':sorted({m['period'] for m in ready}),
                            'state':'derived_readable','read_entry':{'tool':'query_research','metric_ids':[name], 'period_type':'single_quarter'}})
        if not routes:continue
        periods=sorted({p for x in routes for p in x.get('periods',[x.get('period')]) if p})
        r['availability']={'source_status':r['status'],'source_label':r.get('status_label',{'processing':'已有待处理','missing':'尚未取得','readable':'已有可读'}.get(r['status'],r['status'])),'source_periods':r['period'],
            'available_periods':periods,'routes':routes,
            'coverage':'listed_periods_only','numeric_policy':'原文可读不代表数值已准入；disclosed_blank仍为空值。'}
        r['status']='readable';r['status_label']=PUBLIC_STATUS['original_readable'];r['period']=periods
        if all(x['state']=='alternative_readable' for x in routes):
            from .sw_industry import LABEL
            r['status_label']=LABEL
        elif all(x['state']=='issuer_disclosed_none_or_not_applicable' for x in routes):
            r['status_label']=PUBLIC_STATUS['verified_none']
        elif all(x['state'] in {'ready','derived_readable'} for x in routes):
            r['status_label']=PUBLIC_STATUS['formal_numeric']
    return data


def resolved_view(item,period=None):
    availability=item['availability']
    routes=[r for r in availability['routes'] if not period or r.get('period')==period or period in r.get('periods',[])]
    if period:
        routes=[{**r,'periods':[period]} if 'periods' in r else r for r in routes]
    return {'title':item['title'],'available_periods':[period] if period and routes else availability['available_periods'] if not period else [],
            'materials':routes,'coverage':'仅以上列明期间与指标已有可用结果；其他原始字段未因此视为齐全。',
            'status':'readable' if routes else availability['source_status'],
            'status_label':item['status_label'] if routes else PUBLIC_STATUS['processing'] if availability['source_status']=='processing' else PUBLIC_STATUS['unavailable']}
