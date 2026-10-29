import copy
import unittest
from bench import compare_extended, validate_extended


class ExtendedComparisonTest(unittest.TestCase):
    def fixture(self):
        plan = dict(trace=[], steps=0)
        ordinary, finite = compare_extended.expected_ids(plan)
        rows = []
        for key in sorted(ordinary | finite):
            limited = key in finite
            params = dict(q='918', search_cutoff_ms=int(key[-1]) if limited else 5000)
            row = dict(query_id=key, params=params, finite_budget=limited,
                       status=408 if limited else 200,
                       outcome='http_error' if limited else 'success')
            if not limited:
                row.update(response=dict(found=1, hits=[dict(document=dict(id='d', maker=dict(name='brand')))]),
                           normalised=dict(found=1, hits=[dict(id='d')]))
            row['semantic'] = validate_extended.semantic(row)
            rows.append(row)
        return plan, rows

    def test_rejects_missing_duplicate_and_unsignalled_cutoff(self):
        plan, rows = self.fixture()
        self.assertEqual(len(compare_extended.validate_rows(plan, rows)), len(rows))
        with self.assertRaisesRegex(ValueError, 'Missing'):
            compare_extended.validate_rows(plan, rows[:-1])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            compare_extended.validate_rows(plan, rows + [rows[0]])
        changed = copy.deepcopy(rows)
        row = next(row for row in changed if row['finite_budget'])
        row['status'], row['outcome'] = 500, 'http_error'
        with self.assertRaisesRegex(ValueError, 'explicit cutoff'):
            compare_extended.validate_rows(plan, changed)

    def test_rejects_partial_result_without_cutoff(self):
        plan, rows = self.fixture()
        row = next(row for row in rows if row['finite_budget'])
        row.update(status=200, outcome='success', response=dict(found=0, hits=[]),
                   normalised=dict(found=0, hits=[]))
        row['semantic'] = validate_extended.semantic(row)
        with self.assertRaisesRegex(ValueError, 'Uncut finite-budget'):
            compare_extended.validate_rows(plan, rows)

    def test_compares_joined_payload_and_build_configuration(self):
        plan, rows = self.fixture()
        a = dict(plan=plan, rows=compare_extended.validate_rows(plan, rows), build={},
                 summary=dict(export_digests={}), mutations=[])
        b = copy.deepcopy(a)
        self.assertTrue(compare_extended.compare(a, b)['passed'])
        key = next(key for key in b['rows'] if key.startswith('join/'))
        b['rows'][key]['semantic']['documents'][0]['maker']['name'] = 'changed'
        self.assertFalse(compare_extended.compare(a, b)['passed'])
        b = copy.deepcopy(a)
        b['build']['command'] = ['different-flags']
        with self.assertRaisesRegex(ValueError, 'configuration'):
            compare_extended.compare(a, b)

    def test_trace_is_seeded_and_mutations_have_valid_lifetimes(self):
        trace = validate_extended.random_trace(3130, 400)
        self.assertEqual(trace, validate_extended.random_trace(3130, 400))
        self.assertNotEqual(trace, validate_extended.random_trace(42, 400))
        live = {doc['id'] for doc in validate_extended.initial_random(3130)}
        for op in trace:
            if op['kind'] == 'delete':
                self.assertIn(op['id'], live)
                live.remove(op['id'])
            elif op['kind'] == 'upsert':
                live.add(op['document']['id'])


if __name__ == '__main__':
    unittest.main()
