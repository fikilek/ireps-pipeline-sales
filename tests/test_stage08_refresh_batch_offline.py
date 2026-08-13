from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path



# The authoritative 400-batch source gate intentionally contains only patch targets.
# Stub non-target Stage 06 helper dependencies so these pure refresh-I/O tests can run
# without copying or modifying those already-reviewed source files.
address_stub = types.ModuleType("sales_address_enrichment")
address_stub.ADDRESS_MAP_FIELDS = ("strNo", "strName", "strType")
address_stub.ADDRESS_STAGING_COLUMNS = ("strNo", "strName", "strType")
address_stub.address_map_from_row = lambda row: {key: row.get(key, "") for key in address_stub.ADDRESS_MAP_FIELDS}
address_stub.validate_address_values = lambda *args, **kwargs: None
_old_address_module = sys.modules.get("sales_address_enrichment")
sys.modules["sales_address_enrichment"] = address_stub

monthly_stub = types.ModuleType("sales_pipeline_monthly_source_support")
monthly_stub.COMMERCIAL_JSON_FIELDS = ()
monthly_stub.COMMERCIAL_SCALAR_FIELDS = ()
monthly_stub.canonical_sha256 = lambda payload: "stub-sha"
monthly_stub.normalize_meter = lambda value: str(value or "").replace(" ", "").upper()
monthly_stub.safe_str = lambda value: "" if value is None else str(value).strip()
_old_monthly_module = sys.modules.get("sales_pipeline_monthly_source_support")
sys.modules["sales_pipeline_monthly_source_support"] = monthly_stub

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sales_pipeline_sales_all_refresh.py"
SPEC = importlib.util.spec_from_file_location("stage08_refresh_batch", SCRIPT)
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
assert SPEC.loader is not None
try:
    SPEC.loader.exec_module(refresh)
finally:
    if _old_address_module is None:
        sys.modules.pop("sales_address_enrichment", None)
    else:
        sys.modules["sales_address_enrichment"] = _old_address_module
    if _old_monthly_module is None:
        sys.modules.pop("sales_pipeline_monthly_source_support", None)
    else:
        sys.modules["sales_pipeline_monthly_source_support"] = _old_monthly_module


class Ref:
    def __init__(self, db, doc_id):
        self.db = db
        self.id = doc_id
        self.path = f"sales-all-meters/{doc_id}"


class Snapshot:
    def __init__(self, ref, payload, update_time):
        self.reference = ref
        self.id = ref.id
        self.exists = payload is not None
        self.payload = json.loads(json.dumps(payload)) if payload is not None else None
        self.update_time = update_time

    def to_dict(self):
        return json.loads(json.dumps(self.payload)) if self.payload is not None else None


class Collection:
    def __init__(self, db):
        self.db = db

    def document(self, doc_id):
        return Ref(self.db, doc_id)


class Batch:
    def __init__(self, db):
        self.db = db
        self.operations = []

    def create(self, ref, payload):
        self.operations.append(("create", ref, dict(payload), None))

    def update(self, ref, updates, option=None):
        self.operations.append(("update", ref, dict(updates), option))

    def commit(self):
        self.db.batch_sizes.append(len(self.operations))
        self.db.commit_calls += 1
        if self.db.commit_errors:
            raise self.db.commit_errors.pop(0)
        return [object() for _ in self.operations]


class DB:
    def __init__(self, docs=None, commit_errors=None):
        self.docs = dict(docs or {})
        self.versions = {key: 1 for key in self.docs}
        self.get_all_sizes = []
        self.batch_sizes = []
        self.commit_calls = 0
        self.commit_errors = list(commit_errors or [])

    def collection(self, unused):
        return Collection(self)

    def get_all(self, refs):
        refs = list(refs)
        self.get_all_sizes.append(len(refs))
        return [Snapshot(ref, self.docs.get(ref.id), self.versions.get(ref.id)) for ref in refs]

    def batch(self):
        return Batch(self)


