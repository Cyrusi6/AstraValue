"""Research-scoped alternative SW level-2 classification."""
from pathlib import Path
from datetime import datetime
import json
from urllib.parse import urlparse, parse_qs
from .workspace import ResearchError, read_json, sha, digest

LABEL = '已有替代分类资料，原供应商字段为空'


def verify(record):
    if sha(Path(record['result_path'])) != record['result_sha256']:
        raise ResearchError('industry_result_changed')
    for source in record['sources']:
        if sha(Path(source['path'])) != source['sha256']:
            raise ResearchError('industry_source_changed')


def validate_sources(data):
    """Check names, code namespace, level and membership against saved responses."""
    from bs4 import BeautifulSoup
    sources=data.get('sources', [])
    directories=[s for s in sources if urlparse(s['url']).hostname=='legulegu.com'
                 and urlparse(s['url']).path=='/stockdata/sw-industry-overview']
    members=[s for s in sources if urlparse(s['url']).hostname=='www.swsresearch.com'
             and urlparse(s['url']).path=='/institute-sw/api/index_publish/details/component_stocks/']
    if len(directories)!=1 or len(members)!=1:raise ResearchError('industry_sources_required')
    for source in sources:
        if sha(Path(source['path']))!=source['sha256']:raise ResearchError('industry_source_changed')
    soup=BeautifulSoup(Path(directories[0]['path']).read_bytes(),'lxml')
    level=soup.find(id='level2Items')
    if not level:raise ResearchError('industry_level2_missing')
    matches=[]
    for link in level.find_all('a'):
        code=link.find(class_='lg-industries-item-chinese-title')
        name=link.find(class_='lg-industries-item-number')
        if code and name and code.get_text(strip=True)==data['display_index_code']:
            matches.append((link,name))
    if len(matches)!=1:raise ResearchError('industry_directory_ambiguous')
    link,name=matches[0];parent=name.find(class_='parent-industry-name')
    if (name.get_text(strip=True).split('(')[0]!=data['industry_name'] or not parent
        or parent.get_text(strip=True).strip('[]')!=data['parent_industry']
        or urlparse(link.get('href','')).path!='/stockdata/sw-industry-2021'
        or data['classification']!='申万2021版'
        or data['display_index_code']!=data['industry_index_code']+'.SI'):
        raise ResearchError('industry_definition_mismatch')
    if parse_qs(urlparse(members[0]['url']).query).get('swindexcode')!=[data['industry_index_code']]:
        raise ResearchError('industry_membership_index_mismatch')
    body=json.loads(Path(members[0]['path']).read_bytes())
    values=body.get('data',{})
    if str(body.get('code'))!='200' or values.get('next') or len(values.get('results',[]))!=values.get('count'):
        raise ResearchError('industry_membership_incomplete')
    company=[r for r in values['results'] if r['stockcode']==data['ticker']]
    if len(company)!=1 or company[0]['stockname']!=data['company']:
        raise ResearchError('industry_company_not_in_index')


def register(w, rid, result_path):
    state, _, _ = w.pack(rid)
    p = Path(result_path)
    data = read_json(p)
    if data.get('ticker') != state['ticker']:
        raise ResearchError('industry_company_mismatch')
    if data.get('level') != 2 or not data.get('industry_name') or not data.get('industry_index_code'):
        raise ResearchError('industry_result_incomplete')
    if data.get('temporal_scope') != 'current constituents only; no historical membership reconstruction':
        raise ResearchError('industry_temporal_scope_missing')
    observed=datetime.fromisoformat(data['observed_at'])
    if observed.tzinfo is None:raise ResearchError('industry_observation_timezone_required')
    validate_sources(data)
    payload = {**data, 'result_path': str(p.resolve()), 'result_sha256': sha(p),
               'registration_version': 'sw-alternative-v1'}
    return w.artifact(rid, 'sw_industry_classification', payload)


def enrich(data, records, state):
    data['items']=[r for r in data['items'] if r['reader']!='sw_classification']
    if not records:
        return data
    items = data['items']
    # Latest explicitly registered observation, never silently refresh from network.
    for record in [max(records,key=lambda r:datetime.fromisoformat(r['observed_at']))]:
        if record['ticker']!=state['ticker']:raise ResearchError('industry_company_mismatch')
        verify(record)
        ident = 'material-' + digest(['sw-alternative-v1', record['result_sha256']])[:24]
        if any(x['material_id'] == ident for x in items):
            continue
        classification = {
            'classification': record['classification'], 'level': record['level'],
            'industry_name': record['industry_name'],
            'industry_index_code': record['industry_index_code'],
            'display_index_code': record['display_index_code'],
            'parent_industry': record['parent_industry'],
            'observed_at': record['observed_at'], 'temporal_scope': record['temporal_scope'],
            'period_semantics': '资料观察日期，不是财务报告期或历史归属生效日期',
            'effective_date': None, 'original_provider_value': None,
            'code_namespace': '申万行业指数代码；未确认与SWINDUSTRY_CODE2编码相同',
            'research_as_of': state['as_of'],
            'after_research_cutoff': record['observed_at'][:10]>state['as_of'],
            'usage': '补充分类参考；不可作为研究截止日历史归属证据，不需重复补采原供应商空字段',
            'sources': [{k:v for k,v in s.items() if k!='path'} for s in record['sources']],
        }
        items.append({'material_id': ident, 'category': 'industry',
            'title': '申万二级行业替代分类', 'period': [record['observed_at'][:10]],
            'status': 'readable', 'status_label': '已有可读', 'reader': 'sw_classification',
            'purpose': '研究所需的申万分类替代资料；原供应商字段为空', 'payload': classification})
    return data


def field_routes(items):
    return [{'period': x['period'][0], 'state': 'alternative_readable',
             'finding': x['payload']['industry_name'],
             'scope': x['payload']['usage'], 'period_semantics':x['payload']['period_semantics'],
             'observed_at':x['payload']['observed_at'],
             'after_research_cutoff':x['payload']['after_research_cutoff'],
             'original_provider_value':None,
             'read_entry': {'tool': 'read_material', 'material_id': x['material_id']}}
            for x in items if x['reader'] == 'sw_classification']
