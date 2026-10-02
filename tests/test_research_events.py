import json
from datetime import datetime, timezone

import pytest

from test_research_workspace import workspace
from analysis.models import EventRecord
from analysis.research.catalog import Catalog
from analysis.research.drafts import Drafts
from analysis.research.workspace import ResearchError, sha


def event_record(ident, **updates):
    now = datetime(2024, 6, 26, tzinfo=timezone.utc)
    data = dict(event_id=ident, ticker='600519', event_type='dividend',
        event_subtype='cash_dividend_plan_terms', announced_at=now, available_at=now,
        period_end='2023-12-31', lifecycle_state='announced_plan', status_updated_at=now,
        summary='现金分红方案 '+ident, source_ids=['source-dividend'], data_snapshot_id='input-snapshot',
        event_terms={'cash_dividend_per_share_plan':1.3, 'nature':'announced_plan','unit':'CNY_per_share'},
        metadata={'structured_dataset_id':'dividend','original_value':13,'original_unit':'CNY_per_10_shares',
                  'multiplier':'0.1','decimal_value':'1.3','original_lifecycle':'实施分配',
                  'structured_snapshot_id':'response-snapshot','structured_field_path':'$.PRETAX_BONUS_RMB'})
    data.update(updates)
    return EventRecord(**data).model_dump(mode='json')


def freeze_events(workspace, events):
    pack = workspace.root/'packs/600519/2025-01-01/lite-pack-test'
    body = json.loads((pack/'core-pack.json').read_text('utf8'))
    body['events'] = events
    (pack/'core-pack.json').write_text(json.dumps(body,ensure_ascii=False),encoding='utf8')
    manifest = json.loads((pack/'manifest.json').read_text('utf8'))
    manifest['output_hashes']['core-pack.json'] = sha(pack/'core-pack.json')
    (pack/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    return pack


def test_current_event_schema_is_readable_by_existing_topics_and_bounded_pages(workspace):
    records = [event_record('plan'), event_record('update',previous_event_id='plan',root_event_id='plan'),
               event_record('guarantee',event_type='guarantee',event_terms={},summary='担保披露')]
    freeze_events(workspace,records)
    rid = workspace.prepare_research('600519','2025-01-01')['research_id']
    first = workspace.query_research(rid,topic='capital',page_size=1)
    second = workspace.query_research(rid,topic='capital',page_size=1,page=2)
    assert first['total']==2 and first['next_page']==2 and second['next_page'] is None
    assert first['rows'][0]['event_id']=='plan' and first['rows'][0]['event']==records[0]
    assert second['rows'][0]['event']==records[1]
    assert first['rows'][0]['read_entry']=={'tool':'read_evidence','evidence_id':'plan'}
    assert workspace.query_research(rid,topic='governance')['total']==3
    assert workspace.query_research(rid,topic='evidence')['total']==3
    assert workspace.query_research(rid,topic='business')['total']==0
    with pytest.raises(ResearchError,match='page_size'):
        workspace.query_research(rid,topic='capital',page_size=41)


@pytest.mark.parametrize('cached_catalog',[False,True])
def test_event_catalog_reuses_evidence_reader_and_preserves_original_terms_for_citation(workspace,cached_catalog):
    record = event_record('plan')
    freeze_events(workspace,[record])
    state = workspace.prepare_research('600519','2025-01-01');rid=state['research_id']
    if cached_catalog:
        old=workspace.root/'eventless-catalog.json'
        old.write_text(json.dumps({'items':[],'audit_scope':{'projection_files':[]}}),encoding='utf8')
        old_hash=sha(old)
        workspace.artifact(rid,'material_catalog',{'path':str(old),'sha256':old_hash})
    catalog = Catalog(workspace)
    assert any(row['category']=='capital' for row in catalog.catalog_overview(rid)['categories'])
    if cached_catalog:assert sha(old)==old_hash
    item = catalog.list_materials(rid,'capital')['items'][0]
    assert item['status']=='original_readable'
    result = catalog.read_material(rid,item['material_id'])
    assert result['event_id']=='plan' and result['numeric_admission'] is False
    assert json.loads(result['content'])==record
    proof = next(row for row in workspace.artifacts(rid,'evidence_read') if row['artifact_id']==result['evidence_id'])
    assert proof['source_record']==record and proof['source_ids']==record['source_ids']
    assert proof['source_record']['lifecycle_state']=='announced_plan'
    assert proof['source_record']['metadata']['original_value']==13
    Drafts(workspace).save_section(rid,state['snapshot_id'],4,'原记录的分红方案。','按披露记录分析',
                                   [result['evidence_id']])
    chunks=[];page=1
    while page is not None:
        part=workspace.read_evidence(rid,'plan',page=page,max_tokens=100)
        chunks.append(part['content']);page=part['next_page']
    assert len(chunks)>1 and json.loads(''.join(chunks))==record


def test_events_keep_company_and_shanghai_cutoff_boundaries(workspace):
    future = event_record('future',available_at='2025-01-01T16:00:00Z')
    freeze_events(workspace,[future])
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    assert workspace.query_research(rid,topic='capital')['rows']==[]
    assert Catalog(workspace).list_materials(rid,'capital')['items']==[]


def test_event_company_mismatch_is_rejected(workspace):
    freeze_events(workspace,[event_record('other',ticker='000858')])
    rid=workspace.prepare_research('600519','2025-01-01')['research_id']
    with pytest.raises(ResearchError,match='event_company_mismatch'):
        workspace.query_research(rid,topic='governance')
