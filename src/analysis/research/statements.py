"""Complete filing statements on demand and evidence-backed missing-cell resolution."""
import json
import re
from collections import Counter
from pathlib import Path
from .workspace import ResearchError, sha, digest, read_json
from .pack_outputs import output_hashes, read_pack_outputs

TITLES = {'balance_sheet':'合并资产负债表','income_statement':'合并利润表','cash_flow_statement':'合并现金流量表'}
LABELS = {'accounts_receivable':'应收账款','contract_assets':'合同资产','goodwill':'商誉'}


def extract_statements(source, ticker, cutoff):
    import fitz
    if source['published_at'] > cutoff or sha(Path(source['path'])) != source['sha256']:
        raise ResearchError('statement_source_date_or_hash_invalid')
    with fitz.open(source['path']) as doc:
        first = re.sub(r'\s+','',doc[0].get_text())
        if ticker not in first:
            raise ResearchError('statement_company_mismatch')
        m = re.search(r'(20\d{2})年(年度报告|半年度报告|第一季度报告|第三季度报告)',first[:180])
        if not m:
            raise ResearchError('filing_period_not_identified')
        period=m[1]+{'年度报告':'-12-31','半年度报告':'-06-30','第一季度报告':'-03-31','第三季度报告':'-09-30'}[m[2]]
        if period != source['period']:
            raise ResearchError('statement_period_mismatch')
        texts=[p.get_text() for p in doc]
        offsets=[]; full=''
        for t in texts:
            offsets.append(len(full));full+=t+'\n'
        # First statutory statements only; later accounting-policy restatements are not substituted.
        heads=list(re.finditer(r'(?m)^\s*((?:合并|母公司)(?:资产负债表|利润表|现金流量表|所有者权益变动表))\s*$',full))
        result=[]
        for kind,title in TITLES.items():
            match=next((h for h in heads if h[1]==title),None)
            if not match:continue
            end=next((h.start() for h in heads if h.start()>match.start()),len(full))
            # Signed end of cash flow statement prevents including unrelated notes when no next statement exists.
            signed=re.search(r'公司负责人[：:]',full[match.end():end])
            if signed:end=match.end()+signed.start()
            pages=[i+1 for i,t in enumerate(texts) if offsets[i]<end and offsets[i]+len(t)>match.start()]
            content=full[match.start():end].strip()
            if not all(x in content for x in {'balance_sheet':['资产总计','负债合计'], 'income_statement':['净利润'], 'cash_flow_statement':['经营活动','投资活动','筹资活动']}[kind]):
                raise ResearchError('incomplete_statement:'+kind)
            row={**source,'statement':kind,'scope':'consolidated','pages':pages,'content':content,'period':period}
            row['evidence_id']='statement-'+digest({k:v for k,v in row.items() if k!='content'})[:24]
            cells=[]
            if kind=='balance_sheet':
                for page in pages:
                    page_obj=doc[page-1]
                    cutoff_y=page_obj.rect.height
                    if page==pages[-1]:
                        boundaries=[]
                        for marker in ['公司负责人','母公司资产负债表','合并利润表']:
                            boundaries.extend(page_obj.search_for(marker))
                        if boundaries:cutoff_y=min(b.y0 for b in boundaries)
                    for table in doc[page-1].find_tables().tables:
                        for table_row,cells_row in zip(table.rows,table.extract()):
                            if table_row.bbox[1]>=cutoff_y:continue
                            if not cells_row:continue
                            label=re.sub(r'\s+','',cells_row[0] or '')
                            if label in LABELS.values() and len(cells_row) in (3,4):
                                cells.append({'label':label,'cells':cells_row,'page':page})
                row['target_rows']=cells
            result.append(row)
        if len(result)!=3:
            raise ResearchError('three_statements_not_identified')
        return result


