#!/usr/bin/env python3
"""Exercise real server search, curation, cache, and updates before diffing builds.

Each run owns its server and collection. Only explicitly exhaustive, single-field
queries use an independent oracle; all other cases retain exact response evidence.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def fixtures():
    docs = []
    for i in range(32):
        docs.append(dict(id=f'd{i:02d}', orgId='target' if i < 8 else 'other',
            item_code='918' if i == 0 else ('918' if i % 3 else '625') + f'{i % 13:06d}',
            title='shared alpha item' if i % 2 else 'shared beta item', arc=i % 2 == 0, uOn=1000 + i))
    return docs


def cases():
    fixed = dict(q='918', query_by='item_code', prefix='true', num_typos=0,
        use_cache='false', enable_synonyms='false', split_join_tokens='off',
        drop_tokens_threshold=0, typo_tokens_threshold=0, max_candidates=128,
        sort_by='uOn:desc', per_page=5, page=1, search_cutoff_ms=5000)
    result = []
    for label, filter_by in [('none', None), ('target', 'orgId:=target'),
                              ('absent', 'orgId:=absent'), ('or', 'orgId:=target || arc:=true')]:
        for k in [1, 4, 128]:
            params = dict(fixed, max_candidates=k)
            if filter_by:
                params['filter_by'] = filter_by
            result.append(dict(id=f'{label}-k{k}', params=params,
                               oracle=k == 128 and label != 'or'))
    variants = [
        ('exact', dict(q='918', prefix='false'), True),
        ('no-prefix-match', dict(q='777'), True),
        ('page-two', dict(page=2), True),
        ('business-sort', dict(sort_by='arc:asc,uOn:desc'), False),
        ('text-score', dict(sort_by='_text_match:desc,uOn:desc'), False),
        ('hidden', dict(hidden_hits='d00,d01'), False),
        ('pinned-empty-filter', dict(filter_by='orgId:=absent', pinned_hits='d00:1', filter_curated_hits='false'), False),
        ('pinned-respects-filter', dict(filter_by='orgId:=absent', pinned_hits='d00:1', filter_curated_hits='true'), False),
        ('facets', dict(facet_by='orgId,arc', max_facet_values=20), False),
        ('groups', dict(group_by='orgId', group_limit=3), False),
        ('multiple-fields', dict(q='shared', query_by='item_code,title'), False),
        ('multiple-tokens', dict(q='shared alp', query_by='title'), False),
        ('typo', dict(q='alhpa', query_by='title', num_typos=1), False),
    ]
    for label, updates, oracle in variants:
        result.append(dict(id=label, params=dict(fixed, **updates), oracle=oracle))
    return result


def oracle(documents, params):
    org = params.get('filter_by', '').removeprefix('orgId:=') or None
    docs = [doc for doc in documents if (org is None or doc['orgId'] == org) and
            (doc['item_code'].startswith(params['q']) if params['prefix'] == 'true'
             else doc['item_code'] == params['q'])]
    docs.sort(key=lambda doc: doc['uOn'], reverse=True)
    start = (params['page'] - 1) * params['per_page']
    return dict(found=len(docs), ids=[d['id'] for d in docs[start:start + params['per_page']]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--source')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    documents = fixtures()
    all_cases = cases()
    study.save_json(output / 'environment.json', study.environment(args.source, args.binary))
    study.save_json(output / 'cases.json', all_cases)
    study.save_json(output / 'initial-documents.json', documents)
    failures, checked = [], 0
    with study.owned_server(args.binary, output, 30) as client, \
            (output / 'requests.jsonl').open('w', buffering=1) as samples, \
            (output / 'mutations.jsonl').open('w', buffering=1) as mutations:
        schema = dict(name='retrieval_validation_' + uuid.uuid4().hex[:12], default_sorting_field='uOn', fields=[
            dict(name='orgId', type='string', facet=True), dict(name='item_code', type='string'),
            dict(name='title', type='string'), dict(name='arc', type='bool', facet=True), dict(name='uOn', type='int64')])
        client.json('POST', '/collections', schema)
        base = '/collections/' + schema['name']
        study.save_json(output / 'debug.json', client.json('GET', '/debug'))
        for doc in documents:
            imported = client.json('POST', base + '/documents', doc)
            if imported['id'] != doc['id']:
                raise RuntimeError('Unexpected imported ID')
        def snapshot(stage):
            nonlocal checked
            with client.request('GET', base + '/documents/export') as response:
                exported = study.multiset_digest(json.loads(line) for line in response if line.strip())
            if exported != study.multiset_digest(documents):
                raise RuntimeError('Mutated collection does not match expected contents')
            for case in all_cases:
                row = study.sample(client, base + '/documents/search', case)
                row['stage'] = stage
                samples.write(study.canonical(row) + '\n')
                if row['outcome'] != 'success':
                    failures.append(dict(stage=stage, query_id=case['id'], outcome=row['outcome']))
                    continue
                if case['oracle']:
                    expected = oracle(documents, case['params'])
                    observed = dict(found=row['normalised'].get('found'),
                        ids=[h['id'] for h in row['normalised'].get('hits', [])])
                    checked += 1
                    if expected != observed:
                        failures.append(dict(stage=stage, query_id=case['id'], expected=expected, observed=observed))
            # Retain cache responses separately; mutation-time invalidation is not assumed.
            for repetition in range(2):
                cached_case = dict(id='cached-target', params=dict(all_cases[5]['params'], use_cache='true'))
                row = study.sample(client, base + '/documents/search', cached_case)
                row.update(stage=stage, cache_repetition=repetition)
                samples.write(study.canonical(row) + '\n')
                if row['outcome'] != 'success':
                    failures.append(dict(stage=stage, query_id='cached-target', outcome=row['outcome']))
        snapshot('initial')
        update = dict(item_code='625999999', uOn=9000)
        result = client.json('PATCH', base + '/documents/d01', update)
        mutations.write(study.canonical(dict(action='update', id='d01', body=update, response=result)) + '\n')
        next(d for d in documents if d['id'] == 'd01').update(update)
        for doc_id in ['d02', 'd20']:
            result = client.json('DELETE', base + '/documents/' + doc_id)
            mutations.write(study.canonical(dict(action='delete', id=doc_id, response=result)) + '\n')
            documents[:] = [d for d in documents if d['id'] != doc_id]
        snapshot('updated-and-deleted')
        new_doc = dict(id='d02', orgId='target', item_code='918777777', title='shared alpha restored', arc=False, uOn=9999)
        result = client.json('POST', base + '/documents', new_doc)
        mutations.write(study.canonical(dict(action='reinsert', id='d02', response=result)) + '\n')
        documents.append(new_doc)
        snapshot('reinserted')
        timeout_case = dict(id='zero-budget', params=dict(all_cases[0]['params'], search_cutoff_ms=0))
        timeout_row = study.sample(client, base + '/documents/search', timeout_case)
        timeout_row['stage'] = 'deadline'
        samples.write(study.canonical(timeout_row) + '\n')
        if timeout_row.get('status') != 408 and not timeout_row.get('normalised', {}).get('search_cutoff'):
            failures.append(dict(stage='deadline', expected='408 or explicit cutoff', observed=timeout_row))
        snapshot('after-deadline')
    result = dict(oracle_checks=checked, failures=failures, passed=not failures,
                  stages=['initial', 'updated-and-deleted', 'reinserted', 'after-deadline'],
                  search_requests=4 * (len(all_cases) + 2) + 1,
                  note='Static cache responses retained; no claim of mutation-time cache invalidation.')
    study.save_json(output / 'summary.json', result)
    print(study.canonical(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
