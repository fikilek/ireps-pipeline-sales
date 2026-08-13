from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sales_pipeline_visibility_reconciliation_dev.py"
SPEC = importlib.util.spec_from_file_location("visibility_batch_offline", SCRIPT)
visibility = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = visibility
assert SPEC.loader is not None
SPEC.loader.exec_module(visibility)


class Ref:
    def __init__(self, collection: str, doc_id: str):
        self.id = doc_id
        self.path = f"{collection}/{doc_id}"


class Collection:
    def __init__(self, name: str):
        self.name = name

    def document(self, doc_id: str):
        return Ref(self.name, doc_id)


class Snapshot:
    def __init__(self, reference: Ref, payload):
        self.reference = reference
        self.exists = payload is not None
        self._payload = copy.deepcopy(payload)

    def to_dict(self):
        return copy.deepcopy(self._payload)


class DB:
    def __init__(self, storage=None, *, retry_mutator=None):
        self.storage = copy.deepcopy(storage or {})
        self.get_all_sizes = []
        self.transaction_max_attempts = []
        self.transactions = []
        self.retry_mutator = retry_mutator
        self.closed = False

    def collection(self, name: str):
        return Collection(name)

    def _snapshots(self, refs):
        return [Snapshot(ref, self.storage.get(ref.path)) for ref in refs]

    def get_all(self, refs):
        refs = list(refs)
        self.get_all_sizes.append(len(refs))
        return self._snapshots(refs)

    def transaction(self, max_attempts=None):
        self.transaction_max_attempts.append(max_attempts)
        transaction = FakeTransaction(self)
        self.transactions.append(transaction)
        return transaction

    def close(self):
        self.closed = True


class FakeTransaction:
    def __init__(self, db: DB):
        self.db = db
        self.callback_invocations = 0
        self.attempt_events = []
        self._active_events = None
        self._pending_updates = []

    def get_all(self, refs):
        refs = list(refs)
        self._active_events.append(("read", len(refs)))
        return self.db._snapshots(refs)

    def update(self, ref, updates, option=None):
        self._active_events.append(("write", ref.path, copy.deepcopy(updates), option))
        self._pending_updates.append((ref, copy.deepcopy(updates)))

    def _attempt(self, callback):
        self.callback_invocations += 1
        self._active_events = []
        self._pending_updates = []
        result = callback(self)
        self.attempt_events.append(list(self._active_events))
        pending = list(self._pending_updates)
        return result, pending

    @staticmethod
    def _apply_dotted_update(payload, dotted_path, value):
        parts = dotted_path.split(".")
        target = payload
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    def _commit(self, pending):
        for ref, updates in pending:
            payload = self.db.storage[ref.path]
            for dotted_path, value in updates.items():
                self._apply_dotted_update(payload, dotted_path, value)

    def execute(self, callback):
        result, pending = self._attempt(callback)
        if self.db.retry_mutator is not None:
            self.db.retry_mutator(self.db.storage)
            result, pending = self._attempt(callback)
        self._commit(pending)
        return result


class FakeCredentials:
    @staticmethod
    def from_service_account_file(_path):
        return object()


def make_google_modules(db: DB):
    google = types.ModuleType("google")
    google_cloud = types.ModuleType("google.cloud")
    firestore = types.ModuleType("google.cloud.firestore")
    api_core = types.ModuleType("google.api_core")
    exceptions = types.ModuleType("google.api_core.exceptions")
    oauth2 = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")

    firestore.Client = lambda project, credentials: db

    def transactional(callback):
        def wrapper(transaction):
            return transaction.execute(callback)

        return wrapper

    firestore.transactional = transactional
    service_account.Credentials = FakeCredentials
    api_core.exceptions = exceptions
    oauth2.service_account = service_account
    google_cloud.firestore = firestore
    google.cloud = google_cloud

    return {
        "google": google,
        "google.cloud": google_cloud,
        "google.cloud.firestore": firestore,
        "google.api_core": api_core,
        "google.api_core.exceptions": exceptions,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": service_account,
    }


