#!/usr/bin/env python3
"""Measure this host's safe index envelope, stopping before memory exhaustion.

This is an admission probe, not a largest-dataset contest. Keep a fixed reserve;
do not import a projected next tier that exceeds the measured memory headroom.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.validate_extended import import_docs, parameters, verify_binary


def memory():
    values = {key: int(value.split()[0]) * 1024 for key, value in
              (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()) if value.strip().endswith('kB')}
    vmstat = {key: int(value) for key, value in (line.split() for line in Path('/proc/vmstat').read_text().splitlines())}
    return dict(mem_total=values['MemTotal'], mem_available=values['MemAvailable'], swap_free=values['SwapFree'],
        swap_total=values['SwapTotal'], swap_out_bytes=vmstat['pswpout'] * os.sysconf('SC_PAGE_SIZE'),
        memory_pressure=Path('/proc/pressure/memory').read_text())


def footprint(resources):
    return sum(int(resources['status'][key].split()[0]) * 1024 for key in ('VmRSS', 'VmSwap'))


def document(index):
    return dict(id=f'c{index:010d}', code=('918' if index % 10 else '777') + f'{index:010d}',
                tenant='target' if index < 16 else 'other', score=index,
                title='synthetic prefix capacity sample')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--build-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--tiers', default='100000,300000,1000000,3000000,10000000')
    parser.add_argument('--reserve-mib', type=study.positive, default=1536)
    parser.add_argument('--batch-size', type=study.positive, default=2000)
    args = parser.parse_args()
    tiers = [int(value) for value in args.tiers.split(',')]
    if sorted(set(tiers)) != tiers or not tiers or tiers[0] <= 0:
        parser.error('tiers must be positive, increasing and unique')
    build = verify_binary(args.binary, args.build_manifest)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    reserve = args.reserve_mib * 1024 ** 2
    study.save_json(out / 'build.json', build)
    study.save_json(out / 'environment.json', study.environment(binary=args.binary))
    study.save_json(out / 'plan.json', dict(tiers=tiers, reserve_bytes=reserve, batch_size=args.batch_size,
        producer_sha256=study.sha256(Path(__file__)), guard='Stop below reserve or when conservative next-tier projection exceeds headroom. Never force OOM.',
        interpretation='Safe working-set envelope of this shared host; not maximum physical capacity or algorithm throughput.'))
    checkpoints, loaded, reason, slope = [], 0, None, 0
    with study.owned_server(args.binary, out, 120) as client, \
            (out / 'imports.jsonl').open('w', buffering=1) as imports, \
            (out / 'requests.jsonl').open('w', buffering=1) as queries:
        client.json('POST', '/collections', dict(name='capacity_probe', default_sorting_field='score', fields=[
            dict(name='code', type='string'), dict(name='tenant', type='string', facet=True),
            dict(name='score', type='int64'), dict(name='title', type='string')]))
        base = '/collections/capacity_probe'
        before = study.process_resources(client.process.pid)
        initial_memory = memory()
        study.save_json(out / 'initial-resources.json', dict(server=before, host=initial_memory))
        for target in tiers:
            host = memory()
            projected = max(0, target - loaded) * slope * 1.25
            if loaded and projected > max(0, host['mem_available'] - reserve):
                reason = dict(kind='projected_memory_headroom', loaded=loaded, next_tier=target,
                    measured_bytes_per_document=slope, projection_factor=1.25,
                    projected_additional_bytes=projected, available_bytes=host['mem_available'], reserve_bytes=reserve)
                break
            started = time.monotonic()
            while loaded < target:
                host = memory()
                if host['mem_available'] < reserve:
                    reason = dict(kind='memory_reserve', loaded=loaded, next_tier=target,
                                  available_bytes=host['mem_available'], reserve_bytes=reserve)
                    break
                if shutil.disk_usage(ROOT).free < 8 * 1024 ** 3:
                    reason = dict(kind='disk_reserve', loaded=loaded, next_tier=target, reserve_bytes=8 * 1024 ** 3)
                    break
                end = min(target, loaded + args.batch_size)
                batch = [document(i) for i in range(loaded, end)]
                import_docs(client, base, batch)
                loaded = end
                imports.write(study.canonical(dict(imported=loaded, batch_documents=len(batch), all_rows_successful=True,
                    host=host, server=study.process_resources(client.process.pid))) + '\n')
            if client.json('GET', base)['num_documents'] != loaded:
                raise RuntimeError('Successful import count differs from collection')
            with client.request('GET', base + '/documents/export') as response:
                exported = study.multiset_digest(json.loads(line) for line in response if line.strip())
            expected = study.multiset_digest(document(i) for i in range(loaded))
            if expected != exported:
                raise RuntimeError('Export differs from the deterministic fixture')
            current = study.process_resources(client.process.pid)
            slope = max(slope, (footprint(current) - footprint(before)) / max(loaded, 1))
            for name, updates in [('empty', dict(filter_by='tenant:=absent')),
                                   ('target', dict(filter_by='tenant:=target')),
                                   ('unfiltered', {}),
                                   ('exact', dict(q='9180000000001', prefix='false'))]:
                row = study.sample(client, base + '/documents/search', dict(id=f'{loaded}/{name}',
                    params=parameters(max_candidates=4, **updates)))
                queries.write(study.canonical(row) + '\n')
                if row['outcome'] not in ('success', 'cutoff') and row.get('status') != 408:
                    raise RuntimeError('Unexpected resource-probe query failure')
                if name == 'exact' and (row['outcome'] != 'success' or row['normalised']['found'] != 1 or
                        [h['id'] for h in row['normalised']['hits']] != ['c0000000001']):
                    raise RuntimeError('Exact query recovery failed')
            checkpoint = dict(documents=loaded, tier=target, tier_completed=loaded == target,
                import_verify_seconds=time.monotonic() - started, export_digest=exported,
                server=current, host=memory(), conservative_bytes_per_document=slope)
            checkpoints.append(checkpoint)
            study.save_json(out / 'progress.json', dict(loaded=loaded, checkpoints=checkpoints, stop_reason=reason))
            print(study.canonical(dict(documents=loaded, rss=current['status']['VmRSS'],
                swap=current['status']['VmSwap'], available_bytes=checkpoint['host']['mem_available'])), flush=True)
            if reason:
                break
        final = dict(server=study.process_resources(client.process.pid), host=memory())
    # This is a sizing estimate for exactly this fixture. Include OS headroom;
    # real corpora, replicas and independent clients need a separate estimate.
    projected_target = footprint(before) + tiers[-1] * slope * 1.25 + 2 * 1024 ** 3
    summary = dict(completed=True, imported=loaded, requested_largest_tier=tiers[-1],
        all_imported_documents_verified=True, checkpoints=checkpoints, stop_reason=reason,
        final=final, observed_bytes_per_document=slope,
        projected_largest_tier_memory_gib=math.ceil(projected_target / 1024 ** 3),
        interpretation='Measured shared-host working-set admission limit. Projection is an estimate for this fixture, not a minimum rental SKU or proof that all projects need this scale.')
    study.save_json(out / 'summary.json', summary)
    print(study.canonical({key: value for key, value in summary.items() if key not in ('checkpoints', 'final')}))


if __name__ == '__main__':
    main()
