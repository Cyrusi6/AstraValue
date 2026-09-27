from analysis.research.note_tables import expand_headers, header_size, tabulate, caption
from analysis.research.topics import matrix,topic_content
import pytest


def test_multilevel_headers_do_not_copy_ratio_into_carrying_value():
    rows=[['类别','期末余额',None,None,None,None,'期初余额',None,None,None,None],
          [None,'账面余额',None,'坏账准备',None,'账面价值','账面余额',None,'坏账准备',None,'账面价值'],
          [None,'金额','比例(%)','金额','计提比例(%)',None,'金额','比例(%)','金额','计提比例(%)',None],
          ['组合','10','100','1','10','9','8','100','0','0','8']]
    size=header_size(rows);headers=expand_headers(rows[:size])
    assert size==3
    assert headers[5]=='期末余额 / 账面价值'
    assert headers[10]=='期初余额 / 账面价值'
    cells,issues,_=tabulate(rows[size:],headers,'应收账款','分类','2025-06-30','元','ref',1,currency='CNY')
    assert not issues
    assert cells[1]['unit']=='%' and cells[1]['currency'] is None
    assert cells[4]['value']=='9' and cells[4]['unit']=='元'


def test_asset_movement_keeps_increase_decrease_and_continuation_context():
    headers=['项目','房屋','合计']
    rows=[['一、账面原值',None,None],['2.本期增加金额','5','5'],['（1）外币折算','1','1'],
          ['3.本期减少金额','3','3'],['（1）外币折算','2','2']]
    cells,issues,context=tabulate(rows,headers,'固定资产','情况','2025-12-31','元','ref',1)
    assert not issues
    labels={c['label'] for c in cells}
    assert '账面原值 / 本期增加金额 / 外币折算' in labels
    assert '账面原值 / 本期减少金额 / 外币折算' in labels
    more,issues,_=tabulate([['4.期末余额','20','20']],headers,'固定资产','情况','2025-12-31','元','ref',2,context)
    assert more[0]['label']=='账面原值 / 期末余额'
    assert more[0]['period_type']=='filing_stock_column'


def test_captions_separate_cash_received_and_paid():
    assert caption('(1)\n收到其他与经营活动有关的现金\n√适用□不适用\n单位：元','现金')=='收到其他与经营活动有关的现金'
    assert caption('(2)支付其他与经营活动有关的现金\n单位：元','现金')=='支付其他与经营活动有关的现金'
    assert caption('其他应付款\n项目列示\n单位：元','其他应付款')=='项目列示'
    assert caption('应付股利\n单位：元','其他应付款')=='应付股利'


def test_blank_zero_text_and_comparative_columns_are_preserved():
    cells,issues,_=tabulate([['材料','','0','正在办理中']],['项目','期末余额','期初余额','原因'],
                           '资产','明细','2025-12-31','元','ref',1)
    assert not issues
    assert [(c['value'],c['state']) for c in cells]==[(None,'disclosed_blank'),('0','ready'),('正在办理中','disclosed_text')]
    assert cells[2]['unit']=='原文文字'


def test_topic_subtable_selection_does_not_mix_other_sections():
    first,_,_=tabulate([['合计','10']],['项目','金额'],'现金','收到','2025-12-31','元','s',1)
    second,_,_=tabulate([['合计','7']],['项目','金额'],'现金','支付','2025-12-31','元','s',2)
    root={'title':'现金','material_id':'topic','payload':{'children':[],'cells':[], 'table_cells':first+second}}
    preview=topic_content(root,{'items':[]})
    assert len(preview['table_index'])==2
    assert preview['tables'][0]['rows'][0]['values']==['10']
    selected=topic_content(root,{'items':[]},table_id=preview['table_index'][1]['table_id'])
    assert selected['tables'][0]['rows'][0]['values']==['7']


def test_real_pdf_continuation_and_cell_locations(tmp_path):
    import fitz
    from analysis.research.note_tables import extract_tables
    from analysis.research.workspace import sha
    path=tmp_path/'continuation.pdf'
    with fitz.open() as doc:
        for index,rows in enumerate(([['项目','期末余额','期初余额'],['材料','10.00','8.00']],
                                    [['商品','20.00','18.00'],['合计','30.00','26.00']])):
            page=doc.new_page()
            if index==0:
                page.insert_text((40,40),'(1) 资产情况',fontname='china-s',fontsize=10)
                page.insert_text((40,65),'单位：元 币种：人民币',fontname='china-s',fontsize=10)
            for y in (100,140,180):page.draw_line((40,y),(400,y))
            for x in (40,160,280,400):page.draw_line((x,100),(x,180))
            for row_no,row in enumerate(rows):
                for col,text in enumerate(row):
                    page.insert_text((45+col*120,125+row_no*40),text,fontname='china-s',fontsize=10)
        doc.save(path)
    item={'period':'2025-12-31','material_id':'original',
          'payload':{'path':str(path),'sha256':sha(path),'page':1,'end_page':2,'start_y':0,'end_y':200}}
    cells,summary=extract_tables(item,'资产')
    assert summary['processed_tables']==summary['detected_tables']==1
    assert summary['tables'][0]['pages']==[1,2]
    assert len(cells)==6 and {c['label'] for c in cells}=={'材料','商品','合计'}
    assert all(c['currency']=='CNY' for c in cells)
    with fitz.open(path) as doc:
        for cell in cells:
            loc=cell['locator']
            assert cell['value'] in doc[loc['page']-1].get_textbox(fitz.Rect(loc['bbox']))


@pytest.mark.parametrize('pages,expected',[
    ([[['名称','期末余额']],[['','账面余额','减值准备','账面价值'],['材料','10','1','9']]],3),
    ([[['项目','期末余额','期初余额'],['材料','10','8']],[['','',''],['商品','20','18']]],4),
])
def test_split_parent_header_and_blank_continuation_header(tmp_path,pages,expected):
    import fitz
    from analysis.research.note_tables import extract_tables
    from analysis.research.workspace import sha
    path=tmp_path/'split.pdf'
    with fitz.open() as doc:
        for index,rows in enumerate(pages):
            page=doc.new_page();width=360/len(rows[0])
            if index==0:page.insert_text((40,60),'(1) 分类情况\n单位：元 币种：人民币',fontname='china-s',fontsize=10)
            for row in range(len(rows)+1):page.draw_line((40,100+row*40),(400,100+row*40))
            for col in range(len(rows[0])+1):page.draw_line((40+col*width,100),(40+col*width,100+len(rows)*40))
            for row_no,row in enumerate(rows):
                for col,text in enumerate(row):
                    if text:page.insert_text((45+col*width,125+row_no*40),text,fontname='china-s',fontsize=10)
        doc.save(path)
    item={'period':'2025-12-31','material_id':'original',
          'payload':{'path':str(path),'sha256':sha(path),'page':1,'end_page':2,'start_y':0,'end_y':200}}
    cells,summary=extract_tables(item,'资产')
    assert len(cells)==expected
    assert summary['state']=='detected_tables_processed' and not summary['pending']
    assert all('期末余额' in c['column'] or '期初余额' in c['column'] for c in cells)
