from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_monthly_categories_offline import (
    categories, refresh, fixture_package, payload, VALUE, CREATED, NOW,
)


def write(directory, name, value):
    path = directory / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path), "sha256": categories.sha(path)}


def fixture(directory):
    import openpyxl
    package = fixture_package(directory)
    previous = copy.deepcopy(package)
    june_snapshot = write(directory, "june.snapshot.json", {
        "schemaVersion": 1, "month": "2026-06", "members": ["M1"],
        "provider": "contour", "lmPcode": "ZA5241", "completeness": {"complete": True},
    })
    june_package = write(directory, "june.package.json", {
        "month": "2026-06", "projectId": "ireps2", "categories": {"M1": VALUE},
        "populationSnapshot": june_snapshot,
    })
    previous.update(categories={"M1": VALUE, "M2": VALUE}, historySources=[{
        "month": "2026-06", "categoryPackage": june_package, "populationSnapshot": june_snapshot,
    }])
    extra = write(directory, "stage06.placeholder.json", {})
    previous["creationStage06"] = {"ids": ["M2"], "input": extra,
                                    "manifest": extra, "sourceBinding": extra}
    july_snapshot = write(directory, "july.snapshot.json", {
        "schemaVersion": 1, "month": "2026-07", "members": ["M1", "M2"],
        "provider": "contour", "lmPcode": "ZA5241", "completeness": {"complete": True},
    })
    previous["populationSnapshot"] = july_snapshot
    previous_ref = write(directory, "july.package.json", previous)
    report_ref = write(directory, "july.execution.json", {
        "status": "PASS", "result": "REFRESH_VERIFIED", "preflightOnly": False,
        "projectId": "ireps2", "collection": "sales-all-meters",
        "verification": {"status": "PASS", "documentsVerified": 2},
    })
    verify_ref = write(directory, "july.readback.json", {"status": "PASS"})
    attestation = write(directory, "july.finalized.json", {
        "status": "FINALIZED_LOCAL_AFTER_ACTUAL_WRITE_AND_FULL_READBACK",
        "projectId": "ireps2", "collection": "sales-all-meters", "month": "2026-07",
        "provider": "contour", "lmPcode": "ZA5241", "memberCount": 2,
        "snapshot": july_snapshot, "package": previous_ref,
        "stage08Report": report_ref, "verification": verify_ref,
    })
    source = directory / "august.xlsx"
    book = openpyxl.Workbook()
    book.active.title = "Prepaid_30Month_Analysis"
    book.active.append(["MeterNumber", "August_2026_Category", "Risk_Tier", "Risk_Score"])
    for mid in ("M1", "M2"):
        book.active.append([mid, VALUE["leakageCategory"], VALUE["riskTier"], VALUE["riskScore"]])
    book.save(source)
    book.close()
    package.update(
        month="2026-08", lmPcode="ZA5241", provider="contour", executionIds=["M1", "M2"],
        source={"path": str(source), "sha256": categories.sha(source), "sheet": "Prepaid_30Month_Analysis"},
        previousMonth="2026-07",
        previousPopulationSnapshot=dict(july_snapshot, finalizationAttestation=attestation),
        existingStage06=previous["creationStage06"],
        categories={"M1": VALUE, "M2": VALUE},
        historicalCategories={"M1": {"2026-06": VALUE, "2026-07": VALUE}, "M2": {"2026-07": VALUE}},
        historySources=previous["historySources"] + [{
            "month": "2026-07", "categoryPackage": previous_ref, "populationSnapshot": july_snapshot,
        }],
    )
    snap = {
        "schemaVersion": 1, "month": "2026-08", "members": ["M1", "M2"],
        "provider": "contour", "lmPcode": "ZA5241", "completeness": {"complete": True},
        "sourceSha256": package["source"]["sha256"], "previousMonth": "2026-07",
        "previousSnapshotSha256": july_snapshot["sha256"],
    }
    package["populationSnapshot"] = write(directory, "august.snapshot.json", snap)
    pins = {"2026-08": {"month": "2026-07", "snapshotSha256": july_snapshot["sha256"],
                        "finalizationSha256": attestation["sha256"]}}
    return package, snap, pins


