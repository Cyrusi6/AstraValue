"""Six public availability states; raw provider and disclosure states stay separate."""
LABELS={'formal_numeric':'已有正式数值','original_readable':'已有原文','verified_none':'已核实无事项',
        'alternative':'已有替代资料','unavailable':'尚未取得','processing':'待处理'}


def route_status(route):
    state=route['state']
    if state in {'ready','derived_readable'}:return 'formal_numeric'
    if state=='issuer_disclosed_none_or_not_applicable':return 'verified_none'
    if state=='alternative_readable':return 'alternative'
    return 'original_readable'


def combine(states):
    states=set(states)
    if not states:return 'unavailable'
    if len(states)==1:return next(iter(states))
    # A mixed collection cannot claim every record is a verified absence or formal number.
    if states & {'original_readable','verified_none','formal_numeric'}:return 'original_readable'
    if 'alternative' in states:return 'alternative'
    return 'processing' if 'processing' in states else 'unavailable'


def status(item,period=None):
    a=item.get('availability')
    if a:
        routes=[r for r in a['routes'] if not period or r.get('period')==period or period in r.get('periods',[])]
        if routes:return combine(route_status(r) for r in routes)
        return 'processing' if a['source_status']=='processing' else 'unavailable'
    periods=item.get('period',[]);periods=periods if isinstance(periods,list) else [periods]
    if period and period not in periods:return 'unavailable'
    if item['status']=='missing':return 'unavailable'
    if item['status']=='processing':return 'processing'
    reader=item['reader'];payload=item['payload']
    if reader=='sw_classification':return 'alternative'
    if reader=='metrics':
        rows=[r for r in payload if not period or r['period']==period]
        states=[]
        for row in rows:
            if row['state']=='ready' and row.get('fact'):states.append('formal_numeric')
            elif row['state']=='disclosed_blank' and row.get('resolution',{}).get('evidence_id'):
                states.append('original_readable')
            else:states.append('processing')
        return combine(states)
    if reader in {'filing_check','api_record'}:return route_status(payload)
    return 'original_readable'


def present(item,period=None):
    value=status(item,period);source=item.get('availability',{}).get('source_status',item['status'])
    result={'status':value,'status_label':LABELS[value],'source_status':source,
            'scope':'仅列明期间及资料范围；原文可读不表示已进入正式计算'}
    if value=='alternative':result['reason']='已有替代资料，原供应商字段保留为空；观察日期不等于历史生效日期'
    if isinstance(item.get('payload'),dict) and item['payload'].get('scope'):result['scope']=item['payload']['scope']
    return result


def period_statuses(item):
    periods=item.get('period',[]);periods=periods if isinstance(periods,list) else [periods]
    groups={}
    for period in periods:
        if not period:continue
        key=status(item,period);groups.setdefault(key,[]).append(period)
    return [{'status':key,'status_label':LABELS[key],'periods':dates} for key,dates in groups.items()]
