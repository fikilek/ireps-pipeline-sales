import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sales_recovery_plan import build_recovery


def fixture():
    fields = {"salesStatus": {"stringValue": "targeted"}, "updated": {"stringValue": "old"},
              "monthlyCategories": {"mapValue": {"fields": {"2026-05": {"mapValue": {"fields": {}}}}}}}
    old = {"masterId": "000123", "exists": True, "document": {"fields": fields, "updateTime": "2026-09-01T01:00:00Z"}}
    cat = {"mapValue": {"fields": {"category": {"stringValue": "CAT2"}}}}
    plan = {"masterId": "000123", "classification": "UPDATED", "preconditionUpdateTime": old["document"]["updateTime"],
            "afterPatch": {"monthlyCategories.2026-06": cat, "updated": {"stringValue": "new"}}}
    current = copy.deepcopy(old)
    current["document"]["fields"]["monthlyCategories"]["mapValue"]["fields"]["2026-06"] = cat
    current["document"]["fields"]["updated"] = {"stringValue": "new"}
    current["document"]["fields"]["salesStatus"] = {"stringValue": "visited"}
    current["document"]["updateTime"] = "2026-09-05T01:00:00Z"
    report = {"projectId": "ireps2", "collection": "sales-all-meters", "preflightOnly": False,
              "sourceEvidence": {"scopeDocumentIds": ["000123"],
                                 "scopeDocumentIdsSha256": hashlib.sha256(b'["000123"]').hexdigest()},
              "recoveryEvidence": {"complete": True, "records": 1}, "planEvidence": {"complete": True, "records": 1}}
    return report, [old], [plan], [current]


class RecoveryTests(unittest.TestCase):
    def test_actual_writer_protobuf_encoding_is_accepted(self):
        from sales_monthly_categories import encode_before_image, encode_write_plan
        from types import SimpleNamespace
        before_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
        current_time = datetime(2026, 9, 5, tzinfo=timezone.utc)
        old_payload = {"metadata": {"updatedAt": before_time}, "salesStatus": "targeted"}
        new_payload = {"metadata": {"updatedAt": current_time}, "salesStatus": "visited",
                       "monthlyCategories": {"2026-06": {"category": "CAT2"}}}
        def snap(payload, time):
            return SimpleNamespace(exists=True, create_time=before_time, update_time=time,
                                   to_dict=lambda: payload)
        item = {"masterId": "000123"}
        before = encode_before_image(item, snap(old_payload, before_time))
        current = encode_before_image(item, snap(new_payload, current_time))
        plan = encode_write_plan({"masterId": "000123", "classification": "UPDATED", "updateTime": before_time,
                                  "updates": {"metadata.updatedAt": current_time,
                                              "monthlyCategories.2026-06": {"category": "CAT2"}}}, item)
        report = fixture()[0]
        proposal = build_recovery(report, [before], [plan], [current])["proposals"][0]
        self.assertEqual(proposal["restoreFields"]["metadata.updatedAt"], {"timestampValue": "2026-09-01T00:00:00Z"})
        self.assertEqual(proposal["deleteFields"], ["monthlyCategories.2026-06"])

    def test_only_touched_fields_proposed_operational_changes_preserved(self):
        args = fixture(); original = copy.deepcopy(args)
        result = build_recovery(*args)
        self.assertEqual(result["firestoreWrites"], 0)
        self.assertFalse(result["automaticExecution"])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["restoreFields"], {"updated": {"stringValue": "old"}})
        self.assertEqual(proposal["deleteFields"], ["monthlyCategories.2026-06"])
        self.assertEqual(proposal["preconditionUpdateTime"], args[3][0]["document"]["updateTime"])
        self.assertEqual(args, original)

    def test_concurrent_change_to_written_path_blocks_entire_document(self):
        args = fixture(); args[3][0]["document"]["fields"]["updated"] = {"stringValue": "newer"}
        result = build_recovery(*args)
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["exceptions"][0]["reason"], "CURRENT_STATE_DIFFERS_FROM_PLANNED_WRITE")

    def test_created_document_is_never_deleted(self):
        args = fixture(); args[2][0]["classification"] = "CREATED"
        result = build_recovery(*args)
        self.assertEqual(result["proposals"], [])
        self.assertIn("NO_DELETE", result["exceptions"][0]["reason"])

    def test_wrong_project_preflight_duplicate_missing_scope_and_version_block(self):
        for mutation in (lambda a: a[0].update(projectId="prod"), lambda a: a[0].update(preflightOnly=True),
                         lambda a: a[1].append(a[1][0]), lambda a: a[3].clear(),
                         lambda a: a[2][0].update(preconditionUpdateTime="changed")):
            args = fixture(); mutation(args)
            with self.assertRaises(ValueError): build_recovery(*args)

    def test_missing_current_precondition_and_overlapping_paths_block(self):
        args = fixture(); del args[3][0]["document"]["updateTime"]
        with self.assertRaises(ValueError): build_recovery(*args)
        args = fixture(); args[2][0]["afterPatch"]["monthlyCategories"] = {"mapValue": {"fields": {}}}
        with self.assertRaises(ValueError): build_recovery(*args)

    def test_cli_binds_exact_bytes_and_refuses_modified_evidence(self):
        script = Path(__file__).resolve().parents[1] / "scripts/sales_recovery_plan.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); report, before, plan, current = fixture(); hashes = {}; paths = {}
            for name, rows in (("before", before), ("plan", plan), ("current", current)):
                paths[name] = root / (name + ".jsonl")
                paths[name].write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                hashes[name] = hashlib.sha256(paths[name].read_bytes()).hexdigest()
            report["recoveryEvidence"]["sha256"] = hashes["before"]
            report["planEvidence"]["sha256"] = hashes["plan"]
            paths["report"] = root / "report.json"; paths["report"].write_text(json.dumps(report))
            hashes["report"] = hashlib.sha256(paths["report"].read_bytes()).hexdigest()
            args = [sys.executable, "-B", str(script)]
            for name in ("report", "before", "plan", "current"):
                args.extend(["--" + name, str(paths[name]), "--" + name + "-sha256", hashes[name]])
            args.extend(["--output", str(root / "proposal.json")])
            process = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            paths["current"].write_text("[]")
            args[-1] = str(root / "must-not-exist.json")
            process = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse((root / "must-not-exist.json").exists())


if __name__ == "__main__":
    unittest.main()