class Stage08RefreshBatchTests(unittest.TestCase):
    def test_10216_partition_is_25x400_plus_216(self):
        waves = list(refresh._chunks(list(range(10216))))
        self.assertEqual([len(wave) for wave in waves], [400] * 25 + [216])

    def test_bulk_read_rejects_401_refs(self):
        db = DB()
        collection = db.collection(refresh.COLLECTION)
        refs = [collection.document(str(index)) for index in range(401)]
        with self.assertRaisesRegex(ValueError, "400-reference"):
            refresh._bulk_snapshots(db, refs)
        self.assertEqual(db.get_all_sizes, [])

    def test_update_batch_receives_last_update_precondition(self):
        db = DB({"ABC123": {"masterId": "ABC123"}})
        collection = db.collection(refresh.COLLECTION)
        ref = collection.document("ABC123")
        snapshot = list(db.get_all([ref]))[0]
        batch = refresh._build_write_batch(
            db=db,
            collection=collection,
            operations=[{
                "masterId": "ABC123",
                "classification": "UPDATED",
                "updates": {"adr": {"strNo": "42", "strName": "Mckenzie", "strType": "Street"}},
                "snapshot": snapshot,
            }],
            last_update_option_cls=lambda value: ("lastUpdate", value),
        )
        self.assertEqual(batch.operations[0][3], ("lastUpdate", 1))

    def test_missing_document_uses_create(self):
        db = DB()
        collection = db.collection(refresh.COLLECTION)
        batch = refresh._build_write_batch(
            db=db,
            collection=collection,
            operations=[{
                "masterId": "ABC123",
                "classification": "CREATED",
                "expected": {"masterId": "ABC123"},
                "snapshot": None,
            }],
            last_update_option_cls=lambda value: value,
        )
        self.assertEqual(batch.operations[0][0], "create")

    def test_one_bounded_concurrency_retry_and_no_per_doc_fallback(self):
        class ConcurrencyError(Exception):
            pass

        db = DB(commit_errors=[ConcurrencyError("first batch stale")])
        collection = db.collection(refresh.COLLECTION)
        item = {"masterId": "ABC123", "expected": {"masterId": "ABC123"}}
        ref = collection.document("ABC123")
        snapshots = refresh._bulk_snapshots(db, [ref])
        classified = [refresh._classify_snapshot(item, refresh._snapshot_for(snapshots, ref))]
        stats = refresh.RefreshStats(rows=1)
        refresh._write_operations_for_wave(
            db=db,
            collection=collection,
            wave_items=[item],
            classified=classified,
            stats=stats,
            preserved_before={},
            last_update_option_cls=lambda value: value,
            concurrency_exceptions=(ConcurrencyError,),
        )
        self.assertEqual(db.batch_sizes, [1, 1])
        self.assertEqual((stats.write_waves_attempted, stats.write_waves_committed), (2, 1))
        self.assertEqual((stats.writes_attempted, stats.writes_succeeded), (2, 1))
        self.assertEqual(stats.created, 1)
        self.assertEqual(db.get_all_sizes, [1, 1])

    def test_second_concurrency_failure_stops(self):
        class ConcurrencyError(Exception):
            pass

        db = DB(commit_errors=[ConcurrencyError("first"), ConcurrencyError("second")])
        collection = db.collection(refresh.COLLECTION)
        item = {"masterId": "ABC123", "expected": {"masterId": "ABC123"}}
        ref = collection.document("ABC123")
        snapshots = refresh._bulk_snapshots(db, [ref])
        classified = [refresh._classify_snapshot(item, refresh._snapshot_for(snapshots, ref))]
        with self.assertRaisesRegex(RuntimeError, "no per-document fallback"):
            refresh._write_operations_for_wave(
                db=db,
                collection=collection,
                wave_items=[item],
                classified=classified,
                stats=refresh.RefreshStats(rows=1),
                preserved_before={},
                last_update_option_cls=lambda value: value,
                concurrency_exceptions=(ConcurrencyError,),
            )
        self.assertEqual(db.commit_calls, 2)

    def test_source_has_no_transaction_or_individual_get_fallback(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("db.transaction(", text)
        self.assertNotIn("firestore.transactional", text)
        self.assertNotRegex(text, r"\.document\([^\n]+\)\.get\(")
        self.assertIn('"perDocumentFallback": False', text)


if __name__ == "__main__":
    unittest.main()
