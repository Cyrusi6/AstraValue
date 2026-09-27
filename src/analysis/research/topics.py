"""Cross-period reading views; never mutate source facts or infer zero from blanks."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .workspace import ResearchError, digest, sha


def clean(value):
    return re.sub(r'\s+', '', value or '')


def number(value):
    value = clean(value).replace(',', '').replace('，', '')
    if value == '':
        return None, 'disclosed_blank'
    if value in {'-', '—', '/', '不适用'}:
        return None, 'not_applicable_or_dash'
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise InvalidOperation
        return str(result), 'ready'
    except InvalidOperation:
        raise ValueError('unrecognized_numeric_cell')


def matrix(cells):
    """Align like definitions only, retaining conflicting observations and all refs."""
    groups = defaultdict(list)
    for c in cells:
        key = tuple(c.get(k) for k in ('table', 'period_type', 'scope', 'unit', 'currency'))
        groups[key].append(c)
    output = []
    for key, observations in sorted(groups.items(), key=lambda x: str(x[0])):
        periods = sorted({c['period'] for c in observations})
        rows = {}
        for c in observations:
            rows.setdefault(c['label'], defaultdict(list))[c['period']].append(c)
        aligned = []
        for label, dates in rows.items():
            values, states, refs = [], [], []
            for period in periods:
                obs = dates.get(period, [])
                distinct = {digest([c['value'], c['state']]) for c in obs}
                values.append(obs[0]['value'] if len(distinct) == 1 else None)
                states.append(obs[0]['state'] if len(distinct) == 1 else 'conflict' if obs else 'missing')
                refs.append(sorted({c['ref'] for c in obs}))
            row = {'label': label, 'values': values, 'states': states, 'refs': refs}
            conflicts = {p: dates[p] for p, state in zip(periods, states) if state == 'conflict'}
            if conflicts:
                row['conflicts'] = conflicts
            aligned.append(row)
        output.append(dict(zip(('table', 'period_type', 'scope', 'unit', 'currency'), key)) |
                      {'periods': periods, 'rows': aligned})
    return output


def metric_cells(rows):
    cells = []
    for row in rows:
        fact = row.get('fact') or {}
        period_type = row['period_type']
        # Annual YTD is comparable with annual YTD; H1/Q1/Q3 get their own panels.
        if period_type == 'cumulative':
            period_type += ':' + row['period'][5:]
        cells.append({'table': '标准指标', 'label': row.get('label', row['metric_id']),
                      'period': row['period'], 'period_type': period_type,
                      'scope': fact.get('scope', 'unspecified'), 'unit': fact.get('unit'),
                      'currency': fact.get('currency'), 'value': fact.get('value'),
                      'state': row['state'], 'ref': row.get('fact_ref') or row.get('requirement_id', row['metric_id'])})
    return cells


def parse_note_table(rows, title, period, unit, ref):
    """Recognize explicit current/opening stock columns, not arbitrary PDF numbers."""
    if len(rows) < 3:
        return []
    header = [clean(x) for x in rows[0]]
    inventory = title == '存货构成与减值'
    period_type = 'instant'
    if inventory:
        if len(header) != 7 or header[1] != '期末余额' or header[4] != '期初余额':
            return []
        second = [clean(x) for x in rows[1]]
        if len(second) != 7 or second[1] != '账面余额' or second[3] != '账面价值' or '准备' not in second[2]:
            return []
        columns, start = [(1, '账面余额'), (2, '减值准备'), (3, '账面价值')], 2
    else:
        stock = len(header) == 3 and header[1] in {'期末余额', '期末数'} and header[2] in {'期初余额', '期初数'}
        flow = len(header) == 3 and header[1] in {'本期发生额', '本期金额'} and header[2] in {'上期发生额', '上期金额'}
        if not stock and not flow:
            return []
        if flow:
            period_type = 'cumulative:' + period[5:]
        columns, start = [(1, '本期发生额' if flow else '期末余额')], 1
    cells = []
    names = set()
    try:
        for row in rows[start:]:
            if len(row) != len(header):
                return []
            label = clean(row[0])
            if not label or label in names:
                return []
            names.add(label)
            for col, measure in columns:
                value, state = number(row[col])
                cells.append({'table': title + ' / ' + measure, 'label': label, 'period': period,
                              'period_type': period_type, 'scope': 'consolidated', 'unit': unit,
                              'currency': 'CNY', 'value': value, 'state': state, 'ref': ref})
        if inventory:
            # Gross and net totals must reconcile without using blank reserves as zero.
            for measure in ('账面余额', '账面价值'):
                selected = [c for c in cells if c['table'].endswith('/ ' + measure)]
                totals = [c for c in selected if c['label'] == '合计']
                parts = [c for c in selected if c['label'] != '合计']
                if len(totals) != 1 or not parts or any(c['value'] is None for c in selected):
                    return []
                if abs(sum(Decimal(c['value']) for c in parts) - Decimal(totals[0]['value'])) > Decimal('0.02'):
                    return []
            for label in names:
                values = [next(c['value'] for c in cells if c['label'] == label and c['table'].endswith('/ ' + m))
                          for m in ('账面余额', '减值准备', '账面价值')]
                if all(v is not None for v in values) and abs(Decimal(values[0])-Decimal(values[1])-Decimal(values[2])) > Decimal('0.02'):
                    return []
    except ValueError:
        return []
    return cells


def extract_note(item, title):
    """Only unambiguous supported tables inside the bounded consolidated note."""
    import fitz
    p = item['payload']
    if sha(Path(p['path'])) != p['sha256']:
        raise ResearchError('material_original_changed')
    candidates = []
    with fitz.open(p['path']) as doc:
        for page_no in range(p['page'], p.get('end_page', p['page']) + 1):
            page = doc[page_no - 1]
            top = p.get('start_y', 0) if page_no == p['page'] else 0
            bottom = p.get('end_y', page.rect.height) if page_no == p.get('end_page') else page.rect.height
            if bottom <= top:
                continue
            clip = fitz.Rect(0, top, page.rect.width, bottom)
            text = clean(page.get_text(clip=clip))
            units = re.findall(r'单位[：:](亿元|万元|千元|元)', text)
            if len(set(units)) != 1 or '人民币' not in text:
                continue
            for table in page.find_tables(clip=clip).tables:
                parsed = parse_note_table(table.extract(), title, item['period'], units[0], item['material_id'])
                if parsed:
                    candidates.append(parsed)
    # Multiple identically headed tables may have different populations: keep originals.
    return candidates[0] if len(candidates) == 1 else []


def group_topics(items, snapshot_id):
    groups = defaultdict(list)
    for item in list(items.values()):
        if item['category'] == 'notes' and item['reader'] == 'pdf':
            title = clean(item['title'])
            if title == '存货':
                title = '存货构成与减值'
            title = {'1年内到期的非流动负债':'一年内到期的非流动负债',
                     '所有权或使用权受到限制的资产':'所有权或使用权受限资产'}.get(title,title)
            groups[('notes', title)].append(item)
        elif item['reader'] == 'statement':
            title = {'balance_sheet': '合并资产负债表', 'income_statement': '合并利润表',
                     'cash_flow_statement': '合并现金流量表'}[item['payload']['statement']]
            groups[('statements', title)].append(item)
    for (category, title), children in groups.items():
        ident = 'material-' + digest([snapshot_id, category, 'topic-v1:' + title])[:24]
        periods = sorted({r['period'] for r in children})
        cells = []
        table_cells=[];processing=[]
        if category == 'notes':
            from .note_tables import extract_tables
            for child in children:
                cells.extend(extract_note(child, title))
                extracted,summary=extract_tables(child,title)
                table_cells.extend(extracted);processing.append(summary)
        for child in children:
            child['parent_id'] = ident
        items[ident] = {'material_id': ident, 'category': category, 'title': title, 'period': periods,
                       'status': 'readable', 'status_label': '已有可读', 'reader': 'topic',
                       'purpose': '跨期数据及逐期原文' if cells or table_cells else '同主题逐期原文',
                       'payload': {'cells': cells, 'table_cells':table_cells,'processing':processing,
                                   'children': [r['material_id'] for r in children]}}


def topic_content(item, data, period=None, table_id=None):
    children = [r for r in data['items'] if r['material_id'] in item['payload']['children']
                and (period is None or r['period'] == period)]
    cells = [c for c in item['payload']['cells'] if period is None or c['period'] == period]
    structured=[c for c in item['payload'].get('table_cells',[]) if period is None or c['period']==period]
    sections=list(dict.fromkeys(c['section'] for c in structured))
    table_index=[]
    for section in sections:
        ident='table-'+digest([item['material_id'],section])[:16]
        table_index.append({'table_id':ident,'title':section,
            'periods':sorted({c['period'] for c in structured if c['section']==section}),
            'read_entry':{'tool':'read_material','material_id':item['material_id'],'table_id':ident}})
    if table_id:
        selected=next((t for t in table_index if t['table_id']==table_id),None)
        if not selected:raise ResearchError('topic_table_not_found')
        cells=[c for c in structured if c['section']==selected['title']]
    elif not cells and sections:
        cells=[c for c in structured if c['section']==sections[0]]
    represented = {c['ref'] for c in cells+structured}
    return {'title': item['title'], 'tables': matrix(cells),
            'table_index':table_index,
            'processing':[{'period':x['period'],'state':x['state'],
                           'processed_tables':x['processed_tables'],'detected_tables':x['detected_tables']}
                          for x in item['payload'].get('processing',[]) if period is None or x['period']==period],
            'sources': [{'period': r['period'], 'material_id': r['material_id'],
                         'data_view': '跨期表' if r['material_id'] in represented else '原文可读',
                         'read_entry': {'tool': 'read_material', 'material_id': r['material_id']}}
                        for r in sorted(children, key=lambda r: (r['period'], r['material_id']))],
            'legend': 'values/states/refs与periods逐列对应；disclosed_blank为原表留空，missing为本表无该项，0才是明确零。filing_column/filing_stock_column按报告期对齐，期初和上期仍按原列名保留，不冒充期末数；table_index可按子表读取。'}
