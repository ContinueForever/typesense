#!/usr/bin/env python3
"""Deterministic production-server checks for lazy filters, JOINs and mutations.

The input trace is saved before execution. Ordinary replies include full hit
documents in the comparison; only explicitly signalled short-budget replies may
be partial. This is a correctness workload, not a performance measurement.
"""
import argparse
import copy
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def verify_binary(binary, manifest_path):
    manifest = json.loads(Path(manifest_path).read_text())
    if (manifest.get('status') != 'success' or not manifest.get('provenance_valid') or
            manifest.get('source_after', {}).get('head') != study.UPSTREAM_SHA or
            manifest.get('binary_sha256') != study.sha256(Path(binary))):
        raise ValueError('Binary does not match a completed build at the pinned source')
    return manifest


def semantic(row):
    result = copy.deepcopy(row.get('normalised', {}))
    body = row.get('response', {})
    if 'hits' in body:
        result['documents'] = [hit['document'] for hit in body['hits']]
    if 'grouped_hits' in body:
        result['group_documents'] = [[hit['document'] for hit in group['hits']]
                                     for group in body['grouped_hits']]
    return result


def parameters(**updates):
    return dict(dict(q='918', query_by='code', prefix='true', num_typos=0,
        enable_synonyms='false', split_join_tokens='off', drop_tokens_threshold=0,
        typo_tokens_threshold=0, use_cache='false', max_candidates=512,
        sort_by='score:desc', per_page=30, search_cutoff_ms=5000), **updates)


def initial_random(seed):
    rng = random.Random(seed)
    return [dict(id=f'r{i:04d}', code=rng.choice(['918', '625', '777']) + f'{rng.randrange(40):03d}',
        title=rng.choice(['shared alpha item', 'shared beta item', 'delta alpha', 'beta gamma']),
        org='target' if i % 4 == 0 else 'other', active=i % 2 == 0, score=1000 + i)
        for i in range(384)]


def random_trace(seed, steps):
    rng = random.Random(seed + 1)
    live = {doc['id']: dict(doc) for doc in initial_random(seed)}
    result = []
    for step in range(steps):
        if step % 4 == 0:
            doc_id = f'r{rng.randrange(440):04d}'
            if doc_id in live and rng.random() < 0.35:
                result.append(dict(kind='delete', id=doc_id))
                del live[doc_id]
            else:
                doc = dict(id=doc_id, code=rng.choice(['918', '625', '777']) + f'{rng.randrange(40):03d}',
                    title=rng.choice(['shared alpha item', 'shared beta item', 'delta alpha', 'beta gamma']),
                    org=rng.choice(['target', 'other']), active=rng.choice([False, True]),
                    score=10000 + step)
                live[doc_id] = doc
                result.append(dict(kind='upsert', document=doc))
        else:
            query = rng.choice(['918', '91', '625', '777', '000', '*', '918003'])
            params = parameters(q=query, per_page=rng.choice([1, 7, 30]),
                max_candidates=rng.choice([1, 4, 512]), page=rng.choice([1, 2]),
                enable_lazy_filter=rng.choice(['true', 'false']))
            filter_by = rng.choice(['', 'org:=target', 'org:=absent', 'score:>=1050',
                                    'org:=target && active:=true', 'org:=target || active:=true'])
            if filter_by:
                params['filter_by'] = filter_by
            option = rng.randrange(9)
            if option == 0:
                params.update(q='"shared alpha"', query_by='title')
            elif option == 1:
                params.update(q='alhpa', query_by='title', num_typos=1, typo_tokens_threshold=1)
            elif option == 2:
                params.update(q='shared alp', query_by='code,title')
            elif option == 3:
                params.update(facet_by='org,active')
            elif option == 4:
                params.update(group_by='org', group_limit=3)
            elif option == 5:
                params.update(hidden_hits='r0000,r0001')
            elif option == 6:
                params.update(pinned_hits='r0002:1', filter_curated_hits=rng.choice(['true', 'false']))
            elif option == 7:
                params.update(prefix='false')
            # Wildcard queries without curation/grouping/faceting admit a simple
            # independent oracle, regardless of the candidate cap.
            oracle = params['q'] == '*' and option in (7, 8)
            result.append(dict(kind='query', params=params, oracle=oracle))
    return result


