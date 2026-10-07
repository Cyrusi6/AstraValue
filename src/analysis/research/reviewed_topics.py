"""Expose frozen, user-confirmed filing conclusions through the material catalog."""
from pathlib import Path

from .workspace import ResearchError, digest, sha


def enrich(data, pack, pack_path, state):
    evidence = {row['evidence_id']: row for row in
                pack.get('evidence', []) + pack.get('supplemental_evidence', [])}
    parents = {row['payload']['dataset_id']: row['material_id'] for row in data['items']
               if row['reader'] == 'missing' and row['payload'].get('dataset_id')}
    known = {row['material_id'] for row in data['items']}
    verified = set()
    for row in pack.get('deferred_topic_resolutions', []):
        if (row.get('source_projection_role') != 'reviewed_filing'
                or row.get('decision_basis') != 'user_confirmed'
                or row.get('consumption_status') != 'ready'):
            continue
        if (row.get('available_at') or '')[:10] > state['as_of']:
            continue
        originals = []
        for ident in row.get('evidence_ids', []):
            original = evidence.get(ident)
            if original is None:
                raise ResearchError('reviewed_topic_evidence_missing:' + ident)
            path, expected = original['original_path'], original['original_sha256']
            if (path, expected) not in verified:
                if sha(Path(path)) != expected:
                    raise ResearchError('reviewed_topic_original_changed')
                verified.add((path, expected))
            originals.append({'evidence_id': ident, 'path': path, 'sha256': expected,
                              'source_url': original.get('source_url'),
                              'locator': original.get('locator')})
        if not originals:
            raise ResearchError('reviewed_topic_evidence_required')
        source = pack_path / 'core-pack.json'
        for period in row.get('covered_periods') or [row['period']]:
            if period.split('/')[-1] > state['as_of']:
                continue
            ident = 'material-' + digest([state['snapshot_id'], row['record_id'], period])[:24]
            if ident in known:
                continue
            known.add(ident)
            numeric = row['status'] == 'ready' and bool(row.get('fact_ids'))
            status = ('issuer_disclosed_none_or_not_applicable'
                      if row['status'] in {'verified_none', 'not_applicable'}
                      else 'ready' if numeric else 'original_readable')
            data['items'].append({
                'material_id': ident, 'parent_id': parents.get(row['dataset_id']),
                'category': 'business' if row['dataset_id'] == 'customers_peer' else 'governance',
                'title': row['dataset_id'] + '：' + row['conclusion_text'], 'period': period,
                'status': 'readable', 'status_label': '已有原文', 'reader': 'filing_check',
                'purpose': '已确认的报告披露与结构化结论',
                'payload': {**row, 'period': period, 'state': status,
                            'finding': row['conclusion_text'], 'scope': row.get('scope', period),
                            'path': str(source), 'sha256': sha(source), 'originals': originals},
            })
    return data
