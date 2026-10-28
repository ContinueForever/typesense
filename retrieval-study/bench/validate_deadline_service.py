#!/usr/bin/env python3
"""Probe real HTTP deadlines after deletes leave conservative ART score bounds.

Diagnostic samples only: no fixed wall-clock guarantee or performance percentile
is inferred from this small sample. Partial deadline results may differ.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def documents(groups):
    for i in range(groups):
        for suffix in 'abc':
            yield dict(id=f'{i:09d}{suffix}', code=f'918{i:09d}{suffix}',
                       sibling=suffix, tenant='target' if i == 0 else 'other',
                       score=1_000_000_000 + i if suffix == 'a' else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--source')
    parser.add_argument('--output', required=True)
    parser.add_argument('--groups', type=study.positive, default=32768)
    parser.add_argument('--repeats', type=study.positive, default=5)
    args = parser.parse_args()
    if args.groups < 2048 or args.groups > 100000:
        parser.error('diagnostic groups must be between 2048 and 100000')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    study.save_json(output / 'environment.json', study.environment(args.source, args.binary))
    study.save_json(output / 'fixture.json', dict(groups=args.groups, repeats=args.repeats,
        initial_documents=3 * args.groups, live_documents=2 * args.groups,
        generator_sha256=study.sha256(Path(__file__)),
        note='Delete each highest-scoring sibling; preserve two low-scoring sibling terms.'))
    failures, rows = [], []
    with study.owned_server(args.binary, output, 120) as client, \
            (output / 'requests.jsonl').open('w', buffering=1) as samples:
        name = 'deadline_validation_' + uuid.uuid4().hex[:12]
        client.json('POST', '/collections', dict(name=name, default_sorting_field='score', fields=[
            dict(name='code', type='string'), dict(name='sibling', type='string', facet=True),
            dict(name='tenant', type='string', facet=True), dict(name='score', type='int64')]))
        base = '/collections/' + name
        study.save_json(output / 'debug.json', client.json('GET', '/debug'))
        batch, imported = [], 0
        def import_batch():
            nonlocal imported
            with client.request('POST', base + '/documents/import',
                                ('\n'.join(batch) + '\n').encode()) as response:
                confirmations = [json.loads(line) for line in response if line.strip()]
            if len(confirmations) != len(batch) or not all(r.get('success') for r in confirmations):
                raise RuntimeError('Every synthetic fixture import must succeed')
            imported += len(batch)
            batch.clear()
        for doc in documents(args.groups):
            batch.append(study.canonical(doc))
            if len(batch) == 1000:
                import_batch()
        if batch:
            import_batch()
        if client.json('GET', base)['num_documents'] != imported:
            raise RuntimeError('Imported collection count differs')
        deleted = client.json('DELETE', base + '/documents', params=dict(filter_by='sibling:=a'))
        if deleted.get('num_deleted') != args.groups:
            raise RuntimeError('Expected every high-scoring sibling to be deleted')
        with client.request('GET', base + '/documents/export') as response:
            exported = study.multiset_digest(json.loads(line) for line in response if line.strip())
        expected = study.multiset_digest(d for d in documents(args.groups) if d['sibling'] != 'a')
        if exported != expected or client.json('GET', base)['num_documents'] != 2 * args.groups:
            raise RuntimeError('Post-delete fixture content differs')
        study.save_json(output / 'import-verification.json', dict(imported=imported, deleted=deleted,
                                                                 expected=expected, exported=exported))
        fixed = dict(q='918', query_by='code', prefix='true', num_typos=0,
            use_cache='false', enable_synonyms='false', split_join_tokens='off',
            drop_tokens_threshold=0, typo_tokens_threshold=0, max_candidates=1,
            sort_by='_text_match:desc,score:desc', per_page=10, search_cutoff_ms=5000)
        def request(label, params, repetition, warmup=False):
            row = study.sample(client, base + '/documents/search', dict(id=label, params=params))
            row.update(repetition=repetition, warmup=warmup)
            samples.write(study.canonical(row) + '\n')
            if not warmup:
                rows.append(row)
            if row['outcome'] not in ('success', 'cutoff') and row.get('status') != 408:
                failures.append(dict(query_id=label, repetition=repetition, outcome=row['outcome']))
            return row
        for r in range(3):
            request('warmup', fixed, r, warmup=True)
        for r in range(args.repeats):
            for label, extra in [('unfiltered', {}), ('target', dict(filter_by='tenant:=target'))]:
                for budget in (1, 2, 5, 20, 5000):
                    row = request(f'{label}-budget-{budget}', dict(fixed, **extra, search_cutoff_ms=budget), r)
                    if budget == 5000 and row['outcome'] != 'success':
                        failures.append(dict(query_id=row['query_id'], expected='ordinary successful control'))
            recovery = request('exact-recovery', dict(fixed, q='918000000000b', prefix='false'), r)
            ids = [h['id'] for h in recovery.get('normalised', {}).get('hits', [])]
            if recovery['outcome'] != 'success' or recovery.get('normalised', {}).get('found') != 1 or ids != ['000000000b']:
                failures.append(dict(query_id='exact-recovery', repetition=r, expected_id='000000000b'))
        resources = study.process_resources(client.process.pid)
    summaries = {}
    for key in sorted({r['query_id'] for r in rows}):
        group = [r for r in rows if r['query_id'] == key]
        summaries[key] = dict(samples=len(group),
            outcomes={outcome: sum(r['outcome'] == outcome for r in group) for outcome in sorted({r['outcome'] for r in group})},
            median_elapsed_ms=statistics.median(r['elapsed_ms'] for r in group),
            engine_ms=[r.get('engine_ms') for r in group])
    result = dict(passed=not failures, failures=failures, requests=len(rows), cases=summaries,
                  resources=resources, scope='HTTP diagnostic with controlled deletes; not a formal throughput or percentile benchmark.')
    study.save_json(output / 'summary.json', result)
    print(study.canonical({k: result[k] for k in ('passed', 'requests', 'failures')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
