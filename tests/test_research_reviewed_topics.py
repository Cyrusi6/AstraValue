import json
from datetime import datetime, timezone

import pytest

from analysis.research.catalog import Catalog
from analysis.research.workspace import ResearchError, sha
from test_research_workspace import workspace


def attach_review(workspace, status='verified_none', available_at='2024-12-31',
                  decision_basis='user_confirmed'):
    pack = workspace.root / 'packs/600519/2025-01-01/lite-pack-test'
    original = workspace.root / 'filing.txt'
    original.write_text('主要股东明确披露无质押、标记或冻结。', encoding='utf8')
    core = json.loads((pack / 'core-pack.json').read_text('utf8'))
    core['evidence'] = [{'evidence_id': 'official-pledge', 'group': 'D', 'period': '2024-12-31',
                         'original_path': str(original), 'original_sha256': sha(original),
                         'locator': 'page:1', 'excerpt': original.read_text('utf8')}]
    core['deferred_topic_resolutions'] = [{
        'record_id': 'reviewed-pledge', 'dataset_id': 'pledge', 'status': status,
        'available_at': available_at + 'T00:00:00Z', 'source_projection_role': 'reviewed_filing',
        'decision_basis': decision_basis, 'consumption_status': 'ready',
        'period': '2024-12-31', 'covered_periods': ['2024-12-31'],
        'conclusion_text': '主要股东明确披露无质押、标记或冻结。',
        'fields': {'PLEDGED_SHARES': 0}, 'evidence_ids': ['official-pledge'],
    }]
    (pack / 'core-pack.json').write_text(json.dumps(core, ensure_ascii=False), encoding='utf8')
    manifest = json.loads((pack / 'manifest.json').read_text('utf8'))
    manifest['output_hashes']['core-pack.json'] = sha(pack / 'core-pack.json')
    (pack / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    return original


def test_verified_primary_reply_is_readable_and_original_checked(workspace):
    original = attach_review(workspace, decision_basis='primary_source_verified')
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    catalog = Catalog(workspace)
    _, data = catalog._load(rid)
    reviewed = next(r for r in data['items'] if r['reader'] == 'filing_check')
    result = catalog.read_material(rid, reviewed['material_id'])
    assert json.loads(result['content'])['decision_basis'] == 'primary_source_verified'
    original.write_text('changed', encoding='utf8')
    with pytest.raises(ResearchError, match='reviewed_topic_original_changed'):
        Catalog(workspace)._load(rid)


@pytest.mark.parametrize('basis', ['unreviewed', 'api_empty', None])
def test_unverified_decision_basis_is_not_consumed(workspace, basis):
    attach_review(workspace, decision_basis=basis)
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    _, data = Catalog(workspace)._load(rid)
    assert not any(r['reader'] == 'filing_check' for r in data['items'])


def test_adopted_review_is_readable_and_survives_catalog_refresh(workspace):
    attach_review(workspace)
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    catalog = Catalog(workspace)
    for _ in range(2):
        _, data = catalog._load(rid)
        parent = next(r for r in data['items'] if r['reader'] == 'missing'
                      and r['payload']['dataset_id'] == 'pledge')
        result = catalog.read_material(rid, parent['material_id'])
        assert result['status'] == 'verified_none'
        route = json.loads(result['content'])['materials'][0]
        content = catalog.read_material(rid, route['read_entry']['material_id'])
        assert json.loads(content['content'])['fields']['PLEDGED_SHARES'] == 0
        assert '无质押' in content['content']
        catalog.refresh_catalog(rid)


def test_reviewed_original_is_checked_on_read(workspace):
    original = attach_review(workspace)
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    catalog = Catalog(workspace)
    catalog._load(rid)
    original.write_text('changed', encoding='utf8')
    with pytest.raises(ResearchError, match='reviewed_topic_original_changed'):
        catalog._load(rid)


@pytest.mark.parametrize('basis', ['user_confirmed', 'primary_source_verified'])
def test_after_cutoff_review_is_not_consumed(workspace, basis):
    attach_review(workspace, available_at='2025-01-02', decision_basis=basis)
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    _, data = Catalog(workspace)._load(rid)
    assert not any(r['reader'] == 'filing_check' for r in data['items'])


def test_undisclosed_review_remains_a_readable_disclosure_status(workspace):
    attach_review(workspace, status='not_disclosed')
    rid = workspace.prepare_research('600519', '2025-01-01')['research_id']
    _, data = Catalog(workspace)._load(rid)
    reviewed = next(r for r in data['items'] if r['reader'] == 'filing_check')
    assert reviewed['payload']['state'] == 'original_readable'
    assert reviewed['payload']['status'] == 'not_disclosed'


def test_note_review_keeps_material_and_claim_categories_and_source_type(workspace):
    from analysis.structured.report_semantics import semantic_inputs

    attach_review(workspace)
    pack=workspace.root/'packs/600519/2025-01-01/lite-pack-test'
    core=json.loads((pack/'core-pack.json').read_text('utf8'))
    row=core['deferred_topic_resolutions'][0]
    row.update(dataset_id='financial_notes',material_category='notes',claim_category='quality')
    core['evidence'][0].update(source_type='supplier-structured',published_at='2024-12-31T00:00:00Z',
                               retrieved_at='2025-01-03T00:00:00Z')
    core['supplemental_evidence']=core.pop('evidence')
    core['governance']=[row]
    (pack/'core-pack.json').write_text(json.dumps(core,ensure_ascii=False),encoding='utf8')
    manifest=json.loads((pack/'manifest.json').read_text('utf8'))
    manifest['output_hashes']['core-pack.json']=sha(pack/'core-pack.json')
    (pack/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    _,data=Catalog(workspace)._load(rid)
    assert next(r for r in data['items'] if r['reader']=='filing_check')['category']=='notes'
    semantic=semantic_inputs(pack,{'ticker':'600519','periods':{'annual':[]}},core,
                             datetime(2025,1,1,tzinfo=timezone.utc),lambda _:[])
    claim=next(r for r in semantic['claims'] if '无质押' in r.text)
    assert claim.category=='quality'
    source=next(s for s in semantic['sources'] if s.source_id in claim.evidence_source_ids)
    assert source.source_type=='supplier-structured'
    assert source.retrieved_at==datetime(2025,1,3,tzinfo=timezone.utc)


def test_reviewed_report_projection_preserves_filing_provenance(workspace):
    from analysis.structured.report_semantics import semantic_inputs

    original = attach_review(workspace)
    pack = workspace.root / 'packs/600519/2025-01-01/lite-pack-test'
    core = json.loads((pack / 'core-pack.json').read_text('utf8'))
    core['supplemental_evidence'] = core.pop('evidence')
    core['supplemental_evidence'][0].update(published_at='2024-01-01T00:00:00Z',
                                          retrieved_at='2025-02-01T00:00:00Z')
    core['governance'] = core['deferred_topic_resolutions']
    manifest = {'ticker': '600519', 'periods': {'annual': []}}
    result = semantic_inputs(pack, manifest, core, datetime(2025, 1, 1, tzinfo=timezone.utc), lambda _: [])
    reviewed = next(r for r in result['claims'] if '无质押' in r.text)
    source = next(r for r in result['sources'] if r.source_id in reviewed.evidence_source_ids)
    assert source.source_type == 'issuer-filing'
    assert source.document_hash == sha(original)
    assert source.published_at == source.available_at == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert source.retrieved_at == datetime(2025, 2, 1, tzinfo=timezone.utc)
    assert reviewed.evidence_ids == ['official-pledge']
    original.write_text('changed', encoding='utf8')
    with pytest.raises(ValueError, match='report_reviewed_original_changed'):
        semantic_inputs(pack, manifest, core, datetime(2025, 1, 1, tzinfo=timezone.utc), lambda _: [])


@pytest.mark.parametrize('dataset', ['pledge', 'goodwill'])
def test_reviewed_routes_preserve_other_periods(dataset, tmp_path):
    from analysis.research.availability import reconcile

    def check(period, reviewed):
        return {'material_id': period, 'reader': 'filing_check' if reviewed else 'api_record',
                'status': 'readable', 'period': period, 'payload': {
                    'dataset_id': dataset, 'state': 'issuer_disclosed_none_or_not_applicable' if reviewed else 'api_records_readable',
                    'source_projection_role': 'reviewed_filing' if reviewed else 'api',
                    'finding': period, 'scope': period}}

    data = {'items': [
        {'material_id': 'parent', 'reader': 'missing', 'status': 'missing', 'period': [],
         'payload': {'dataset_id': dataset}},
        check('2024-12-31', True), check('2025-03-31', False)]}
    pack = {'metrics': []}
    if dataset == 'goodwill':
        original = tmp_path / 'blank.txt'
        original.write_text('商誉栏为空白', encoding='utf8')
        pack['statements'] = [{'evidence_id': 'blank', 'period': '2026-06-30',
                               'statement': 'balance_sheet', 'path': str(original), 'sha256': sha(original)}]
        pack['financial_cell_resolutions'] = [{'evidence_id': 'blank', 'period': '2026-06-30',
                                               'state': 'disclosed_blank', 'metric_id': 'goodwill', 'page': 1}]
        metric = {'metric_id': 'goodwill', 'period': '2023-12-31', 'state': 'ready',
                  'period_type': 'instant', 'fact_ref': 'F1',
                  'fact': {'fact_id': 'g1', 'value': 0, 'unit': 'CNY'}}
        pack['metrics'] = [metric]
        data['items'].append({'material_id': 'metric', 'reader': 'metrics', 'status': 'readable',
                              'period': ['2023-12-31'], 'payload': [metric]})
    result = reconcile(data, pack, {dataset: {'g1'}})['items'][0]
    assert {'2024-12-31', '2025-03-31'} <= set(result['period'])
    if dataset == 'goodwill':
        assert '2023-12-31' in result['period']
        assert '2026-06-30' in result['period']
