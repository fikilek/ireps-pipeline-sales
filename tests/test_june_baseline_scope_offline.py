import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sales_june_baseline as june
import sales_pipeline_sales_all_refresh as refresh


class JuneScopeTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f"J{i:05d}" for i in range(10216)]
        self.rows = [{"masterId": mid} for mid in self.ids]
        self.decisions = [{"masterId": mid, "classification": "UNCHANGED", "updates": {}} for mid in self.ids]
        self.plan = [{"waveNumber": n // 400 + 1, "decisions": self.decisions[n:n+400]}
            for n in range(0, len(self.ids), 400)]

    def test_exact_baseline_and_26_waves(self):
        june.exact_ids(reversed(self.ids), self.ids)
        june.guard_plan(self.rows, self.plan, self.ids)
        self.assertEqual([len(w['decisions']) for w in self.plan], [400]*25+[216])

    def test_rejects_cumulative_10271_before_classification(self):
        with self.assertRaisesRegex(ValueError, "exact approved"):
            june.exact_ids(self.ids + [f"LATE{i}" for i in range(55)], self.ids)

    def test_same_count_substitution_missing_and_duplicate_fail(self):
        for actual in [self.ids[:-1]+["LATER"], self.ids[:-1], self.ids[:-1]+[self.ids[0]]]:
            with self.subTest(actualTail=actual[-1]):
                with self.assertRaises(ValueError):
                    june.exact_ids(actual, self.ids)

    def test_prewrite_gate_rejects_outside_scope_before_any_batch(self):
        self.plan[-1]['decisions'][-1]['masterId'] = 'LATER'
        stats = refresh.RefreshStats(10216)
        stats.global_preflight_gate_passed = True
        def forbidden_batch():
            self.fail('A batch was opened before the June scope gate')
        with self.assertRaisesRegex(ValueError, 'exact approved'):
            refresh.execute_global_plan(db=SimpleNamespace(batch=forbidden_batch), collection=None,
                rows=self.rows, plan=self.plan, stats=stats, last_update_option_cls=None,
                concurrency_exceptions=(), scope_guard=lambda rows, plan: june.guard_plan(rows, plan, self.ids))
        self.assertEqual(stats.writes_attempted, 0)

    def test_creates_whole_history_and_other_paths_fail(self):
        for classification, updates in [('CREATED', {}), ('UPDATED', {'monthlyCategories': {}}),
                ('UPDATED', {'monthlyCategories.2026-07': {}}), ('UPDATED', {'salesStatus': {}})]:
            with self.subTest(classification=classification, updates=updates):
                plan = copy.deepcopy(self.plan)
                plan[0]['decisions'][0].update(classification=classification, updates=updates)
                with self.assertRaises(ValueError):
                    june.guard_plan(self.rows, plan, self.ids)


if __name__ == '__main__':
    unittest.main()
