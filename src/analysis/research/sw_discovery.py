"""Automatic SW directory/member discovery, with a resumable scan and no ticker map."""
import json
from pathlib import Path
from bs4 import BeautifulSoup
from .workspace import ResearchError,read_json
from .supplement_transport import dump,Checkpoint
from .sw_industry import register


def discover(w,rid,folder,transport):
    state,_,_=w.pack(rid);folder=Path(folder);result=folder/'sw-result.json'
    if result.exists():return register(w,rid,result)
    directory=transport.fetch('https://legulegu.com/stockdata/sw-industry-overview')
    soup=BeautifulSoup(Path(directory['path']).read_bytes(),'lxml');level=soup.find(id='level2Items')
    if not level:
        transport.invalidate('https://legulegu.com/stockdata/sw-industry-overview')
        raise ResearchError('sw_directory_level2_missing')
    candidates=[]
    for a in level.find_all('a'):
        code=a.find(class_='lg-industries-item-chinese-title');name=a.find(class_='lg-industries-item-number')
        if not code or not name:continue
        parent=name.find(class_='parent-industry-name')
        if not parent:continue
        candidates.append({'display_index_code':code.get_text(strip=True),'industry_name':name.get_text(strip=True).split('(')[0],
            'parent_industry':parent.get_text(strip=True).strip('[]')})
    if not candidates or len({r['display_index_code'] for r in candidates})!=len(candidates):raise ResearchError('sw_directory_ambiguous')
    # Previously matched indices are only search priorities, never company classifications.
    with w.connect() as con:
        records=con.execute("SELECT payload FROM research_artifacts WHERE kind='sw_industry_classification'").fetchall()
    priority={json.loads(r[0]).get('display_index_code') for r in records}
    candidates.sort(key=lambda r:r['display_index_code'] not in priority)
    checkpoint=folder/'sw-scan.json';progress=read_json(checkpoint) if checkpoint.exists() else {'checked':[]}
    for candidate in candidates:
        code=candidate['display_index_code'].split('.')[0]
        if code in progress['checked']:continue
        source=transport.fetch('https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/',
            {'swindexcode':code,'page':'1','page_size':'10000'})
        try:
            body=read_json(Path(source['path']));data=body.get('data') or {}
        except (ValueError,AttributeError):
            transport.invalidate('https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/',
                {'swindexcode':code,'page':'1','page_size':'10000'})
            raise ResearchError('sw_member_response_invalid:'+code)
        if str(body.get('code'))!='200' or data.get('next') or len(data.get('results',[]))!=data.get('count'):
            transport.invalidate('https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/',
                {'swindexcode':code,'page':'1','page_size':'10000'})
            raise ResearchError('sw_member_response_incomplete:'+code)
        rows=[r for r in data['results'] if r.get('stockcode')==state['ticker']]
        if rows:
            if len(rows)!=1:raise ResearchError('sw_member_ambiguous')
            value={**candidate,'company':rows[0]['stockname'],'ticker':state['ticker'],'classification':'申万2021版',
                'level':2,'industry_index_code':code,'code_namespace':'SW industry index, not supplier classification code',
                'observed_at':source['observed_at'],'effective_date':None,
                'temporal_scope':'current constituents only; no historical membership reconstruction',
                'membership_beginningdate_reported':rows[0].get('beginningdate'),'sources':[directory,source]}
            dump(result,value);return register(w,rid,result)
        progress['checked'].append(code);dump(checkpoint,progress)
    return {'status':'unavailable','reason':'company_not_in_current_level2_constituents','indices_checked':len(progress['checked'])}
