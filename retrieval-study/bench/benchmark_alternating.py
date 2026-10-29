#!/usr/bin/env python3
"""Alternate pinned production binaries with raw samples and resource accounting.

One host, serial closed loop, fixed CPU sets, fresh identical indexes per run.
This measures the stated local workload, not an independent service capacity.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import random
import resource
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.validate_deadline_service import documents
from bench.validate_extended import import_docs, parameters, verify_binary, verify_export


def host_resources():
    return dict(monotonic_ns=time.monotonic_ns(), recorded_at_utc=study.utc_now(),
        memory=Path('/proc/meminfo').read_text(), cpu=Path('/proc/stat').read_text(),
        vmstat=Path('/proc/vmstat').read_text(), loadavg=Path('/proc/loadavg').read_text(),
        pressure={kind: Path('/proc/pressure', kind).read_text() for kind in ('cpu', 'memory', 'io')},
        client_cpu_seconds=resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime)


def distribution(rows):
    values = sorted(row['elapsed_ms'] for row in rows)
    result = dict(samples=len(values), median_ms=statistics.median(values),
        min_ms=values[0], max_ms=values[-1],
        outcomes={outcome: sum(row['outcome'] == outcome for row in rows)
                  for outcome in sorted({row['outcome'] for row in rows})},
        statuses={str(status): sum(row.get('status') == status for row in rows)
                  for status in {row.get('status') for row in rows}},
        population='All recorded client completions, including explicit cutoffs and transport failures')
    # The complete request population is retained; no timeout/error trimming.
    if len(values) >= 10000:
        result['p99_ms'] = values[(99 * len(values) + 99) // 100 - 1]
    return result


def cases():
    fixed = parameters(max_candidates=1, sort_by='_text_match:desc,score:desc', per_page=10)
    result = []
    for name, extra in [('unfiltered', {}), ('target', dict(filter_by='tenant:=target'))]:
        for label, budget in [('short', 1), ('full', 5000)]:
            result.append(dict(id=name + '-' + label, params=dict(fixed, **extra, search_cutoff_ms=budget)))
    result.append(dict(id='exact', params=dict(fixed, q='918000000000b', prefix='false')))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for variant in ('baseline', 'candidate'):
        parser.add_argument('--' + variant, required=True)
        parser.add_argument('--' + variant + '-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--groups', type=study.positive, default=32768)
    parser.add_argument('--samples-per-case', type=study.positive, default=3500)
    parser.add_argument('--order', default='ABBAAB')
    parser.add_argument('--warmup', type=study.positive, default=20)
    parser.add_argument('--seed', type=int, default=3130)
    parser.add_argument('--writes-per-phase', type=study.positive, default=200)
    args = parser.parse_args()
    if not args.order or not set(args.order) <= {'A', 'B'} or args.order.count('A') != args.order.count('B'):
        parser.error('Order must have equal positive A and B counts')
    if not 2048 <= args.groups <= 100000:
        parser.error('groups must be 2048..100000')
    binaries = {'A': str(Path(args.baseline).resolve()), 'B': str(Path(args.candidate).resolve())}
    builds = {'A': verify_binary(args.baseline, args.baseline_manifest),
              'B': verify_binary(args.candidate, args.candidate_manifest)}
    for key in ('command', 'build_compatibility_after', 'dependency_download_cache'):
        if builds['A'].get(key) != builds['B'].get(key):
            raise ValueError('Different build configuration: ' + key)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    affinity = sorted(os.sched_getaffinity(0))
    if len(affinity) < 3:
        raise RuntimeError('This colocated protocol needs two server CPUs and a separate client CPU')
    server_cpus, client_cpu = affinity[:2], affinity[2]
    all_cases = cases()
    plan = dict(config={k: v for k, v in vars(args).items() if k not in
        ('baseline', 'candidate', 'baseline_manifest', 'candidate_manifest', 'output')},
        binaries={label: build['binary_sha256'] for label, build in builds.items()},
        source=study.UPSTREAM_SHA, server_cpus=server_cpus, client_cpu=client_cpu,
        cases=all_cases, producer_sha256=study.sha256(Path(__file__)),
        planned_measured_requests=len(args.order) * len(all_cases) * args.samples_per_case,
        planned_write_requests=len(args.order) * 3 * args.writes_per_phase,
        model='Serial closed loop; one client CPU and two server CPUs on the same desktop VM. Other host work is observed, not excluded.',
        exclusions='No independent service capacity, production tail latency, physical CPU isolation or hard deadline guarantee.')
    study.save_json(output / 'plan.json', plan)
    study.save_json(output / 'builds.json', builds)
    study.save_json(output / 'environment.json', study.environment())
    accumulated = {variant: {case['id']: [] for case in all_cases} for variant in 'AB'}
    rounds, failures, golden = [], [], None
    for index, variant in enumerate(args.order):
        folder = output / f'{index:02d}-{variant}'
        folder.mkdir()
        os.sched_setaffinity(0, affinity)
        rows = []
        with study.owned_server(binaries[variant], folder, 30, cpu_set=server_cpus) as client, \
                gzip.open(folder / 'requests.jsonl.gz', 'wt', compresslevel=6) as stream:
            os.sched_setaffinity(0, {client_cpu})
            base = '/collections/deadline_benchmark'
            client.json('POST', '/collections', dict(name='deadline_benchmark', default_sorting_field='score', fields=[
                dict(name='code', type='string'), dict(name='sibling', type='string', facet=True),
                dict(name='tenant', type='string', facet=True), dict(name='score', type='int64')]))
            import_docs(client, base, list(documents(args.groups)))
            deleted = client.json('DELETE', base + '/documents', params=dict(filter_by='sibling:=a'))
            if deleted.get('num_deleted') != args.groups:
                raise RuntimeError('Wrong deletion count')
            digest = verify_export(client, base, [d for d in documents(args.groups) if d['sibling'] != 'a'])
            study.save_json(folder / 'fixture.json', dict(imported=3 * args.groups, live=2 * args.groups, digest=digest))
            controls = {}
            control_rows = []
            for case in all_cases:
                if case['id'].endswith('-short'):
                    continue
                row = study.sample(client, base + '/documents/search', case)
                if row['outcome'] != 'success':
                    raise RuntimeError('Full-budget control did not complete')
                controls[case['id']] = row['normalised']
                control_rows.append(row)
            study.save_json(folder / 'controls.json', control_rows)
            if golden is None:
                golden = controls
                study.save_json(output / 'ordinary-controls.json', controls)
            if controls != golden:
                raise RuntimeError('Ordinary results differ between rounds or binaries')
            sequence = []
            rng = random.Random(args.seed)
            for repetition in range(args.samples_per_case):
                shuffled = list(all_cases)
                rng.shuffle(shuffled)
                sequence.extend((repetition, case) for case in shuffled)
            for warmup in range(args.warmup):
                for case in all_cases:
                    row = study.sample(client, base + '/documents/search', case)
                    row.update(phase='warmup', repetition=warmup, variant=variant, run=index)
                    stream.write(study.canonical(row) + '\n')
                    explicit = row.get('status') == 408 or row.get('normalised', {}).get('search_cutoff') is True
                    if row['outcome'] != 'success' and not (case['id'].endswith('-short') and explicit):
                        failures.append(dict(run=index, phase='warmup', reason='Unexpected warmup failure'))
            before = dict(host=host_resources(), server=study.process_resources(client.process.pid))
            resource_samples = []
            measured_start = time.monotonic()
            for number, (repetition, case) in enumerate(sequence):
                row = study.sample(client, base + '/documents/search', case)
                row.update(phase='measure', repetition=repetition, sequence=number, variant=variant, run=index)
                stream.write(study.canonical(row) + '\n')
                # Retain only the fields needed for percentile recomputation in
                # memory; full replies remain in the compressed raw evidence.
                compact = {k: row.get(k) for k in ('query_id', 'elapsed_ms', 'engine_ms', 'status', 'outcome')}
                rows.append(compact)
                accumulated[variant][case['id']].append(compact)
                control_key = case['id'].replace('-short', '-full')
                explicit = row.get('status') == 408 or row.get('normalised', {}).get('search_cutoff') is True
                if row['outcome'] == 'success':
                    if row['normalised'] != controls[control_key]:
                        failures.append(dict(run=index, sequence=number, reason='Uncut result differs from full control'))
                elif not (case['id'].endswith('-short') and explicit):
                    failures.append(dict(run=index, sequence=number, reason='Unexpected failure or ordinary cutoff',
                                         outcome=row['outcome'], status=row.get('status')))
                if number % 500 == 0:
                    resource_samples.append(dict(sequence=number, host=host_resources(),
                                                 server=study.process_resources(client.process.pid)))
                    study.save_json(output / 'progress.json', dict(run=index, variant=variant,
                        recorded_in_round=len(rows), planned_in_round=len(sequence), failures=len(failures)))
            elapsed = time.monotonic() - measured_start
            after = dict(host=host_resources(), server=study.process_resources(client.process.pid))
            study.save_json(folder / 'resources.json', dict(before=before, after=after, samples=resource_samples))
            # A separate write phase checks ordinary write cost without changing
            # the measured query fixture. It is not mixed-load capacity data.
            client.json('POST', '/collections', dict(name='write_control', default_sorting_field='score', fields=[
                dict(name='code', type='string'), dict(name='score', type='int64')]))
            write_before = dict(host=host_resources(), server=study.process_resources(client.process.pid))
            write_rows = []
            with gzip.open(folder / 'writes.jsonl.gz', 'wt', compresslevel=6) as write_log:
                for action in ('create', 'update', 'delete'):
                    for i in range(args.writes_per_phase):
                        doc = dict(id=f'w{i:05d}', code=f'918{i:09d}',
                                   score=i + (10000 if action in ('update', 'delete') else 0))
                        row = dict(query_id=action, index=i, variant=variant, run=index)
                        started = time.perf_counter_ns()
                        try:
                            if action == 'delete':
                                request = client.request('DELETE', '/collections/write_control/documents/' + doc['id'])
                            else:
                                request = client.request('POST', '/collections/write_control/documents', doc,
                                                         dict(action='create' if action == 'create' else 'upsert'))
                            with request as reply:
                                response = json.load(reply)
                                row['status'] = reply.status
                            row.update(outcome='success', response=response)
                            if response != doc:
                                raise RuntimeError('Write reply differs from intended document')
                        except Exception as exc:
                            row.update(status=getattr(exc, 'code', None), outcome='error', error=str(exc))
                            failures.append(dict(run=index, phase='write', action=action, index=i, reason=str(exc)))
                        row['elapsed_ms'] = (time.perf_counter_ns() - started) / 1e6
                        write_log.write(study.canonical(row) + '\n')
                        write_rows.append(row)
            write_after = dict(host=host_resources(), server=study.process_resources(client.process.pid))
            if client.json('GET', '/collections/write_control')['num_documents'] != 0:
                failures.append(dict(run=index, phase='write', reason='Nonempty collection after deleting all controls'))
            study.save_json(folder / 'write-resources.json', dict(before=write_before, after=write_after))
        round_result = dict(index=index, variant=variant, planned=len(sequence), recorded=len(rows),
            elapsed_seconds=elapsed, completed_closed_loop_per_second=len(rows) / elapsed,
            server_cpu_seconds=sum(after['server'][key] - before['server'][key]
                for key in ('user_cpu_seconds', 'system_cpu_seconds')),
            client_cpu_seconds=after['host']['client_cpu_seconds'] - before['host']['client_cpu_seconds'],
            cases={case['id']: distribution([row for row in rows if row['query_id'] == case['id']]) for case in all_cases},
            writes={action: distribution([row for row in write_rows if row['query_id'] == action])
                    for action in ('create', 'update', 'delete')}, write_requests=len(write_rows),
            write_server_cpu_seconds=sum(write_after['server'][key] - write_before['server'][key]
                for key in ('user_cpu_seconds', 'system_cpu_seconds')))
        rounds.append(round_result)
        study.save_json(folder / 'summary.json', round_result)
        print(study.canonical(dict(round=index, variant=variant, recorded=len(rows), failures=len(failures))), flush=True)
    os.sched_setaffinity(0, affinity)
    recorded = sum(round_result['recorded'] for round_result in rounds)
    if (recorded != plan['planned_measured_requests'] or
            sum(row['write_requests'] for row in rounds) != plan['planned_write_requests']):
        failures.append(dict(reason='Missing measured requests'))
    summary = dict(passed=not failures, failures=failures, planned=plan['planned_measured_requests'],
        recorded=recorded, rounds=rounds,
        aggregate={variant: {name: distribution(rows) for name, rows in queries.items()}
                   for variant, queries in accumulated.items()}, model=plan['model'], exclusions=plan['exclusions'])
    study.save_json(output / 'summary.json', summary)
    print(study.canonical(dict(passed=summary['passed'], recorded=recorded)))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
