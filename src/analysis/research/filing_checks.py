"""Narrow annual-filing checks; absence findings stay scoped to the disclosure."""
from functools import lru_cache
from pathlib import Path
import re

from .workspace import ResearchError, sha, digest


def parse_page(text):
    # Require a section heading and its immediate checkbox, not a passing mention.
    rules={
        'guarantee':(r'(?m)^\s*[（(][一二三四五六七八九十]+[)）]\s*担保情况\s*\n\s*(□\s*适用\s*[√✓]\s*不适用)',
                     '担保情况栏目披露不适用','该年度报告担保情况栏目；不扩展为历史从未担保'),
        'litigation':(r'(?m)^\s*[一二三四五六七八九十]+、\s*重大诉讼、仲裁事项\s*\n\s*(□\s*本年度公司有重大诉讼、仲裁事项\s*[√✓]\s*本年度公司无重大诉讼、仲裁事项)',
                      '本年度公司无重大诉讼、仲裁事项','该报告年度的重大诉讼和仲裁；不涵盖所有普通案件'),
    }
    found=[]
    for dataset,(pattern,judgment,scope) in rules.items():
        for match in re.finditer(pattern,text):
            found.append({'dataset_id':dataset,'finding':judgment,'scope':scope,
                          'excerpt':match.group(0).strip(),'state':'issuer_disclosed_none_or_not_applicable'})
    extra=[
        ('guarantee',r'(?m)^\s*(?:[（(][一二三四五六七八九十]+[)）]|2)\s*报告期内履行的及尚未履行完毕的重大担保情况\s*\n\s*□\s*适用\s*[√✓]\s*不适用','重大担保情况披露不适用','半年度报告列明范围的重大担保'),
        ('litigation',r'(?m)^\s*[一二三四五六七八九十]+、\s*重大诉讼、仲裁事项\s*\n\s*□\s*本报告期公司有重大诉讼、仲裁事项\s*[√✓]\s*本报告期公司无重大诉讼、仲裁事项','本报告期公司无重大诉讼、仲裁事项','仅该报告期重大诉讼仲裁，不涵盖普通案件'),
        ('unlock_peer',r'(?m)^\s*[（(][一二三四五六七八九十]+[)）]\s*限售股份变动情况\s*\n\s*□\s*适用\s*[√✓]\s*不适用','限售股份变动情况披露不适用','仅报告期内限售股份变动，不代表未来无解禁'),
        ('seo',r'(?m)^\s*[（(]一[)）]\s*截至报告期内证券发行情况\s*\n\s*□\s*适用\s*[√✓]\s*不适用','报告期内证券发行情况披露不适用','该报告期内证券发行，不代表历史从未融资'),
        ('allotment',r'(?m)^\s*[（(]一[)）]\s*截至报告期内证券发行情况\s*\n\s*□\s*适用\s*[√✓]\s*不适用','报告期内证券发行情况披露不适用','该报告期内证券发行，不代表历史从未配股'),
        ('bond_issuance',r'(?m)^\s*一、\s*(?:公司债券（含企业债券）和非金融企业债务融资工具|企业债券、公司债券和非金融企业债务融资工具)\s*\n\s*□\s*适用\s*[√✓]\s*不适用','公司债券及非金融企业债务融资工具栏目不适用','仅该栏目列明债券与融资工具范围'),
    ]
    for ds,pattern,judgment,scope in extra:
        for m in re.finditer(pattern,text):found.append({'dataset_id':ds,'finding':judgment,'scope':scope,'excerpt':m.group(0).strip(),'state':'issuer_disclosed_none_or_not_applicable'})
    # Reading routes deliberately retain positive disclosures and unknown cells.
    readable=[('customers_peer',r'前五名客户销售额','客户与供应商集中度披露','仅已披露集中度，不承诺客户实名或逐户明细'),
              ('pledge',r'质押、标记\s*或\s*冻\s*结\s*情况','前十名股东质押、标记或冻结情况','仅前十名股东表，未知保持未知，不推断全部股东无质押'),
              ('violation',r'(?m)^\s*[一二三四五六七八九十]+、\s*上市公司及其董事','违法违规及整改事项原文','报告内列明事项与公告引用，不代表事件最终结果')]
    for ds,pattern,title,scope in readable:
        m=re.search(pattern,text)
        if not m:continue
        # Keep the page for shareholder tables; short paragraphs for other topics.
        excerpt=text if ds=='pledge' else text[m.start():m.start()+1800]
        if ds=='violation':
            end=re.search(r'\n\s*[一二三四五六七八九十]+、',text[m.end():])
            excerpt=text[m.start():m.end()+end.start()] if end else excerpt
        found.append({'dataset_id':ds,'finding':title,'scope':scope,'excerpt':excerpt.strip(),'state':'original_readable'})
    return found


@lru_cache(maxsize=64)
def _extract(path,source_hash,period):
    import fitz
    results=[]
    try:
        with fitz.open(path) as doc:
            cover=re.sub(r'\s+','',doc[0].get_text()) if len(doc) else ''
            title='年年度报告' if period.endswith('-12-31') else '年半年度报告'
            if period[:4]+title not in cover:return ()
            for number,page in enumerate(doc,1):
                results.extend({**r,'page':number} for r in parse_page(page.get_text()))
    except fitz.FileDataError:
        return ()
    return tuple(results)


def enrich(data,pack):
    """Add reproducible reading entries derived only from current frozen filings."""
    items=data['items'];known={r['material_id'] for r in items};seen=set()
    parents={r['payload']['dataset_id']:r['material_id'] for r in items if r['reader']=='missing' and r['payload'].get('dataset_id')}
    for s in pack.get('statements',[]):
        if not s['period'].endswith(('-12-31','-06-30')):continue
        key=(s['period'],s['sha256'])
        if key in seen:continue
        seen.add(key)
        if sha(Path(s['path']))!=s['sha256']:raise ResearchError('material_original_changed')
        results=_extract(s['path'],s['sha256'],s['period'])
        for result in results:
            ident='material-'+digest(['filing_check_v2',key,result])[:24]
            if ident in known:continue
            known.add(ident)
            items.append({'material_id':ident,'category':'governance','title':result['finding'],
                **({'parent_id':parents[result['dataset_id']]} if result['dataset_id'] in parents else {}),
                'period':s['period'],'status':'readable','status_label':'已有可读','reader':'filing_check',
                'purpose':'指定报告期的公司原文核查',
                'payload':{**result,'period':s['period'],'path':s['path'],'sha256':s['sha256'],
                           'source_url':s.get('source_url'),'formula_version':'filing-check-v2'}})
    return data
