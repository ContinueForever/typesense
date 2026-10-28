import copy
import json
from pathlib import Path
import tempfile
import unittest

from bench import compare_deadline_service as comparison


class DeadlineComparisonTest(unittest.TestCase):
    def write_run(self, folder):
        folder = Path(folder)
        for name, value in {
            'fixture.json': {'groups': 2048, 'repeats': 1},
            'summary.json': {'passed': True, 'requests': 11},
            'debug.json': {'version': 'fixture-version'},
            'import-verification.json': {'expected': {'count': 4096}, 'exported': {'count': 4096}},
        }.items():
            (folder / name).write_text(json.dumps(value))
        rows = []
        for label in ['unfiltered', 'target']:
            for budget in [1, 2, 5, 20, 5000]:
                rows.append(dict(query_id=f'{label}-budget-{budget}', repetition=0,
                    params={'search_cutoff_ms': budget}, status=200, outcome='success',
                    normalised={'found': 1, 'hits': [{'id': 'b'}], 'search_cutoff': False}, elapsed_ms=0.2))
        rows.append(dict(query_id='exact-recovery', repetition=0, params={'q': 'exact'},
                         status=200, outcome='success', normalised={'found': 1, 'hits': [{'id': 'b'}]}, elapsed_ms=0.1))
        (folder / 'requests.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return rows

    def test_partial_deadline_results_are_allowed_but_ordinary_results_must_agree(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_run(folder)
            left = comparison.load(folder)
            right = copy.deepcopy(left)
            partial = right['rows'][('target-budget-1', 0)]
            partial.update(outcome='cutoff', normalised={'found': 0, 'hits': [], 'search_cutoff': True})
            self.assertTrue(comparison.compare(left, right)['passed'])
            changed = right['rows'][('exact-recovery', 0)]
            changed['normalised']['hits'][0]['id'] = 'wrong'
            self.assertFalse(comparison.compare(left, right)['passed'])
            right = copy.deepcopy(left)
            right['rows'][('target-budget-1', 0)]['params']['search_cutoff_ms'] = 2
            with self.assertRaises(ValueError):
                comparison.compare(left, right)

    def test_incomplete_duplicate_and_unsignalled_cutoff_samples_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = self.write_run(folder)
            path = Path(folder) / 'requests.jsonl'
            for changed in [rows[:-1], rows + [rows[0]]]:
                path.write_text(''.join(json.dumps(row) + '\n' for row in changed))
                with self.assertRaises(ValueError):
                    comparison.load(folder)
            rows[0]['outcome'] = 'cutoff'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            with self.assertRaises(ValueError):
                comparison.load(folder)


if __name__ == '__main__':
    unittest.main()
