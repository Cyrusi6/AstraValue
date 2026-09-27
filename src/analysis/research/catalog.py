"""Research-scoped, lazy material discovery over frozen inputs and registered caches."""
from pathlib import Path
from collections import Counter, defaultdict
import json
import re
import sqlite3
from typing import Annotated, Literal
from pydantic import Field
from .workspace import ResearchError, digest, sha, read_json, validate_integer_parameter

CATEGORIES={'metrics':'财务指标与字段','statements':'完整合并三大报表','notes':'报表附注',
    'business':'业务与经营','governance':'治理与风险','capital':'资本配置与分红',
    'valuation':'行情与估值','industry':'行业与竞争','knowledge':'理论知识'}
GROUP={'A':'business','B':'metrics','C':'governance','D':'capital','E':'industry','F':'valuation'}
STATUS={'readable':'已有原文','processing':'待处理','missing':'尚未取得'}
from .material_status import present, period_statuses


def classify_filing(title):
    """Explicit research uses; procedural notices are not part of the default study."""
    if any(x in title for x in ('取消会议','会议通知','股东大会的通知','股东大会通知','法律意见书','表决结果','英文')):
        return None,'procedural_or_duplicate_language'
    if '摘要' in title:return None,'summary_duplicates_full_filing'
    rules=[('statements',r'年度报告|半年度报告|季度报告'),('business',r'经营|产销|调价|重大事项|业绩说明|投资者关系'),
        ('governance',r'留置|立案|处罚|诉讼|担保|关联交易|内部控制|风险评估|辞职|聘任|董事会.*决议|监事会.*决议'),
        ('capital',r'分红|利润分配|权益分派|回购|增发|募集资金|投资项目|增持|减持|质押|解禁|股权激励|股东大会.*决议')]
    for category,pattern in rules:
        if re.search(pattern,title):return category,'research_relevant_filing'
    return None,'no_default_eight_step_use_identified'


