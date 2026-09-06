import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sales_sg_erf_plan import build_plan


def erf(erf_id="E1", key="ORIGINAL-PARCEL-KEY"):
    return {"erfId": erf_id, "admin": {"localMunicipality": {"pcode": "ZA5241"}},
            "sg": {"prclKey": key, "erfNo": "0012"}}


def row():
    return {"masterId": "000123", "expected": {"lmPcode": "ZA5241", "hasUsableGps": True,
            "erfCandidates": [{"ErfId": "E1", "LmPcode": "ZA5241", "ErfNumber": "0012"}],
            "master": {"visibility": "VISIBLE"}, "metadata": {"createdByUid": "original"}}}


class EnrichmentPlanTests(unittest.TestCase):
    def test_resolves_source_evidence_without_inventing_destination_or_mutating_input(self):
        source = row(); before = copy.deepcopy(source)
        plan = build_plan([source], [erf()], lm_pcode="ZA5241")
        self.assertEqual(source, before)
        self.assertEqual(plan["authoritativeResolutions"], [{"masterId": "000123", "erfId": "E1", "sourceParcelKey": "ORIGINAL-PARCEL-KEY", "erfNo": "0012"}])
        self.assertEqual(plan["proposedFirestoreWrites"], [])
        self.assertEqual(plan["result"], "PROJECTION_CONTRACT_REQUIRED")

    def test_ambiguous_parcel_is_an_exception(self):
        p = build_plan([row()], [erf(), erf("E2")], lm_pcode="ZA5241")
        self.assertEqual(p["exceptions"][0]["reason"], "AMBIGUOUS_AUTHORITATIVE_PARCEL_KEY")

    def test_candidate_requires_independent_authority(self):
        p = build_plan([row()], [erf("E2")], lm_pcode="ZA5241")
        self.assertEqual(p["authoritativeOneToOneCount"], 0)

    def test_mismatch_preserves_exception(self):
        r = row(); r["expected"]["erfCandidates"][0]["ErfNumber"] = "99"
        self.assertEqual(build_plan([r], [erf()], lm_pcode="ZA5241")["exceptions"][0]["reason"], "CANDIDATE_ERF_NUMBER_MISMATCH")

    def test_missing_candidate_is_not_fabricated(self):
        r = row(); r["expected"]["erfCandidates"] = []
        p = build_plan([r], [erf()], lm_pcode="ZA5241")
        self.assertEqual((p["exceptionCount"], p["recordsInspected"], p["firestoreWrites"]), (1, 1, 0))

    def test_duplicate_erf_or_meter_fails_closed(self):
        for rows, erfs in [([row()], [erf(), erf()]), ([row(), row()], [erf()])]:
            with self.assertRaises(ValueError):build_plan(rows, erfs, lm_pcode="ZA5241")

    def test_scope_mismatch_fails_closed(self):
        r = row(); r["expected"]["lmPcode"] = "WRONG"
        with self.assertRaises(ValueError):build_plan([r], [erf()], lm_pcode="ZA5241")


if __name__ == "__main__":
    unittest.main()
