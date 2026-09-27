import pytest
from analysis.research.filing_checks import parse_page,enrich
from analysis.research.availability import reconcile,resolved_view
from analysis.research.workspace import sha,ResearchError


def test_checked_absence_and_scope():
    text='(二) 担保情况\n□适用 √不适用\n九、重大诉讼、仲裁事项\n□本年度公司有重大诉讼、仲裁事项 √本年度公司无重大诉讼、仲裁事项\n'
    rows=parse_page(text)
    assert {r['dataset_id'] for r in rows}=={'guarantee','litigation'}
    assert '普通案件' in next(r for r in rows if r['dataset_id']=='litigation')['scope']


@pytest.mark.parametrize('text',[
 '三、违规担保情况\n□适用 √不适用',
 '(4).关联担保情况\n□适用 √不适用',
 '(二) 担保情况\n√适用 □不适用',
 '九、重大诉讼、仲裁事项\n√本年度公司有重大诉讼、仲裁事项 □本年度公司无重大诉讼、仲裁事项',
 '九、重大诉讼、仲裁事项\n详见下一章节\n□本年度公司有重大诉讼、仲裁事项 √本年度公司无重大诉讼、仲裁事项',
 '本公司说明担保情况\n□适用 √不适用',
 '(二) 担保情况\n□适用 □不适用'])
def test_no_inferred_absence(text):assert parse_page(text)==[]


def test_pdf_period_hash_routes_and_idempotence(tmp_path):
    import fitz
    from copy import deepcopy
    path=tmp_path/'annual.pdf'
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((30,40),'2025 年年度报告',fontname='china-s')
        page.insert_text((30,70),'(二) 担保情况',fontname='china-s')
        # Checkbox glyphs are tested above; inject extraction for this PDF identity test.
        doc.save(path)
    from unittest.mock import patch
    statement={'evidence_id':'s','period':'2025-12-31','sha256':sha(path),'path':str(path)}
    pack={'statements':[statement],'metrics':[]}
    data={'items':[{'material_id':'g','reader':'missing','status':'missing','period':[], 'title':'guarantee', 'payload':{'dataset_id':'guarantee'}}]}
    found=({'dataset_id':'guarantee','finding':'不适用','scope':'年度','excerpt':'checkbox','state':'issuer_disclosed_none_or_not_applicable','page':1},)
    with patch('analysis.research.filing_checks._extract',return_value=found):
        d=enrich(deepcopy(data),pack);assert enrich(deepcopy(d),pack)==d
        r=reconcile(d,pack)['items'][0]
        assert r['period']==['2025-12-31'] and r['status']=='readable'
        assert resolved_view(r,'2024-12-31')['status']=='missing'
        path.write_bytes(b'changed')
        with pytest.raises(ResearchError,match='original_changed'):enrich(deepcopy(data),pack)


def test_cover_period_guard(tmp_path):
    import fitz
    from analysis.research.filing_checks import _extract
    path=tmp_path/'half.pdf'
    with fitz.open() as doc:
        p=doc.new_page();p.insert_text((30,40),'2025 年半年度报告',fontname='china-s');doc.save(path)
    assert _extract(str(path),sha(path),'2025-12-31')==()


def test_halfyear_and_older_headings():
    text='2\n报告期内履行的及尚未履行完毕的重大担保情况\n□适用√不适用\n七、重大诉讼、仲裁事项\n□本报告期公司有重大诉讼、仲裁事项√本报告期公司无重大诉讼、仲裁事项\n一、企业债券、公司债券和非金融企业债务融资工具\n□适用√不适用\n'
    rows=parse_page(text)
    assert {r['dataset_id'] for r in rows}=={'guarantee','litigation','bond_issuance'}
    assert all('普通案件' in r['scope'] for r in rows if r['dataset_id']=='litigation')


def test_shareholder_unknown_is_readable_not_no_pledge():
    rows=parse_page('股东名称\n质押、标记\n或冻结情况\n甲公司\n无\n乙公司\n未知')
    assert len(rows)==1 and rows[0]['dataset_id']=='pledge'
    assert rows[0]['state']=='original_readable' and '未知' in rows[0]['excerpt']


def test_positive_governance_is_not_discarded():
    rows=parse_page('八、上市公司及其董事、高级管理人员涉嫌违法违规及整改情况\n√适用□不适用\n详见留置公告。\n九、诚信状况\n良好')
    assert rows[0]['state']=='original_readable' and '留置' in rows[0]['excerpt']
    assert '诚信' not in rows[0]['excerpt']
