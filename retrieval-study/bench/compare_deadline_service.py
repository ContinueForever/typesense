#!/usr/bin/env python3
"""Compare complete HTTP deadline probes without equating partial results.

Full-budget controls and post-deadline recovery must agree. Short budgets retain
their timing and cutoff outcomes as diagnostic observations, not a speedup claim.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def load(folder):
    folder = Path(folder)
    fixture = json.loads((folder / 'fixture.json').read_text())
    summary = json.loads((folder / 'summary.json').read_text())
    if not summary.get('passed'):
        raise ValueError('Deadline probe did not pass: ' + str(folder))
    expected = {(f'{label}-budget-{budget}', r) for label in ('unfiltered', 'target')
                for budget in (1, 2, 5, 20, 5000) for r in range(fixture['repeats'])}
    expected |= {('exact-recovery', r) for r in range(fixture['repeats'])}
    rows = {}
    for row in study.jsonl(folder / 'requests.jsonl'):
        if row.get('warmup'):
            continue
        key = row['query_id'], row['repetition']
        if key in rows:
            raise ValueError('Duplicate deadline probe sample')
        if row['outcome'] not in ('success', 'cutoff') and row.get('status') != 408:
            raise ValueError('Unexpected probe error')
        if row['outcome'] == 'cutoff' and not row.get('normalised', {}).get('search_cutoff'):
            raise ValueError('A cutoff response must explicitly signal its partial-result state')
        if row['outcome'] == 'success' and (row.get('status') != 200 or row.get('normalised', {}).get('search_cutoff')):
            raise ValueError('An ordinary response must be successful and uncut')
        rows[key] = row
    if set(rows) != expected or summary['requests'] != len(rows):
        raise ValueError('Incomplete deadline probe samples')
    verification = json.loads((folder / 'import-verification.json').read_text())
    if verification['exported'] != verification['expected']:
        raise ValueError('Probe data verification failed')
    return dict(fixture=fixture, rows=rows, data=verification['expected'],
                version=json.loads((folder / 'debug.json').read_text()).get('version'))


def compare(left, right):
    if left['fixture'] != right['fixture'] or left['data'] != right['data'] or left['version'] != right['version']:
        raise ValueError('Probe versions, fixture or verified data differ')
    differences, finite_pairs, diagnostics = [], 0, {}
    for key in sorted(left['rows']):
        a, b = left['rows'][key], right['rows'][key]
        if a['params'] != b['params']:
            raise ValueError('Probe query parameters differ')
        control = key[0] == 'exact-recovery' or key[0].endswith('-budget-5000')
        if control:
            if a['outcome'] != 'success' or b['outcome'] != 'success' or a['normalised'] != b['normalised']:
                differences.append(dict(request=key, reason='ordinary control or recovery differs'))
        else:
            finite_pairs += 1
            control_key = (key[0].split('-budget-')[0] + '-budget-5000', key[1])
            for variant, run, row in [('baseline', left, a), ('candidate', right, b)]:
                if row['outcome'] == 'success' and row['normalised'] != run['rows'][control_key]['normalised']:
                    differences.append(dict(request=key, variant=variant, reason='uncut response differs from its full-budget control'))
    for label in sorted({key[0] for key in left['rows']}):
        diagnostics[label] = {}
        for variant, run in [('baseline', left), ('candidate', right)]:
            rows = [r for key, r in run['rows'].items() if key[0] == label]
            diagnostics[label][variant] = dict(samples=len(rows),
                explicit_cutoffs=sum(r['outcome'] == 'cutoff' or r.get('status') == 408 for r in rows),
                median_elapsed_ms=statistics.median(r['elapsed_ms'] for r in rows),
                engine_ms=[r.get('engine_ms') for r in rows])
    return dict(passed=not differences, differences=differences, compared=len(left['rows']),
                ordinary_controls_compared=len(left['rows']) - finite_pairs,
                finite_budget_pairs=finite_pairs, diagnostic_samples=diagnostics,
                scope='Small sequential HTTP diagnostic; no throughput, percentile, or stable speedup claim.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline')
    parser.add_argument('candidate')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = compare(load(args.baseline), load(args.candidate))
    study.save_json(args.output, result)
    print(study.canonical({k: result[k] for k in ('passed', 'compared', 'ordinary_controls_compared', 'finite_budget_pairs')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