def matching(doc, expr):
    if not expr:
        return True
    if ' || ' in expr:
        return any(matching(doc, part) for part in expr.split(' || '))
    if ' && ' in expr:
        return all(matching(doc, part) for part in expr.split(' && '))
    if expr.startswith('org:='):
        return doc['org'] == expr.removeprefix('org:=')
    if expr == 'active:=true':
        return doc['active']
    if expr.startswith('score:>='):
        return doc['score'] >= int(expr.removeprefix('score:>='))
    raise ValueError('Unsupported independent-oracle predicate: ' + expr)


def wildcard_oracle(docs, params):
    selected = sorted((d for d in docs.values() if matching(d, params.get('filter_by', ''))),
                      key=lambda d: d['score'], reverse=True)
    start = (params.get('page', 1) - 1) * params['per_page']
    return dict(found=len(selected), ids=[d['id'] for d in selected[start:start + params['per_page']]])


def import_docs(client, base, documents):
    for start in range(0, len(documents), 1000):
        batch = documents[start:start + 1000]
        body = ''.join(study.canonical(doc) + '\n' for doc in batch).encode()
        with client.request('POST', base + '/documents/import', body, {'action': 'create'}) as response:
            rows = [json.loads(line) for line in response if line.strip()]
        if len(rows) != len(batch) or any(row.get('success') is not True for row in rows):
            raise RuntimeError('Fixture import failed: ' + study.canonical(rows[:3]))


def verify_export(client, base, documents):
    with client.request('GET', base + '/documents/export') as response:
        digest = study.multiset_digest(json.loads(line) for line in response if line.strip())
    expected = study.multiset_digest(documents)
    if digest != expected or client.json('GET', base)['num_documents'] != len(documents):
        raise RuntimeError('Collection count or full exported document digest differs')
    return digest


