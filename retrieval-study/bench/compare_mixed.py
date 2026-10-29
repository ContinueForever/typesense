#!/usr/bin/env python3
"""Audit every mixed-load operation; compare only schedule-independent facts."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def audit(folder):
    folder = Path(folder)
    plan = json.loads((folder / 'plan.json').read_text())
    summary = json.loads((folder / 'summary.json').read_text())
    if not summary['passed'] or json.loads((folder / 'server-exit.json').read_text())['exit_code'] != 0:
        raise ValueError('Mixed validation failed or server exited abnormally')
    cfg = plan['config']
    expected = {f'read/{w}/{i}' for w in range(cfg['read_workers']) for i in range(cfg['reads_per_worker'])}
    expected |= {f'write/{i}' for i in range(cfg['writes'])}
    expected |= {f'deadline/{i}' for i in range(cfg['flood'])}
    expected |= {f'disconnect/{i}' for i in range(cfg['disconnects'])}
    rows, writes = {}, []
    for row in study.jsonl(folder / 'operations.jsonl'):
        key = row['operation_id']
        if key in rows or not row['issued'] or row['issued_offset_ms'] > row['completed_offset_ms']:
            raise ValueError('Duplicate, unissued or invalid operation timestamps')
        rows[key] = row
        kind = key.split('/')[0]
        if kind != row['kind']:
            raise ValueError('Operation kind differs from planned ID')
        if kind == 'read':
            observed = row.get('normalised', {})
            if (row['outcome'] != 'success' or row.get('status') != 200 or observed.get('found') != 64 or
                    [hit['id'] for hit in observed.get('hits', [])] != [f's{i:03d}' for i in reversed(range(64))] or
                    observed != study.normalise_response(row['response'])):
                raise ValueError('Stable read failed independent oracle')
        elif kind == 'write':
            operation = plan['writes'][int(key.split('/')[1])]
            if row['outcome'] != 'success' or row['operation'] != operation:
                raise ValueError('Write differs from planned trace')
            if (operation['kind'] == 'delete' and row['response']['id'] != operation['id'] or
                    operation['kind'] == 'upsert' and row['response'] != operation['document']):
                raise ValueError('Write reply differs from planned document')
            writes.append((key, row['response']))
        elif kind == 'deadline':
            if not (row['outcome'] == 'success' or row.get('status') == 408 or
                    row.get('normalised', {}).get('search_cutoff') is True):
                raise ValueError('Deadline response lacks explicit cutoff')
        elif kind == 'disconnect' and (row['outcome'] != 'client_closed_after_send' or row['bytes_sent'] <= 0):
            raise ValueError('Disconnect probe was not sent completely')
    if set(rows) != expected or summary['recorded'] != len(rows) or summary['issued'] != len(rows):
        raise ValueError('Missing or unexpected operations')
    for i in range(3):
        row = json.loads((folder / f'recovery-{i}.json').read_text())
        if (row['outcome'] != 'success' or row['normalised']['found'] != 64 or
                [hit['id'] for hit in row['normalised']['hits']] != [f's{j:03d}' for j in reversed(range(64))]):
            raise ValueError('Post-load recovery failed')
    return dict(plan=plan, summary=summary, writes=sorted(writes),
                build=json.loads((folder / 'build.json').read_text()))


def compare(a, b):
    if a['plan'] != b['plan'] or a['writes'] != b['writes']:
        raise ValueError('Different mixed input trace or write replies')
    for key in ('command', 'build_compatibility_after', 'dependency_download_cache'):
        if a['build'].get(key) != b['build'].get(key):
            raise ValueError('Different build configuration')
    for key in ('final_digest', 'deadline_digest', 'planned', 'recorded', 'issued', 'kinds'):
        if a['summary'][key] != b['summary'][key]:
            raise ValueError('Different verified state/accounting: ' + key)
    return dict(passed=True, operations_per_variant=a['summary']['recorded'],
        stable_reads_checked_per_variant=a['summary']['kinds']['read'],
        writes_per_variant=a['summary']['kinds']['write'],
        finite_budget_requests_per_variant=a['summary']['kinds']['deadline'],
        disconnect_probes_per_variant=a['summary']['kinds']['disconnect'],
        recovery_checks_per_variant=3,
        limitations='Concurrent mutable out_of counts and partial deadline hits may reflect different legal schedules. Disconnect does not prove backend cancellation. This run does not measure capacity.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left')
    parser.add_argument('right')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = compare(audit(args.left), audit(args.right))
    study.save_json(args.output, result)
    print(study.canonical(result))


if __name__ == '__main__':
    main()
