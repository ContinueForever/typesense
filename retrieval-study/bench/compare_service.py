#!/usr/bin/env python3
"""Diff completed service runs, keeping finite-deadline contracts separate."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def load_run(folder):
    folder = Path(folder)
    summary = json.loads((folder / 'summary.json').read_text())
    if not summary.get('passed'):
        raise ValueError('Service validation did not pass: ' + str(folder))
    cases = json.loads((folder / 'cases.json').read_text())
    parameters = {case['id']: case['params'] for case in cases}
    expected = {(stage, case['id'], 0) for stage in
        ('initial', 'updated-and-deleted', 'reinserted', 'after-deadline') for case in cases}
    expected |= {(stage, 'cached-target', r) for stage in
        ('initial', 'updated-and-deleted', 'reinserted', 'after-deadline') for r in (0, 1)}
    expected.add(('deadline', 'zero-budget', 0))
    rows = {}
    for row in study.jsonl(folder / 'requests.jsonl'):
        key = (row['stage'], row['query_id'], row.get('cache_repetition', 0))
        if key in rows:
            raise ValueError('Duplicate service request: ' + str(key))
        if row['query_id'] == 'zero-budget':
            expected_params = dict(cases[0]['params'], search_cutoff_ms=0)
        elif row['query_id'] == 'cached-target':
            expected_params = dict(parameters['target-k128'], use_cache='true')
        else:
            expected_params = parameters.get(row['query_id'])
        if row['params'] != expected_params:
            raise ValueError('Service request parameters differ from the case manifest')
        if row['stage'] == 'deadline':
            if row.get('status') != 408 and not row.get('normalised', {}).get('search_cutoff'):
                raise ValueError('Deadline contract failed')
            rows[key] = dict(deadline_signalled=True)
        else:
            if row['outcome'] != 'success':
                raise ValueError('Ordinary service query returned error or cutoff')
            rows[key] = {k: row[k] for k in ('status', 'params', 'normalised')}
    if set(rows) != expected:
        raise ValueError('Missing or unexpected service requests')
    return dict(rows=rows, cases=cases,
                documents=json.loads((folder / 'initial-documents.json').read_text()),
                debug=json.loads((folder / 'debug.json').read_text()))


def compare(left, right):
    if left['cases'] != right['cases'] or left['documents'] != right['documents']:
        raise ValueError('Service fixtures or parameters differ')
    if left['debug'].get('version') != right['debug'].get('version'):
        raise ValueError('Different server versions are not a same-version compatibility test')
    changes = [dict(request=key, left=left['rows'].get(key), right=right['rows'].get(key))
               for key in sorted(left['rows'].keys() | right['rows'].keys())
               if left['rows'].get(key) != right['rows'].get(key)]
    return dict(compared=len(left['rows']), differences=changes, passed=not changes,
                deadline_policy='Verify explicit cutoff/408; do not require identical partial hits.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left')
    parser.add_argument('right')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = compare(load_run(args.left), load_run(args.right))
    study.save_json(args.output, result)
    print(study.canonical({k: result[k] for k in ('compared', 'passed')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