def run(args):
    output = Path(args.output)
    manifest = verify_binary(args.binary, args.build_manifest)
    output.mkdir(parents=True, exist_ok=False)
    trace = random_trace(args.seed, args.steps)
    plan = dict(seed=args.seed, steps=args.steps, trace=trace, lazy_documents=32768,
        lazy_thresholds=dict(string=20000, numeric=20000, compute=25000),
        dense_numeric_high_cap_prefix='9180000',
        generator_sha256=study.sha256(Path(__file__)),
        scope='Deterministic correctness; ordinary semantics and full documents; no capacity claims')
    study.save_json(output / 'plan.json', plan)
    study.save_json(output / 'build.json', manifest)
    study.save_json(output / 'environment.json', study.environment(binary=args.binary))
    failures, checked, counts, exports = [], 0, {}, {}
    with study.owned_server(args.binary, output, 30) as client, \
            (output / 'requests.jsonl').open('w', buffering=1) as stream, \
            (output / 'mutations.jsonl').open('w', buffering=1) as mutations:
        def query(base, query_id, params, expected=None, finite=False):
            nonlocal checked
            row = study.sample(client, base + '/documents/search', dict(id=query_id, params=params))
            row['semantic'] = semantic(row)
            row['finite_budget'] = finite
            stream.write(study.canonical(row) + '\n')
            counts['searches'] = counts.get('searches', 0) + 1
            explicit = row.get('status') == 408 or row.get('normalised', {}).get('search_cutoff') is True
            if row['outcome'] != 'success' and not (finite and explicit):
                failures.append(dict(query_id=query_id, reason='Unexpected error/cutoff', row=row))
            if expected is not None and row['outcome'] == 'success':
                observed = dict(found=row['normalised'].get('found'),
                                ids=[h['id'] for h in row['normalised'].get('hits', [])])
                checked += 1
                if observed != expected:
                    failures.append(dict(query_id=query_id, expected=expected, observed=observed))
            return row

        fields = [dict(name='code', type='string'), dict(name='title', type='string'),
            dict(name='org', type='string', facet=True), dict(name='active', type='bool', facet=True),
            dict(name='score', type='int64')]
        client.json('POST', '/collections', dict(name='extended_lazy', default_sorting_field='score', fields=fields))
        base = '/collections/extended_lazy'
        docs = [dict(id=f'l{i:05d}', code='918' + f'{i % 256:04d}', title='shared alpha item',
                     org='wide' if i < 28672 else 'narrow', active=i % 2 == 0, score=i)
                for i in range(32768)]
        import_docs(client, base, docs)
        exports['lazy_initial'] = verify_export(client, base, docs)
        filters = ['org:=wide', 'score:>=0', 'org:!=narrow', 'org:=wide && score:>=16',
                   'org:=narrow || score:>=0', 'org:=absent']
        # The first five estimates exceed the production compute threshold.
        # This records a source-derived path precondition, not dynamic tracing.
        study.save_json(output / 'lazy-path-preconditions.json', dict(documents=len(docs),
            wide_matches=28672, numeric_matches=32768, source_files=[
                'include/filter.h', 'include/filter_result_iterator.h',
                'src/filter_result_iterator.cpp', 'src/index.cpp'],
            qualification='Source-derived thresholds; no additional tracing in measured production binaries'))
        for stage in ('initial', 'mutated', 'recovered'):
            for lazy in ('false', 'true'):
                for index, predicate in enumerate(filters):
                    for cap in (1, 512):
                        query(base, f'lazy/{stage}/{lazy}/{index}/{cap}', parameters(
                            q='9180000' if index in (1, 3, 4) and cap == 512 else '918',
                            filter_by=predicate, enable_lazy_filter=lazy, max_candidates=cap))
                # A wildcard has no term-candidate truncation; verify it independently.
                selected = sorted((d for d in docs if d['org'] == 'wide'), key=lambda d: d['score'], reverse=True)
                query(base, f'lazy/{stage}/{lazy}/oracle', parameters(q='*', filter_by='org:=wide',
                      enable_lazy_filter=lazy), dict(found=len(selected), ids=[d['id'] for d in selected[:30]]))
                if stage != 'recovered':
                    query(base, f'lazy/{stage}/{lazy}/curation-control', parameters(
                        filter_by='org:=wide', enable_lazy_filter=lazy, pinned_hits='l00000:1',
                        hidden_hits='l00001', filter_curated_hits='true'))
                    for budget in (0, 1):
                        query(base, f'lazy/{stage}/{lazy}/curation-budget-{budget}', parameters(
                            filter_by='org:=wide', enable_lazy_filter=lazy, search_cutoff_ms=budget,
                            pinned_hits='l00000:1', hidden_hits='l00001', filter_curated_hits='true'), finite=True)
            if stage == 'initial':
                for doc_id in ('l00000', 'l28671', 'l32767'):
                    response = client.json('DELETE', base + '/documents/' + doc_id)
                    mutations.write(study.canonical(dict(stage=stage, kind='delete', id=doc_id, response=response)) + '\n')
                    docs = [doc for doc in docs if doc['id'] != doc_id]
                exports['lazy_mutated'] = verify_export(client, base, docs)
            elif stage == 'mutated':
                doc = dict(id='l00000', code='9189999', title='restored alpha', org='wide', active=True, score=90000)
                response = client.json('POST', base + '/documents', doc)
                mutations.write(study.canonical(dict(stage=stage, kind='reinsert', document=doc, response=response)) + '\n')
                docs.append(doc)
                exports['lazy_recovered'] = verify_export(client, base, docs)

        client.json('POST', '/collections', dict(name='extended_brands', fields=[
            dict(name='name', type='string'), dict(name='rating', type='int32')]))
        brands = [dict(id=f'b{i:02d}', name='brand' + str(i), rating=i % 5) for i in range(16)]
        import_docs(client, '/collections/extended_brands', brands)
        client.json('POST', '/collections', dict(name='extended_join', default_sorting_field='score', fields=fields + [
            dict(name='brand', type='string', reference='extended_brands.id')]))
        base = '/collections/extended_join'
        docs = [dict(doc, id=f'j{i:04d}', brand=brands[i % 16]['id'])
                for i, doc in enumerate(initial_random(args.seed))]
        import_docs(client, base, docs)
        for stage in ('initial', 'updated'):
            for lazy in ('false', 'true'):
                for i, expr in enumerate(['$extended_brands(rating:>=3)',
                        '$extended_brands(rating:<0)', '$extended_brands(rating:>=0) && org:=target',
                        '$extended_brands(rating:>=3) || org:=target']):
                    for q in ('918', '*'):
                        params = parameters(q=q, filter_by=expr, enable_lazy_filter=lazy,
                            include_fields='id,code,score,brand,$extended_brands(id,name,rating, strategy:nest) as maker')
                        row = query(base, f'join/{stage}/{lazy}/{i}/{q}', params)
                        if row['outcome'] == 'success':
                            lookup = {brand['id']: brand for brand in brands}
                            for hit in row['response']['hits']:
                                doc = hit['document']
                                if doc.get('maker') != lookup[doc['brand']]:
                                    failures.append(dict(query_id=row['query_id'], reason='Incorrect joined document', document=doc))
                            checked += 1
            if stage == 'initial':
                response = client.json('PATCH', '/collections/extended_brands/documents/b00', dict(rating=4, name='changed'))
                brands[0].update(rating=4, name='changed')
                mutations.write(study.canonical(dict(kind='join-update', response=response)) + '\n')
        exports['brands_final'] = verify_export(client, '/collections/extended_brands', brands)
        exports['join_final'] = verify_export(client, base, docs)

        client.json('POST', '/collections', dict(name='extended_random', default_sorting_field='score', fields=fields))
        base = '/collections/extended_random'
        live = {doc['id']: doc for doc in initial_random(args.seed)}
        import_docs(client, base, list(live.values()))
        for step, op in enumerate(trace):
            if op['kind'] == 'query':
                expected = wildcard_oracle(live, op['params']) if op['oracle'] else None
                query(base, f'random/{step:05d}', op['params'], expected)
            elif op['kind'] == 'delete':
                response = client.json('DELETE', base + '/documents/' + op['id'])
                if response['id'] != op['id']:
                    raise RuntimeError('Delete ID mismatch')
                del live[op['id']]
                mutations.write(study.canonical(dict(step=step, operation=op, response=response)) + '\n')
            else:
                doc = op['document']
                response = client.json('POST', base + '/documents', doc, dict(action='upsert'))
                if response != doc:
                    raise RuntimeError('Upsert response differs from planned full document')
                live[doc['id']] = doc
                mutations.write(study.canonical(dict(step=step, operation=op, response=response)) + '\n')
        exports['random_final'] = verify_export(client, base, list(live.values()))
        study.save_json(output / 'debug.json', client.json('GET', '/debug'))
        study.save_json(output / 'resources-final.json', study.process_resources(client.process.pid))
    result = dict(passed=not failures, failures=failures, oracle_checks=checked,
        counts=counts, export_digests=exports, trace_operations=len(trace))
    study.save_json(output / 'summary.json', result)
    print(study.canonical({k: result[k] for k in ('passed', 'oracle_checks', 'counts', 'trace_operations')}))
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--build-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seed', type=int, default=3130)
    parser.add_argument('--steps', type=study.positive, default=400)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
