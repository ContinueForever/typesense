import gzip
from pathlib import Path
import tempfile
import unittest
import study
from bench.benchmark_alternating import distribution


class BenchmarkEvidenceTest(unittest.TestCase):
    def test_cutoffs_and_errors_remain_in_latency_population(self):
        rows = [dict(elapsed_ms=1, status=200, outcome='success') for _ in range(9800)]
        rows.extend(dict(elapsed_ms=20, status=408, outcome='http_error') for _ in range(150))
        rows.extend(dict(elapsed_ms=30000, status=None, outcome='timeout') for _ in range(50))
        result = distribution(rows)
        self.assertEqual(result['samples'], 10000)
        self.assertEqual(result['p99_ms'], 20)
        self.assertEqual(result['outcomes']['timeout'], 50)
        self.assertEqual(result['max_ms'], 30000)
        self.assertNotIn('p99_ms', distribution(rows[:9999]))

    def test_published_compressed_raw_records_remain_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'requests.jsonl.gz'
            with gzip.open(path, 'wt') as stream:
                stream.write('{"id":1}\n{"id":2}\n')
            self.assertEqual(list(study.jsonl(path)), [dict(id=1), dict(id=2)])
            self.assertEqual(list(study.jsonl(Path(temp) / 'requests.jsonl')), [dict(id=1), dict(id=2)])


if __name__ == '__main__':
    unittest.main()
