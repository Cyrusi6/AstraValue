"""Materialize all detected note tables, retaining header paths and continuation rows.

These are source-table reading views, not newly admitted financial facts. A report
period column identifies the filing: opening/comparative columns keep their names.
"""
from collections import Counter
from pathlib import Path
import re

from .workspace import ResearchError, sha, digest
from .topics import clean, number

HEADER_LABELS = {'项目','类别','账龄','单位名称','名称','合同分类','种类','被投资单位','债权项目',
                 '关联方','公司名称','债务人名称','借款单位','债券品种'}


def is_number(text):
    try:
        return number(text)[1] == 'ready'
    except ValueError:
        return False


def header_size(rows):
    if not rows:
        return 0
    first = [clean(c) for c in rows[0]]
    # A continuation begins with a real data row or an asset movement group.
    if any(is_number(x) for x in first[1:]) or re.match(r'^[一二三四]+[、.]', first[0]):
        return 0
    if first[0] not in HEADER_LABELS and first[0] and not any(
            re.search(r'期[初末]|本期|上期|账面|金额|比例|余额|合计|房屋|土地|机器|软件|原值|原因', x) for x in first[1:]):
        return 0
    size = 1
    while size < len(rows) and size < 4:
        row = [clean(c) for c in rows[size]]
        if row[0] and row[0] not in HEADER_LABELS:
            break
        if any(is_number(x) for x in row[1:]):
            break
        size += 1
    return size


def expand_headers(rows):
    """Fill horizontal merged parent cells only; vertical blanks inherit parents."""
    if not rows:
        return []
    paths = [[] for _ in rows[0]]
    for row in rows:
        above = [p[:] for p in paths]
        parent = ''
        for col, text in enumerate(row):
            text = clean(text)
            if text:
                parent = text
            elif row[col] is None and (not above[col] or col>0 and above[col]==above[col-1]):
                text = parent
            if text and (not paths[col] or paths[col][-1] != text):
                paths[col].append(text)
    return [' / '.join(p) for p in paths]


