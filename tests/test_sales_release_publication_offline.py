import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sales_release_publication import build_publication


def entry(month="2026-06", snapshot_sha="a" * 64, previous=None):
    snapshot = {"schemaVersion": 1, "lmPcode": "ZA5241", "provider": "contour", "month": month,
                "members": ["000123"], "previousSnapshotSha256": previous,
                "sourceSha256": "c" * 64, "replacements": [], "exceptions": [],
                "completeness": {"complete": True, "evidenceSha256": "c" * 64}}
    report = {"projectId": "ireps2", "collection": "sales-all-meters", "status": "PASS",
              "result": "REFRESH_VERIFIED", "preflightOnly": False, "rowsRead": 1, "recordsInspected": 1,
              "createdCount": 0, "updatedCount": 1, "unchangedCount": 0, "conflictCount": 0, "failedCount": 0,
              "writeAttemptCount": 1, "writeSuccessCount": 1, "firestoreWrites": 1,
              "globalPreflight": {"complete": True, "gatePassed": True},
              "sourceEvidence": {"lmPcode": "ZA5241", "provider": "contour", "governedMonth": month,
                                 "populationSnapshotSha256": snapshot_sha, "categoryPackageSha256": "d" * 64},
              "verification": {"status": "PASS", "documentsVerified": 1,
                               "preservationVerifiedExistingDocuments": 1,
                               "preservedProjectionBeforeSha256": "e" * 64, "preservedProjectionAfterSha256": "e" * 64},
              "recoveryEvidence": {"complete": True, "records": 1, "sha256": "f" * 64},
              "batchEvidence": {"failedWriteWave": None, "writeWavesAttempted": 1, "writeWavesCommitted": 1}}
    report["sourceEvidence"].update(scopeDocumentIds=["000123"], scopeRecordCount=1,
        scopeDocumentIdsSha256=hashlib.sha256(b'["000123"]').hexdigest())
    report["planEvidence"] = {"complete": True, "records": 1, "sha256": "9" * 64,
        "scopeDocumentIdsSha256": report["sourceEvidence"]["scopeDocumentIdsSha256"],
        "categoryPackageSha256": report["sourceEvidence"]["categoryPackageSha256"]}
    return snapshot, snapshot_sha, report, "b" * 64


def publish(entries):
    return build_publication(entries, project_id="ireps2", lm_pcode="ZA5241", provider="contour", baseline_month="2026-06")


class PublicationTests(unittest.TestCase):
    def test_malformed_or_unsupported_population_relations_block(self):
        for replacements, exceptions in [([None], []),
                ([{"predecessor": "111111", "successor": "000123"}], []),
                ([], [{"meterId": "000123", "reason": ""}]), ([], [None])]:
            e = entry(); e[0].update(replacements=replacements, exceptions=exceptions)
            with self.subTest(replacements=replacements, exceptions=exceptions), self.assertRaises(ValueError):
                publish([e])

    def test_verified_chain_publishes_one_common_latest_month(self):
        entries = [entry(), entry("2026-07", "e" * 64, "a" * 64), entry("2026-08", "f" * 64, "e" * 64)]
        before = copy.deepcopy(entries)
        result = publish(entries)
        self.assertEqual(result["latestMonth"], "2026-08")
        self.assertEqual(list(result["months"]), ["2026-06", "2026-07", "2026-08"])
        self.assertEqual(entries, before)

    def test_preflight_never_publishes(self):
        e = entry(); e[2]["preflightOnly"] = True
        with self.assertRaises(ValueError):publish([e])

    def test_failed_partial_incomplete_or_unverified_runs_never_publish(self):
        for key, value in [("status", "FAIL"), ("result", "REFRESH_ABORTED_PARTIAL"), ("recordsInspected", 0),
                           ("conflictCount", 1), ("failedCount", 1), ("writeSuccessCount", 0)]:
            e = entry(); e[2][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):publish([e])
        e = entry(); e[2]["verification"]["documentsVerified"] = 0
        with self.assertRaises(ValueError):publish([e])

    def test_zero_write_idempotent_verified_run_can_publish(self):
        e = entry()
        e[2].update(updatedCount=0, unchangedCount=1, writeAttemptCount=0, writeSuccessCount=0, firestoreWrites=0)
        e[2]["batchEvidence"].update(writeWavesAttempted=0, writeWavesCommitted=0)
        self.assertEqual(publish([e])["latestMonth"], "2026-06")

    def test_missing_month_or_predecessor_blocks(self):
        with self.assertRaises(ValueError):publish([entry(), entry("2026-08", "e" * 64, "a" * 64)])
        with self.assertRaises(ValueError):publish([entry(), entry("2026-07", "e" * 64, "f" * 64)])

    def test_wrong_project_scope_snapshot_or_month_report_binding_blocks(self):
        for key, value in [("populationSnapshotSha256", "e" * 64), ("governedMonth", "2026-07"), ("lmPcode", "WRONG")]:
            e = entry(); e[2]["sourceEvidence"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):publish([e])
        e = entry(); e[2]["projectId"] = "ireps-test"
        with self.assertRaises(ValueError):publish([e])

    def test_missing_membership_attestation_and_duplicate_members_block(self):
        e = entry(); e[0]["completeness"]["complete"] = False
        with self.assertRaises(ValueError):publish([e])

    def test_scope_and_write_wave_evidence_cannot_claim_unchecked_population(self):
        e = entry(); e[0]["members"].append("999999")
        with self.assertRaises(ValueError):publish([e])
        for waves in [-1, 0, True]:
            e = entry(); e[2]["batchEvidence"].update(writeWavesAttempted=waves, writeWavesCommitted=waves)
            with self.subTest(waves=waves), self.assertRaises(ValueError):publish([e])

    def test_preservation_and_recovery_are_required(self):
        e = entry(); e[2]["verification"]["preservedProjectionAfterSha256"] = "a" * 64
        with self.assertRaises(ValueError):publish([e])
        e = entry(); e[2]["recoveryEvidence"]["complete"] = False
        with self.assertRaises(ValueError):publish([e])
        e = entry(); e[2]["planEvidence"]["scopeDocumentIdsSha256"] = "1" * 64
        with self.assertRaises(ValueError):publish([e])

    def test_cli_creates_proposal_only_for_actual_verified_report_inputs(self):
        script = Path(__file__).resolve().parents[1] / "scripts/sales_release_publication.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); snapshot, _, report, _ = entry()
            sp = root / "snapshot.json"; sp.write_text(json.dumps(snapshot))
            report["sourceEvidence"]["populationSnapshotSha256"] = hashlib.sha256(sp.read_bytes()).hexdigest()
            rp = root / "report.json"; rp.write_text(json.dumps(report)); out = root / "publication.json"
            args = [sys.executable, "-B", str(script), "--project-id", "ireps2", "--lm-pcode", "ZA5241",
                    "--provider", "contour", "--baseline-month", "2026-06", "--snapshot", str(sp),
                    "--verified-report", str(rp), "--output", str(out)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())["latestMonth"], "2026-06")
            report["preflightOnly"] = True; rp.write_text(json.dumps(report)); args[-1] = str(root / "must-not-exist.json")
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "must-not-exist.json").exists())
        e = entry(); e[0]["members"] *= 2
        with self.assertRaises(ValueError):publish([e])


if __name__ == "__main__":
    unittest.main()