class Statements:
    def __init__(self,workspace):self.w=workspace

    def statement_catalog(self,research_id:str):
        """List complete consolidated statements and periods; not just selected core metrics."""
        s,_,p=self.w.pack(research_id)
        return {'research_id':research_id,'snapshot_id':s['snapshot_id'],
            'statements':[{k:r[k] for k in ('evidence_id','statement','period','scope','pages')} for r in p.get('statements',[])],
            'next_action':'read_statement'}

    def read_statement(self,research_id:str,statement:str,period:str,page:int=1,max_tokens:int=2000):
        """Read every line of one complete consolidated filing statement, with bounded continuation."""
        from analysis.structured.research_lite import _split_utf8
        if not 1<=max_tokens<=4000 or page<1:raise ResearchError('invalid_statement_pagination')
        s,_,p=self.w.pack(research_id)
        row=next((r for r in p.get('statements',[]) if r['statement']==statement and r['period']==period),None)
        if not row:return {'status':'material_gap','next_action':'statement_catalog'}
        if sha(Path(row['path']))!=row['sha256']:raise ResearchError('statement_original_changed')
        chunks=_split_utf8(row['content'],max_tokens*2)
        if page>len(chunks):raise ResearchError('statement_page_out_of_range')
        return {k:row[k] for k in ('evidence_id','statement','period','scope','pages','source_url')} | {
            'research_id':research_id,'snapshot_id':s['snapshot_id'],'content':chunks[page-1],
            'page':page,'next_page':page+1 if page<len(chunks) else None,'original_hash_verified':True}

    def prepare_statements(self,research_id:str,document_sources:list[dict]):
        """Index verified cached filings and resolve blank balance-sheet rows into a new snapshot candidate."""
        s,old,p=self.w.pack(research_id)
        manifest=read_json(old/'manifest.json')
        inherited=read_pack_outputs(old,manifest)
        statements=[]
        for source in document_sources:
            statements.extend(extract_statements(source,s['ticker'],s['as_of']))
        if len({(r['period'],r['statement']) for r in statements})!=len(statements):
            raise ResearchError('conflicting_statement_versions')
        resolutions=[]
        for m in p['metrics']:
            if m['state']!='pending' or m['metric_id'] not in LABELS:continue
            r=next((r for r in statements if r['period']==m['period'] and r['statement']=='balance_sheet'),None)
            if not r:continue
            hits=[x for x in r['target_rows'] if x['label']==LABELS[m['metric_id']]]
            if len(hits)!=1:continue
            h=hits[0];current=h['cells'][-2]
            # Literal blank status is not numeric zero, nor a claim of not-applicable.
            if current is not None and not current.strip():
                resolution={'metric_id':m['metric_id'],'period':m['period'],'period_type':m['period_type'],
                    'state':'disclosed_blank','evidence_id':r['evidence_id'],'page':h['page'],'source_cells':h['cells'],
                    'reason':'原始合并资产负债表该行本期金额留空；不填零'}
                m.update(state='disclosed_blank',reason=resolution['reason'],resolution=resolution)
                resolutions.append(resolution)
        unique={ (r['metric_id'],r['period'],r['period_type']):r for r in resolutions }
        p['statements']=statements;p['financial_cell_resolutions']=list(unique.values())
        for r in statements:
            p.setdefault('supplemental_evidence',[]).append({'evidence_id':r['evidence_id'],'title':r['period']+TITLES[r['statement']],
                'period':r['period'],'group':'C','content':r['content'],'excerpt':r['content'][:200],
                'original_path':r['path'],'original_sha256':r['sha256'],'source_url':r['source_url'],
                'locator':'pages:'+','.join(map(str,r['pages'])),'boundary':'完整合并报表原文；空白不自动作为零','numeric_admission':False})
        identity={'parent':s['snapshot_id'],'sources':document_sources,'version':'statements-v2',
                  'parent_manifest_sha256':sha(old/'manifest.json')}
        ident='lite-pack-'+digest(identity)[:24]
        p['pack_id']=ident
        coverage=read_json(old/'core-coverage.json')
        for rows in (coverage['requirements'],p['coverage_requirements']):
            for row in rows:
                match=next((m for m in p['metrics'] if m['requirement_id']==row['requirement_id']),None)
                if match and match['state']=='disclosed_blank':row.update(state='disclosed_blank',reason=match['reason'])
        coverage['counts']=dict(Counter(x['state'] for x in coverage['requirements']))
        md=(old/'core-pack.md').read_text('utf8').replace(s['snapshot_id'],ident)
        for label in LABELS.values():
            # Replace pending only when every pending occurrence for this metric was resolved.
            if not any(m['label']==label and m['state']=='pending' for m in p['metrics']):
                md='\n'.join(line.replace('待补','留空') if line.startswith('| '+label+' |') else line for line in md.splitlines())
        from .processing import audit_metrics
        p['processing_audit']=audit_metrics(p)
        p['processing_audit']['resolved_blank_cells']=len(unique)
        work=read_json(old/'next-work.json')
        work['financial_cell_resolutions']=list(unique.values())
        target=self.w.state/'processing-packs'/s['ticker']/s['as_of']/ident
        outputs={'core-pack.json':json.dumps(p,ensure_ascii=False,indent=2),'core-pack.md':md,
            'core-coverage.json':json.dumps(coverage,ensure_ascii=False,indent=2),
            'next-work.json':json.dumps(work,ensure_ascii=False,indent=2),
            'processing-audit.json':json.dumps(p['processing_audit'],ensure_ascii=False,indent=2)}
        from analysis.structured.research_lite import _count_tokens
        count=_count_tokens(md)
        if count['count']>p['token_budget']:raise ResearchError('statement_core_budget_exceeded')
        p['token_count']=count;outputs['core-pack.json']=json.dumps(p,ensure_ascii=False,indent=2)
        outputs=inherited | {name:text.encode('utf8') for name,text in outputs.items()}
        manifest.update(pack_id=ident,parent_snapshot_id=s['snapshot_id'],token_count=count,
            statement_sources=document_sources,pack_identity_hash=digest(identity),output_hashes=output_hashes(outputs))
        if target.exists():
            self.w._verify_pack(target)
            if read_json(target/'manifest.json')['output_hashes']!=manifest['output_hashes']:
                raise ResearchError('immutable_statement_pack_conflict')
        else:
            target.mkdir(parents=True)
            for name,content in outputs.items():
                destination=target/name
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(content)
            (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
        return self.w.artifact(research_id,'snapshot_candidate',{'candidate_pack_path':str(target),'candidate_snapshot_id':ident,
            'build_status':'ready','reason':'三类财务空值核对及完整三大报表目录；原表留空不填零',
            'resolved_cells':len(unique),'statement_count':len(statements)})