class AugustPackageTests(unittest.TestCase):
    def admit(self, directory, package, pins, extra_ids=("M2",)):
        ref = write(directory, "august.package.json", package)
        supplemental = [{"masterId": mid, "expected": payload(mid)} for mid in extra_ids]
        with patch.object(categories, "APPROVED_PREDECESSORS", pins), \
                patch.object(categories, "APPROVED_CREATOR_SCOPE_SHA", package["creatorScope"]["sha256"]), \
                patch.object(refresh, "load_and_validate", return_value=(supplemental, {})):
            return categories.load_package(ref["path"], ref["sha256"],
                [{"masterId": "M1", "expected": payload()}, {"masterId": "OUTSIDE", "expected": payload("OUTSIDE")}],
                "ireps2")

    def test_finalized_predecessor_admits_two_existing_updates_without_outside_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, _, pins = fixture(directory)
            selected, evidence = self.admit(directory, package, pins)
            self.assertEqual(evidence["exactExecutionIds"], ["M1", "M2"])
            self.assertTrue(evidence["categoryOnly"])
            self.assertEqual(evidence["creationIds"], [])
            for row in selected:
                self.assertNotIn("createOnly", row)
                self.assertNotIn("metadataRefresh", row)
                doc = payload(row["masterId"])
                doc["monthlyCategories"] = copy.deepcopy(package["historicalCategories"][row["masterId"]])
                doc.update(monthlySalesC={}, monthlyUnits={}, previousMeterNumber="PREDECESSOR",
                           installationDate="keep", previousInstallationDate="keep", geofenceRefs={"keep": 1})
                before = copy.deepcopy(doc)
                snap = SimpleNamespace(exists=True, create_time=CREATED, to_dict=lambda: copy.deepcopy(doc))
                decision = refresh._classify_snapshot(row, snap)
                self.assertEqual(decision["classification"], "UPDATED")
                self.assertEqual(set(decision["updates"]), {
                    "monthlyCategories.2026-08", "metadata.updatedAt",
                    "metadata.updatedByUid", "metadata.updatedByUser"})
                self.assertEqual(doc, before)

    def test_all_predecessor_reference_mutations_fail_even_when_candidate_is_rehashed(self):
        mutations = ["missingMonth", "wrongMonth", "missingReference", "wrongSha", "missingSnapshotMonth",
                     "wrongSnapshotMonth", "missingSnapshotSha", "wrongSnapshotSha", "skippedPredecessor",
                     "missingFinalization", "unapprovedFinalization"]
        for case in mutations:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                package, snap, pins = fixture(directory)
                if case == "missingMonth": package.pop("previousMonth")
                if case == "wrongMonth": package["previousMonth"] = "2026-06"
                if case == "missingReference": package.pop("previousPopulationSnapshot")
                if case == "wrongSha": package["previousPopulationSnapshot"]["sha256"] = "0" * 64
                if case == "missingSnapshotMonth": snap.pop("previousMonth")
                if case == "wrongSnapshotMonth": snap["previousMonth"] = "2026-06"
                if case == "missingSnapshotSha": snap.pop("previousSnapshotSha256")
                if case == "wrongSnapshotSha": snap["previousSnapshotSha256"] = "0" * 64
                if case == "skippedPredecessor":
                    package["previousMonth"] = snap["previousMonth"] = "2026-06"
                if case == "missingFinalization": package["previousPopulationSnapshot"].pop("finalizationAttestation")
                if case == "unapprovedFinalization":
                    package["previousPopulationSnapshot"]["finalizationAttestation"] = write(directory, "forged.json", {"finalized": True})
                package["populationSnapshot"] = write(directory, "august.snapshot.json", snap)
                with self.assertRaisesRegex(ValueError, "predecessor"):
                    self.admit(directory, package, pins)

    def test_changed_predecessor_bytes_fail_without_new_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, _, pins = fixture(directory)
            Path(package["previousPopulationSnapshot"]["path"]).write_text("{}")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                self.admit(directory, package, pins)

    def test_unexecuted_predecessor_cannot_pass_with_a_finalized_label(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, snap, pins = fixture(directory)
            ref = package["previousPopulationSnapshot"]["finalizationAttestation"]
            att = json.loads(Path(ref["path"]).read_text())
            att["stage08Report"] = write(directory, "preflight.json", {
                "status": "PASS", "result": "PREFLIGHT_PASS", "preflightOnly": True})
            new_ref = write(directory, "not-executed.json", att)
            package["previousPopulationSnapshot"]["finalizationAttestation"] = new_ref
            pins["2026-08"]["finalizationSha256"] = new_ref["sha256"]
            with self.assertRaisesRegex(ValueError, "execution/full-readback"):
                self.admit(directory, package, pins)

    def test_copied_july_history_or_rehashed_historical_package_fails(self):
        for case in ("missingJuly", "changedJune", "changedJuly"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                package, _, pins = fixture(directory)
                if case == "missingJuly":
                    package["historySources"].pop()
                else:
                    index = 0 if case == "changedJune" else 1
                    package["historySources"][index]["categoryPackage"] = write(directory, "forged-history.json", {})
                with self.assertRaisesRegex(ValueError, "history"):
                    self.admit(directory, package, pins)

    def test_supplement_rebinding_duplicate_and_outside_ids_fail(self):
        for case in ("rebound", "duplicates", "outside"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                package, _, pins = fixture(directory)
                extra_ids = ("M2",)
                if case == "rebound": package["existingStage06"]["input"] = write(directory, "forged-input.json", {})
                if case == "duplicates": extra_ids = ("M2", "M2")
                if case == "outside": extra_ids = ("OUTSIDE",)
                with self.assertRaisesRegex(ValueError, "Existing Stage06"):
                    self.admit(directory, package, pins, extra_ids)

    def test_july_create_only_cannot_be_reused_for_august(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, _, pins = fixture(directory)
            package["creationStage06"] = package.pop("existingStage06")
            with self.assertRaisesRegex(ValueError, "existing-document"):
                self.admit(directory, package, pins)

    def test_absent_august_target_is_conflict_not_create(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, _, pins = fixture(directory)
            selected, _ = self.admit(directory, package, pins)
            self.assertEqual(refresh._classify_snapshot(selected[-1], None)["classification"], "CONFLICT")

    def test_newly_missing_metadata_cannot_expand_august_write_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package, _, pins = fixture(directory)
            selected, _ = self.admit(directory, package, pins)
            doc = payload()
            doc["monthlyCategories"]["2026-07"] = VALUE
            del doc["metadata"]["createdAt"]
            result = refresh._classify_snapshot(selected[0], SimpleNamespace(
                exists=True, create_time=CREATED, to_dict=lambda: doc))
            self.assertEqual(result["classification"], "CONFLICT")
            self.assertEqual(result["updates"], {})
            self.assertIn("backfill not permitted", result["reason"])


class AugustIdentityTests(unittest.TestCase):
    def source(self, directory, corrected_ids, month="2026-08"):
        import openpyxl
        path = directory / "source.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Prepaid_30Month_Analysis"
        column = "August_2026_Category" if month == "2026-08" else "July_2026_Category"
        book.active.append(["MeterNumber", "PreviousMeterNumber", "CorrectedMeterNumber", column, "Risk_Tier", "Risk_Score"])
        for mid in corrected_ids:
            book.active.append(["00000000001", "00000000002", mid, "Normal - No Leakage Flag", "Normal", 0])
        book.save(path)
        book.close()
        return path

    def test_configured_july_and_august_preserve_exact_thirteen_character_identity(self):
        for month in ("2026-07", "2026-08"):
            with self.subTest(month=month), tempfile.TemporaryDirectory() as temp:
                path = self.source(Path(temp), ["0261249145812"], month)
                values, errors, aliases = categories.ingest_workbook(path, categories.sha(path),
                    "Prepaid_30Month_Analysis", month, {"0261249145812"},
                    identity_field="CorrectedMeterNumber")
                self.assertEqual(set(values), {"0261249145812"})
                self.assertEqual(errors + aliases, [])

    def test_unconfigured_source_still_uses_meter_number(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.source(Path(temp), ["0261249145812"])
            values, errors, aliases = categories.ingest_workbook(path, categories.sha(path),
                "Prepaid_30Month_Analysis", "2026-08", {"00000000001"})
            self.assertEqual(set(values), {"00000000001"})
            self.assertEqual(errors + aliases, [])

    def test_blank_duplicate_malformed_and_nontext_corrected_ids_fail(self):
        for ids in ([None], [""], ["123", "123"], ["123X"], [123], [" 123"], ["000"], ["123.0"]):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as temp:
                path = self.source(Path(temp), ids)
                with self.assertRaises(ValueError):
                    categories.ingest_workbook(path, categories.sha(path), "Prepaid_30Month_Analysis",
                        "2026-08", {"123"}, identity_field="CorrectedMeterNumber")

    def test_corrected_identity_is_not_padded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.source(Path(temp), ["4297839708"])
            with self.assertRaisesRegex(ValueError, "outside execution"):
                categories.ingest_workbook(path, categories.sha(path), "Prepaid_30Month_Analysis",
                    "2026-08", {"04297839708"}, identity_field="CorrectedMeterNumber")


if __name__ == "__main__":
    unittest.main()