class Catalog:
    def __init__(self,w):self.w=w

    def refresh_topic_tables(self,research_id:str,material_ids:list[str]):
        """Resume processing selected cached note originals; user/data-layer entry only."""
        from .note_tables import extract_tables
        s,data=self._load(research_id)
        selected=set(material_ids)
        originals={r['material_id']:r for r in data['items'] if r['reader']=='pdf' and r['category']=='notes'}
        if not selected or selected-set(originals):raise ResearchError('invalid_note_reprocessing_selection')
        for root in data['items']:
            if root['reader']!='topic' or root['category']!='notes':continue
            chosen=selected & set(root['payload']['children'])
            if not chosen:continue
            payload=root['payload']
            payload['table_cells']=[c for c in payload.get('table_cells',[]) if c['ref'] not in chosen]
            payload['processing']=[r for r in payload.get('processing',[]) if r['material_id'] not in chosen]
            for ident in sorted(chosen):
                cells,summary=extract_tables(originals[ident],root['title'])
                payload['table_cells'].extend(cells);payload['processing'].append(summary)
            payload['table_cells'].sort(key=lambda c:(c['period'],c['locator']['page'],c['locator']['source_row'],c['locator']['column']))
            payload['processing'].sort(key=lambda r:r['period'])
            root['purpose']='跨期数据及逐期原文' if payload['table_cells'] or payload['cells'] else '同主题逐期原文'
        data['catalog_version']='topics-v2'
        ident=digest(data);target=self.w.state/'catalogs'/f'{ident}.json'
        if not target.exists():target.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
        return self.w.artifact(research_id,'material_catalog',{'path':str(target),'sha256':sha(target),
            'catalog_id':ident,'items':len(data['items']),'audit':data['audit_scope'],
            'unindexed_selected':data['unindexed_selected'],'reprocessed_notes':sorted(selected)})

    def _load(self,rid):
        s,_,pack=self.w.pack(rid)
        records=self.w.artifacts(rid,'material_catalog')
        if not records:
            self.refresh_catalog(rid);records=self.w.artifacts(rid,'material_catalog')
        r=records[-1];p=Path(r['path'])
        if sha(p)!=r['sha256']:raise ResearchError('catalog_integrity_failed')
        from .availability import reconcile
        from .filing_checks import enrich
        from .api_materials import enrich as enrich_api
        from .sw_industry import enrich as enrich_sw
        view = enrich_sw(enrich_api(enrich(read_json(p),pack),self.w.artifacts(rid,'catalog_api_materials')),
                         self.w.artifacts(rid,'sw_industry_classification'),s)
        return s,reconcile(view,pack)

    def refresh_catalog(self,research_id:str):
        """Audit registered local caches against useful research materials. No network or blanket ingestion."""
        from analysis.structured.scope import load_scope
        s,path,pack=self.w.pack(research_id);manifest=read_json(path/'manifest.json')
        items={};ledger=[];seen_sources=set()
        def add(category,title,period,status,reader,payload,purpose,source_key):
            ident='material-'+digest([s['snapshot_id'],category,source_key])[:24]
            items[ident]={'material_id':ident,'category':category,'title':title,'period':period,
                'status':status,'status_label':STATUS[status],'purpose':purpose,'reader':reader,'payload':payload}
            ledger.append({'source_key':source_key,'decision':'indexed','material_id':ident})
            return ident
        def exclude(key,reason):ledger.append({'source_key':key,'decision':'excluded','reason':reason})
        by_metric=defaultdict(list)
        for m in pack['metrics']:by_metric[m['metric_id']].append(m)
        for metric,rows in by_metric.items():
            values=rows;ready=any(r['state']=='ready' or r['state']=='disclosed_blank' for r in values)
            add('metrics',rows[0].get('label',metric),sorted({r['period'] for r in rows}),
                'readable' if ready else 'missing','metrics',values,'财务趋势、质量及估值的标准输入','metric:'+metric)
        statement_ids=set()
        for r in pack.get('statements',[]):
            statement_ids.add(r['evidence_id']);seen_sources.add(r['sha256'])
            add('statements',r['period']+' '+r['statement'],r['period'],'readable','statement',
                {'statement':r['statement'],'period':r['period']},'读取完整合并报表及列头',r['evidence_id'])
        for e in pack.get('evidence',[])+pack.get('supplemental_evidence',[]):
            if e['evidence_id'] in statement_ids:continue
            if e.get('original_sha256'):seen_sources.add(e['original_sha256'])
            add(GROUP.get(e.get('group'),'business'),e.get('title') or e.get('slot_id') or '公司原文',e.get('period'),
                'readable','evidence',{'evidence_id':e['evidence_id']},'已选研究证据，可核对上下文',e['evidence_id'])
        scope=load_scope()['datasets'];seen_files=set();present=set()
        for descriptor in manifest.get('source_inputs',[])+manifest.get('auxiliary_inputs',[]):
            file=descriptor.get('files',{}).get('normalized-records.jsonl')
            if not file or file['path'] in seen_files:continue
            seen_files.add(file['path']);p=Path(file['path'])
            if sha(p)!=file['sha256']:raise ResearchError('catalog_projection_changed')
            datasets=defaultdict(list)
            for line in p.read_text('utf8').splitlines():
                if not line:continue
                row=json.loads(line);key=str(p)+':'+row['record_id'];rule=scope.get(row['dataset_id'],{})
                if row.get('company')!=s['ticker']:exclude(key,'other_company');continue
                if rule.get('selection')=='excluded' or not rule.get('question_ids'):exclude(key,'outside_research_scope');continue
                if (row.get('period') or '')>s['as_of']:exclude(key,'period_after_cutoff');continue
                datasets[row['dataset_id']].append(row);present.add(row['dataset_id'])
            for ds,rows in datasets.items():
                key=str(p)+':'+ds;rule=scope[ds]
                allowed=set(rule['consume_fields'])
                rows=[{**r,'fields':{k:v for k,v in r.get('fields',{}).items() if k in allowed}} for r in rows]
                status='readable' if any(any(v is not None for v in r['fields'].values()) for r in rows) else 'processing'
                parent_id=add('metrics',ds+' 原始字段',sorted({r['period'] for r in rows if r.get('period')}),status,
                    'records',{'rows':rows,'fields':sorted(allowed),'source_path':str(p),'source_sha256':file['sha256'],
                        'numeric_consumption':'仅已有正式fact_id可作正式计算输入'},rule['purpose'],key)
                for field in sorted(allowed):
                    has_value=any(r['fields'].get(field) is not None for r in rows)
                    child=add('metrics',ds+'.'+field,items[parent_id]['period'],'readable' if has_value else 'processing',
                        'field',{'parent_id':parent_id,'field':field},rule['purpose'],key+':field:'+field)
                    items[child]['parent_id']=parent_id
                material_id='material-'+digest([s['snapshot_id'],'metrics',key])[:24]
                for r in rows:ledger.append({'source_key':str(p)+':'+r['record_id'],'decision':'indexed','material_id':material_id})
        for ds,rule in scope.items():
            if rule['selection']=='required' and ds not in present:
                add('metrics',ds,[], 'missing','missing',{'dataset_id':ds,'next_action':'request_materials'},rule['purpose'],'missing-dataset:'+ds)
        # Index topical notes from already verified filings; original pages remain available.
        documents={r['sha256']:r for r in pack.get('statements',[])}
        import fitz
        for r in documents.values():
            with fitz.open(r['path']) as doc:
                active=False;expected=1;note_titles=set();last_note=None
                for i,page in enumerate(doc):
                    text=page.get_text();min_y=0
                    section=re.search(r'(?m)^\s*[一二三四五六七八九十]+[、．]\s*合并财务报表项目注释\s*$',text)
                    if section:
                        active=True;expected=1
                        positions=page.search_for(section[0].strip())
                        min_y=positions[-1].y0 if positions else 0
                        text=text[section.end():]
                    if not active:continue
                    end_section=re.search(r'(?m)^\s*[一二三四五六七八九十]+[、．]\s*(?:研发支出|合并范围的变更|在其他主体中的权益|与金融工具相关的风险|母公司财务报表主要项目注释)\s*$',text)
                    end_y=None
                    if end_section:
                        positions=page.search_for(end_section[0].strip())
                        end_y=positions[0].y0 if positions else 0
                        text=text[:end_section.start()]
                    for hit in re.finditer(r'(?m)^\s*(\d{1,2})\s*[、．]\s*([^\n]{2,35})\s*$',text):
                        title=hit[2].strip()
                        # Some PDFs lose the first glyph in the main heading; the
                        # immediately following full subsection supplies the exact title.
                        if title=='金流量表补充资料' and '现金流量表补充资料' in text[hit.end():hit.end()+120]:
                            title='现金流量表补充资料'
                        if not re.search('[\u4e00-\u9fff]',title):continue
                        key=r['sha256']+':note:'+str(i+1)+':'+title
                        if int(hit[1])!=expected:
                            exclude(key,'nested_or_outside_main_note_sequence');continue
                        expected+=1
                        positions=page.search_for(hit[0].strip()) or page.search_for(title)
                        positions=[p for p in positions if p.y0>=min_y]
                        y=positions[0].y0 if positions else min_y
                        min_y=y
                        if last_note:
                            items[last_note]['payload'].update(end_page=i+1,end_y=y)
                            last_note=None
                        if title in note_titles or not re.search(r'现金|存货|应收|收入|成本|资金|合同|商誉|固定资产|在建|无形|费用|分红|关联|投资|减值|借款|股本|税|负债|受限|使用权|租赁|债权|应付|限制|研发|股权',title):
                            exclude(key,'no_selected_financial_note_use_or_duplicate');continue
                        note_titles.add(title)
                        last_note=add('notes',title,r['period'],'readable','pdf',{'path':r['path'],'sha256':r['sha256'],
                            'source_url':r['source_url'],'page':i+1,'pages_total':len(doc),'start_y':y,
                            'end_page':len(doc),'end_y':doc[-1].rect.height},
                            '读取该主题附注原文',key)
                    if end_section:
                        if last_note:items[last_note]['payload'].update(end_page=i+1,end_y=end_y)
                        last_note=None;active=False
        examined=0
        earliest=min(pack.get('periods',{}).get('annual',[s['as_of']]))[:4]+'-01-01'
        cache_roots=[]
        for cache in self.w.config.get('valuation_caches',[]):
            db=Path(cache['db']);root=Path(cache['data_root']);cache_roots.append(str(db))
            if not db.exists():exclude(str(db),'configured_cache_unavailable');continue
            with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as con:
                if not con.execute("select name from sqlite_master where name='raw_resource_snapshots'").fetchone():continue
                for (raw,) in con.execute("select payload from raw_resource_snapshots where resource_role='content'"):
                    row=json.loads(raw);key=row['snapshot_id'];examined+=1
                    published=row.get('published_at') or row.get('available_at','')
                    if not earliest<=published[:10]<=s['as_of']:exclude(key,'outside_default_research_window');continue
                    if row['sha256'] in seen_sources:exclude(key,'already_indexed_same_original');continue
                    p=root/row['archive_relative_path']
                    if not p.exists():exclude(key,'cache_file_missing');continue
                    if sha(p)!=row['sha256']:raise ResearchError('cached_original_hash_mismatch')
                    try:
                        with fitz.open(p) as doc:
                            title=re.sub(r'\s+','',doc[0].get_text())[:240];pages=len(doc)
                            title=re.split(r'本公司董事会|本公司及董事会|本公司全体董事',title)[0]
                    except Exception:exclude(key,'unreadable_cache_requires_triage');continue
                    if s['ticker'] not in title and s['company'] not in title:exclude(key,'company_not_identified');continue
                    category,reason=classify_filing(title)
                    if not category:exclude(key,reason);continue
                    seen_sources.add(row['sha256'])
                    add(category,title[:110],published[:10],'readable','pdf',{'path':str(p),'sha256':row['sha256'],
                        'source_url':row.get('canonical_url'),'page':1,'pages_total':pages},
                        '与八步研究相关的缓存公告原文；数值尚不自动成为标准指标',key)
        from .knowledge import Knowledge
        knowledge = Knowledge(self.w)
        knowledge_index = knowledge.available_methods()
        knowledge_bundle_id = knowledge_index.get('bundle_id')
        for method in knowledge_index.get('items', []):
            add('knowledge', method['title'], None, 'readable', 'knowledge',
                {'card_id': method['id'], 'bundle_id': knowledge_bundle_id,
                 'version': method['version'], 'content_sha256': method['content_sha256']},
                '按问题选用已核实理论', method['id'] + '@' + str(knowledge_bundle_id))
        if not any(i['category']=='knowledge' for i in items.values()):
            add('knowledge','已发布理论知识',None,'missing','missing',
                {'next_action':'search_knowledge', 'bundle_id': knowledge_bundle_id},
                '知识按需使用，不阻塞研究','knowledge:none@' + str(knowledge_bundle_id))
        from .topics import group_topics
        group_topics(items,s['snapshot_id'])
        data={'snapshot_id':s['snapshot_id'],'catalog_version':'topics-v2','knowledge_bundle_id':knowledge_bundle_id,
            'items':list(items.values()),'ledger':ledger,
            'audit_scope':{'registered_archive_databases':cache_roots,'projection_files':sorted(seen_files),
                'default_period_start':earliest,'as_of':s['as_of'],'cached_originals_examined':examined,
                'boundary':'核对已登记缓存和当前快照；未登记路径不宣称覆盖，未取得指这些输入中未发现可交付材料'},
            'unindexed_selected':[]}
        selected={r['material_id'] for r in ledger if r['decision']=='indexed'}
        data['unindexed_selected']=sorted(selected-set(items))
        ident=digest(data);target=self.w.state/'catalogs'/f'{ident}.json';target.parent.mkdir(exist_ok=True,parents=True)
        if not target.exists():target.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
        return self.w.artifact(research_id,'material_catalog',{'path':str(target),'sha256':sha(target),
            'catalog_id':ident,'items':len(items),'audit':data['audit_scope'],'unindexed_selected':data['unindexed_selected']})

    def catalog_overview(self,research_id:str):
        """Light first package: category purpose, date span, status counts and expansion links only."""
        s,data=self._load(research_id);groups=[]
        for cat,title in CATEGORIES.items():
            rows=[r for r in data['items'] if r['category']==cat and not r.get('parent_id')]
            if not rows:continue
            periods=sorted({p for r in rows for p in (r['period'] if isinstance(r['period'],list) else [r['period']]) if p})
            groups.append({'category':cat,'title':title,'summary':{'metrics':'标准指标及研究范围内的原始字段','notes':'按主题定位财务附注','statements':'完整合并财务报表'}.get(cat,title+'相关材料'),
                'period_start':periods[0] if periods else None,'period_end':periods[-1] if periods else None,
                'status_counts':dict(Counter(present(r)['status_label'] for r in rows)),
                'read_entry':{'tool':'list_materials','category':cat}})
        return {'research_id':research_id,'snapshot_id':s['snapshot_id'],'categories':groups,
            'policy':'展开目录后选择条目读取；原始字段不自动成为正式指标。'}

    def list_materials(self,research_id:str,
                       category:Literal['metrics','statements','notes','business','governance','capital','valuation','industry','knowledge'],
                       query:str='',page:Annotated[int,Field(ge=1)]=1,
                       page_size:Annotated[int,Field(ge=1,le=40)]=15,parent_id:str|None=None):
        """Expand a category with page_size 1..40; follow next_page using the same filters and size.

        Supply a dataset parent_id to list its individual useful fields.
        """
        if category not in CATEGORIES:
            raise ResearchError(f"invalid_catalog_query:category must be one of {', '.join(CATEGORIES)}; received {category!r}")
        validate_integer_parameter(page,'page','invalid_catalog_query')
        validate_integer_parameter(page_size,'page_size','invalid_catalog_query',40)
        s,data=self._load(research_id)
        rows=[{k:r[k] for k in ('material_id','title','period','purpose')} | present(r) | {'period_states':period_statuses(r),'read_entry':'read_material',
            'purpose':'跨期数据及逐期原文' if r['reader']=='topic' and r['payload'].get('table_cells') else r['purpose'],
            'coverage': (min(r['period'])+'—'+max(r['period'])) if isinstance(r['period'],list) and r['period'] else r['period'],
            'data_view':({'table_periods':sorted({c['period'] for c in r['payload']['cells']+r['payload'].get('table_cells',[])}),
                          'all_detected_tables_processed_periods':sorted(x['period'] for x in r['payload'].get('processing',[]) if x['state']=='detected_tables_processed'),
                          'original_periods':r['period']} if r['reader']=='topic' else None),
            'expand_entry':{'tool':'list_materials','category':category,'parent_id':r['material_id']} if r['reader'] in {'records','topic'} else None}
            for r in data['items'] if r['category']==category and r.get('parent_id')==parent_id
            and (not query or query.lower() in (r['title']+r['purpose']).lower())]
        start=(page-1)*page_size
        return {'research_id':research_id,'snapshot_id':s['snapshot_id'],'total':len(rows),'items':rows[start:start+page_size],
            'next_page':page+1 if start+page_size<len(rows) else None}

    def read_material(self,research_id:str,material_id:str,
                      page:Annotated[int,Field(ge=1)]=1,
                      max_tokens:Annotated[int,Field(ge=1,le=4000)]=2000,
                      period:str|None=None,table_id:str|None=None):
        """Read per-period material; max_tokens is 1..4000 and must stay fixed while following next_page."""
        validate_integer_parameter(page,'page','invalid_material_pagination')
        validate_integer_parameter(max_tokens,'max_tokens','invalid_material_pagination',4000)
        _,data=self._load(research_id)
        item=next((r for r in data['items'] if r['material_id']==material_id),None)
        if not item:raise ResearchError('material_not_in_current_catalog')
        result=self._read_material(research_id,material_id,page,max_tokens,period,table_id,loaded=data)
        public=present(item,period)
        if result.get('reason')=='原件需要OCR':public.update(status='processing',status_label='待处理')
        return result|public

    def _read_material(self,research_id:str,material_id:str,page:int=1,max_tokens:int=2000,period:str|None=None,table_id:str|None=None,loaded=None):
        """Read one item, period or topic table_id; follow next_page for continuation."""
        validate_integer_parameter(page,'page','invalid_material_pagination')
        validate_integer_parameter(max_tokens,'max_tokens','invalid_material_pagination',4000)
        s=self.w.task(research_id)[0];data=loaded if loaded is not None else self._load(research_id)[1];r=next((r for r in data['items'] if r['material_id']==material_id),None)
        if not r:raise ResearchError('material_not_in_current_catalog')
        p=r['payload'];reader=r['reader'];common={'material_id':material_id,'status':r['status'],'status_label':r['status_label'],'snapshot_id':s['snapshot_id']}
        if table_id and reader!='topic':raise ResearchError('table_id_requires_topic')
        if reader=='sw_classification':
            if period and period not in r['period']:raise ResearchError('material_period_not_available')
            from analysis.structured.research_lite import _split_utf8
            chunks=_split_utf8(json.dumps(p,ensure_ascii=False),max_tokens*2)
            if page>len(chunks):raise ResearchError(f'material_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}')
            return common|{'content':chunks[page-1],'next_page':page+1 if page<len(chunks) else None}
        if reader in {'filing_check','api_record'}:
            if period and period!=r['period']:raise ResearchError('material_period_not_available')
            if sha(Path(p['path']))!=p['sha256']:raise ResearchError('material_original_changed')
            from analysis.structured.research_lite import _split_utf8
            chunks=_split_utf8(json.dumps({k:v for k,v in p.items() if k!='path'},ensure_ascii=False),max_tokens*2)
            if page>len(chunks):raise ResearchError(f'material_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}')
            return common|{'content':chunks[page-1],'next_page':page+1 if page<len(chunks) else None}
        if r.get('availability'):
            from .availability import resolved_view
            from analysis.structured.research_lite import _split_utf8
            view=resolved_view(r,period)|present(r,period)
            from .material_status import route_status, LABELS
            view['materials']=[{**route,'source_state':route['state'],'state':route_status(route),
                'status_label':LABELS[route_status(route)]} for route in view['materials']]
            chunks=_split_utf8(json.dumps(view,ensure_ascii=False),max_tokens*2)
            if page>len(chunks):raise ResearchError(f'material_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}')
            return common|{'status':view['status'],'status_label':view['status_label'],
                'content':chunks[page-1],'next_page':page+1 if page<len(chunks) else None}
        if reader=='topic':
            from .topics import topic_content
            for child in data['items']:
                if child['material_id'] in p['children'] and child['reader']=='pdf':
                    if sha(Path(child['payload']['path']))!=child['payload']['sha256']:
                        raise ResearchError('material_original_changed')
            p=topic_content(r,data,period,table_id)
        if reader=='field':
            parent=next(x for x in data['items'] if x['material_id']==p['parent_id'])
            p={'field':p['field'],'rows':[{'period':x.get('period'),'record_id':x['record_id'],'snapshot_id':x['snapshot_id'],
                'value':x['fields'].get(p['field'])} for x in parent['payload']['rows'] if not period or x.get('period')==period],
                'numeric_consumption':parent['payload']['numeric_consumption'],
                'source_path':parent['payload']['source_path'],'source_sha256':parent['payload']['source_sha256']}
        elif reader=='metrics' and period:p=[x for x in p if x['period']==period]
        elif reader=='records' and period:p={**p,'rows':[x for x in p['rows'] if x.get('period')==period]}
        if reader=='metrics':
            from .topics import matrix,metric_cells
            p={'tables':matrix(metric_cells(p))}
        if reader in {'records','field'} and sha(Path(p['source_path']))!=p['source_sha256']:
            raise ResearchError('material_projection_changed')
        if reader in {'records','field'}:
            from .topics import matrix
            cells=[]
            for row in p['rows']:
                for field,value in (row.get('fields') or {p.get('field','value'):row.get('value')}).items():
                    cells.append({'table':'供应商字段','label':field,'period':row.get('period') or '未标期间',
                        'period_type':'supplier_defined','scope':'supplier_defined','unit':'见字段定义',
                        'currency':None,'value':value,'state':'ready' if value is not None else 'missing',
                        'ref':row['record_id']+'@'+row['snapshot_id']})
            p={k:v for k,v in p.items() if k!='rows'}|{'tables':matrix(cells)}
        if reader=='missing':return common|p
        if reader=='statement':
            from .statements import Statements
            return common|Statements(self.w).read_statement(research_id,**p,page=page,max_tokens=max_tokens)
        if reader=='evidence':return common|self.w.read_evidence(research_id,p['evidence_id'],page,max_tokens)
        if reader=='knowledge':
            from .knowledge import Knowledge
            return common|Knowledge(self.w).search_knowledge(
                card_id=p['card_id'], page=page, bundle_id=p.get('bundle_id'))
        if reader=='pdf':
            import fitz
            if sha(Path(p['path']))!=p['sha256']:raise ResearchError('material_original_changed')
            from analysis.structured.research_lite import _split_utf8
            chunks=[]
            with fitz.open(p['path']) as doc:
                for actual in range(p['page'],p.get('end_page',len(doc))+1):
                    source_page=doc[actual-1]
                    top=p.get('start_y',0) if actual==p['page'] else 0
                    bottom=p.get('end_y',source_page.rect.height) if actual==p.get('end_page') else source_page.rect.height
                    if bottom<=top:continue
                    text=source_page.get_text(clip=fitz.Rect(0,top,source_page.rect.width,bottom))
                    chunks.extend((actual,c) for c in _split_utf8(text,max_tokens*2))
            if not chunks:return common|{'status':'processing','status_label':STATUS['processing'],'next_action':'request_materials','reason':'原件需要OCR'}
            if page>len(chunks):raise ResearchError('material_page_out_of_range')
            actual,text=chunks[page-1]
            proof=self.w.artifact(research_id,'evidence_read',{'content':text,'original_path':p['path'],'original_sha256':p['sha256'],
                'source_url':p['source_url'],'locator':'page:'+str(actual),'original_hash_verified':True})
            return common|{'content':text,'evidence_id':proof['artifact_id'],'document_page':actual,
                'next_page':page+1 if page<len(chunks) else None}
        from analysis.structured.research_lite import _split_utf8
        text=json.dumps(p,ensure_ascii=False);chunks=_split_utf8(text,max_tokens*2)
        if page>len(chunks):raise ResearchError(f'material_page_out_of_range:page must be 1..{len(chunks)} for max_tokens={max_tokens}; received {page}')
        proof={}
        if reader=='topic':
            artifact=self.w.artifact(research_id,'evidence_read',{'content':chunks[page-1],
                'locator':material_id+':chunk:'+str(page),'source_role':'cross_period_reading_view',
                'source_material_ids':r['payload']['children'],'numeric_admission':False,
                'original_hash_verified':True})
            proof={'evidence_id':artifact['artifact_id']}
        if reader in {'records','field'}:
            artifact=self.w.artifact(research_id,'evidence_read',{'content':chunks[page-1],
                'original_path':p['source_path'],'original_sha256':p['source_sha256'],
                'locator':material_id+':chunk:'+str(page),'source_url':None,'source_role':'normalized_supplier_record',
                'original_hash_verified':True,'numeric_admission':False})
            proof={'evidence_id':artifact['artifact_id']}
        return common|{'content':chunks[page-1],'next_page':page+1 if page<len(chunks) else None}|proof

    def catalog_audit(self,research_id:str,page:int=1,page_size:int=30):
        if page<1 or not 1<=page_size<=100:raise ResearchError('invalid_audit_pagination')
        _,d=self._load(research_id);start=(page-1)*page_size;rows=d['ledger']
        return {'scope':d['audit_scope'],'decision_counts':dict(Counter(r['decision'] for r in rows)),
            'exclusion_reasons':dict(Counter(r.get('reason') for r in rows if r['decision']=='excluded')),
            'unindexed_selected':d['unindexed_selected'],'rows':rows[start:start+page_size],
            'next_page':page+1 if start+page_size<len(rows) else None}
