import tempfile
from pathlib import Path
import unittest

from benchmarks import Benchmarks


class BenchmarksTests(unittest.TestCase):
    def test_exact_mapping_reasoning_and_unknown_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Benchmarks(Path(tmp))
            b.import_records({'records':[{'model_id':'glm-5.2','benchmark_model':'GLM-5.2 high','reasoning':'high',
                'source':'LiveBench','source_url':'https://livebench.ai/','date':'2026-06-25','coding':80,'agentic':60,'general':70}]})
            models=b.enrich([{'id':'glm-5.2'},{'id':'glm-5.3'},{'id':'glm-5.2-preview'}],{})
            self.assertEqual(models[0]['benchmark']['reasoning'],'high')
            self.assertEqual(models[0]['recommendation_score'],70)
            self.assertIsNone(models[1]['benchmark'])
            self.assertIsNone(models[2]['benchmark'])

    def test_cost_is_observed_and_zero_is_not_infinite_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Benchmarks(Path(tmp))
            b.import_records({'records':[{'model_id':'a','source':'LiveBench','source_url':'https://livebench.ai/',
                'date':'2026-06-25','coding':80}]})
            rows=b.enrich([{'id':'a'}],{'a':{'samples':3,'credit':0,'tokens':1000}})
            self.assertEqual(rows[0]['cost_observation']['credits_per_million_tokens'],0)
            self.assertIsNone(rows[0]['value_score'])
            with self.assertRaises(ValueError):
                b.import_records({'records':[{'model_id':'a','source':'invented','source_url':'javascript:alert(1)',
                    'date':'2026-06-25','coding':99}]})


if __name__=='__main__':unittest.main()
