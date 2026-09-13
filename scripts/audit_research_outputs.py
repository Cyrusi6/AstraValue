"""独立检查交付文件的来源哈希、计算输入和重复执行字节稳定性。"""
from __future__ import annotations
import argparse
import json
from decimal import Decimal
from pathlib import Path

from analysis.structured.materialization_replay import _hash_file
from analysis.structured.research import read_jsonl, write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare',type=Path)
    parser.add_argument('--audit-file',type=Path,required=True)
    args=parser.parse_args()
    fingerprints={}; companies=[]
    for summary_path in sorted(args.output.glob('*/question-coverage-summary.json')):
        folder=summary_path.parent
        facts={f.get('fact_id',f.get('dimensional_fact_id')):f for f in read_jsonl(folder/'coverage-facts.jsonl')}
        checked=0
        for computed in read_jsonl(folder/'computed-facts.jsonl'):
            inputs=[facts[key] for key in computed['derived_from_fact_ids']]
            if len(inputs)!=2:raise ValueError('computed_indicator_input_count')
            if any((f['period_start'],f['period_end'],f['period_type'],f['scope'],f['currency'])!=
                (computed['period_start'],computed['period_end'],computed['period_type'],computed['scope'],computed['currency']) for f in inputs):
                raise ValueError('computed_indicator_scope_mismatch')
            values=[Decimal(f['metadata']['decimal_value']) for f in inputs]
            expected=values[0]-abs(values[1]) if computed['metric_id']=='operating_cash_flow_less_asset_purchase_proxy' else abs(values[0])/values[1] if computed['metric_id']=='capex_to_revenue' else values[0]/values[1]
            if expected!=Decimal(computed['metadata']['decimal_value']):raise ValueError('independent_indicator_mismatch')
            checked+=1
        for name in ('coverage-facts.jsonl','computed-facts.jsonl','question-coverage.jsonl','gaps.jsonl','normalized-records.jsonl','next-work.json'):
            file=folder/name
            if file.exists():fingerprints[str(file.relative_to(args.output))]=_hash_file(file)
        for row in read_jsonl(folder/'question-coverage.jsonl'):
            if any(fid not in facts for fid in row['fact_ids']):raise ValueError('coverage_dangling_fact')
            if any(facts[fid].get('derived_from_fact_ids') for fid in row['fact_ids']):raise ValueError('raw_requirement_filled_by_derived_period')
        summary=json.loads(summary_path.read_text(encoding='utf8'))
        companies.append({'ticker':folder.name,'checked_computed':checked,'selected_facts':len(facts),
            'states':summary['states'],'coverage_rows':summary['period_inputs']})
    originals={}
    manifests=[args.output/'live-documents.json',*sorted((args.output/'selected-documents').glob('*.json'))]
    for manifest in manifests:
        if not manifest.exists():continue
        for document in json.loads(manifest.read_text(encoding='utf8'))['documents']:
            if document.get('parse_status')!='parsed':continue
            path=Path(document['original_path']);sha=_hash_file(path)
            if sha!=document['original_sha256']:raise ValueError('original_document_hash_changed')
            originals[sha]=str(path)
    official=0
    for fact in read_jsonl(args.output/'official-table-facts.jsonl'):
        if Decimal(fact['original_value'])*Decimal(fact['multiplier'])!=Decimal(fact['value']):raise ValueError('official_table_conversion_mismatch')
        if fact['original_sha256'] not in originals:raise ValueError('official_table_original_missing')
        official+=1
    if args.compare:
        prior=json.loads(args.compare.read_text(encoding='utf8'))
        if prior['fingerprints']!=fingerprints:raise ValueError('repeat_output_fingerprints_changed')
        if prior['originals']!=originals:raise ValueError('repeat_original_set_changed')
    report={'companies':companies,'fingerprints':fingerprints,'originals':originals,
        'verified_originals':len(originals),'verified_official_values':official,
        'repeat_equal':bool(args.compare),'network_requests':0}
    write_json(args.audit_file,report)
    print(json.dumps({'companies':len(companies),'independent_computations':sum(c['checked_computed'] for c in companies),
        'originals':len(originals),'official_values':official,'repeat_equal':bool(args.compare)},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
