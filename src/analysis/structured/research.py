"""八步数据层编排：复用现有物化器、原件和解析器，输出逐题证据包。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from analysis.acquisition.repository import AcquisitionRepository
from analysis.documents import parse_research_original
from .materialization import StructuredFactMaterializer
from .materialization_replay import _export_result, _hash_file
from .reading import select_research_document
from .scope import LITE_PROFILE_ID, ROOT, load_research_profile, load_scope
from .storage import StructuredStorage, canonical_json, canonical_sha256


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+".tmp")
    temp.write_text(canonical_json(value)+"\n", encoding="utf-8")
    temp.replace(path)


def read_jsonl(path):
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def readonly(db):
    connection = sqlite3.connect(db.resolve().as_uri()+"?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def is_source_text(value):
    from decimal import Decimal, InvalidOperation
    if not isinstance(value, str): return False
    if value.strip() in {'','-','--','—','…','N/A','NA','null'}:return False
    try:
        Decimal(value)
        return False
    except InvalidOperation:
        return bool(value.strip())


def compute_financial_indicators(facts):
    """复用已登记公式；先匹配主体/期间/币种/来源，金融企业不走普通公式。"""
    from decimal import Decimal
    from analysis.formulas import income_metrics, cashflow_metrics, balance_metrics
    from analysis.models import FactRecord, VerificationStatus
    aliases={'operating_income':'revenue','operating_cost':'cost_of_revenue','net_profit':'net_income',
        'parent_net_profit':'net_income_parent','deducted_parent_net_profit':'net_income_excl',
        'research_expense':'rd_expense','long_asset_cash_purchase':'capital_expenditure'}
    groups=defaultdict(dict)
    for fact in facts:
        m=fact.metadata
        if fact.unit!='CNY' or m.get('statement_org_type')!='通用':continue
        key=(fact.ticker,fact.period_start,fact.period_end,fact.period_type,fact.scope,fact.currency,
            m.get('source_definition_id'),m.get('source_definition_version'),m.get('contract_hash'))
        name=aliases.get(fact.metric_id,fact.metric_id)
        groups[key].setdefault(name,[]).append(fact)
    dependencies={
        'net_margin':('net_income','revenue'),'parent_net_margin':('net_income_parent','revenue'),
        'adjusted_net_margin':('net_income_excl','revenue'),'selling_expense_ratio':('selling_expense','revenue'),
        'administrative_expense_ratio':('administrative_expense','revenue'),'rd_ratio':('rd_expense','revenue'),
        'finance_expense_ratio':('finance_expense','revenue'),'cash_profit_ratio':('operating_cash_flow','net_income'),
        'operating_cash_flow_margin':('operating_cash_flow','revenue'),'capex_to_revenue':('capital_expenditure','revenue'),
        'free_cash_flow':('operating_cash_flow','capital_expenditure'),
        'current_ratio':('current_assets','current_liabilities'),'goodwill_to_equity':('goodwill','total_equity')}
    result=[]
    for key,bucket in sorted(groups.items(),key=lambda item:str(item[0])):
        chosen={name:items[0] for name,items in bucket.items() if len({(f.metadata.get('decimal_value'),f.unit) for f in items})==1}
        values={name:Decimal(f.metadata['decimal_value']) for name,f in chosen.items()}
        calculated={**income_metrics(values),**cashflow_metrics(values),**balance_metrics(values)}
        for metric,names in dependencies.items():
            if metric not in calculated or calculated[metric] is None or not all(n in chosen for n in names):continue
            if metric!='free_cash_flow' and values[names[-1]]<=0:continue
            parents=[chosen[n] for n in names];value=calculated[metric]
            output_metric='operating_cash_flow_less_asset_purchase_proxy' if metric=='free_cash_flow' else metric
            formula='research-financial-formulas-v1.0.0:'+metric
            identity={'formula':formula,'inputs':[f.fact_id for f in parents]}
            result.append(FactRecord(fact_id='research-derived-'+canonical_sha256(identity)[:32],ticker=key[0],
                metric_id=output_metric,value=float(value),unit='CNY' if metric=='free_cash_flow' else 'ratio',
                currency=key[5],period_start=key[1],period_end=key[2],period_type=key[3],scope=key[4],
                as_of=max(f.as_of for f in parents),source_ids=sorted({s for f in parents for s in f.source_ids}),
                verification_status=VerificationStatus.DERIVED,derived_from_fact_ids=identity['inputs'],method_ref=formula,
                metadata={'formula_version':formula,'formula_authority':'analysis.formulas',
                    'decimal_value':str(value),'input_fact_ids':identity['inputs'],'nature':'deterministic',
                    'cash_flow_boundary':'cash flow proxy; not FCFF','available_at':max(f.as_of for f in parents).isoformat()}))
    return result


def periods(mode, as_of, latest):
    annual = [f"{year}-12-31" for year in range(as_of.year-5, as_of.year)]
    quarter = latest.year*4+(latest.month-1)//3
    quarter_ends = [(3,31),(6,30),(9,30),(12,31)]
    quarters = [date(n//4, *quarter_ends[n%4]).isoformat() for n in range(quarter-11,quarter+1)]
    half_year = as_of.year if latest.month >= 6 and latest.year == as_of.year else as_of.year-1
    if "FIN" in mode: return sorted(set(annual+quarters))
    if "BIZ" in mode: return sorted(set(annual+[f"{y}-06-30" for y in (half_year-1,half_year)]))
    if "EVT" in mode: return [f"{as_of.year-5}-01-01/{as_of.isoformat()}"]
    if "IND" in mode: return [f"{as_of.year-5}-01-01/{as_of.isoformat()}"]
    return [as_of.isoformat()]


def required_latest_period(as_of):
    """覆盖需求的法定披露窗口；不是上游已披露或字段期间的推断。"""
    if as_of>=date(as_of.year,10,31):return date(as_of.year,9,30)
    if as_of>=date(as_of.year,8,31):return date(as_of.year,6,30)
    if as_of>=date(as_of.year,4,30):return date(as_of.year,3,31)
    return date(as_of.year-1,9,30)


def build_coverage(connection, run_id, ticker, output, as_of, projection_dirs=None):
    registry = json.loads((ROOT/"config/structured_data/research_requirements.v1.json").read_text(encoding="utf-8"))
    projection_dirs=projection_dirs or [output]
    manifest = json.loads((projection_dirs[-1]/"manifest.json").read_text(encoding="utf-8"))
    selected = set(manifest["selected_fact_ids"]+manifest["selected_dimensional_fact_ids"])
    contributing_runs={manifest['run_id']}
    facts = list(read_jsonl(projection_dirs[-1]/"facts.jsonl"))+list(read_jsonl(projection_dirs[-1]/"dimensional-facts.jsonl"))
    previous_manifests=[p/'manifest.json' for p in projection_dirs[:-1]]
    for folder in projection_dirs:
        previous_manifests.extend((folder/'history').glob('*/manifest.json'))
    for previous in sorted(previous_manifests):
        prior=json.loads(previous.read_text(encoding='utf8'))
        if prior.get('contract_hash')==manifest['contract_hash'] and prior.get('source_namespace_id')==manifest.get('source_namespace_id'):
            contributing_runs.add(prior['run_id'])
            selected.update(prior['selected_fact_ids']+prior['selected_dimensional_fact_ids'])
            facts.extend(read_jsonl(previous.parent/'facts.jsonl'))
            facts.extend(read_jsonl(previous.parent/'dimensional-facts.jsonl'))
    from analysis.models import FactRecord, DimensionalFactRecord
    from .materialization import _select, _derive, _derive_gross_margin
    by_id={}
    for fact in facts:
        fid=fact.get('fact_id',fact.get('dimensional_fact_id'))
        if fact['ticker'] != ticker: raise ValueError('coverage_company_mismatch')
        if str(fact['metadata'].get('available_at',fact.get('as_of',fact.get('available_at',''))))[:10]>as_of.isoformat():continue
        if fid in by_id and by_id[fid]!=fact:raise ValueError('conflicting_immutable_fact_payload')
        by_id[fid]=fact
    numeric=[FactRecord.model_validate(f) for f in by_id.values() if 'fact_id' in f and not f.get('derived_from_fact_ids')]
    dimensional=[DimensionalFactRecord.model_validate(f) for f in by_id.values() if 'dimensional_fact_id' in f]
    selected_numeric, numeric_conflicts=_select(numeric)
    selected_dimensions, dimension_conflicts=_select(dimensional)
    derived, period_gaps = _derive(selected_numeric)
    selected_numeric.extend(derived)
    ratios, ratio_gaps = _derive_gross_margin(selected_numeric)
    selected_numeric.extend(ratios)
    computed=compute_financial_indicators(selected_numeric)
    selected_numeric.extend(computed)
    for fact in (*derived, *ratios,*computed): by_id[fact.fact_id]=fact.model_dump(mode='json')
    selected={f.fact_id for f in selected_numeric}|{f.dimensional_fact_id for f in selected_dimensions}
    facts=list(by_id.values())
    index = defaultdict(list)
    latest = required_latest_period(as_of)
    for fact in facts:
        fid = fact.get("fact_id", fact.get("dimensional_fact_id"))
        if fid not in selected: continue
        if fact.get('derived_from_fact_ids'):continue
        meta = fact["metadata"]
        raw = meta.get("structured_field_path", "").removeprefix("$.")
        end = fact.get("period_end")
        if end and end <= as_of.isoformat():
            if fact["period_type"] in {"cumulative", "single_quarter"}: latest=max(latest,date.fromisoformat(end))
            index[(meta.get("structured_dataset_id"),raw,end)].append(fid)
    raw_index = defaultdict(list)
    dataset_counts = Counter()
    records_by_dataset=defaultdict(list)
    normalized=[]
    from dataclasses import asdict
    from .mappings import record_dates, record_dimensions, lifecycle_state, map_counterparty_record
    contributing_runs=sorted(contributing_runs)
    placeholders=','.join('?' for _ in contributing_runs)
    scope=load_scope()
    seen_records=set()
    for row in connection.execute("SELECT j.dataset_id,j.purpose,r.payload FROM structured_jobs j JOIN structured_records r ON r.job_id=j.job_id JOIN structured_pages p ON p.page_id=r.page_id AND p.job_id=j.job_id AND p.snapshot_id=r.snapshot_id WHERE j.run_id IN ("+placeholders+") AND EXISTS (SELECT 1 FROM acquisition_attempt_events e WHERE e.attempt_id=p.attempt_id AND e.event_type='outcome_terminal' AND e.outcome='success') ORDER BY j.run_id,j.dataset_id,r.record_version_id", contributing_runs):
        if row["purpose"] in {"company_type", "report_catalog"}: continue
        dataset = row["dataset_id"]
        if load_scope()["datasets"][dataset]["selection"] == "excluded": continue
        record = json.loads(row["payload"]); raw = record["raw_row"]
        if str(record['available_at'])[:10]>as_of.isoformat() or record['record_version_id'] in seen_records:continue
        seen_records.add(record['record_version_id'])
        dataset_counts[dataset] += 1
        period = str(raw.get("REPORT_DATE") or raw.get("END_DATE") or raw.get("TRADE_DATE") or "")[:10]
        observation={'dataset_id':dataset,'company':ticker,'record_id':record['record_version_id'],'snapshot_id':record['snapshot_id'],
            'row_key':record['row_key'],'period':period or None,'available_at':record['available_at'],'dimensions':record_dimensions(dataset,raw),
            'dates':record_dates(dataset,raw),'lifecycle':asdict(lifecycle_state(dataset,raw)),
            'fields':{k:raw[k] for k in load_scope()['datasets'][dataset]['consume_fields'] if k in raw},
            'numeric_consumption':'only_selected_fact_ids','text_consumption':'supplier_statement_not_research_conclusion'}
        if dataset=='customers_peer':
            observation['counterparty']=asdict(map_counterparty_record(raw))
            observation['counterparty']['total_amount_semantics']='provider_denominator_not_confirmed_disclosed'
            observation['counterparty']['remainder_is_estimate']=str(raw.get('RANK')) not in {'1','2','3','4','5'}
        records_by_dataset[dataset].append(observation)
        normalized.append(observation)
        for name in load_scope()["datasets"][dataset]["consume_fields"]:
            if raw.get(name) is not None:
                raw_index[(dataset,name,period)].append({"record_id":record["record_version_id"],"snapshot_id":record["snapshot_id"],"row_key":record["row_key"],"field_path":"$."+name,"value":raw[name]})
    rows = []
    text_index=defaultdict(list)
    calculation_routes={r['route_id']:r for r in registry['calculation_routes']}
    calculation_metrics={'K02':{'gross_margin','net_margin','parent_net_margin','selling_expense_ratio','administrative_expense_ratio','rd_ratio','finance_expense_ratio'},
        'K04':{'cash_profit_ratio'},'K06':{'operating_cash_flow_less_asset_purchase_proxy','capex_to_revenue','operating_cash_flow_margin'}}
    for item in read_jsonl(output.parent/'document-evidence.jsonl'):
        if item['company']==ticker:
            text_index[(item['route_id'],item['period'])].append(item)
    for req in registry["requirements"]:
        for path in req["paths"]:
            override=load_scope().get('requirement_overrides',{}).get(req['requirement_id'],{})
            path=dict(path)
            if 'dataset_id' in override:
                path.update(dataset_id=override['dataset_id'],raw_name=override['raw_name'],scope_basis=override['basis'])
            for period in periods(path["period_semantics"],as_of,latest):
                dataset, raw = path.get("dataset_id"),path.get("raw_name")
                ids = index.get((dataset,raw,period),[])
                effective_period=period
                if dataset=='market_cap' and 'NOW' in path['period_semantics']:
                    available=sorted(p for d,k,p in index if d==dataset and k==raw and p<=as_of.isoformat())
                    if available and (as_of-date.fromisoformat(available[-1])).days<=14:
                        effective_period=available[-1];ids=index[(dataset,raw,effective_period)]
                observations = raw_index.get((dataset,raw,period),[])
                if effective_period != period: observations=raw_index.get((dataset,raw,effective_period),[])
                if '/' in period:
                    start,end=period.split('/')
                    observations=[v for (d,k,p),values in raw_index.items() if d==dataset and k==raw and start<=p<=end for v in values]
                # Current/company context is not fabricated as a historical period.
                if "NOW" in path["period_semantics"] and "FIN" not in path["period_semantics"] and dataset == "company_basic":
                    observations = [v for (d,k,p),values in raw_index.items() if d==dataset and k==raw for v in values]
                period_unconfirmed=not observations and bool(raw_index.get((dataset,raw,'')))
                if period_unconfirmed:observations=raw_index[(dataset,raw,'')]
                status,reason = "pending", "input_not_acquired"
                if ids: status,reason = "ready", None
                elif observations:
                    reason = "semantic_definition_or_period_unconfirmed"
                    if all(is_source_text(o['value']) for o in observations) and raw not in {"TYPE", "TYPE_CODE"}:
                        status,reason = "source_text_available", "source_statement_requires_question_review"
                elif path["kind"] == "calculation": reason = "calculation_inputs_or_method_pending"
                elif path["kind"] == "reading_section": reason = "document_semantic_evidence_pending"
                elif path["kind"] in {"research_context", "coverage_snapshot"}: reason = "research_context_pending"
                elif path["kind"] == "gap": reason = "registered_source_or_definition_gap"
                elif path["kind"] == "record_set" and records_by_dataset.get(dataset):
                    reason='record_set_available_lifecycle_or_field_semantics_pending'
                if not ids and period_unconfirmed:
                    status,reason='pending','observed_field_period_unconfirmed'
                if dataset and load_scope()["datasets"][dataset]["selection"] == "excluded": reason="excluded_legacy_requirement_needs_scoped_alternative"
                if override.get('scope_disposition')=='not_required_by_default':
                    status,reason='not_applicable','versioned_scope_does_not_require_input'
                text_evidence=text_index.get((path.get('route_id'),period),[])
                if '/' in period:
                    start,end=period.split('/')
                    text_evidence=[e for (route,p),items in text_index.items() if route==path.get('route_id') and p and start<=p<=end for e in items]
                if path['kind']=='reading_section' and text_evidence:
                    status,reason='source_text_available','source_passages_organized_question_review_pending'
                calculation_status=None
                if path['kind']=='calculation':
                    route=calculation_routes[path['route_id']]
                    missing=[]
                    for ref in route['input_refs']:
                        if ref.startswith('f:'):
                            dataset_name,field=ref[2:].split('.',1)
                            if not index.get((dataset_name,field,period)):missing.append(ref)
                        else:missing.append(ref)
                    calculation_status={'route_id':route['route_id'],'missing_verified_inputs':missing,
                        'boundary':route['formula_boundary'],'complete_route_integrated':False,
                        'available_computed_fact_ids':[f.fact_id for f in selected_numeric if f.period_end.isoformat()==period and
                            (f.metric_id in calculation_metrics.get(route['route_id'],set()) or route['route_id']=='K01' and f.derived_from_fact_ids and f.period_type in {'single_quarter','ttm'})]}
                    reason='calculation_verified_inputs_pending' if missing else 'calculation_route_integration_pending'
                rows.append({"company":ticker,"question_id":req["question_id"],"requirement_id":req["requirement_id"],
                    "requiredness":req["requiredness"],"period":period,"effective_input_period":effective_period,"scope_override":override,"input":path,"state":status,"reason":reason,
                    "fact_ids":ids,"observations":observations[:8],"observation_count":len(observations),"text_evidence_ids":[e['evidence_id'] for e in text_evidence],
                    'calculation_status':calculation_status,
                    "next_action":None if status in {'ready','not_applicable'} else "semantic_review" if observations or text_evidence else "acquire_exact_input" if path['kind']=='raw_field' else "prepare_"+path['kind']})
    target=output/"question-coverage.jsonl"
    with (output/'normalized-records.jsonl').open('w',encoding='utf8') as stream:
        for row in normalized:stream.write(canonical_json(row)+'\n')
    with target.open("w",encoding="utf-8") as stream:
        for row in rows: stream.write(canonical_json(row)+"\n")
    with (output/'coverage-facts.jsonl').open('w',encoding='utf8') as stream:
        for fid,fact in sorted(by_id.items()):
            if fid in selected:stream.write(canonical_json(fact)+'\n')
    with (output/'computed-facts.jsonl').open('w',encoding='utf8') as stream:
        for fact in sorted(computed,key=lambda f:f.fact_id):stream.write(canonical_json(fact.model_dump(mode='json'))+'\n')
    question_counts = {q["question_id"]:dict(Counter(r["state"] for r in rows if r["question_id"]==q["question_id"])) for q in registry["questions"]}
    # A precise list is not an instruction to re-fetch already observed, unprocessed fields.
    gaps=[r for r in rows if r["state"] not in {"ready","not_applicable"}]
    with (output/"gaps.jsonl").open("w",encoding="utf-8") as stream:
        for row in gaps: stream.write(canonical_json(row)+"\n")
    summary={"ticker":ticker,"questions":len(question_counts),"requirements":len(registry['requirements']),"period_inputs":len(rows),
        "states":dict(Counter(r['state'] for r in rows)),"reason_counts":dict(Counter(r['reason'] for r in gaps)),
        "dataset_records_in_scope":dict(dataset_counts),"question_counts":question_counts,"latest_financial_period":latest.isoformat(),
        "coverage_sha256":_hash_file(target),"market_records_count_as_financial_coverage":False}
    summary.update(contributing_run_ids=contributing_runs, selection_conflicts=sorted(numeric_conflicts|dimension_conflicts),
        derivation_gaps=sorted(period_gaps|ratio_gaps), selected_coverage_facts=len(selected))
    summary['computed_financial_indicators']=len(computed)
    work=[]
    for row in gaps:
        kind=row['input']['kind']
        if row['observations'] or row['text_evidence_ids']:
            stage='semantic_processing'
        elif kind=='raw_field':stage='acquisition'
        elif kind=='reading_section':stage='document_reading'
        elif kind=='calculation':stage='deterministic_calculation'
        else:stage='research_context'
        work.append({'company':ticker,'question_id':row['question_id'],'requirement_id':row['requirement_id'],
            'period':row['period'],'stage':stage,'dataset_id':row['input'].get('dataset_id'),
            'raw_name':row['input'].get('raw_name'),'route_id':row['input'].get('route_id'),
            'reason':row['reason'],'acquire_allowed':stage=='acquisition' and row['reason']=='input_not_acquired'})
    write_json(output/'next-work.json',{'scope_sha256':scope['content_sha256'],'company':ticker,'items':work})
    write_json(output/"question-coverage-summary.json",summary)
    return summary


def materialize_cache(
    db,
    data_root,
    output,
    as_of,
    tickers=None,
    recompute=False,
    research_profile_id=None,
):
    with readonly(db) as connection:
        namespace=connection.execute("SELECT namespace_id FROM storage_namespaces").fetchone()[0]
        runs=connection.execute("SELECT run_id,ticker FROM structured_run_contexts ORDER BY ticker,run_id").fetchall()
        counts=Counter(r['ticker'] for r in runs)
        projections=defaultdict(list)
        reuse_flags=defaultdict(list)
        recompute_checks=defaultdict(list)
        before=_hash_file(db)
        storage=StructuredStorage(db,namespace,initialize=False)
        repository=AcquisitionRepository(db,initialize=False)
        result_rows=[]
        for run in runs:
            if tickers and run['ticker'] not in tickers: continue
            folder=output/run['ticker']
            if counts[run['ticker']]>1:folder=folder/'runs'/run['run_id']
            folder.mkdir(parents=True,exist_ok=True)
            stamp=folder/'cache-binding.json'
            identity={'source_db_sha256':before,'run_id':run['run_id'],'scope_hash':load_scope()['content_sha256'],
                'interpretation_hash':json.loads((ROOT/'config/structured_data/interpretations/eastmoney-financial-interpretation-v1.0.0.json').read_text(encoding='utf8'))['content_sha256']}
            if research_profile_id:
                identity['research_profile_hash']=load_research_profile(research_profile_id)['content_sha256']
            reuse=stamp.exists() and json.loads(stamp.read_text(encoding='utf8'))==identity and (folder/'manifest.json').exists() and not recompute
            old=json.loads((folder/'manifest.json').read_text(encoding='utf8')) if (folder/'manifest.json').exists() else None
            if not reuse:
                materialized=StructuredFactMaterializer(storage,repository).materialize(
                    run['run_id'],
                    interpretation_contract='eastmoney-financial-interpretation-v1.0.0',
                    research_scope=True,
                    research_profile_id=research_profile_id,
                )
                if recompute and old and old['contract_hash']==materialized.contract_hash and old['materialization_hash']!=materialized.materialization_hash:
                    raise ValueError('repeat_materialization_hash_mismatch')
                _export_result(materialized,folder,namespace)
                # Independent unit recalculation and source bytes verification.
                from decimal import Decimal
                hashes={}; checked=0
                for fact in (*materialized.facts,*materialized.dimensional_facts):
                    meta=fact.metadata
                    if 'original_value' not in meta: continue
                    if Decimal(str(meta['original_value']))*Decimal(meta['multiplier']) != Decimal(meta['decimal_value']):
                        raise ValueError('independent_numeric_mismatch')
                    checked+=1
                    sid=meta['structured_snapshot_id']
                    if sid not in hashes:
                        snapshot=repository.get_raw_resource_snapshot(sid)
                        path=(data_root/snapshot.archive_relative_path).resolve()
                        if not path.is_relative_to(data_root.resolve()) or _hash_file(path)!=snapshot.sha256: raise ValueError('snapshot_bytes_mismatch')
                        hashes[sid]=snapshot.sha256
                write_json(folder/'independent-audit.json',{'checked_direct_facts':checked,'snapshot_count':len(hashes),'snapshot_hashes':hashes,'source_db_sha256':before})
                write_json(stamp,identity)
            projections[run['ticker']].append(folder)
            reuse_flags[run['ticker']].append(reuse)
            recompute_checks[run['ticker']].append(bool(recompute and old and old['contract_hash']==materialized.contract_hash))
        for ticker,folders in sorted(projections.items()):
            run_id=json.loads((folders[-1]/'manifest.json').read_text(encoding='utf8'))['run_id']
            summary=build_coverage(connection,run_id,ticker,output/ticker,as_of,folders)
            reuse=all(reuse_flags[ticker])
            result_rows.append(summary|{'cache_reused':reuse,'recomputed_equal':all(recompute_checks[ticker])})
            print(canonical_json({'company':ticker,'coverage':summary['states'],'cache_reused':reuse}),flush=True)
        after=_hash_file(db)
        if before!=after: raise ValueError('source_database_changed_during_readonly_run')
    summary={'scope':research_profile_id or load_scope()['scope_id'],'source_database_unchanged':True,'source_db_sha256':before,'companies':result_rows,
        'cache_replay':True,'current_network':False,'manual_acceptance':'pending'}
    write_json(output/'cache-summary.json',summary)
    return summary


def process_cached_documents(db, data_root, output, as_of, research_profile_id=None):
    with readonly(db) as connection:
        snapshots={}
        for row in connection.execute("SELECT payload FROM raw_resource_snapshots WHERE payload LIKE '%application/pdf%'"):
            s=json.loads(row[0]); snapshots[s['canonical_resource_id']]=s
        # Identity comes from the frozen acquisition run, never a name guess.
        run_tickers={r['run_id']:r['ticker'] for r in connection.execute('SELECT run_id,ticker FROM acquisition_runs')}
        attempt_tickers={r['attempt_id']:run_tickers.get(r['run_id']) for r in connection.execute('SELECT attempt_id,run_id FROM acquisition_attempts')}
        entries={}
        for row in connection.execute('SELECT payload FROM discovered_resources'):
            entry=json.loads(row[0]); ticker=(entry.get('metadata') or {}).get('ticker') or attempt_tickers.get(entry.get('discovery_attempt_id'))
            entry['ticker']=ticker
            choice=select_research_document(
                entry, as_of=as_of, research_profile_id=research_profile_id
            )
            if choice['selected']: entries[entry['canonical_resource_id']]=(entry,choice)
        results=[]
        for identity,(entry,choice) in sorted(entries.items()):
            snapshot=snapshots.get(identity)
            row={"ticker":entry['ticker'],"title":entry['title'],"resource_id":identity,"url":entry['resource_url'],**choice}
            if not snapshot:
                results.append(row|{'download_status':'missing','parse_status':'not_started','semantic_status':'not_started'});continue
            path=(data_root/snapshot['archive_relative_path']).resolve()
            if not path.is_relative_to(data_root.resolve()) or _hash_file(path)!=snapshot['sha256']: raise ValueError('document_original_hash_mismatch')
            try:
                parsed=parse_research_original(path,mime_type=snapshot['mime_type'],output_dir=output/'parsed')
                row.update({k:v for k,v in parsed.items() if k!='units'})
                row['unit_count']=len(parsed['units'])
                row['page_count']=sum(u['kind']=='page' for u in parsed['units'])
                row['audit_chapter_preserved']=any('审计报告' in u.get('text','') for u in parsed['units'])
            except Exception as exc:
                row.update(parse_status='parse_failed',reason=f'{type(exc).__name__}:{exc}')
            results.append(row|{'download_status':'cached','snapshot_id':snapshot['snapshot_id'],'original_sha256':snapshot['sha256']})
        write_json(output/'documents.json',{'documents':results,'selected':len(results),'download_counts':dict(Counter(r['download_status'] for r in results)),
            'parse_counts':dict(Counter(r['parse_status'] for r in results)),'performed_network_io':False})
    print(canonical_json({'selected_documents':len(results),'parsed':sum(r['parse_status']=='parsed' for r in results)}),flush=True)
    return results


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'lite':
        lite_parser=argparse.ArgumentParser(description='构建八步轻量核心研究输入包（默认离线）')
        lite_parser.add_argument('action',choices=['lite'])
        lite_parser.add_argument('--input',type=Path,required=True,help='主事实投影根目录')
        lite_parser.add_argument('--supplement',type=Path,action='append',default=[],help='显式补充投影根目录；不重绑namespace')
        lite_parser.add_argument('--ticker',action='append',required=True)
        lite_parser.add_argument('--as-of',type=date.fromisoformat,required=True)
        lite_parser.add_argument('--output',type=Path,required=True)
        lite_parser.add_argument('--profile',default=LITE_PROFILE_ID)
        lite_parser.add_argument('--max-tokens',type=int)
        lite_parser.add_argument('--execute',action='store_true',help='显式联网刷新期后公告目录；不下载未触发正文')
        lite_parser.add_argument('--network-output',type=Path,help='联网目录证据输出；默认位于轻量输出根目录下')
        args=lite_parser.parse_args(argv)
        from .research_lite import build_lite_pack, refresh_lite_catalog
        results=[]
        supplements=list(args.supplement)
        network_output=args.network_output or args.output/'network-audit'/args.as_of.isoformat()
        network_results=[]
        if args.execute:
            for ticker in args.ticker:
                network_results.append(refresh_lite_catalog(output_root=network_output,ticker=ticker,
                    as_of=args.as_of,profile_id=args.profile))
            supplements.append(network_output)
        for ticker in args.ticker:
            result=build_lite_pack(input_root=args.input,ticker=ticker,as_of=args.as_of,
                output_root=args.output,supplements=tuple(supplements),profile_id=args.profile,
                max_tokens=args.max_tokens)
            if network_results:
                result['network_update']=next(item for item in network_results if item['ticker']==ticker)
            results.append(result);print(canonical_json(result),flush=True)
        return 2 if any(result['status']=='budget_exceeded' for result in results) else 0
    if argv and argv[0] == 'evidence':
        evidence_parser=argparse.ArgumentParser(description='按核心包证据ID分页读取有界原文')
        evidence_parser.add_argument('action',choices=['evidence'])
        evidence_parser.add_argument('--pack',type=Path,required=True)
        evidence_parser.add_argument('--evidence-id',required=True)
        evidence_parser.add_argument('--page',type=int,default=1)
        evidence_parser.add_argument('--max-tokens',type=int)
        args=evidence_parser.parse_args(argv)
        from .research_lite import read_evidence
        print(canonical_json(read_evidence(pack_dir=args.pack,evidence_id=args.evidence_id,
            page=args.page,max_tokens=args.max_tokens)),flush=True)
        return 0
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['cache','documents','fetch-documents','fetch-selected','index-documents'])
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--as-of',type=date.fromisoformat,required=True)
    parser.add_argument('--ticker',action='append')
    parser.add_argument('--recompute',action='store_true')
    parser.add_argument('--entries',type=Path,help='已登记问题触发的目录条目 JSON 列表')
    parser.add_argument('--research-profile',help='显式版本化研究范围')
    args=parser.parse_args(argv)
    if args.output.resolve().is_relative_to(args.data_root.resolve()): parser.error('output must be outside source data-root')
    if args.action=='cache': materialize_cache(args.db,args.data_root,args.output,args.as_of,args.ticker,args.recompute,args.research_profile)
    elif args.action=='documents': process_cached_documents(args.db,args.data_root,args.output,args.as_of,args.research_profile)
    elif args.action=='index-documents': index_document_evidence(args.output)
    elif args.action=='fetch-selected':
        if not args.entries:parser.error('fetch-selected requires --entries')
        fetch_selected_documents(json.loads(args.entries.read_text(encoding='utf8')),args.output,args.as_of,args.research_profile)
    else: fetch_required_documents(args.output,args.as_of,args.ticker,args.research_profile)
    return 0


def fetch_selected_documents(entries, output, as_of, research_profile_id=None):
    """按已发现条目精确补正文，复用同一原件适配器与解析器。"""
    from analysis.acquisition.research_fetch import ResearchFetch
    transport=ResearchFetch(output/'acquisition'); results=[]
    for entry in entries:
        choice=select_research_document(
            entry,
            as_of=as_of,
            question_ids=entry.get('question_ids',()),
            trigger_reason=entry.get('trigger_reason'),
            research_profile_id=research_profile_id,
        )
        row={'ticker':entry.get('ticker'),'title':entry['title'],'resource_id':entry['canonical_resource_id'],
            'url':entry['resource_url'],'published_at':entry.get('published_at'),'metadata':entry.get('metadata',{}),**choice}
        if not choice['selected']:
            results.append(row|{'download_status':'not_selected','parse_status':'not_requested'});continue
        try:
            path,proof=transport.fetch(entry['resource_url'])
            mime=proof['mime_type']
            if mime.split(';')[0]=='application/octet-stream' and len(entry.get('expected_mime_types',()))==1:
                mime=entry['expected_mime_types'][0]
            parsed=parse_research_original(path,mime_type=mime,output_dir=output/'parsed')
            results.append(row|{k:v for k,v in parsed.items() if k!='units'}|{
                'download_status':'cached' if proof['cache_reused'] else 'downloaded',
                'request_key':proof['request_key'],'unit_count':len(parsed['units'])})
        except Exception as exc:
            results.append(row|{'download_status':'failed','reason':f'{type(exc).__name__}:{exc}'})
    result={'documents':results,'requests':transport.requests,'scope_sha256':load_scope()['content_sha256'],
        'research_profile_id':research_profile_id,
        'research_profile_sha256':load_research_profile(research_profile_id)['content_sha256'] if research_profile_id else None}
    target=output/'selected-documents'/('selection-'+canonical_sha256(entries)+'.json')
    write_json(target,result)
    normalize_official_tables(output)
    return result


def normalize_official_tables(output):
    """已核对的国家统计局 HTML 表 1 百分比列；绝对量不猜单位。"""
    from decimal import Decimal, InvalidOperation
    from urllib.parse import urlsplit
    facts={}; gaps=[]
    for selection in sorted((output/'selected-documents').glob('*.json')):
        for document in json.loads(selection.read_text(encoding='utf8'))['documents']:
            if document.get('parse_status')!='parsed' or urlsplit(document['url']).hostname!='www.stats.gov.cn':continue
            metadata=document.get('metadata',{}); schema=metadata.get('table_schema')
            if schema not in {'nbs-monthly-growth-v1','nbs-ppi-growth-v1'}:continue
            period=metadata.get('period')
            try: end=date.fromisoformat(period+'-01')
            except (TypeError,ValueError):
                gaps.append({'url':document['url'],'reason':'explicit_month_period_missing'});continue
            parsed=json.loads(Path(document['parsed_path']).read_text(encoding='utf8'))
            cells={(u['row'],u['column']):u for u in parsed['units'] if u.get('kind')=='cell' and u.get('table')==1}
            headers=[u['text'] for (row,col),u in sorted(cells.items()) if row<= (1 if schema=='nbs-ppi-growth-v1' else 2)]
            if schema=='nbs-ppi-growth-v1':
                valid=all(label in headers for label in ('环比涨跌幅（%）','同比涨跌幅（%）',f'1—{end.month}月同比涨跌幅（%）'))
                columns={2:'month_on_month',3:'year_on_year',4:'cumulative_year_on_year'};first=2
            else:
                valid=f'{end.month}月' in headers and f'1—{end.month}月' in headers and sum('同比增长（%）' in h for h in headers)==2
                columns={3:'year_on_year',5:'cumulative_year_on_year'};first=3
            if not valid:
                gaps.append({'url':document['url'],'reason':'official_table_header_schema_changed','headers':headers});continue
            for (row,col),unit in sorted(cells.items()):
                if row<first or col not in columns:continue
                label=cells.get((row,1),{}).get('text','')
                try:value=Decimal(unit['text'])
                except InvalidOperation:continue
                if not value.is_finite() or not label:continue
                item={'metric_label':label,'period':period,'period_type':columns[col],'value':str(value/100),
                    'unit':'ratio','original_value':unit['text'],'original_unit':'%','multiplier':'0.01',
                    'scope':'China; official statistical population described in original publication',
                    'original_sha256':document['original_sha256'],'url':document['url'],'locator':unit['locator'],
                    'header_evidence':headers,'published_at':document['published_at'],'schema':schema,
                    'data_nature':'official_industry_observation','company_financial_formula_eligible':False}
                item['fact_id']='official-table-'+canonical_sha256(item)[:24];facts[item['fact_id']]=item
    with (output/'official-table-facts.jsonl').open('w',encoding='utf8') as stream:
        for _,fact in sorted(facts.items()):stream.write(canonical_json(fact)+'\n')
    write_json(output/'official-table-summary.json',{'facts':len(facts),'gaps':gaps,'coverage_boundary':'selected months only; no complete historical series or company market-share denominator'})
    return list(facts.values())


def fetch_required_documents(output, as_of, tickers=None, research_profile_id=None):
    from datetime import datetime, timezone
    from analysis.acquisition.research_fetch import ResearchFetch
    transport=ResearchFetch(output/'acquisition')
    tickers=tickers or ['600519','000858','000568','600809','002304','000596','603369']
    cached_path=output/'documents.json'
    cached=json.loads(cached_path.read_text(encoding='utf8'))['documents'] if cached_path.exists() else []
    by_resource={r['resource_id']:r for r in cached if r['download_status']=='cached'}
    stock_path,_=transport.fetch('https://www.cninfo.com.cn/new/data/szse_stock.json')
    stocks={r['code']:r for r in json.loads(stock_path.read_bytes())['stockList']}
    results=[]; catalogs=[]; failures=[]
    for ticker in tickers:
        if ticker not in stocks:
            failures.append({'ticker':ticker,'reason':'company_identity_missing'});continue
        stock=stocks[ticker]; entries={}; pages=[]; expected=None
        try:
            for page in range(1,21):
                start_year = as_of.year - (3 if research_profile_id else 5)
                params={'stock':ticker+','+stock['orgId'],'tabName':'fulltext','pageSize':30,'pageNum':page,'column':'szse',
                    'category':'category_ndbg_szsh;category_bndbg_szsh;','seDate':f'{start_year}-01-01~{as_of}',
                    'searchkey':'','sortName':'time','sortType':'desc','isHLtitle':'true'}
                path,proof=transport.fetch('https://www.cninfo.com.cn/new/hisAnnouncement/query',data=params)
                payload=json.loads(path.read_bytes()); rows=payload.get('announcements')
                if not isinstance(rows,list): raise ValueError('catalog_shape_invalid')
                expected=payload.get('totalAnnouncement'); ids=[]
                for item in rows:
                    if item.get('secCode')!=ticker: raise ValueError('catalog_company_mismatch')
                    identity='cninfo:'+item['announcementId']; ids.append(identity)
                    entry={'canonical_resource_id':identity,'ticker':ticker,'title':item['announcementTitle'],
                        'resource_url':'https://static.cninfo.com.cn/'+item['adjunctUrl'],
                        'expected_mime_types':['application/pdf'],'metadata':{'ticker':ticker,'org_id':stock['orgId'],'formal_category':item['announcementType']},
                        'published_at':datetime.fromtimestamp(item['announcementTime']/1000,timezone.utc).isoformat(),'catalog_sha256':proof['sha256']}
                    if identity in entries: raise ValueError('catalog_duplicate_across_pages')
                    entries[identity]=entry
                pages.append({'page':page,'rows':len(rows),'has_more':payload.get('hasMore'),'sha256':proof['sha256']})
                if not payload.get('hasMore'): break
            else: raise ValueError('catalog_page_bound_exceeded')
            if expected is not None and len(entries)!=expected: raise ValueError('catalog_total_mismatch')
            catalogs.append({'ticker':ticker,'count':len(entries),'expected':expected,'pages':pages,'terminal':True})
        except Exception as exc:
            failures.append({'ticker':ticker,'stage':'catalog','reason':str(exc),'pages':pages});continue
        for identity,entry in entries.items():
            choice=select_research_document(entry,as_of=as_of,research_profile_id=research_profile_id)
            row={'ticker':ticker,'title':entry['title'],'resource_id':identity,'url':entry['resource_url'],'catalog_sha256':entry['catalog_sha256'],**choice}
            if not choice['selected']:
                results.append(row|{'download_status':'not_selected','parse_status':'not_requested'});continue
            if identity in by_resource:
                results.append(by_resource[identity]|{'cache_reused':True});continue
            try:
                path,proof=transport.fetch(entry['resource_url'])
                if not path.read_bytes().startswith(b'%PDF-'): raise ValueError('selected_report_is_not_pdf')
                parsed=parse_research_original(path,mime_type='application/pdf',output_dir=output/'parsed')
                results.append(row|{k:v for k,v in parsed.items() if k!='units'}|{'download_status':'cached' if proof['cache_reused'] else 'downloaded','request_key':proof['request_key'],
                    'page_count':sum(u['kind']=='page' for u in parsed['units']),'audit_chapter_preserved':any('审计报告' in u.get('text','') for u in parsed['units'])})
            except Exception as exc:
                results.append(row|{'download_status':'failed','parse_status':'not_complete','reason':f'{type(exc).__name__}:{exc}'})
        print(canonical_json({'document_company':ticker,'selected':sum(r.get('selected',False) for r in results if r['ticker']==ticker)}),flush=True)
        write_json(output/'live-documents.json',{'documents':results,'catalogs':catalogs,'failures':failures,'requests':transport.requests})
    # Every required baseline slot is kept, including companies with failed directories.
    slots=[]
    for ticker in tickers:
        if research_profile_id:
            annual_year = as_of.year - (1 if as_of >= date(as_of.year, 4, 30) else 2)
            interim_year = as_of.year if as_of >= date(as_of.year, 8, 31) else as_of.year - 1
            required_periods = [f'{annual_year}-12-31', f'{interim_year}-06-30']
        else:
            required_periods = [f'{y}-12-31' for y in range(as_of.year-5,as_of.year)]+[f'{y}-06-30' for y in (as_of.year-1,as_of.year)]
        for period in required_periods:
            matching=[r for r in results if r['ticker']==ticker and r.get('selected') and r.get('period')==period]
            slots.append({'ticker':ticker,'period':period,'state':'parsed' if any(r.get('parse_status')=='parsed' for r in matching) else 'missing',
                'resource_ids':[r['resource_id'] for r in matching],'reason':None if matching else 'required_full_report_not_obtained'})
    summary={'documents':results,'catalogs':catalogs,'failures':failures,'requests':transport.requests,'required_slots':slots,
        'slot_counts':dict(Counter(s['state'] for s in slots)),'download_counts':dict(Counter(r['download_status'] for r in results)),
        'current_network':any(not r.get('cache_reused',False) for r in transport.requests),'manual_acceptance':'pending'}
    write_json(output/'live-documents.json',summary)
    index_document_evidence(output)
    return summary


def index_document_evidence(output):
    """整理原文段落供逐题阅读。抽取原文可消费，研究判断仍不自动生成。"""
    import re
    patterns={
        'RD01':r'主要业务|经营模式|收入确认|公司业务概要',
        'RD02':r'主营业务分|主要客户|主要供应商|前[五5].{0,4}(客户|供应商)|分产品|分地区|销售模式',
        'RD03':r'生产量|销售量|库存量|产能|投产',
        'RD04':r'研发投入|研发人员|资本化研发|研发费用',
        'RD05':r'会计政策|会计估计|合并范围|账龄|资产减值',
        'RD06':r'受限.{0,8}资金|受限制|有息债务|短期借款|长期借款|折旧|摊销',
        'RD07':r'实际控制人|关联交易|董事.{0,6}变更|任职|承诺履行',
        'RD08':r'利润分配|回购股份|募集资金使用|资产收购|项目进度',
        'RD09':r'行业发展|行业情况|市场规模|行业竞争|供需',
        'RD10':r'经营计划|发展战略|风险因素|可能面对的风险',
        'RD11':r'股权激励|股份支付|员工持股|限制性股票|股票期权',
        'RD12':r'我们审计了|审计意见|无保留意见|保留意见|内部控制评价|内部控制缺陷',
    }
    live_path=output/'live-documents.json'
    payload=json.loads(live_path.read_text(encoding='utf8')) if live_path.exists() else {'documents':[]}
    documents={d['resource_id']:d for d in payload['documents']}
    for selection in sorted((output/'selected-documents').glob('*.json')):
        for d in json.loads(selection.read_text(encoding='utf8'))['documents']:
            documents[d['resource_id']]=d
    evidence=[]; seen=set()
    for document in documents.values():
        if not document.get('selected') or document.get('parse_status')!='parsed':continue
        parsed=json.loads(Path(document['parsed_path']).read_text(encoding='utf8'))
        for route,pattern in patterns.items():
            count=0
            for unit in parsed['units']:
                if unit['kind']!='page':continue
                matches=list(re.finditer(pattern,unit['text']))
                if not matches:continue
                # A page containing only the table of contents is not topic evidence.
                if '目录' in unit['text'][:100] and unit['text'].count('……')>2:continue
                key=(document['original_sha256'],route,unit['locator'])
                if key in seen:continue
                seen.add(key); start=max(0,matches[0].start()-160)
                item={'company':document['ticker'],'period':document['period'],'route_id':route,'document_class':document['document_class'],
                    'original_sha256':document['original_sha256'],'original_url':document['url'],'locator':unit['locator'],
                    'parser_version':parsed['parser_version'],'text':unit['text'][start:start+2200],
                    'matched_terms':sorted({m.group(0) for m in matches}), 'data_nature':'source_text',
                    'semantic_status':'organized_literal_source_passage','consumption_status':'source_text_evidence',
                    'question_answer_status':'not_evaluated','numeric_formula_eligible':False}
                item['evidence_id']='document-evidence-'+canonical_sha256(item)[:24];evidence.append(item);count+=1
                if count>=4:break
    from calendar import monthrange
    for observation in read_jsonl(output/'official-table-facts.jsonl'):
        matching=[d for d in documents.values() if d.get('original_sha256')==observation['original_sha256']]
        year,month=map(int,observation['period'].split('-'))
        for document in matching:
            item={'company':document['ticker'],'period':date(year,month,monthrange(year,month)[1]).isoformat(),
                'route_id':'RD09','document_class':'D19','original_sha256':observation['original_sha256'],
                'original_url':observation['url'],'locator':observation['locator'],
                'text':observation['metric_label']+': '+observation['original_value']+'%',
                'data_nature':'official_industry_observation','semantic_status':'explicit_header_and_unit_verified',
                'consumption_status':'source_text_evidence','question_answer_status':'partial_month_only',
                'numeric_formula_eligible':False,'official_table_fact_id':observation['fact_id']}
            item['evidence_id']='document-evidence-'+canonical_sha256(item)[:24];evidence.append(item)
    with (output/'document-evidence.jsonl').open('w',encoding='utf8') as stream:
        for item in sorted(evidence,key=lambda x:x['evidence_id']):stream.write(canonical_json(item)+'\n')
    write_json(output/'document-evidence-summary.json',{'evidence_count':len(evidence),'route_counts':dict(Counter(e['route_id'] for e in evidence)),
        'company_counts':dict(Counter(e['company'] for e in evidence)),'semantic_boundary':'source statements; no automatic research conclusion or numerical table inference'})
    counts=Counter(e['original_sha256'] for e in evidence)
    for document in payload['documents']:
        count=counts.get(document.get('original_sha256'),0)
        if count:
            document.update(semantic_status='source_passages_organized',consumption_status='source_text_evidence',
                question_answer_status='not_evaluated',organized_passage_count=count)
    if live_path.exists():write_json(live_path,payload)
    print(canonical_json({'organized_document_passages':len(evidence)}),flush=True)
    return evidence


if __name__=='__main__':
    raise SystemExit(main())
