"""Registered note formulas; reading tables alone does not admit numeric inputs."""
from decimal import Decimal
from pathlib import Path
import re

from .workspace import ResearchError, sha, digest

METHODS={'inventory_composition','inventory_allowance_ratio'}


def compute(method, cells):
    """Calculate only a unique, complete consolidated inventory classification."""
    if not cells:raise ResearchError('note_inputs_missing')
    if len({(c['period'],c['ref'],c['scope'],c['unit'],c['currency'],c['period_type']) for c in cells})!=1:
        raise ResearchError('note_input_definition_mismatch')
    if cells[0]['scope']!='consolidated' or cells[0]['currency']!='CNY' or cells[0]['unit'] not in {'元','千元','万元','亿元'} or cells[0]['period_type']!='filing_stock_column':
        raise ResearchError('note_input_definition_mismatch')
    selected={}
    for c in cells:
        key=(c['label'],c['column'])
        if key in selected:raise ResearchError('ambiguous_note_input')
        if c['state']!='ready' or c['value'] is None:raise ResearchError('note_numeric_input_missing:'+c['label'])
        value=Decimal(c['value'])
        if not value.is_finite() or value<0:raise ResearchError('invalid_note_amount')
        selected[key]=value
    if method=='inventory_composition':
        rows={label:v for (label,col),v in selected.items() if col=='期末余额 / 账面价值'}
        total=rows.pop('合计',None)
        if total is None or not rows:raise ResearchError('note_total_missing')
        if total<=0:raise ResearchError('nonpositive_denominator')
        # At most half a cent of source-unit rounding per printed component.
        if abs(sum(rows.values())-total)>Decimal('0.005')*(len(rows)+1):raise ResearchError('note_components_do_not_sum')
        result={'unit':'ratio','components':[{'label':k,'value':str(v/total)} for k,v in rows.items()],
                'formula':'各分类期末账面价值 / 存货期末账面价值合计'}
        if {'在产品','自制半成品'}<=rows.keys():
            result.update(value=str((rows['在产品']+rows['自制半成品'])/total),
                          value_label='在产品及自制半成品占比',
                          value_formula='(在产品期末账面价值 + 自制半成品期末账面价值) / 存货期末账面价值合计')
        else:result['production_share_status']='所需分类未分别披露；未将缺少类别视为零'
        return result
    if method!='inventory_allowance_ratio':raise ResearchError('unsupported_note_formula')
    amounts={col:v for (label,col),v in selected.items() if label=='合计'}
    gross=amounts.get('期末余额 / 账面余额');net=amounts.get('期末余额 / 账面价值')
    reserves=[v for col,v in amounts.items() if col.startswith('期末余额 / ') and '准备' in col]
    if gross is None or net is None or len(reserves)!=1:raise ResearchError('note_total_missing')
    reserve=reserves[0]
    if gross<=0:raise ResearchError('nonpositive_denominator')
    if abs(gross-reserve-net)>Decimal('0.015'):raise ResearchError('note_gross_net_mismatch')
    return {'value':str(reserve/gross),'unit':'ratio','value_label':'存货减值准备率',
            'formula':'存货期末减值准备合计 / 存货期末账面余额合计'}


def verify_cells(cells, originals):
    """Re-read PDF positions, independently of stored table extraction."""
    import fitz
    from contextlib import ExitStack
    with ExitStack() as stack:
        docs={}
        for c in cells:
            source=originals[c['ref']]['payload'];path=source['path']
            if path not in docs:
                if sha(Path(path))!=source['sha256']:raise ResearchError('material_original_changed')
                docs[path]=stack.enter_context(fitz.open(path))
            loc=c['locator']
            if not loc.get('bbox'):raise ResearchError('note_cell_locator_missing')
            text=docs[path][loc['page']-1].get_textpage().extractTextbox(fitz.Rect(loc['bbox']))
            normalized=re.sub(r'\s+','',text).replace(',','').replace('，','')
            try:matches=Decimal(normalized)==Decimal(c['value'])
            except Exception:matches=False
            if not matches:raise ResearchError('note_source_value_mismatch')


def calculate_note(w,rid,method,bindings,assumptions):
    from .catalog import Catalog
    if set(bindings)!={'period'} or assumptions:raise ResearchError('note_calculation_requires_period_only')
    period=bindings['period']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',period):raise ResearchError('invalid_note_period')
    state,data=Catalog(w)._load(rid)
    topics=[r for r in data['items'] if r['reader']=='topic' and r['title']=='存货构成与减值']
    if len(topics)!=1:raise ResearchError('note_topic_missing_or_ambiguous')
    cells=[c for c in topics[0]['payload'].get('table_cells',[]) if c['period']==period and c['section']=='存货分类'
           and c['column'].startswith('期末余额 / ')]
    if method=='inventory_composition':cells=[c for c in cells if c['column']=='期末余额 / 账面价值']
    else:cells=[c for c in cells if c['label']=='合计']
    result=compute(method,cells)
    originals={r['material_id']:r for r in data['items']}
    verify_cells(cells,originals)
    sources={c['ref']:originals[c['ref']]['payload']['sha256'] for c in cells}
    record=w.artifact(rid,'calculation',{'method':method,'formula_version':method+'-v1',
        'bindings':bindings,'assumptions':{},'result':result,'input_fact_ids':{},
        'input_definitions':cells,'source_hashes':sources,'catalog_view_hash':digest(data),
        'validation':'source_hash_and_pdf_cells_and_formula_checks','status':'ready'})
    if 'value' in result:record['value_reference']='{{value:'+record['artifact_id']+'.value}}'
    return record
