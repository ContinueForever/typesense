#!/usr/bin/env python3
"""Re-run differential and deterministic cutoff checks on real core binaries."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    differences = []
    compared = 0
    def execute(binary, n, repeats, mode, label):
        path = output / (label + '.jsonl')
        with path.open('w') as stream:
            subprocess.run([str(Path(binary).resolve()), str(n), str(repeats), mode], stdout=stream, check=True)
        return list(study.jsonl(path))
    for n in (50, 1000, 10000, 100000):
        before = execute(args.baseline, n, 3, 'normal', f'n{n}-before')
        after = execute(args.candidate, n, 3, 'normal', f'n{n}-after')
        if len(before) != len(after):
            raise ValueError('Missing requests in core differential')
        for left, right in zip(before, after):
            compared += 1
            fields = ('n', 'round', 'order', 'filter', 'k', 'tokens', 'cutoff')
            if any(left[f] != right[f] for f in fields):
                differences.append(dict(left=left, right=right))
    before = execute(args.baseline, 2048, 1, 'stale-expired', 'stale-before')
    after = execute(args.candidate, 2048, 1, 'stale-expired', 'stale-after')
    def relevant(rows):
        return next(r for r in rows if r['order'] == 'max_score' and r['filter'] == 'none' and r['k'] == 1)
    old, new = relevant(before), relevant(after)
    regression_fixed = not old['cutoff'] and new['cutoff'] and not new['tokens']
    summary = dict(compared=compared, differences=differences,
        deadline_regression_fixed=regression_fixed, deadline_before=old, deadline_after=new,
        baseline_sha256=study.sha256(args.baseline), candidate_sha256=study.sha256(args.candidate),
        scope='Materialized ID filter only; no HTTP/lazy iterator equivalence claim.',
        passed=not differences and regression_fixed)
    study.save_json(output / 'summary.json', summary)
    print(study.canonical({k: summary[k] for k in ('compared', 'deadline_regression_fixed', 'passed')}))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
