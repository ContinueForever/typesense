#!/usr/bin/env python3
"""Fail closed on incomplete extended runs; compare full ordinary semantics."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.validate_extended import semantic


def expected_ids(plan):
    ordinary, finite = set(), set()
    for stage in ('initial', 'mutated', 'recovered'):
        for lazy in ('false', 'true'):
            ordinary.update(f'lazy/{stage}/{lazy}/{i}/{cap}' for i in range(6) for cap in (1, 512))
            ordinary.add(f'lazy/{stage}/{lazy}/oracle')
            if stage != 'recovered':
                ordinary.add(f'lazy/{stage}/{lazy}/curation-control')
                finite.update(f'lazy/{stage}/{lazy}/curation-budget-{budget}' for budget in (0, 1))
    ordinary.update(f'join/{stage}/{lazy}/{i}/{q}' for stage in ('initial', 'updated')
                    for lazy in ('false', 'true') for i in range(4) for q in ('918', '*'))
    ordinary.update(f'random/{i:05d}' for i, op in enumerate(plan['trace']) if op['kind'] == 'query')
    return ordinary, finite


def validate_rows(plan, rows):
    ordinary, finite = expected_ids(plan)
    by_id = {}
    for row in rows:
        key = row['query_id']
        if key in by_id:
            raise ValueError('Duplicate request: ' + key)
        if bool(row['finite_budget']) != (key in finite):
            raise ValueError('Wrong finite-budget classification: ' + key)
        if row.get('semantic') != semantic(row):
            raise ValueError('Stored semantic response does not match raw response: ' + key)
        if key in ordinary and (row.get('status') != 200 or row['outcome'] != 'success' or
                                row.get('normalised') != study.normalise_response(row['response'])):
            raise ValueError('Ordinary query did not complete successfully: ' + key)
        if key in finite:
            explicit = row.get('status') == 408 or row.get('normalised', {}).get('search_cutoff') is True
            if not explicit and (row.get('status') != 200 or row['outcome'] != 'success'):
                raise ValueError('Finite budget lacks success or an explicit cutoff: ' + key)
        by_id[key] = row
    if set(by_id) != ordinary | finite:
        raise ValueError('Missing or unexpected requests')
    for key in finite:
        row = by_id[key]
        control = by_id[key.rsplit('/', 1)[0] + '/curation-control']
        if dict(row['params'], search_cutoff_ms=5000) != control['params']:
            raise ValueError('Finite-budget parameters do not match full-budget control')
        if row['outcome'] == 'success' and row['semantic'] != control['semantic']:
            raise ValueError('Uncut finite-budget result differs from full result')
    return by_id


def load(folder):
    folder = Path(folder)
    summary = json.loads((folder / 'summary.json').read_text())
    if not summary.get('passed'):
        raise ValueError('Extended validation failed: ' + str(folder))
    if json.loads((folder / 'server-exit.json').read_text()).get('exit_code') != 0:
        raise ValueError('Server did not exit cleanly')
    plan = json.loads((folder / 'plan.json').read_text())
    if len(plan['trace']) != plan['steps']:
        raise ValueError('Trace length differs from declared operations')
    rows = validate_rows(plan, study.jsonl(folder / 'requests.jsonl'))
    if summary['counts']['searches'] != len(rows):
        raise ValueError('Search count differs from raw samples')
    build = json.loads((folder / 'build.json').read_text())
    env = json.loads((folder / 'environment.json').read_text())
    if (build.get('status') != 'success' or not build.get('provenance_valid') or
            build.get('source_after', {}).get('head') != study.UPSTREAM_SHA or
            build.get('binary_sha256') != env['binary']['sha256']):
        raise ValueError('Pinned binary build provenance mismatch')
    return dict(plan=plan, rows=rows, build=build, summary=summary,
                mutations=list(study.jsonl(folder / 'mutations.jsonl')))


def compare(left, right):
    if left['plan'] != right['plan']:
        raise ValueError('Different generated input traces')
    for key in ('command', 'build_compatibility_after', 'dependency_download_cache'):
        if left['build'].get(key) != right['build'].get(key):
            raise ValueError('Different build/dependency configuration: ' + key)
    if left['summary']['export_digests'] != right['summary']['export_digests']:
        raise ValueError('Different final collection contents')
    if left['mutations'] != right['mutations']:
        raise ValueError('Mutation replies differ')
    if left['rows'].keys() != right['rows'].keys():
        raise ValueError('Different request set')
    changes, finite_count = [], 0
    for key, a in left['rows'].items():
        b = right['rows'][key]
        if a['params'] != b['params'] or a['finite_budget'] != b['finite_budget']:
            raise ValueError('Different query parameters: ' + key)
        if a['finite_budget']:
            finite_count += 1
            continue
        if (a['status'], a['semantic']) != (b['status'], b['semantic']):
            changes.append(dict(query_id=key, left=a['semantic'], right=b['semantic']))
    return dict(passed=not changes, compared=len(left['rows']), ordinary=len(left['rows']) - finite_count,
        finite_budget_pairs=finite_count, differences=changes,
        policy='Full hit documents and normalised search semantics; explicit cutoff may return partial hits.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left')
    parser.add_argument('right')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = compare(load(args.left), load(args.right))
    study.save_json(args.output, result)
    print(study.canonical({k: v for k, v in result.items() if k != 'differences'}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
