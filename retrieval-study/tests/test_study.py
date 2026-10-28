import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import study


class DatasetTest(unittest.TestCase):
    def config(self, **changes):
        return dict(documents=128, target_documents=8, target_matches=1,
                    other_unique=120, hot_terms=100, seed=3130, **changes)

    def test_target_stays_identical_as_foreign_cardinality_changes(self):
        small = list(study.document_stream(**self.config()))
        bigger = self.config()
        bigger.update(documents=1024, other_unique=1016, hot_terms=1016)
        large = list(study.document_stream(**bigger))
        self.assertEqual(small[:8], large[:8])
        self.assertEqual(sum(d['item_code'].startswith('918') for d in small[:8]), 1)

    def test_cardinality_repetition_and_hot_term_controls(self):
        cfg = self.config()
        cfg.update(other_unique=30, hot_terms=17)
        docs = list(study.document_stream(**cfg))
        self.assertEqual(len(docs), 128)
        self.assertEqual(len({d['id'] for d in docs}), 128)
        foreign = docs[8:]
        self.assertEqual(len({d['item_code'] for d in foreign}), 30)
        self.assertEqual(len({d['item_code'] for d in foreign if d['item_code'].startswith('918')}), 17)
        self.assertEqual(len({d['item_code'] for d in docs}), 38)

    def test_digest_ignores_export_order_but_detects_changes_and_duplicates(self):
        docs = list(study.document_stream(**self.config()))
        digest = study.multiset_digest(docs)
        self.assertEqual(digest, study.multiset_digest(reversed(docs)))
        self.assertNotEqual(digest, study.multiset_digest(docs[:-1]))
        self.assertNotEqual(digest, study.multiset_digest(docs + [docs[0]]))
        changed = [dict(d) for d in docs]
        changed[0]['uOn'] += 1
        self.assertNotEqual(digest, study.multiset_digest(changed))

    def test_generator_is_deterministic_and_will_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ('first', 'second')]
            for path in paths:
                args = argparse.Namespace(output=str(path), documents=128, target_documents=8,
                    target_matches=1, other_unique=30, hot_terms=17, seed=3130, infix_index=False)
                with contextlib.redirect_stdout(io.StringIO()):
                    study.generate(args)
            self.assertEqual((paths[0] / 'documents.jsonl').read_bytes(),
                             (paths[1] / 'documents.jsonl').read_bytes())
            self.assertEqual(study.sha256(paths[0] / 'documents.jsonl'),
                             json.loads((paths[0] / 'manifest.json').read_text())['documents_sha256'])
            with self.assertRaises(FileExistsError):
                study.generate(args)

    def test_invalid_shape_is_rejected_before_creating_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'invalid'
            args = argparse.Namespace(output=str(output), documents=100, target_documents=8,
                target_matches=9, other_unique=None, hot_terms=None, seed=3130, infix_index=False)
            with self.assertRaises(ValueError):
                study.generate(args)
            self.assertFalse(output.exists())


