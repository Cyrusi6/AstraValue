"""User-only cache accounting and bounded read probes; never a model prompt."""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from analysis.research.catalog import Catalog
from analysis.research.briefing import Briefing
from analysis.research.workspace import ResearchWorkspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--research-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--no-refresh',action='store_true',help='Verify the already frozen catalog without rebuilding it')
    args=parser.parse_args()
    w=ResearchWorkspace(Path(__file__).resolve().parents[1]);api=Catalog(w)
    if not args.no_refresh:api.refresh_catalog(args.research_id)
    state,data=api._load(args.research_id)
    keys={r['source_key'] for r in data['ledger']};checks=[]
    for db in data['audit_scope']['registered_archive_databases']:
        with sqlite3.connect(Path(db).as_uri()+'?mode=ro',uri=True) as con:
            if not con.execute("select name from sqlite_master where name='raw_resource_snapshots'").fetchone():continue
            expected={r[0] for r in con.execute("select snapshot_id from raw_resource_snapshots where resource_role='content'")}
        assert expected<=keys
        checks.append({'cache':db,'expected_originals':len(expected),'unaccounted':len(expected-keys)})
    for path in data['audit_scope']['projection_files']:
        expected={path+':'+json.loads(line)['record_id'] for line in Path(path).read_text('utf8').splitlines() if line}
        assert expected<=keys
        checks.append({'cache':path,'expected_records':len(expected),'unaccounted':len(expected-keys)})
    probes=[];readers=set()
    for r in data['items']:
        if r['status']!='readable' or r['reader'] in readers:continue
        readers.add(r['reader']);result=api.read_material(args.research_id,r['material_id'],max_tokens=4000)
        assert result.get('content')
        probes.append({'reader':r['reader'],'material_id':r['material_id'],'read_ok':True})
    first=Briefing(w).get_research_brief(args.research_id)
    assert not any(x in json.dumps(first) for x in ('ledger','exclusion_reasons','outside_research_scope'))
    summary={'snapshot_id':state['snapshot_id'],'first_package_characters':len(json.dumps(first,ensure_ascii=False)),
        'categories':len(first['categories']),'top_level_items':sum(not r.get('parent_id') for r in data['items']),
        'field_items':sum(bool(r.get('parent_id')) for r in data['items']),
        'status_counts':dict(Counter(r['status_label'] for r in data['items'])),
        'cache_checks':checks,'read_probes':probes,
        'exclusion_reasons':dict(Counter(r['reason'] for r in data['ledger'] if r['decision']=='excluded'))}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'用户核验明细.json').write_text(json.dumps({'summary':summary,'scope':data['audit_scope'],'ledger':data['ledger']},ensure_ascii=False,indent=2),encoding='utf8')
    (args.output/'模型实际首包.json').write_text(json.dumps(first,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
