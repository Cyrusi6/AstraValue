from copy import deepcopy
from decimal import Decimal
import pytest
from analysis.research.availability import reconcile, resolved_view
from analysis.research.workspace import sha, ResearchError
from analysis.research.note_calculations import compute, verify_cells


def resolved_fixture(tmp_path):
    p=tmp_path/'original';p.write_text('原文',encoding='utf8')
    pack={'metrics':[], 'statements':[{'evidence_id':'s','period':'2025-12-31','statement':'balance_sheet','path':str(p),'sha256':sha(p)}],
          'financial_cell_resolutions':[{'evidence_id':'s','metric_id':'goodwill','period':'2025-12-31','state':'disclosed_blank','page':1}]}
    data={'items':[{'reader':'field','status':'processing','status_label':'已有待处理','period':['1998-12-31','2025-12-31'],
                    'title':'GOODWILL','payload':{'field':'GOODWILL'}}]}
    return data,pack,p


def test_resolution_is_readable_blank_not_zero_and_old_period_remains_pending(tmp_path):
    data,pack,_=resolved_fixture(tmp_path);d=reconcile(data,pack);r=d['items'][0]
    assert r['status']=='readable' and r['period']==['2025-12-31']
    assert resolved_view(r)['materials'][0]['value'] is None
    assert resolved_view(r,'1998-12-31')['status']=='processing'
    assert data['items'][0]['status']=='processing'
    assert reconcile(d,pack)==d
    pack['financial_cell_resolutions']=[]
    assert reconcile(d,pack)==data


@pytest.mark.parametrize('change',['period','missing','hash'])
def test_resolution_requires_matching_intact_source(tmp_path,change):
    data,pack,p=resolved_fixture(tmp_path)
    if change=='period':pack['statements'][0]['period']='2024-12-31'
    if change=='missing':pack['statements']=[]
    if change=='hash':
        p.write_text('changed')
        with pytest.raises(ResearchError,match='original_changed'):reconcile(data,pack)
    else:assert reconcile(data,pack)['items'][0]['status']=='processing'


def test_derived_route_is_limited_and_unrelated_missing_is_retained():
    metric={'metric_id':'net_profit','period':'2025-03-31','period_type':'single_quarter','state':'ready','fact':{'value':'1'}}
    data={'items':[{'reader':'metrics','payload':[metric],'material_id':'m'},
        {'reader':'missing','title':'income_quarter','status':'missing','period':[],'payload':{'dataset_id':'income_quarter'}},
        {'reader':'missing','title':'litigation','status':'missing','period':[],'payload':{'dataset_id':'litigation'}}]}
    r=reconcile(data,{'metrics':[metric]})['items']
    assert r[1]['period']==['2025-03-31']
    assert r[1]['availability']['routes'][0]['read_entry']['metric_ids']==['net_profit']
    assert r[2]['status']=='missing'


def cell(label,value,column='期末余额 / 账面价值'):
    return dict(label=label,value=value,column=column,period='2025-12-31',ref='source',scope='consolidated',unit='元',currency='CNY',period_type='filing_stock_column',state='ready')


def inventory():return [cell('原材料','10'),cell('在产品','20'),cell('自制半成品','30'),cell('合计','60')]


def test_inventory_formula_and_other_company_missing_category():
    r=compute('inventory_composition',inventory());assert Decimal(r['value'])==Decimal(50)/60
    rows=[cell('商品','60'),cell('合计','60')]
    r=compute('inventory_composition',rows)
    assert 'value' not in r and r['components'][0]['value']=='1'


@pytest.mark.parametrize('change,error',[('blank','numeric_input_missing'),('duplicate','ambiguous'),('sum','do_not_sum'),('unit','definition_mismatch'),('negative','invalid_note_amount'),('zero','nonpositive')])
def test_invalid_note_inputs_are_rejected(change,error):
    rows=inventory()
    if change=='blank':rows[0].update(value=None,state='disclosed_blank')
    if change=='duplicate':rows.append(deepcopy(rows[0]))
    if change=='sum':rows[0]['value']='11'
    if change=='unit':rows[0]['unit']='万元'
    if change=='negative':rows[0]['value']='-1'
    if change=='zero':rows[-1]['value']='0'
    with pytest.raises(ResearchError,match=error):compute('inventory_composition',rows)


def test_allowance_uses_gross_and_explicit_zero_only():
    rows=[cell('合计','100','期末余额 / 账面余额'),cell('合计','2','期末余额 / 减值准备'),cell('合计','98')]
    assert compute('inventory_allowance_ratio',rows)['value']=='0.02'
    rows[1]['value']='0';rows[2]['value']='100'
    assert compute('inventory_allowance_ratio',rows)['value']=='0'
    rows[1].update(value=None,state='disclosed_blank')
    with pytest.raises(ResearchError,match='numeric_input_missing'):compute('inventory_allowance_ratio',rows)


def test_pdf_cell_independently_verified_and_tamper_rejected(tmp_path):
    import fitz
    p=tmp_path/'source.pdf'
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((40,50),'123.45');doc.save(p)
    originals={'source':{'payload':{'path':str(p),'sha256':sha(p)}}}
    c=cell('合计','123.45');c['locator']={'page':1,'bbox':[35,35,100,60]}
    verify_cells([c],originals)
    c['value']='123.46'
    with pytest.raises(ResearchError,match='source_value_mismatch'):verify_cells([c],originals)
    p.write_bytes(b'changed')
    with pytest.raises(ResearchError,match='original_changed'):verify_cells([c],originals)


def test_small_nonzero_ratio_is_not_rendered_as_zero():
    from analysis.research.reports import format_ratio
    assert format_ratio(Decimal('0.0000209396020786483'))=='0.00209396%'
    assert format_ratio(Decimal('0'))=='0.00%'
    assert format_ratio(Decimal('0.8842309563559'))=='88.42%'
