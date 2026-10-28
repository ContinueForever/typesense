import copy
import unittest
from bench import compare_service


class ServiceComparisonTest(unittest.TestCase):
    def fixture(self):
        return dict(cases=[dict(id='q', params=dict(q='918'))], documents=[dict(id='d1')],
            debug=dict(version='fixed-version'), rows={('initial', 'q', 0): dict(status=200,
                params=dict(q='918'), normalised=dict(found=1, hits=[dict(id='d1')], search_cutoff=False))})

    def test_detects_id_count_and_cutoff_changes(self):
        original = self.fixture()
        self.assertTrue(compare_service.compare(original, copy.deepcopy(original))['passed'])
        for field, value in [('found', 2), ('hits', [dict(id='wrong')]), ('search_cutoff', True)]:
            changed = copy.deepcopy(original)
            changed['rows'][('initial', 'q', 0)]['normalised'][field] = value
            self.assertFalse(compare_service.compare(original, changed)['passed'])

    def test_refuses_different_versions_and_fixtures(self):
        original = self.fixture()
        changed = copy.deepcopy(original)
        changed['debug']['version'] = 'other-version'
        with self.assertRaisesRegex(ValueError, 'Different server versions'):
            compare_service.compare(original, changed)
        changed = copy.deepcopy(original)
        changed['documents'][0]['id'] = 'different-data'
        with self.assertRaisesRegex(ValueError, 'fixtures or parameters differ'):
            compare_service.compare(original, changed)


if __name__ == '__main__':
    unittest.main()
