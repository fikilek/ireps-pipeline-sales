from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "tools" / "sales-all" / "align_sales_adr_v1.py"
SPEC = importlib.util.spec_from_file_location("align_sales_adr_v1", TOOL)
assert SPEC is not None and SPEC.loader is not None
adr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adr
SPEC.loader.exec_module(adr)


class FakeBatch:
    def __init__(self) -> None:
        self.updates = []
        self.commits = 0

    def update(self, ref, data, option=None):
        self.updates.append((ref, data, option))

    def commit(self):
        self.commits += 1


class FakeCollection:
    def document(self, master_id):
        return f"ref:{master_id}"


class FakeDb:
    def __init__(self) -> None:
        self.batches = []

    def collection(self, name):
        self.collection_name = name
        return FakeCollection()

    def batch(self):
        batch = FakeBatch()
        self.batches.append(batch)
        return batch


class LastUpdateOption:
    def __init__(self, value):
        self.value = value


class SalesAdrAlignmentTests(unittest.TestCase):
    def expected(self):
        return {
            "master": {"id": "ABC123", "visibility": "INVISIBLE"},
            "meterNo": "ABC123",
            "meterNoNormalized": "ABC123",
            "provider": "conlog",
            "lmPcode": "ZA5241",
            "town": "Dundee",
            "adr": {"strNo": "42", "strName": "Mc Kenzie", "strType": "Street"},
        }

    def payload(self):
        return {
            "master": {"id": "ABC123", "visibility": "VISIBLE"},
            "meterNo": "ABC123",
            "meterNoNormalized": "ABC123",
            "provider": "conlog",
            "lmPcode": "ZA5241",
            "town": "Dundee",
            "tbRefs": ["TB-1"],
        }

    def test_governed_constants_lock_population_and_batch_size(self):
        self.assertEqual(adr.EXPECTED_ROWS, 10216)
        self.assertEqual(adr.EXPECTED_ADDRESS_ENRICHED, 10117)
        self.assertEqual(adr.EXPECTED_ADDRESS_UNRESOLVED, 99)
        self.assertEqual(adr.EXPECTED_LM_PCODE, "ZA5241")
        self.assertEqual(adr.FIRESTORE_BATCH_SIZE, 400)
        self.assertEqual(
            adr.EXPECTED_CSV_SHA256,
            "1a5a7314547a239e4d7015579a5e8f3ba86d90d827e70fc7afc6c96b5a589cb2",
        )

    def test_10216_partitions_into_25x400_plus_216(self):
        sizes = [len(chunk) for chunk in adr._chunks(list(range(10216)))]
        self.assertEqual(sizes, [400] * 25 + [216])

    def test_project_gate_rejects_wrong_project(self):
        with self.assertRaisesRegex(ValueError, "not approved"):
            adr.validate_project_gate("ireps2", "ireps2", False, "")

    def test_project_gate_requires_exact_repeat(self):
        with self.assertRaisesRegex(ValueError, "confirmation mismatch"):
            adr.validate_project_gate("ireps-test", "ireps-5c3e9", False, "")

    def test_execute_requires_environment_token(self):
        with self.assertRaisesRegex(ValueError, "token mismatch"):
            adr.validate_project_gate("ireps-test", "ireps-test", True, "WRONG")
        adr.validate_project_gate(
            "ireps-test", "ireps-test", True, "ALIGN_SALES_ADR_IREPS_TEST_ZA5241"
        )

    def test_execute_is_blocked_outside_clean_aligned_main(self):
        with self.assertRaisesRegex(ValueError, "only from Git branch main"):
            adr.validate_execution_context_values(
                branch="fix/work", status_porcelain="", head="a", origin_main="a"
            )
        with self.assertRaisesRegex(ValueError, "clean Git"):
            adr.validate_execution_context_values(
                branch="main", status_porcelain=" M x", head="a", origin_main="a"
            )
        with self.assertRaisesRegex(ValueError, "equal origin/main"):
            adr.validate_execution_context_values(
                branch="main", status_porcelain="", head="a", origin_main="b"
            )
        adr.validate_execution_context_values(
            branch="main", status_porcelain="", head="a", origin_main="a"
        )

    def test_missing_document_is_conflict_not_create(self):
        decision = adr.classify_payload(
            master_id="ABC123", payload=None, expected=self.expected(), update_time=None
        )
        self.assertEqual(decision["classification"], "MISSING_DOCUMENT")

    def test_missing_adr_is_the_only_writable_classification(self):
        decision = adr.classify_payload(
            master_id="ABC123",
            payload=self.payload(),
            expected=self.expected(),
            update_time="T1",
        )
        self.assertEqual(decision["classification"], "UPDATE_MISSING_ADR")
        self.assertEqual(decision["expectedAdr"], self.expected()["adr"])

    def test_matching_adr_is_idempotent(self):
        payload = self.payload()
        payload["adr"] = dict(self.expected()["adr"])
        decision = adr.classify_payload(
            master_id="ABC123", payload=payload, expected=self.expected(), update_time="T1"
        )
        self.assertEqual(decision["classification"], "MATCHING_ADR")

    def test_existing_different_adr_is_conflict_not_overwrite(self):
        payload = self.payload()
        payload["adr"] = {"strNo": "43", "strName": "Mc Kenzie", "strType": "Street"}
        decision = adr.classify_payload(
            master_id="ABC123", payload=payload, expected=self.expected(), update_time="T1"
        )
        self.assertEqual(decision["classification"], "ADR_CONFLICT")

    def test_non_adr_delta_blocks_alignment(self):
        payload = self.payload()
        payload["town"] = "Glencoe"
        decision = adr.classify_payload(
            master_id="ABC123", payload=payload, expected=self.expected(), update_time="T1"
        )
        self.assertEqual(decision["classification"], "NON_ADR_CONFLICT")
        self.assertEqual(decision["fields"], ["town"])

    def test_identity_and_root_address_drift_block_alignment(self):
        payload = self.payload()
        payload["strNo"] = "42"
        decision = adr.classify_payload(
            master_id="ABC123", payload=payload, expected=self.expected(), update_time="T1"
        )
        self.assertEqual(decision["classification"], "IDENTITY_CONFLICT")

    def test_preservation_hash_ignores_only_adr(self):
        payload = self.payload()
        h1 = adr._preserved_hash(payload)
        payload["adr"] = dict(self.expected()["adr"])
        h2 = adr._preserved_hash(payload)
        self.assertEqual(h1, h2)
        payload["tbRefs"].append("TB-2")
        self.assertNotEqual(h2, adr._preserved_hash(payload))

    def test_execute_plans_writes_only_adr_and_uses_precondition(self):
        db = FakeDb()
        stats = adr.AlignmentStats(rows=1)
        plan = adr.PlannedUpdate(
            master_id="ABC123",
            expected_adr=dict(self.expected()["adr"]),
            update_time="T1",
            preserved_hash="hash",
        )
        progress = []
        adr.execute_plans(
            db=db,
            plans=[plan],
            last_update_option_cls=LastUpdateOption,
            stats=stats,
            progress=progress.append,
        )
        self.assertEqual(db.collection_name, adr.COLLECTION)
        self.assertEqual(len(db.batches), 1)
        batch = db.batches[0]
        self.assertEqual(batch.commits, 1)
        self.assertEqual(len(batch.updates), 1)
        ref, data, option = batch.updates[0]
        self.assertEqual(ref, "ref:ABC123")
        self.assertEqual(data, {"adr": self.expected()["adr"]})
        self.assertEqual(option.value, "T1")
        self.assertEqual(stats.writes_succeeded, 1)
        self.assertEqual(stats.maximum_write_operations_in_any_batch, 1)
        self.assertTrue(any("WRITE wave 1/1" in message for message in progress))
        self.assertTrue(any("WRITE PROGRESS 1/1" in message for message in progress))

    def test_execute_plans_never_exceeds_400(self):
        db = FakeDb()
        stats = adr.AlignmentStats(rows=801)
        plans = [
            adr.PlannedUpdate(str(i), {"strNo": "1", "strName": "A", "strType": "-"}, i, "h")
            for i in range(801)
        ]
        progress = []
        adr.execute_plans(
            db=db,
            plans=plans,
            last_update_option_cls=LastUpdateOption,
            stats=stats,
            progress=progress.append,
        )
        self.assertEqual([len(batch.updates) for batch in db.batches], [400, 400, 1])
        self.assertTrue(any("WRITE wave 1/3" in message for message in progress))
        self.assertTrue(any("WRITE wave 3/3" in message for message in progress))
        self.assertEqual(stats.maximum_write_operations_in_any_batch, 400)

    def test_frozen_source_gate_rejects_wrong_hash_and_accepts_matching_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "source.csv"
            csv_path.write_bytes(b"test-source\n")
            manifest_path = root / "source.manifest.json"
            csv_sha = adr.hashlib.sha256(csv_path.read_bytes()).hexdigest()
            manifest = {
                "schemaVersion": 2,
                "stage": "06",
                "status": "PASS",
                "sourceContract": {"lmPcode": "ZA5241"},
                "outputContract": {
                    "rows": 10216,
                    "sha256": csv_sha,
                    "addressEnrichment": {
                        "enabled": True,
                        "enrichedRows": 10117,
                        "unresolvedRows": 99,
                        "firestoreProjection": "adr",
                        "rawAddressMutationCount": 0,
                        "fabricatedSpatialRelationshipCount": 0,
                        "stagingColumns": ["strNo", "strName", "strType"],
                    },
                },
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA mismatch"):
                adr.validate_frozen_source(csv_path, manifest_path)
            with mock.patch.object(adr, "EXPECTED_CSV_SHA256", csv_sha):
                contract = adr.validate_frozen_source(csv_path, manifest_path)
            self.assertEqual(contract.rows, 10216)
            self.assertEqual(contract.enriched_rows, 10117)

    def test_source_text_forbids_create_delete_and_per_document_fallback(self):
        text = TOOL.read_text(encoding="utf-8")
        self.assertIn('batch.update(', text)
        self.assertIn('{"adr": dict(plan.expected_adr)}', text)
        self.assertIn('LastUpdateOption', text)
        self.assertNotIn('batch.create(', text)
        self.assertNotIn('batch.delete(', text)
        self.assertNotIn('.set(', text)
        self.assertNotRegex(text, r"await\s+.*\.update\(")
        self.assertIn('"perDocumentFallback": False', text)
        self.assertIn('PREFLIGHT READ wave', text)
        self.assertIn('PREFLIGHT PROGRESS', text)
        self.assertIn('WRITE wave', text)
        self.assertIn('WRITE PROGRESS', text)
        self.assertIn('VERIFY READ wave', text)
        self.assertIn('VERIFY PROGRESS', text)
        self.assertIn('flush=True', text)


if __name__ == "__main__":
    unittest.main()
