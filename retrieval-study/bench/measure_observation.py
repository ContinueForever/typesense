#!/usr/bin/env python3
"""Alternate unobserved/observed core runs and retain every sample.

This is a diagnostic of counter overhead, not a service throughput benchmark.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--observed', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--trials', type=study.positive, default=6)
    parser.add_argument('--documents', type=study.positive, default=100000)
    parser.add_argument('--repeats', type=study.positive, default=5)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    variants = dict(baseline=Path(args.baseline).resolve(), observed=Path(args.observed).resolve())
    manifests = {k: json.loads((v.parent / 'manifest.json').read_text()) for k, v in variants.items()}
    driver = str(ROOT / 'bench/art_probe.cpp')
    if manifests['baseline']['art_source_sha256'] != manifests['observed']['art_source_sha256'] or \
            manifests['baseline']['source_hashes'][driver] != manifests['observed']['source_hashes'][driver]:
        raise ValueError('Source/driver differs beyond observation hooks')
    study.save_json(output / 'run.json', dict(documents=args.documents, repeats=args.repeats,
        trials=args.trials, binaries={k: study.sha256(v) for k, v in variants.items()},
        environment=study.environment(), limitation='Diagnostic only; other host workloads are not controlled.'))
    grouped = defaultdict(lambda: defaultdict(list))
    for trial in range(args.trials):
        pair = {}
        for label in (('baseline', 'observed') if trial % 2 == 0 else ('observed', 'baseline')):
            path = output / f'{trial:02d}-{label}.jsonl'
            with path.open('w') as stream:
                subprocess.run([str(variants[label]), str(args.documents), str(args.repeats), 'normal'], stdout=stream, check=True)
            pair[label] = list(study.jsonl(path))
            for row in pair[label]:
                key = f"{row['order']}/{row['filter']}/k{row['k']}"
                grouped[key][label].append(row['elapsed_us'])
        if len(pair['baseline']) != len(pair['observed']):
            raise ValueError('Observation run dropped requests')
        for a, b in zip(pair['baseline'], pair['observed']):
            if any(a[k] != b[k] for k in ('round', 'order', 'filter', 'k', 'tokens', 'cutoff')):
                raise ValueError('Observation changed query results')
    summary = {}
    for key, records in grouped.items():
        medians = {k: statistics.median(v) for k, v in records.items()}
        summary[key] = dict(samples_per_variant=len(records['baseline']), medians_us=medians,
            observed_to_baseline=medians['observed'] / medians['baseline'],
            limitation='Small diagnostic sample on a shared host; no P99 or stable regression claim.')
    study.save_json(output / 'summary.json', summary)
    print(output.resolve())


if __name__ == '__main__':
    main()