def caption(text, fallback):
    """Use the actual subsection, not table ordinal, to separate same-column tables."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    candidates = []
    for i,line in enumerate(lines):
        match = re.match(r'^(?:[（(]\s*\d+\s*[)）][.．、]?|\d+[、．.])\s*(.*)', line)
        if match:
            value = match[1] or (lines[i+1] if i+1 < len(lines) else '')
            if value and not re.match(r'[√□]|单位', value):
                candidates.append(value)
        elif re.search(r'^(?:收到|支付|取得|处置|购建|偿还|筹资活动产生|经营活动产生|投资活动产生|其他说明|按账龄披露|按坏账|按组合|本期实际核销)', line):
            if len(line) <= 65 and not re.search(r'\d[\d,]*\.\d{2}',line):
                candidates.append(line)
        elif line in {'项目列示','应付股利','应付利息','应收股利','应收利息','工程物资'}:
            candidates.append(line)
    value = clean(candidates[-1]) if candidates else fallback
    return value.strip('：:。')


def normalize_header(text, period):
    year = int(period[:4])
    # Date replacement is explicit relative-to-filing, never a relabelled fact date.
    for y, label in ((year, '本报告年'), (year-1, '上报告年')):
        text = re.sub(fr'(?<!\d){y}(?=年|[-/.])', label, text)
    return text


def column_unit(header, default):
    if '%' in header or '％' in header or '比例' in header or '利率' in header:
        return '%'
    if '股数' in header or '数量' in header:
        return '原表列单位'
    return default or '原文未标单位'


def tabulate(rows, headers, title, section, period, unit, ref, page, group='', currency=None):
    cells, issues = [], []
    context = dict(group) if isinstance(group,dict) else {'group':group,'detail':''}
    duplicates = Counter()
    for index, row in enumerate(rows):
        if len(row) != len(headers):
            issues.append('column_count_mismatch')
            continue
        label = clean(row[0])
        if not label:
            if any(clean(x) for x in row[1:]):
                issues.append('row_label_missing')
            continue
        if (re.match(r'^[一二三四五六七八九十]+[、.]',label) or label in {'其中：','其中:', '商品类型'} or re.match(r'^按.+分类$',label)) and all(not clean(x) for x in row[1:]):
            context={'group':re.sub(r'^[一二三四五六七八九十]+[、.]','',label).strip('：:'),'detail':''}
            continue
        row_label = re.sub(r'^\d+[.、．]\s*','',label)
        if re.match(r'^\d+[.、．]',label):
            context['detail']=row_label
        elif re.match(r'^[（(]\d+[)）]',label) and context.get('detail'):
            row_label=context['detail']+' / '+re.sub(r'^[（(]\d+[)）]','',label)
        row_label = context['group'] + ' / ' + row_label if context.get('group') else row_label
        duplicates[row_label] += 1
        if duplicates[row_label] > 1:
            issues.append('repeated_row_label')
        for col, raw in enumerate(row[1:], 1):
            header = headers[col]
            if not header:
                issues.append('column_header_missing')
                continue
            try:
                value, state = number(raw)
            except ValueError:
                value, state = clean(raw), 'disclosed_text'
            cells.append({'table': title + ' / ' + section + ' / ' + normalize_header(header,period),
                          'label': row_label, 'period': period,
                          'period_type': 'filing_stock_column' if re.search(r'期初|期末',header+row_label) else 'filing_column:' + period[5:], 'scope': 'consolidated',
                          'unit': '原文文字' if state=='disclosed_text' else column_unit(header,unit),
                          'currency': currency if unit and unit!='股' and column_unit(header,unit)==unit and state!='disclosed_text' else None,
                          'value': value, 'state': state, 'ref': ref,
                          'locator': {'page':page, 'row':index+1,'column':col+1,'raw':raw},
                          'column':header,'section':section})
    return cells, sorted(set(issues)), context


def extract_tables(item, title):
    import fitz
    p=item['payload']
    if sha(Path(p['path'])) != p['sha256']:
        raise ResearchError('material_original_changed')
    cells=[]; tables=[]; orphan=[]; unit=None; currency=None; previous=None; full_text=[]; carry=''
    with fitz.open(p['path']) as doc:
        for page_no in range(p['page'],p.get('end_page',p['page'])+1):
            page=doc[page_no-1]
            top=p.get('start_y',0) if page_no==p['page'] else 0
            bottom=p.get('end_y',page.rect.height) if page_no==p.get('end_page') else page.rect.height
            if bottom<=top:continue
            clip=fitz.Rect(0,top,page.rect.width,bottom)
            full_text.append(page.get_text(clip=clip))
            detected=page.find_tables(clip=clip,strategy='lines_strict').tables
            cursor=top
            for table in detected:
                rows=table.extract()
                if not rows or max(map(len,rows))<2:continue
                prefix=carry+page.get_text(clip=fitz.Rect(0,cursor,page.rect.width,table.bbox[1]))
                carry=''
                cursor=table.bbox[3]
                matches=re.findall(r'单位\s*[：:]\s*(亿元|万元|千元|元|股)',prefix)
                if matches:unit=matches[-1]
                currencies=re.findall(r'币种\s*[：:]\s*(人民币|美元|港币|港元|欧元)',prefix)
                if currencies:currency={'人民币':'CNY','美元':'USD','港币':'HKD','港元':'HKD','欧元':'EUR'}[currencies[-1]]
                # Unit and currency may be printed in adjacent lines on the previous page.
                if unit is None:
                    note_prefix='\n'.join(full_text)
                    matches=re.findall(r'单位\s*[：:]\s*(亿元|万元|千元|元|股)',note_prefix[:note_prefix.find(rows[0][0] or '\0')] if rows[0][0] else note_prefix)
                    if matches:unit=matches[-1]
                size=header_size(rows)
                section=caption(prefix,title)
                if size:
                    headers=expand_headers(rows[:size])
                    blank_continuation=previous and page_no==previous['last_page']+1 and not any(headers) and len(headers)==len(previous['headers'])
                    if blank_continuation:
                        headers=previous['headers'];section=previous['section']
                    # A group-only header at the foot of the previous page spans the next grid.
                    if previous and not previous['has_data'] and len(headers)!=len(previous['headers']):
                        old=previous['headers']
                        if not headers[0] and len(old)==3 and '期末' in old[1] and '期初' in old[2] and (len(headers)-1)%2==0:
                            half=(len(headers)-1)//2
                            headers=[headers[0]]+[old[1 if i<=half else 2]+' / '+h for i,h in enumerate(headers[1:],1)]
                            section=previous['section']
                            previous['consumed_header']=True
                        elif not headers[0] and len(old)==2 and re.search(r'期末|期初|本期|上期',old[1]):
                            headers=[old[0]]+[old[1]+' / '+h for h in headers[1:]]
                            section=previous['section'];previous['consumed_header']=True
                    # Repeated headers on continuation pages reuse the preceding table context.
                    continued=blank_continuation or (previous and page_no==previous['last_page']+1 and section==title and headers==previous['headers'])
                    if continued:
                        section=previous['section'];current=previous
                    else:
                        current={'headers':headers,'section':section,'group':'','has_data':False,
                                 'pages':[],'issues':[],'cell_count':0,'unit':unit,'last_page':page_no}
                        tables.append(current)
                    current['headers']=headers
                elif previous and len(rows[0])==len(previous['headers']) and page_no<=previous['last_page']+1 and section==title:
                    current=previous;headers=current['headers'];section=current['section']
                else:
                    orphan.append({'page':page_no,'rows':rows,'reason':'continuation_without_matching_header'})
                    previous=None
                    continue
                current['pages'].append(page_no);current['last_page']=page_no
                current['unit']=unit or current['unit']
                parsed,issues,group=tabulate(rows[size:],headers,title,section,item['period'],current['unit'],
                                            item['material_id'],page_no,current['group'],currency)
                for cell in parsed:
                    loc=cell['locator']
                    source_row=loc['row']-1+size
                    bbox=table.rows[source_row].cells[loc['column']-1]
                    loc.update(source_row=source_row+1,bbox=list(bbox) if bbox else None)
                current['issues'].extend(issues);current['group']=group
                current['has_data']=current['has_data'] or bool(parsed)
                current['cell_count']+=len(parsed)
                cells.extend(parsed);previous=current
            # Carry a printed unit on a header-only page to its data continuation.
            trailing=page.get_text(clip=fitz.Rect(0,cursor,page.rect.width,bottom)) if cursor<bottom else ''
            carry+=trailing+'\n'
            matches=re.findall(r'单位\s*[：:]\s*(亿元|万元|千元|元|股)',trailing)
            if matches:unit=matches[-1]
    # Header-only fragments consumed into a wider continuation are not missing tables.
    actual=[t for t in tables if t['has_data']]
    orphan.extend({'page':t['last_page'],'rows':[],'reason':'header_without_data'}
                  for t in tables if not t['has_data'] and not t.get('consumed_header'))
    text='\n'.join(full_text)
    observations={}
    for c in cells:
        key=tuple(c.get(k) for k in ('table','label','period_type','unit','currency'))
        observations.setdefault(key,set()).add((c['state'],c['value']))
    conflicting={key[0] for key,values in observations.items() if len(values)>1}
    if conflicting:
        for t in actual:
            if any(name.startswith(title+' / '+t['section']+' / ') for name in conflicting):
                t['issues'].append('ambiguous_table_alignment')
    no_tables=not actual and not orphan
    explicit_na=no_tables and bool(re.search(r'□\s*适用\s*√\s*不适用',text)) and not re.search(r'\d[\d,]*\.\d{2}',text)
    unresolved=sum(bool(t['issues']) for t in actual)+len(orphan)
    state='no_applicable_table' if explicit_na else 'narrative_only' if no_tables else 'partial' if unresolved else 'detected_tables_processed'
    summary={'period':item['period'],'material_id':item['material_id'],'state':state,
             'detected_tables':len(actual)+len(orphan),'processed_tables':sum(not t['issues'] for t in actual),
             'cells':len(cells),'unresolved_tables':unresolved,
             'tables':[{'section':t['section'],'headers':t['headers'],'pages':sorted(set(t['pages'])),
                        'cells':t['cell_count'],'issues':sorted(set(t['issues']))} for t in actual],
             'pending':orphan}
    return cells,summary
