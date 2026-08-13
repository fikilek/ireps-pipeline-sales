from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sales_address_enrichment import (  # noqa: E402
    address_map_from_row,
    parse_physical_address,
    raw_address_mutation_count,
    raw_address_snapshot,
    validate_address_values,
)


class SalesAddressEnrichmentTests(unittest.TestCase):
    def assert_address(self, line1, line2, no, name, typ, status="ENRICHABLE"):
        result = parse_physical_address(line1, line2)
        self.assertEqual(result.status, status)
        self.assertEqual((result.strNo, result.strName, result.strType), (no, name, typ))
        return result

    def test_number_first(self):
        self.assert_address("34 BULWER", "", "34", "Bulwer", "-")

    def test_name_first(self):
        self.assert_address("MC KENZIE 45B", "SHOP NO 2", "45B", "Mc Kenzie", "-")

    def test_explicit_type_on_line_one(self):
        self.assert_address("42 MC KENZIE STREET", "SHOP 2", "42", "Mc Kenzie", "Street")

    def test_explicit_type_on_line_two(self):
        self.assert_address("42 MCKENZIE", "STREET", "42", "Mckenzie", "Street")

    def test_str_alias_is_street(self):
        self.assert_address("45 GLADSTONE STR", "FLAT NO 3", "45", "Gladstone", "Street")

    def test_cr_alias_is_crescent(self):
        self.assert_address("12 ALBATROSS CR", "", "12", "Albatross", "Crescent")

    def test_building_line_one_physical_line_two(self):
        self.assert_address("EVENTIDE MEWS H24", "103 MCKENZIE", "103", "Mckenzie", "-")

    def test_zigo_compound(self):
        self.assert_address("ZIGO SITHOLE 168, FAM UNIT 56", "", "168", "Zigo Sithole", "-")

    def test_block_line_one_zigo_line_two(self):
        self.assert_address("BLOCK 42 SIBONGILE", "168 ZIGO SITHOLE", "168", "Zigo Sithole", "-")

    def test_ndumeni_compound(self):
        self.assert_address("NDUMENI 16A , FAM UNIT NO 41", "", "16A", "Ndumeni", "-")

    def test_separated_suffix(self):
        self.assert_address("51 A WILLSON", "", "51A", "Willson", "-")

    def test_ordinal_name_is_not_corrected(self):
        self.assert_address("MBASA 13TH 506", "SIBONGILE", "506", "Mbasa 13th", "-")

    def test_source_spelling_is_not_corrected(self):
        self.assert_address("17 PH0LI", "", "17", "Ph0li", "-")

    def test_mgadi_compound_number_fails_closed(self):
        result = self.assert_address(
            "MGADI 577-1",
            "",
            "",
            "",
            "-",
            status="UNRESOLVED",
        )
        self.assertEqual(
            result.reason,
            "MULTIPLE_RANGE_OR_CONFLICTING_ADDRESS_CANDIDATES",
        )

    def test_mngadi_slash_number_fails_closed(self):
        result = self.assert_address(
            "562/1 MNGADI",
            "",
            "",
            "",
            "-",
            status="UNRESOLVED",
        )
        self.assertEqual(
            result.reason,
            "MULTIPLE_RANGE_OR_CONFLICTING_ADDRESS_CANDIDATES",
        )

    def test_mbele_leading_zero_is_preserved(self):
        self.assert_address("MBELE (GLENCOE) 06", "", "06", "Mbele", "-")

    def test_benville_unit_suffix(self):
        self.assert_address("BENVILLE 3 NO 7", "", "3", "Benville", "-")

    def test_conflicting_range_is_unresolved(self):
        result = self.assert_address("41 & 43 MAGNOLIA", "", "", "", "-", status="UNRESOLVED")
        self.assertEqual(result.reason, "MULTIPLE_RANGE_OR_CONFLICTING_ADDRESS_CANDIDATES")

    def test_zero_is_unresolved(self):
        result = self.assert_address("0 ZIGA SITHOLE", "", "", "", "-", status="UNRESOLVED")
        self.assertEqual(result.reason, "ZERO_OR_INVALID_STREET_NUMBER")

    def test_unit_only_is_unresolved(self):
        result = self.assert_address("FAMILY UNIT NO 9", "", "", "", "-", status="UNRESOLVED")
        self.assertEqual(result.reason, "UNIT_BLOCK_DWELLING_NUMBER_NOT_PROVEN_STRNO")

    def test_no_number_is_unresolved(self):
        result = self.assert_address("AIRFIELD HANGER", "VICTORIA STREET", "", "", "-", status="UNRESOLVED")
        self.assertEqual(result.reason, "NO_USABLE_STRNO_IN_SOURCE_ADDRESS")

    def test_partial_canonical_address_fails(self):
        with self.assertRaisesRegex(ValueError, "both be populated"):
            validate_address_values("42", "", "-")

    def test_unresolved_type_must_be_dash(self):
        with self.assertRaisesRegex(ValueError, "Unresolved address"):
            validate_address_values("", "", "Street")

    def test_address_map_has_exact_three_keys(self):
        result = address_map_from_row({"strNo": "42", "strName": "Mckenzie", "strType": "Street"})
        self.assertEqual(result, {"strNo": "42", "strName": "Mckenzie", "strType": "Street"})
        self.assertEqual(set(result), {"strNo", "strName", "strType"})

    def test_raw_address_mutation_counter_is_computed(self):
        before = raw_address_snapshot(
            {
                "ABC123": {
                    "addressLine1": "42 MCKENZIE",
                    "addressLine2": "STREET",
                    "town": "DUNDEE",
                }
            }
        )
        unchanged = raw_address_snapshot(
            {
                "ABC123": {
                    "addressLine1": "42 MCKENZIE",
                    "addressLine2": "STREET",
                    "town": "DUNDEE",
                }
            }
        )
        changed = raw_address_snapshot(
            {
                "ABC123": {
                    "addressLine1": "43 MCKENZIE",
                    "addressLine2": "STREET",
                    "town": "DUNDEE",
                }
            }
        )
        self.assertEqual(raw_address_mutation_count(before, unchanged), 0)
        self.assertEqual(raw_address_mutation_count(before, changed), 1)


if __name__ == "__main__":
    unittest.main()
