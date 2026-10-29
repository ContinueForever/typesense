#!/usr/bin/env python3
"""Build a small non-synthetic query fixture from pinned upstream examples."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    sources = ['test/documents.jsonl', 'test/multi_field_documents.jsonl', 'test/resources/hnstories.jsonl.gz']
    docs, records = [], []
    for source in sources:
        path = Path(args.source) / source
        pinned = subprocess.check_output(['git', 'show', study.UPSTREAM_SHA + ':' + source], cwd=args.source)
        if hashlib.sha256(pinned).hexdigest() != study.sha256(path):
            raise ValueError('Example differs from pinned upstream file: ' + source)
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt') as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        records.append(dict(path=source, sha256=study.sha256(path), documents=len(rows)))
        for row in rows:
            docs.append(dict(id=f'e{len(docs):04d}', title=row['title'], points=int(row['points']),
                             rank=len(docs), family=source))
    (out / 'documents.jsonl').write_text(''.join(study.canonical(doc) + '\n' for doc in docs))
    study.save_json(out / 'schema.json', dict(name='examples', default_sorting_field='rank', fields=[
        dict(name='title', type='string'), dict(name='points', type='int32'),
        dict(name='rank', type='int64'), dict(name='family', type='string', facet=True)]))
    queries = []
    for i, q in enumerate(['s', 'so', 'software', 'how', 'rocket', 'Google', 'The', 'lost', 'Ron', 'making',
                           '"The Lost Movie"', 'shool', '*']):
        for cap in (1, 4, 512):
            for lazy in ('false', 'true'):
                params = dict(q=q, query_by='title', num_typos=1 if q == 'shool' else 0, prefix='true',
                    use_cache='false', enable_synonyms='false', split_join_tokens='off', drop_tokens_threshold=0,
                    typo_tokens_threshold=0, max_candidates=cap, sort_by='rank:desc', per_page=30,
                    search_cutoff_ms=5000, enable_lazy_filter=lazy)
                if i % 3 == 1:
                    params['filter_by'] = 'points:>=20'
                elif i % 3 == 2:
                    params['facet_by'] = 'family'
                queries.append(dict(id=f'example-{i}-{cap}-{lazy}', params=params, oracle=False))
    study.save_json(out / 'queries.json', queries)
    digest = study.sha256(out / 'documents.jsonl')
    study.save_json(out / 'manifest.json', dict(source_commit=study.UPSTREAM_SHA, sources=records,
        config=dict(documents=len(docs)), documents_sha256=digest, content_digest=study.multiset_digest(docs),
        note='Pinned upstream example text, not a representative production corpus. Title and points are retained; synthetic stable IDs/rank/family are added.'))
    study.save_json(args.manifest, dict(dataset=str(out), sources=records, documents=len(docs),
        queries=len(queries), source_commit=study.UPSTREAM_SHA, documents_sha256=digest,
        generator_sha256=study.sha256(Path(__file__))))
    print(study.canonical(dict(documents=len(docs), queries=len(queries))))


if __name__ == '__main__':
    main()