class ValidationTest(unittest.TestCase):
    def test_oracle_does_not_treat_truncated_modes_as_full_enumeration(self):
        self.assertFalse(any(c.get('oracle') for c in study.query_matrix(10000, 10000)))
        cases = study.query_matrix(128, 128)
        full = [c for c in cases if c.get('oracle')]
        self.assertEqual(len(full), 3)
        self.assertTrue(all(c['params']['max_candidates'] > 128 for c in full))

    def test_oracle_filters_sorts_and_paginates(self):
        docs = [dict(id=str(i), orgId='a' if i % 2 else 'b',
                     item_code='918' + str(i), uOn=i) for i in range(10)]
        params = dict(q='918', filter_by='orgId:=a', prefix='true', num_typos=0,
                      sort_by='uOn:desc', page=2, per_page=2)
        self.assertEqual(study.oracle_results(docs, params), dict(found=5, ids=['5', '3']))

    def test_oracle_rejects_truncation_and_missing_samples_at_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            dataset, run = folder / 'data', folder / 'run'
            args = argparse.Namespace(output=dataset, documents=128, target_documents=8,
                target_matches=1, other_unique=None, hot_terms=None, seed=3130, infix_index=False)
            with contextlib.redirect_stdout(io.StringIO()):
                study.generate(args)
            run.mkdir()
            case = next(c for c in json.loads((dataset / 'queries.json').read_text()) if c['id'] == 'oracle-narrow')
            metadata = dict(dataset=str(dataset), dataset_manifest=json.loads((dataset / 'manifest.json').read_text()),
                            repeats=1, queries=[case])
            study.save_json(run / 'run.json', metadata)
            study.save_json(run / 'complete.json', dict(measured_requests=1))
            expected = study.oracle_results(list(study.jsonl(dataset / 'documents.jsonl')), case['params'])
            row = dict(query_id=case['id'], params=case['params'], phase='measure', round=0, outcome='success',
                       normalised=dict(found=expected['found'], hits=[dict(id=x) for x in expected['ids']]))
            (run / 'requests.jsonl').write_text(study.canonical(row) + '\n')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(study.verify_oracle(argparse.Namespace(run=run)), 0)
            (run / 'requests.jsonl').write_text('')
            with self.assertRaisesRegex(ValueError, 'Missing or unexpected'):
                study.verify_oracle(argparse.Namespace(run=run))
            case['params']['max_candidates'] = 1
            study.save_json(run / 'run.json', metadata)
            with self.assertRaisesRegex(ValueError, 'too small'):
                study.verify_oracle(argparse.Namespace(run=run))

    def test_normalisation_preserves_order_count_score_and_cutoff(self):
        body = dict(found=2, out_of=20, search_cutoff=False, search_time_ms=19,
                    hits=[dict(document=dict(id='b'), text_match=5),
                          dict(document=dict(id='a'), text_match=4)])
        normalised = study.normalise_response(body)
        self.assertNotIn('search_time_ms', normalised)
        self.assertEqual([h['id'] for h in normalised['hits']], ['b', 'a'])
        self.assertEqual(normalised['found'], 2)
        self.assertEqual(normalised['out_of'], 20)
        self.assertFalse(normalised['search_cutoff'])
        self.assertEqual(normalised['hits'][0]['text_match'], 5)

    def test_summary_counts_failures_and_refuses_tiny_sample_p99(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            rows = [dict(query_id='q', phase='measure', outcome=o, elapsed_ms=i)
                    for i, o in enumerate(['success', 'success', 'timeout', 'cutoff', 'http_error'])]
            rows.append(dict(query_id='q', phase='warmup', outcome='success', elapsed_ms=999))
            (folder / 'requests.jsonl').write_text(''.join(study.canonical(r) + '\n' for r in rows))
            result = study.summarise_file(folder / 'requests.jsonl', folder / 'summary.json')['q']
            self.assertEqual(result['attempted'], 5)
            self.assertEqual(result['completed_samples'], 2)
            self.assertEqual(result['outcomes']['timeout'], 1)
            self.assertEqual(result['median_ms'], 0.5)
            self.assertNotIn('p99_ms', result)

    def test_compare_catches_result_and_query_parameter_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            left, right = folder / 'left', folder / 'right'
            left.mkdir(); right.mkdir()
            metadata = dict(dataset_manifest=dict(hash='same'), repeats=1, queries=[dict(id='q', params=dict(q='918'))])
            row = dict(query_id='q', round=0, phase='measure', outcome='success', status=200,
                       params=dict(q='918'),
                       normalised=dict(found=1, hits=[dict(id='a')], search_cutoff=False))
            for target in (left, right):
                study.save_json(target / 'run.json', metadata)
                study.save_json(target / 'complete.json', dict(measured_requests=1))
                (target / 'requests.jsonl').write_text(study.canonical(row) + '\n')
            args = argparse.Namespace(left=left, right=right, output=folder / 'diff.json')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(study.compare(args), 0)
                row['normalised']['hits'][0]['id'] = 'wrong'
                (right / 'requests.jsonl').write_text(study.canonical(row) + '\n')
                self.assertEqual(study.compare(args), 1)
            metadata['queries'][0]['params']['q'] = '91'
            study.save_json(right / 'run.json', metadata)
            with self.assertRaises(ValueError):
                study.compare(args)

    def test_compare_rejects_missing_duplicate_and_incomplete_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            left, right = folder / 'left', folder / 'right'
            left.mkdir(); right.mkdir()
            metadata = dict(dataset_manifest={}, repeats=1, queries=[dict(id='q', params=dict(q='918'))])
            row = dict(query_id='q', params=dict(q='918'), round=0, phase='measure', outcome='success',
                       status=200, normalised=dict(found=1))
            for p in (left, right):
                study.save_json(p / 'run.json', metadata)
                study.save_json(p / 'complete.json', dict(measured_requests=1))
                (p / 'requests.jsonl').write_text(study.canonical(row) + '\n')
            args = argparse.Namespace(left=left, right=right, output=folder / 'diff.json')
            (right / 'requests.jsonl').write_text('')
            with self.assertRaisesRegex(ValueError, 'planned query rounds'):
                study.compare(args)
            (right / 'requests.jsonl').write_text((study.canonical(row) + '\n') * 2)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                study.compare(args)
            (right / 'requests.jsonl').write_text(study.canonical(row) + '\n')
            (right / 'complete.json').unlink()
            with self.assertRaisesRegex(ValueError, 'Run is incomplete'):
                study.compare(args)


if __name__ == '__main__':
    unittest.main()