def make_master(meter_id: str, *, ast_id: str | None = None):
    return {
        "meterNo": {"normalized": meter_id},
        "lmPcode": "ZA5241",
        "refs": {
            "sales": {"id": meter_id, "provider": "contour"},
            "asts": {"id": ast_id if ast_id is not None else f"AST{meter_id}"},
        },
        "operationalKeep": {"source": "meter-master"},
    }


def make_sales(meter_id: str, *, visibility_value: str = "INVISIBLE"):
    return {
        "master": {"id": meter_id, "visibility": visibility_value, "other": "preserve"},
        "meterNoNormalized": meter_id,
        "lmPcode": "ZA5241",
        "provider": "contour",
        "tbRefs": {"batch": f"TB-{meter_id}"},
        "geofenceRefs": [f"GF-{meter_id}"],
        "unknownOperationalRoot": {"preserve": True, "meter": meter_id},
    }


def build_storage(ids, *, visibility_value="INVISIBLE"):
    storage = {}
    for meter_id in ids:
        storage[f"meter_master/{meter_id}"] = make_master(meter_id)
        storage[f"sales-all-meters/{meter_id}"] = make_sales(
            meter_id, visibility_value=visibility_value
        )
    return storage


class VisibilityBatchTests(unittest.TestCase):
    def test_10216_logical_meters_partition_into_52_transaction_waves(self):
        ids = [f"{index:010d}" for index in range(10216)]
        waves = list(visibility.chunks(ids, visibility.LOGICAL_METERS_PER_TRANSACTION))
        self.assertEqual([len(wave) for wave in waves], [200] * 51 + [16])
        self.assertEqual(len(waves), 52)

    def test_200_logical_meters_produce_exactly_400_read_refs(self):
        db = DB()
        ids = [f"{index:010d}" for index in range(200)]
        refs = visibility.pair_refs(db, ids)
        self.assertEqual(len(refs), 400)
        self.assertEqual(sum(ref.path.startswith("meter_master/") for ref in refs), 200)
        self.assertEqual(sum(ref.path.startswith("sales-all-meters/") for ref in refs), 200)

    def test_201_logical_meters_are_rejected_as_more_than_400_refs(self):
        db = DB()
        ids = [f"{index:010d}" for index in range(201)]
        with self.assertRaisesRegex(ValueError, "400-reference"):
            visibility.pair_refs(db, ids)

    def test_preflight_bulk_pair_read_uses_one_get_all_for_200_meters(self):
        db = DB()
        ids = [f"{index:010d}" for index in range(200)]
        refs, snapshots = visibility.bulk_pair_read(db, ids)
        self.assertEqual(len(refs), 400)
        self.assertEqual(db.get_all_sizes, [400])
        self.assertEqual(len(snapshots), 400)
        self.assertTrue(all(not snap.exists for snap in snapshots.values()))

    def test_source_uses_one_transaction_per_wave_not_per_meter(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("for meter_wave in chunks(ids, LOGICAL_METERS_PER_TRANSACTION)", text)
        self.assertIn("db.transaction(max_attempts=TRANSACTION_MAX_ATTEMPTS)", text)
        self.assertIn("transaction.get_all(refs)", text)
        self.assertIn("for sales_ref, expected in pending_updates", text)
        self.assertNotIn("for meter_id in ids:\n            transaction = db.transaction", text)

    def test_preflight_outcomes_do_not_increment_write_counters(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("publish_outcomes(outcomes, committed_write_count=0)", text)
        self.assertIn("publish_outcomes(outcomes, committed_write_count=write_count)", text)


class VisibilityTransactionRuntimeTests(unittest.TestCase):
    def _run(self, ids, db: DB, *, preflight_only: bool):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service_account = root / "ireps2.json"
            service_account.write_text(json.dumps({"project_id": "ireps2"}), encoding="utf-8")
            report_dir = root / "reports"
            args = argparse.Namespace(
                project_id="ireps2",
                confirm_project="ireps2",
                service_account=service_account,
                input=root / "stage05.csv",
                manifest=root / "stage05.manifest.json",
                sales_input=root / "stage06.csv",
                sales_manifest=root / "stage06.manifest.json",
                report_dir=report_dir,
                preflight_only=preflight_only,
            )
            evidence = {
                "provider": "contour",
                "lmPcode": "ZA5241",
                "documentIdsSha256": "scope-hash",
            }
            modules = make_google_modules(db)
            with patch.object(visibility, "parse_args", return_value=args), patch.object(
                visibility, "load_scope", return_value=(list(ids), copy.deepcopy(evidence))
            ), patch.object(
                visibility,
                "bind_stage06_scope",
                return_value={"rows": len(ids), "stage06BuildFingerprint": "fp"},
            ), patch.dict(sys.modules, modules, clear=False):
                visibility.run()

            reports = list(report_dir.glob("*.json"))
            self.assertEqual(len(reports), 1)
            return json.loads(reports[0].read_text(encoding="utf-8"))

    def test_full_200_meter_transaction_wave_reads_400_before_at_most_200_visibility_writes(self):
        ids = [f"M{index:06d}" for index in range(200)]
        db = DB(build_storage(ids))
        before_sales = {
            meter_id: copy.deepcopy(db.storage[f"sales-all-meters/{meter_id}"])
            for meter_id in ids
        }

        report = self._run(ids, db, preflight_only=False)

        self.assertEqual(len(db.transactions), 1)
        tx = db.transactions[0]
        self.assertEqual(tx.callback_invocations, 1)
        self.assertEqual(db.transaction_max_attempts, [visibility.TRANSACTION_MAX_ATTEMPTS])
        self.assertEqual(len(tx.attempt_events), 1)
        events = tx.attempt_events[0]
        self.assertEqual(events[0], ("read", 400))
        self.assertEqual(sum(event[0] == "read" for event in events), 1)
        writes = [event for event in events if event[0] == "write"]
        self.assertEqual(len(writes), 200)
        first_write = next(i for i, event in enumerate(events) if event[0] == "write")
        self.assertTrue(all(event[0] == "read" for event in events[:first_write]))
        for _, path, update, option in writes:
            self.assertTrue(path.startswith("sales-all-meters/"))
            self.assertEqual(update, {"master.visibility": "VISIBLE"})
            self.assertIsNone(option)

        # Non-transactional post-write verification is one governed 400-ref read.
        self.assertEqual(db.get_all_sizes, [400])
        self.assertEqual(report["updatedCount"], 200)
        self.assertEqual(report["writeAttemptCount"], 200)
        self.assertEqual(report["writeSuccessCount"], 200)
        self.assertEqual(report["firestoreWrites"], 200)
        self.assertEqual(report["verification"]["documentsVerified"], 200)
        self.assertEqual(report["verification"]["nonVisibilityPreservationVerified"], 200)
        self.assertEqual(report["batchEvidence"]["maximumReadsInAnyWave"], 400)
        self.assertEqual(report["batchEvidence"]["maximumWritesInAnyTransaction"], 200)
        self.assertEqual(report["batchEvidence"]["transactionWavesAttempted"], 1)
        self.assertEqual(report["batchEvidence"]["transactionWavesCommitted"], 1)

        for meter_id in ids:
            after = db.storage[f"sales-all-meters/{meter_id}"]
            before = before_sales[meter_id]
            self.assertEqual(after["master"]["visibility"], "VISIBLE")
            after_without = copy.deepcopy(after)
            before_without = copy.deepcopy(before)
            after_without["master"].pop("visibility")
            before_without["master"].pop("visibility")
            self.assertEqual(after_without, before_without)
        self.assertTrue(db.closed)

    def test_transaction_retry_reclassifies_changed_meter_master_and_publishes_counters_once(self):
        ids = ["M000001"]
        storage = build_storage(ids)

        def mutate_between_attempts(state):
            # First attempt derives VISIBLE and plans a write. The retry sees
            # changed Meter Master truth and must derive INVISIBLE instead.
            state["meter_master/M000001"]["refs"]["asts"]["id"] = ""

        db = DB(storage, retry_mutator=mutate_between_attempts)
        report = self._run(ids, db, preflight_only=False)

        tx = db.transactions[0]
        self.assertEqual(tx.callback_invocations, 2)
        self.assertEqual(len([e for e in tx.attempt_events[0] if e[0] == "write"]), 1)
        self.assertEqual(len([e for e in tx.attempt_events[1] if e[0] == "write"]), 0)
        self.assertEqual(report["recordsInspected"], 1)
        self.assertEqual(report["updatedCount"], 0)
        self.assertEqual(report["unchangedCount"], 1)
        self.assertEqual(report["conflictCount"], 0)
        self.assertEqual(report["writeAttemptCount"], 0)
        self.assertEqual(report["writeSuccessCount"], 0)
        self.assertEqual(db.storage["sales-all-meters/M000001"]["master"]["visibility"], "INVISIBLE")
        self.assertEqual(report["verification"]["nonVisibilityPreservationVerified"], 1)

    def test_transaction_retry_reclassifies_changed_sales_all_as_conflict_without_stale_write(self):
        ids = ["M000001"]
        storage = build_storage(ids)

        def mutate_between_attempts(state):
            # A concurrent Sales All identity/provider change participates in
            # the retry's transactional read set and must block reconciliation.
            state["sales-all-meters/M000001"]["provider"] = "other-provider"

        db = DB(storage, retry_mutator=mutate_between_attempts)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service_account = root / "ireps2.json"
            service_account.write_text(json.dumps({"project_id": "ireps2"}), encoding="utf-8")
            report_dir = root / "reports"
            args = argparse.Namespace(
                project_id="ireps2",
                confirm_project="ireps2",
                service_account=service_account,
                input=root / "stage05.csv",
                manifest=root / "stage05.manifest.json",
                sales_input=root / "stage06.csv",
                sales_manifest=root / "stage06.manifest.json",
                report_dir=report_dir,
                preflight_only=False,
            )
            evidence = {"provider": "contour", "lmPcode": "ZA5241", "documentIdsSha256": "scope"}
            with patch.object(visibility, "parse_args", return_value=args), patch.object(
                visibility, "load_scope", return_value=(ids, evidence)
            ), patch.object(
                visibility, "bind_stage06_scope", return_value={"rows": 1}
            ), patch.dict(sys.modules, make_google_modules(db), clear=False):
                with self.assertRaisesRegex(RuntimeError, "blocked/incomplete"):
                    visibility.run()

            reports = list(report_dir.glob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))

        tx = db.transactions[0]
        self.assertEqual(tx.callback_invocations, 2)
        self.assertEqual(len([e for e in tx.attempt_events[0] if e[0] == "write"]), 1)
        self.assertEqual(len([e for e in tx.attempt_events[1] if e[0] == "write"]), 0)
        self.assertEqual(report["recordsInspected"], 1)
        self.assertEqual(report["conflictCount"], 1)
        self.assertEqual(report["updatedCount"], 0)
        self.assertEqual(report["firestoreWrites"], 0)
        self.assertIn("provider mismatch", report["conflicts"][0]["reason"])
        self.assertEqual(db.storage["sales-all-meters/M000001"]["master"]["visibility"], "INVISIBLE")

    def test_preflight_uses_bulk_pair_read_and_performs_zero_transactions_or_writes(self):
        ids = [f"M{index:06d}" for index in range(200)]
        db = DB(build_storage(ids))
        before = copy.deepcopy(db.storage)

        report = self._run(ids, db, preflight_only=True)

        self.assertEqual(db.get_all_sizes, [400])
        self.assertEqual(db.transactions, [])
        self.assertEqual(db.storage, before)
        self.assertEqual(report["recordsInspected"], 200)
        self.assertEqual(report["updatedCount"], 200)
        self.assertEqual(report["writeAttemptCount"], 0)
        self.assertEqual(report["writeSuccessCount"], 0)
        self.assertEqual(report["firestoreWrites"], 0)
        self.assertEqual(report["verification"]["status"], "NOT_RUN")
        self.assertEqual(report["batchEvidence"]["readWaves"], 1)
        self.assertEqual(report["batchEvidence"]["transactionWavesAttempted"], 0)
        self.assertEqual(report["batchEvidence"]["transactionWavesCommitted"], 0)


if __name__ == "__main__":
    unittest.main()
