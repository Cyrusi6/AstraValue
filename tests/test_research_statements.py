import json
import os
from pathlib import Path
import pytest
from analysis.research.statements import Statements, extract_statements
from analysis.research.workspace import ResearchError, sha


class Workspace:
    def __init__(self,row): self.row=row
    def pack(self,rid):return {'snapshot_id':'S'},None,{'statements':[self.row]}


def test_full_statement_pagination_and_source_integrity(tmp_path):
    p=tmp_path/'source';p.write_text('original')
    row={'evidence_id':'e','statement':'balance_sheet','period':'2025-12-31','scope':'consolidated','pages':[1,2],
        'source_url':'https://example.com/a.pdf','path':str(p),'sha256':sha(p),'content':'资产负债表正文\n'*1000}
    tools=Statements(Workspace(row));chunks=[];page=1
    while page:
        r=tools.read_statement('r','balance_sheet','2025-12-31',page,100)
        chunks.append(r['content']);page=r['next_page']
    assert ''.join(chunks).replace('\n','') == row['content'].replace('\n','')
    assert tools.statement_catalog('r')['statements'][0]['pages']==[1,2]
    p.write_text('tampered')
    with pytest.raises(ResearchError,match='original_changed'):
        tools.read_statement('r','balance_sheet','2025-12-31')


@pytest.mark.skipif(os.getenv('ASTRAVALUE_REAL_CACHE_TEST')!='1',reason='local cached filings')
def test_real_statement_scope_and_original_blank_rows():
    root=Path(__file__).resolve().parents[1]
    docs=json.loads((root/'tmp/statement-completion/documents.json').read_text('utf8'))
    row=next(r for r in docs if r['period']=='2021-12-31')
    result=extract_statements(row,'600519','2026-09-13')
    assert len(result)==3
    balance=next(r for r in result if r['statement']=='balance_sheet')
    assert '母公司资产负债表' not in balance['content']
    assert len(balance['target_rows'])==3
    assert all(r['cells'][-2]=='' for r in balance['target_rows'])
    with pytest.raises(ResearchError,match='company_mismatch'):
        extract_statements(row,'000858','2026-09-13')
    with pytest.raises(ResearchError,match='date_or_hash'):
        extract_statements(row,'600519','2021-12-31')
