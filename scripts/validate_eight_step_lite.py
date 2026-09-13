"""独立核验七家公司八步轻量核心包，不调用构建器实现函数。"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path


TICKERS = ("600519", "000858", "000568", "000596", "002304", "600809", "603369")
DERIVATIONS = {
    "gross_margin": "one_minus_ratio",
    "parent_net_margin": "ratio",
    "selling_expense_ratio": "ratio",
    "administrative_expense_ratio": "ratio",
    "rd_ratio": "ratio",
    "finance_expense_ratio": "ratio",
    "cash_profit_ratio": "ratio",
    "operating_cash_flow_less_asset_purchase_proxy": "difference",
}


def canonical_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path):
    if path.is_file():
        with path.open(encoding='utf8') as stream:
            for line in stream:
                if line.strip():yield json.loads(line)


def decimal_value(fact):
    return Decimal(str((fact.get('metadata') or {}).get('decimal_value',fact.get('value'))))


def load_pack(output_root,ticker,as_of):
    audit=json.loads((output_root/ticker/as_of/'last-run-audit.json').read_text(encoding='utf8'))
    pack=output_root/ticker/as_of/audit['pack_id']
    return pack,json.loads((pack/'manifest.json').read_text(encoding='utf8')),json.loads((pack/'core-pack.json').read_text(encoding='utf8')),json.loads((pack/'core-coverage.json').read_text(encoding='utf8'))


def source_fact_map(manifest):
    result={}
    for source in manifest['source_inputs']:
        item=source['files'].get('coverage-facts.jsonl')
        if not item:continue
        path=Path(item['path'])
        if file_hash(path)!=item['sha256']:raise AssertionError(f'source_projection_changed:{path}')
        for fact in read_jsonl(path):
            identity=fact.get('fact_id') or fact.get('dimensional_fact_id')
            if identity in result and result[identity]!=fact:raise AssertionError(f'immutable_fact_conflict:{identity}')
            result[identity]=fact
    return result


def verify_derived(payload,sources):
    checked=0

    def verify_fact(fact):
        identity=fact['fact_id']
        if identity.startswith('lite-derived-'):
            left,right=(sources[source_id] for source_id in fact['derived_from_fact_ids'])
            a,b=decimal_value(left),decimal_value(right)
            operation=DERIVATIONS[fact['metric_id']]
            expected=a/b if operation=='ratio' else Decimal(1)-a/b if operation=='one_minus_ratio' else a-b
            if expected!=Decimal(fact['value']):raise AssertionError(f'derived_value_mismatch:{identity}')
        else:
            source=sources.get(identity)
            if source is None:raise AssertionError(f'source_fact_missing:{identity}')
            if decimal_value(source)!=Decimal(fact['value']):raise AssertionError(f'source_fact_value_mismatch:{identity}')
        return Decimal(fact['value'])

    for entry in payload['metrics']:
        fact=entry.get('fact')
        if not fact:continue
        current=verify_fact(fact);checked+=1
        yoy=entry.get('yoy')
        if yoy:
            prior_fact=yoy['prior_fact'];prior=verify_fact(prior_fact)
            if yoy['input_fact_ids']!=[fact['fact_id'],prior_fact['fact_id']]:
                raise AssertionError(f'yoy_input_ids_mismatch:{entry["requirement_id"]}')
            if current!=Decimal(yoy['current_value']) or prior!=Decimal(yoy['prior_value']):
                raise AssertionError(f'yoy_input_values_mismatch:{entry["requirement_id"]}')
            expected=current/prior-Decimal(1)
            if expected!=Decimal(yoy['value']):raise AssertionError(f'yoy_value_mismatch:{entry["requirement_id"]}')
            checked+=1
    return checked


def verify_evidence(pack,payload):
    rows=list(read_jsonl(pack/'evidence-index.jsonl'))
    by_group=Counter(item['group'] for item in rows)
    if not all(by_group[group] for group in ('A','C','D','E')):raise AssertionError('required_evidence_group_empty')
    source_cache={};original_cache={};checked=0
    for item in rows:
        source_path=Path(item['source_evidence_path'])
        if source_path not in source_cache:
            source_cache[source_path]={row['evidence_id']:row for row in read_jsonl(source_path)}
        source=source_cache[source_path][item['evidence_id']]
        if hashlib.sha256(source['text'].encode('utf8')).hexdigest()!=item['text_sha256']:
            raise AssertionError(f'evidence_text_mismatch:{item["evidence_id"]}')
        original=Path(item['original_path'])
        expected=item['original_sha256']
        if original not in original_cache:original_cache[original]=file_hash(original)
        if original_cache[original]!=expected:raise AssertionError(f'original_hash_mismatch:{original}')
        if not item.get('locator'):raise AssertionError('evidence_locator_missing')
        checked+=1
    return checked,dict(by_group)


def validate_company(output_root,ticker,as_of):
    pack,manifest,payload,coverage=load_pack(output_root,ticker,as_of)
    if manifest['status']!='ready' or manifest['token_count']['count']>manifest['token_budget']:
        raise AssertionError(f'pack_budget_not_ready:{ticker}')
    for name,expected in manifest['output_hashes'].items():
        if file_hash(pack/name)!=expected:raise AssertionError(f'output_hash_mismatch:{ticker}:{name}')
    for peer_input in manifest.get('peer_inputs',[]):
        if file_hash(Path(peer_input['path']))!=peer_input['sha256']:
            raise AssertionError(f'peer_input_changed:{ticker}:{peer_input["ticker"]}')
    periods=payload['periods']
    if len(periods['annual'])!=3 or len(periods['quarters'])!=8:raise AssertionError(f'period_window_invalid:{ticker}')
    if coverage['question_count']!=54 or sum(coverage['route_counts'].values())!=54 or not coverage['full_coverage_preserved']:
        raise AssertionError(f'question_routing_invalid:{ticker}')
    if payload['groups']!=list('ABCDEF'):raise AssertionError(f'group_contract_invalid:{ticker}')
    essentials={'operating_income','parent_net_profit','operating_cash_flow','total_assets'}
    for metric in essentials:
        entries=[item for item in payload['metrics'] if item['metric_id']==metric]
        if len(entries)!=11 or any(item['state']!='ready' for item in entries):
            raise AssertionError(f'essential_metric_incomplete:{ticker}:{metric}')
    required_pending=[item for item in coverage['requirements'] if item.get('required') and item['state']=='pending']
    if required_pending:raise AssertionError(f'required_core_pending:{ticker}:{len(required_pending)}')
    sources=source_fact_map(manifest)
    recomputed=verify_derived(payload,sources)
    evidence_checked,evidence_groups=verify_evidence(pack,payload)
    return {
        'ticker':ticker,'company_name':payload['company_name'],'pack_id':manifest['pack_id'],'status':manifest['status'],
        'token_count':manifest['token_count']['count'],'token_kind':manifest['token_count']['kind'],
        'annual_periods':periods['annual'],'quarter_periods':periods['quarters'],
        'coverage_states':coverage['counts'],'question_quality_states':coverage['question_quality_counts'],
        'route_counts':coverage['route_counts'],'fact_requirements_ready':sum(item['state']=='ready' for item in coverage['requirements']),
        'source_text_available':sum(item['state']=='source_text_available' for item in coverage['requirements']),
        'pending':sum(item['state']=='pending' for item in coverage['requirements']),
        'independent_recalculations':recomputed,'evidence_checked':evidence_checked,'evidence_groups':evidence_groups,
        'conflicts':len(payload['conflicts']),'unresolved_conflicts':sum(not item.get('resolved') for item in payload['conflicts']),
        'post_annual_catalog_state':payload['catalog']['post_annual_event_catalog_state'],
        'post_annual_catalog_reason':payload['catalog']['post_annual_event_catalog_reason'],
        'pack_build_network_requests':manifest['request_statistics']['pack_build_network_requests'],
        'manual_acceptance':'pending'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--as-of',default='2026-09-13')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    companies=[validate_company(args.output_root,ticker,args.as_of) for ticker in TICKERS]
    wuliangye=next(item for item in companies if item['ticker']=='000858')
    pack,_,payload,_=load_pack(args.output_root,'000858',args.as_of)
    differences=[item for item in payload['conflicts'] if item['kind']=='reported_ratio_rounding_difference']
    if not differences:raise AssertionError('wuliangye_amount_ratio_difference_not_preserved')
    result={'status':'passed','as_of':args.as_of,'companies':companies,
        'company_count':len(companies),'all_pack_builds_offline':all(not item['pack_build_network_requests'] for item in companies),
        'wuliangye_amount_ratio_differences':len(differences),'manual_acceptance':'pending'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(canonical_json(result)+'\n',encoding='utf8')
    print(canonical_json(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
