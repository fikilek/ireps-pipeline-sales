import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sales_june_analytics_baseline import current_members
from sales_june_baseline import exact_ids, guard_plan


class Book:
    def __init__(self, rows): self.rows = rows
    def __getitem__(self, _): return self
    def iter_rows(self, **_): return iter(self.rows)
    def close(self): pass


class AnalyticsMembershipTests(unittest.TestCase):
    def derive(self, rows, ids=('04302064607', '04298092612')):
        with patch('sales_june_analytics_baseline.verified_bytes', return_value=b'pinned'), \
                patch('openpyxl.load_workbook', return_value=Book(rows)):
            return current_members({'sheet': 'Sheet1'}, set(ids))

    def test_header_not_position_and_previous_is_not_membership(self):
        result = self.derive([['PreviousMeterNumber', 'MeterNumber'], ['04298092612', '04302064607']])
        self.assertEqual(result['members'], ['04302064607'])
        self.assertEqual(result['identityColumn'], 2)

    def test_no_category_or_purchase_filter(self):
        result = self.derive([['MeterNumber', 'Category', 'Purchases'], ['04302064607', None, 0], ['04298092612', 'Normal', 0]])
        self.assertEqual(result['uniqueCurrentMeterCount'], 2)

    def test_unique_leading_zero_comparison(self):
        result = self.derive([['MeterNumber'], [4302064607]])
        self.assertEqual(result['members'], ['04302064607'])

    def test_invalid_duplicate_and_unknown_are_reported(self):
        result = self.derive([['MeterNumber'], ['04302064607'], [4302064607], [None], ['99999999999'], ['0']])
        self.assertEqual(result['duplicates'], ['04302064607'])
        # All-empty row is ignored; non-empty zero identity is invalid.
        self.assertEqual(len(result['invalid']), 1)
        self.assertEqual(len(result['unmatched']), 1)

    def test_ambiguous_numeric_alias_cannot_select_identity(self):
        result = self.derive([['MeterNumber'], [123]], ('0123', '00123'))
        self.assertEqual(result['members'], [])
        self.assertEqual(len(result['ambiguous']), 1)

    def test_missing_or_duplicate_header_rejected(self):
        for headers in [['PreviousMeterNumber'], ['MeterNumber', 'MeterNumber']]:
            with self.assertRaises(ValueError): self.derive([headers])

    def test_scope_count_is_observed_not_forced_and_substitution_fails(self):
        exact_ids(['04302064607'], ['04302064607'])
        with self.assertRaises(ValueError): exact_ids(['04298092612'], ['04302064607'])

    def test_predecessor_write_is_outside_corrected_scope(self):
        with self.assertRaises(ValueError):
            guard_plan([{'masterId': '04302064607'}], [{'decisions': [
                {'masterId': '04298092612', 'classification': 'UPDATED', 'updates': {}}
            ]}], ['04302064607'])


if __name__ == '__main__': unittest.main()
