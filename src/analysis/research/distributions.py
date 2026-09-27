"""Narrow, source-checked announcement arithmetic; no model-supplied fact values."""
import re
from decimal import Decimal
from .workspace import ResearchError
from .calculations import number

METHODS = {'price_change', 'payout_ratio', 'dividend_yield', 'forecast_dividend_yield'}


def calculate_distribution(w, rid, method, bindings, assumptions):
    state, _, pack = w.pack(rid)
    facts = {x['fact_ref']: x for x in pack['metrics'] if x.get('fact')}
    sources, definitions = {}, {}

    def evidence(key):
        e = w.read_evidence(rid, bindings[key], max_tokens=4000)
        if not e.get('original_hash_verified') or e.get('source_role') != 'issuer_disclosure':
            raise ResearchError('verified_issuer_evidence_required')
        if e.get('snapshot_id', state['snapshot_id']) != state['snapshot_id']:
            raise ResearchError('evidence_snapshot_mismatch')
        sources[key] = {k:e.get(k) for k in ('evidence_id','original_sha256','source_url','locator','period')}
        return re.sub(r'\s+', '', e['content'])

    def fact(key, metric, unit):
        x = facts.get(bindings[key])
        if not x or x['metric_id'] != metric or x['fact']['unit'] != unit or x['fact'].get('currency') != 'CNY':
            raise ResearchError('invalid_fact_definition:' + key)
        definitions[key] = x
        return number(x['fact']['value'])

    required = {'price_change': {'start_evidence','end_evidence'},
        'payout_ratio': {'dividend_evidence','earnings'},
        'dividend_yield': {'dividend_evidence','shares','price'},
        'forecast_dividend_yield': {'dividend_evidence','shares','price'}}[method]
    if set(bindings) != required:
        raise ResearchError('required_bindings:' + ','.join(sorted(required)))
    if method != 'forecast_dividend_yield' and assumptions:
        raise ResearchError('historical_calculation_rejects_assumptions')
    if method == 'price_change':
        def price(key):
            text = evidence(key)
            matches = list(re.finditer(r'将([^。；]+?)销售合同价由([\d,]+(?:\.\d+)?)元/瓶(?:调整为|上调至|调整至)([\d,]+(?:\.\d+)?)元/瓶', text))
            if len(matches) != 1:
                raise ResearchError('price_announcement_requires_unique_contract_price_pair')
            m = matches[0]
            sources[key]['quote'] = m.group(0)
            product = re.sub(r'(?:i茅台平台|自营体系)零售价由.*$', '', m.group(1))
            return product, number(m.group(2).replace(',','')), number(m.group(3).replace(',',''))
        product1, start, middle = price('start_evidence')
        product2, previous, end = price('end_evidence')
        if product1 != product2 or middle != previous:
            raise ResearchError('price_product_or_chain_mismatch')
        if not sources['start_evidence']['period'] < sources['end_evidence']['period']:
            raise ResearchError('price_dates_not_increasing')
        if min(start, middle, end) <= 0:
            raise ResearchError('positive_price_required')
        value = end / start - 1
        result = {'value':str(value), 'percent':str(value * 100), 'unit':'ratio',
            'start_price':str(start), 'end_price':str(end), 'absolute_change':str(end-start),
            'product':product1, 'basis':'disclosed_contract_price', 'formula':'end_price/start_price-1'}
    else:
        text = evidence('dividend_evidence')
        matches = list(re.finditer(r'(20\d{2})年度累计派发现金红利([\d,]+(?:\.\d+)?)亿元（([^）]+)）', text))
        if len(matches) != 1:
            raise ResearchError('annual_dividend_total_requires_unique_disclosure')
        m = matches[0]
        dividend = number(m.group(2).replace(',','')) * Decimal('100000000')
        if dividend < 0:
            raise ResearchError('negative_dividend')
        sources['dividend_evidence']['quote'] = m.group(0)
        result = {'dividend_total':str(dividend), 'dividend_year':m.group(1),
            'dividend_basis':m.group(3), 'unit':'ratio'}
        if method == 'payout_ratio':
            earnings = fact('earnings','parent_net_profit','CNY')
            x = definitions['earnings']
            if x['period'] != m.group(1)+'-12-31' or x['period_type'] != 'cumulative' or x['fact'].get('scope') != 'consolidated':
                raise ResearchError('dividend_earnings_year_or_scope_mismatch')
            if earnings <= 0:
                raise ResearchError('positive_earnings_required_for_payout_ratio')
            value = dividend / earnings
            result.update(earnings=str(earnings), formula='annual_dividend_total/annual_parent_net_profit', basis='historical_disclosed_distribution')
        else:
            shares = fact('shares','total_shares','shares')
            price = fact('price','market_price','CNY_per_share')
            if min(shares, price) <= 0:
                raise ResearchError('positive_shares_and_price_required')
            if any(not (x['period_type'] == 'instant' or (x['period_type'] == 'current' and x['fact'].get('period_type') == 'market_quote')) for x in definitions.values()):
                raise ResearchError('instant_market_inputs_required')
            if method == 'forecast_dividend_yield':
                if set(assumptions) != {'dividend_growth','horizon'}:
                    raise ResearchError('explicit_dividend_growth_and_horizon_required')
                for x in assumptions.values():
                    if not isinstance(x,dict) or not str(x.get('reason','')).strip() or 'value' not in x:
                        raise ResearchError('assumption_requires_value_and_reason')
                if assumptions['horizon']['value'] != 'next_12_months':
                    raise ResearchError('next_12_months_horizon_required')
                growth = number(assumptions['dividend_growth']['value'])
                if growth < -1:
                    raise ResearchError('dividend_growth_below_minus_one')
                dividend *= 1 + growth
                result.update(forecast_dividend_total=str(dividend), basis='model_assumption', horizon='next_12_months',
                    formula='historical_dividend_total*(1+dividend_growth)/(current_shares*reference_price)')
            else:
                result.update(basis='historical_dividend_at_reference_price', formula='historical_dividend_total/(current_shares*reference_price)')
            value = dividend / (shares * price)
            result.update(dividend_per_current_share=str(dividend/shares), market_cap=str(shares*price),
                price_date=definitions['price']['fact'].get('period_end',definitions['price']['period']),
                share_date=definitions['shares']['fact'].get('period_end',definitions['shares']['period']),
                share_count_basis='current_shares_held_constant')
        result.update(value=str(value), percent=str(value*100))
    record = w.artifact(rid,'calculation', {'method':method, 'formula_version':'research-distributions-v1',
        'bindings':bindings, 'assumptions':assumptions, 'source_evidence':sources,
        'input_definitions':definitions, 'result':result, 'validation':'deterministic_calculation', 'status':'ready'})
    record['value_reference'] = '{{value:' + record['artifact_id'] + '.value}}'
    return record
