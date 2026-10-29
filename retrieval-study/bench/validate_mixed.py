#!/usr/bin/env python3
"""Exercise shared search/write workers, deadlines and client disconnects.

Stable reads have an independent oracle; concurrent mutable snapshots are not
compared as though they had the same interleaving. Every planned operation is
accounted for. These colocated closed-loop checks do not measure service capacity.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.validate_extended import import_docs, parameters, verify_binary, verify_export
from bench.validate_deadline_service import documents


def write_trace(steps):
    live = {f'w{i:03d}': dict(id=f'w{i:03d}', code=f'9189{i:05d}', partition='mutable', score=1000 + i)
            for i in range(32)}
    initial = list(live.values())
    trace = []
    for i in range(steps):
        doc_id = f'w{i % 32:03d}'
        if i % 3 == 0 and doc_id in live:
            trace.append(dict(kind='delete', id=doc_id))
            del live[doc_id]
        else:
            doc = dict(id=doc_id, code=f'9189{i + 100:07d}', partition='mutable', score=10000 + i)
            trace.append(dict(kind='upsert', document=doc))
            live[doc_id] = doc
    return initial, trace, list(live.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--build-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reads-per-worker', type=study.positive, default=200)
    parser.add_argument('--read-workers', type=study.positive, default=4)
    parser.add_argument('--writes', type=study.positive, default=240)
    parser.add_argument('--flood', type=study.positive, default=160)
    parser.add_argument('--disconnects', type=study.positive, default=32)
    parser.add_argument('--groups', type=study.positive, default=32768)
    args = parser.parse_args()
    if not 2048 <= args.groups <= 100000 or args.read_workers > 16:
        parser.error('Use 2048..100000 groups and at most 16 read workers')
    build = verify_binary(args.binary, args.build_manifest)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    initial, writes, final = write_trace(args.writes)
    planned = args.read_workers * args.reads_per_worker + args.writes + args.flood + args.disconnects
    plan = dict(config={k: v for k, v in vars(args).items() if k not in ('binary', 'build_manifest', 'output')},
        writes=writes, planned_operations=planned, generator_sha256=study.sha256(Path(__file__)),
        deadline_generator_sha256=study.sha256(ROOT / 'bench/validate_deadline_service.py'),
        model='Concurrent closed-loop workers on one host. Not an arrival-rate or capacity benchmark.',
        disconnect_contract='The client sends a complete request then closes its socket; backend cancellation is not assumed.')
    study.save_json(output / 'plan.json', plan)
    study.save_json(output / 'build.json', build)
    study.save_json(output / 'environment.json', study.environment(binary=args.binary))
    failures, rows = [], []
    lock = threading.Lock()
    with study.owned_server(args.binary, output, 30) as client, \
            (output / 'operations.jsonl').open('w', buffering=1) as log:
        stable = [dict(id=f's{i:03d}', code=f'9180{i:05d}', partition='stable', score=i) for i in range(64)]
        client.json('POST', '/collections', dict(name='mixed_items', default_sorting_field='score', fields=[
            dict(name='code', type='string'), dict(name='partition', type='string', facet=True),
            dict(name='score', type='int64')]))
        base = '/collections/mixed_items'
        import_docs(client, base, stable + initial)
        verify_export(client, base, stable + initial)
        client.json('POST', '/collections', dict(name='mixed_deadline', default_sorting_field='score', fields=[
            dict(name='code', type='string'), dict(name='sibling', type='string', facet=True),
            dict(name='tenant', type='string', facet=True), dict(name='score', type='int64')]))
        deadline_base = '/collections/mixed_deadline'
        import_docs(client, deadline_base, list(documents(args.groups)))
        deleted = client.json('DELETE', deadline_base + '/documents', params=dict(filter_by='sibling:=a'))
        if deleted.get('num_deleted') != args.groups:
            raise RuntimeError('Deadline fixture deletion count mismatch')
        deadline_digest = verify_export(client, deadline_base,
                                        [d for d in documents(args.groups) if d['sibling'] != 'a'])
        expected = [d['id'] for d in reversed(stable)]
        read_params = parameters(filter_by='partition:=stable', per_page=64)
        deadline_params = parameters(max_candidates=1, search_cutoff_ms=1,
                                     sort_by='_text_match:desc,score:desc')
        start = time.monotonic_ns()
        before = study.process_resources(client.process.pid)
        gate = threading.Barrier(args.read_workers + 3)

        def retain(row, error=None):
            row['completed_offset_ms'] = (time.monotonic_ns() - start) / 1e6
            with lock:
                rows.append(row)
                log.write(study.canonical(row) + '\n')
                if error:
                    failures.append(dict(operation=row['operation_id'], reason=error))

        def reader(worker):
            gate.wait()
            for i in range(args.reads_per_worker):
                params = dict(read_params, enable_lazy_filter='true' if i % 2 else 'false')
                row = study.sample(client, base + '/documents/search', dict(id=f'read/{worker}/{i}', params=params))
                row.update(operation_id=f'read/{worker}/{i}', kind='read', issued=True,
                           issued_offset_ms=(time.monotonic_ns() - start) / 1e6 - row['elapsed_ms'])
                observed = [hit['id'] for hit in row.get('normalised', {}).get('hits', [])]
                good = row['outcome'] == 'success' and row['normalised']['found'] == 64 and observed == expected
                retain(row, None if good else 'Stable read differs from independent oracle')

        def writer():
            gate.wait()
            for i, operation in enumerate(writes):
                row = dict(operation_id=f'write/{i}', kind='write', issued=True, operation=operation,
                           issued_offset_ms=(time.monotonic_ns() - start) / 1e6)
                error = None
                try:
                    if operation['kind'] == 'delete':
                        response = client.json('DELETE', base + '/documents/' + operation['id'])
                        if response['id'] != operation['id']:
                            raise RuntimeError('Delete reply ID mismatch')
                    else:
                        response = client.json('POST', base + '/documents', operation['document'], dict(action='upsert'))
                        if response != operation['document']:
                            raise RuntimeError('Upsert reply differs from planned document')
                    row.update(outcome='success', response=response)
                except Exception as exc:
                    row.update(outcome='error', error=str(exc))
                    error = 'Write did not complete as planned'
                retain(row, error)

        def flood():
            gate.wait()
            for i in range(args.flood):
                params = dict(deadline_params, search_cutoff_ms=0 if i % 7 == 0 else 1)
                row = study.sample(client, deadline_base + '/documents/search', dict(id=f'deadline/{i}', params=params))
                row.update(operation_id=f'deadline/{i}', kind='deadline', issued=True,
                           issued_offset_ms=(time.monotonic_ns() - start) / 1e6 - row['elapsed_ms'])
                good = row['outcome'] == 'success' or row.get('status') == 408 or row.get('normalised', {}).get('search_cutoff') is True
                retain(row, None if good else 'Deadline query lacks success or explicit cutoff')

        def disconnect():
            gate.wait()
            endpoint = urlsplit(client.url)
            for i in range(args.disconnects):
                params = dict(deadline_params, search_cutoff_ms=5000)
                row = dict(operation_id=f'disconnect/{i}', kind='disconnect', params=params, issued=False,
                           issued_offset_ms=(time.monotonic_ns() - start) / 1e6)
                error = None
                try:
                    path = deadline_base + '/documents/search?' + urlencode(params)
                    request = (f'GET {path} HTTP/1.1\r\nHost: {endpoint.netloc}\r\n'
                               f'X-TYPESENSE-API-KEY: {client.key}\r\nConnection: close\r\n\r\n').encode()
                    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=10) as sock:
                        sock.sendall(request)
                        row.update(issued=True, bytes_sent=len(request), outcome='client_closed_after_send')
                    # This records disconnect behavior without asserting that
                    # the server started or cancelled any particular request.
                except OSError as exc:
                    row.update(outcome='transport_error', error=str(exc))
                    error = 'Client could not send the complete disconnect probe'
                retain(row, error)
                time.sleep(0.002)

        with ThreadPoolExecutor(max_workers=args.read_workers + 3) as pool:
            tasks = [pool.submit(reader, w) for w in range(args.read_workers)]
            tasks += [pool.submit(writer), pool.submit(flood), pool.submit(disconnect)]
            for task in tasks:
                task.result(timeout=600)
        elapsed = (time.monotonic_ns() - start) / 1e9
        after = study.process_resources(client.process.pid)
        final_digest = verify_export(client, base, stable + final)
        for i in range(3):
            recovery = study.sample(client, base + '/documents/search', dict(id=f'recovery/{i}', params=read_params))
            if (recovery['outcome'] != 'success' or recovery['normalised']['found'] != 64 or
                    [hit['id'] for hit in recovery['normalised']['hits']] != expected):
                failures.append(dict(operation=f'recovery/{i}', reason='Post-load recovery failed'))
            study.save_json(output / f'recovery-{i}.json', recovery)
        idle = []
        for _ in range(6):
            time.sleep(0.2)
            idle.append(study.process_resources(client.process.pid))
        study.save_json(output / 'resources.json', dict(before=before, after=after, idle=idle))
        study.save_json(output / 'debug.json', client.json('GET', '/debug'))
    ids = [row['operation_id'] for row in rows]
    if len(set(ids)) != planned or len(rows) != planned or not all(row['issued'] for row in rows):
        failures.append(dict(reason='Missing, duplicate or unissued operations'))
    summary = dict(passed=not failures, failures=failures, planned=planned, recorded=len(rows),
        issued=sum(row['issued'] for row in rows), elapsed_seconds=elapsed,
        kinds={kind: sum(row['kind'] == kind for row in rows) for kind in ('read', 'write', 'deadline', 'disconnect')},
        final_digest=final_digest, deadline_digest=deadline_digest,
        recovery_requests=3, scope=plan['model'], disconnect_contract=plan['disconnect_contract'])
    study.save_json(output / 'summary.json', summary)
    print(study.canonical(summary))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
