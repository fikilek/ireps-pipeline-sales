import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from publish_sales_governed_artifacts import BUCKET, publish_prepared, validate_package
from sales_release_publication import build_publication
from test_sales_release_publication_offline import entry


class Missing(Exception): pass
class Changed(Exception): pass


class FakeBucket:
    def __init__(self):
        self.objects = {}; self.calls = []; self.fail_path = None
    def blob(self, path):
        parent = self
        class Blob:
            generation = None
            def reload(self, **kwargs):
                parent.calls.append(("reload", path, kwargs))
                if path not in parent.objects: raise Missing()
                self.generation = parent.objects[path][0]
            def download_as_bytes(self, **kwargs):
                parent.calls.append(("download", path, kwargs))
                if parent.objects[path][0] != kwargs["if_generation_match"]: raise Changed()
                return parent.objects[path][1]
            def upload_from_string(self, raw, **kwargs):
                parent.calls.append(("upload", path, kwargs))
                if path == parent.fail_path: raise RuntimeError("simulated upload failure")
                generation = parent.objects.get(path, (0,))[0]
                if generation != kwargs["if_generation_match"]: raise Changed()
                parent.objects[path] = (generation + 1, raw)
        return Blob()


def prepared():
    return {"publicationPath": "governed-sales/ZA5241/contour/publication.json", "expectedGeneration": 0,
            "previousBytes": None, "publicationBytes": b'{"new":true}', "latestMonth": "2026-06",
            "uploads": [("governed-sales/ZA5241/contour/snapshots/a.json", b"snapshot"),
                        ("governed-sales/ZA5241/contour/reports/b.json", b"report")]}


def publish(plan, bucket):
    return publish_prepared(plan, bucket, not_found=Missing, precondition_failed=Changed)


class PublishTests(unittest.TestCase):
    def test_immutable_create_only_then_publication_last_with_no_retries(self):
        bucket = FakeBucket(); plan = prepared(); result = publish(plan, bucket)
        uploads = [call for call in bucket.calls if call[0] == "upload"]
        self.assertEqual(uploads[-1][1], plan["publicationPath"])
        self.assertEqual([call[2]["if_generation_match"] for call in uploads], [0, 0, 0])
        self.assertTrue(all(call[2]["retry"] is None and call[2]["timeout"] == 20 for call in bucket.calls))
        self.assertEqual(result["firestoreWrites"], 0)
        self.assertEqual(result["publicationWrites"], 1)

    def test_changed_generation_stops_before_any_upload(self):
        plan = prepared(); bucket = FakeBucket(); bucket.objects[plan["publicationPath"]] = (2, b"old")
        with self.assertRaises(ValueError): publish(plan, bucket)
        self.assertFalse(any(call[0] == "upload" for call in bucket.calls))

    def test_failed_blob_never_publishes(self):
        plan = prepared(); bucket = FakeBucket(); bucket.fail_path = plan["uploads"][1][0]
        with self.assertRaises(RuntimeError): publish(plan, bucket)
        self.assertNotIn(plan["publicationPath"], bucket.objects)

    def test_existing_identical_artifact_reused_without_overwrite(self):
        plan = prepared(); bucket = FakeBucket(); path, raw = plan["uploads"][0]
        bucket.objects[path] = (7, raw)
        result = publish(plan, bucket)
        self.assertEqual(result["immutableReused"], 1)
        self.assertEqual(bucket.objects[path], (7, raw))

    def test_different_immutable_bytes_stop_publication(self):
        plan = prepared(); bucket = FakeBucket(); bucket.objects[plan["uploads"][0][0]] = (1, b"wrong")
        with self.assertRaises(ValueError): publish(plan, bucket)
        self.assertNotIn(plan["publicationPath"], bucket.objects)

    def test_final_pointer_uses_reviewed_generation(self):
        plan = prepared(); plan.update(expectedGeneration=3, previousBytes=b"old")
        bucket = FakeBucket(); bucket.objects[plan["publicationPath"]] = (3, b"old")
        publish(plan, bucket)
        self.assertEqual(bucket.calls[-1][2]["if_generation_match"], 3)
        self.assertEqual(bucket.objects[plan["publicationPath"]][0], 4)

    def test_cli_default_offline_and_changed_package_refused(self):
        script = Path(__file__).resolve().parents[1] / "scripts/publish_sales_governed_artifacts.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def write(name, data):
                path = root / name; path.write_text(json.dumps(data))
                return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            snapshot, _, report, _ = entry()
            sp = write("snapshot.json", snapshot)
            report["sourceEvidence"]["populationSnapshotSha256"] = sp["sha256"]
            rp = write("report.json", report)
            publication = build_publication([(snapshot, sp["sha256"], report, rp["sha256"])],
                project_id="ireps2", lm_pcode="ZA5241", provider="contour", baseline_month="2026-06")
            pp = write("publication.json", publication)
            package = {"schemaVersion": 1, "projectId": "ireps2", "bucket": BUCKET, "lmPcode": "ZA5241",
                       "provider": "contour", "baselineMonth": "2026-06", "expectedPublicationGeneration": 0,
                       "previousPublication": None, "months": [{"snapshot": sp, "report": rp}], "publication": pp}
            pkg = write("package.json", package)
            args = [sys.executable, "-B", str(script), "--package", pkg["path"], "--package-sha256", pkg["sha256"],
                    "--report", str(root / "execution.json")]
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["result"], "OFFLINE_VALIDATED_NOT_PUBLISHED")
            self.assertEqual(json.loads(result.stdout)["storageWrites"], 0)
            Path(sp["path"]).write_text("{}"); args[-1] = str(root / "must-not-exist.json")
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "must-not-exist.json").exists())


if __name__ == "__main__": unittest.main()
