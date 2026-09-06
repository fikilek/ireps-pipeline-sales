from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "04_upload_conlog_monthly_v3.py"
)
SPEC = importlib.util.spec_from_file_location("stage04_refresh_offline", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
STAGE04 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STAGE04
SPEC.loader.exec_module(STAGE04)


class FakeSnapshot:
    def __init__(self, document_id: str, data: dict[str, object] | None, *, update_time="UT"):
        self.id = document_id
        self.exists = data is not None
        self._data = data
        self.update_time = update_time if self.exists else None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class FakeRef:
    def __init__(self, collection: str, document_id: str):
        self.collection_name = collection
        self.id = document_id


class FakeCollection:
    def __init__(self, db, name: str):
        self.db = db
        self.name = name

    def document(self, document_id: str):
        return FakeRef(self.name, document_id)


class FakeBatch:
    def __init__(self):
        self.operations: list[tuple] = []
        self.committed = False

    def create(self, ref, document):
        self.operations.append(("create", ref.collection_name, ref.id, document))

    def update(self, ref, updates, option=None):
        self.operations.append(("update", ref.collection_name, ref.id, updates, option))

    def commit(self):
        self.committed = True
        return []


class FakeDb:
    def __init__(self, snapshots: dict[str, FakeSnapshot] | None = None):
        self.snapshots = snapshots or {}
        self.get_all_calls: list[list[str]] = []
        self.batches: list[FakeBatch] = []

    def collection(self, name: str):
        return FakeCollection(self, name)

    def get_all(self, refs):
        refs = list(refs)
        self.get_all_calls.append([ref.id for ref in refs])
        return [
            self.snapshots.get(ref.id, FakeSnapshot(ref.id, None))
            for ref in refs
        ]

    def batch(self):
        batch = FakeBatch()
        self.batches.append(batch)
        return batch


def monthly_document(
    document_id: str,
    *,
    amount: int = 100,
    units: float = 1.0,
    group_id: str = "GR1",
    group_label: str = "<=99.99",
    source_end_row: int | None = 10,
) -> dict[str, object]:
    meter_no = document_id.split("__")[1]
    return {
        "sourceOrigin": "monthly_source",
        "provider": "contour",
        "lmPcode": "ZA5241",
        "meterNo": meter_no,
        "ym": "2026-06",
        "y": 2026,
        "m": 6,
        "amountTotalC": amount,
        "unitsTotal": units,
        "salesGroupId": group_id,
        "salesGroupLabel": group_label,
        "sourceDocumentId": meter_no,
        "sourceEndRow": source_end_row,
    }


def monthly_dataset(document_ids: list[str]) -> object:
    rows = []
    for document_id in document_ids:
        document = monthly_document(document_id)
        rows.append({"docId": document_id, **document})
    return STAGE04.MonthlyDataset(
        dataset="monthly",
        collection=STAGE04.COLL_MONTHLY,
        path=Path("monthly.csv"),
        frame=pd.DataFrame(rows, columns=STAGE04.MONTHLY_SOURCE_COLUMNS),
        file_sha256="0" * 64,
    )


class Stage04MonthlySourceRefreshTests(unittest.TestCase):
    def test_refresh_is_monthly_source_only(self):
        STAGE04.validate_mode_source_contract("refresh", "monthly_source")
        STAGE04.validate_mode_source_contract("create-only", "atomic")
        STAGE04.validate_mode_source_contract("resume", "atomic")
        with self.assertRaisesRegex(ValueError, "monthly_source"):
            STAGE04.validate_mode_source_contract("refresh", "atomic")

    def test_refresh_classifies_unchanged_mutable_update_and_immutable_conflict(self):
        dataset = monthly_dataset(["ZA5241__METER01__2026-06"])
        expected = monthly_document("ZA5241__METER01__2026-06")

        unchanged = FakeSnapshot("ZA5241__METER01__2026-06", expected, update_time="U1")
        classification, operation, differing = STAGE04.classify_monthly_source_refresh_snapshot(
            dataset, unchanged, expected
        )
        self.assertEqual("UNCHANGED", classification)
        self.assertIsNone(operation)
        self.assertEqual([], differing)

        changed = dict(expected)
        changed["amountTotalC"] = 50
        changed["unitsTotal"] = 0.5
        changed["salesGroupId"] = "GR0"
        changed["sourceEndRow"] = None
        changed_snapshot = FakeSnapshot(
            "ZA5241__METER01__2026-06", changed, update_time="U2"
        )
        classification, operation, differing = STAGE04.classify_monthly_source_refresh_snapshot(
            dataset, changed_snapshot, expected
        )
        self.assertEqual("UPDATED", classification)
        self.assertIsNotNone(operation)
        self.assertEqual("U2", operation.update_time)
        self.assertEqual(
            {"amountTotalC", "unitsTotal", "salesGroupId", "sourceEndRow"},
            set(operation.updates),
        )
        self.assertEqual(set(differing), set(operation.updates))

        conflict = dict(expected)
        conflict["provider"] = "other"
        conflict_snapshot = FakeSnapshot(
            "ZA5241__METER01__2026-06", conflict, update_time="U3"
        )
        classification, operation, differing = STAGE04.classify_monthly_source_refresh_snapshot(
            dataset, conflict_snapshot, expected
        )
        self.assertEqual("CONFLICT", classification)
        self.assertIsNone(operation)
        self.assertEqual(["provider"], differing)

    def test_refresh_rejects_shape_and_type_drift(self):
        dataset = monthly_dataset(["ZA5241__METER01__2026-06"])
        expected = monthly_document("ZA5241__METER01__2026-06")

        extra = {**expected, "unexpected": "value"}
        classification, _, differing = STAGE04.classify_monthly_source_refresh_snapshot(
            dataset,
            FakeSnapshot("ZA5241__METER01__2026-06", extra),
            expected,
        )
        self.assertEqual("CONFLICT", classification)
        self.assertEqual(["unexpected"], differing)

        wrong_type = dict(expected)
        wrong_type["amountTotalC"] = 100.0
        classification, _, differing = STAGE04.classify_monthly_source_refresh_snapshot(
            dataset,
            FakeSnapshot("ZA5241__METER01__2026-06", wrong_type),
            expected,
        )
        self.assertEqual("CONFLICT", classification)
        self.assertEqual(["amountTotalC"], differing)

    def test_refresh_preflight_bulk_reads_in_governed_400_waves(self):
        ids = [f"ZA5241__M{i:04d}__2026-06" for i in range(401)]
        dataset = monthly_dataset(ids)
        snapshots = {
            document_id: FakeSnapshot(document_id, monthly_document(document_id), update_time=f"U{i}")
            for i, document_id in enumerate(ids)
        }
        db = FakeDb(snapshots)
        with (
            mock.patch.object(STAGE04, "scope_query", return_value=object()),
            mock.patch.object(STAGE04, "query_count", return_value=len(ids)),
        ):
            state = STAGE04.inspect_refresh_state(
                db,
                dataset,
                lm_pcode="ZA5241",
                month="2026-06",
            )

        self.assertEqual(401, state.matching)
        self.assertEqual(0, state.missing)
        self.assertEqual(0, state.updated)
        self.assertEqual(0, state.conflicts)
        self.assertEqual(0, state.extra)
        self.assertEqual(2, state.read_waves)
        self.assertEqual([400, 1], [len(call) for call in db.get_all_calls])

    def test_refresh_writer_batches_create_and_preconditioned_update_without_set_or_delete(self):
        document_ids = [
            "ZA5241__METER01__2026-06",
            "ZA5241__METER02__2026-06",
        ]
        dataset = monthly_dataset(document_ids)
        operation = STAGE04.PlannedUpdate(
            document_id=document_ids[1],
            updates={"amountTotalC": 200},
            update_time="UPDATE-TIME",
            differing_fields=("amountTotalC",),
        )
        state = STAGE04.ExistingState(
            count=1,
            matching=0,
            missing=1,
            conflicts=0,
            extra=0,
            missing_ids=[document_ids[0]],
            conflict_examples=[],
            extra_examples=[],
            updated=1,
            update_operations=[operation],
        )
        db = FakeDb()
        with mock.patch.object(
            STAGE04,
            "last_update_option",
            side_effect=lambda update_time: ("LAST_UPDATE", update_time),
        ):
            result = STAGE04.write_refresh_documents(db, dataset, state)

        self.assertEqual(1, result["created"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(1, result["committedBatches"])
        self.assertEqual(2, result["writeOperationsSucceeded"])
        self.assertEqual(2, result["maximumWriteOperationsInAnyBatch"])
        self.assertEqual(1, len(db.batches))
        operations = db.batches[0].operations
        self.assertEqual("create", operations[0][0])
        self.assertEqual("update", operations[1][0])
        self.assertEqual(("LAST_UPDATE", "UPDATE-TIME"), operations[1][-1])

        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("batch.set(", source)
        self.assertNotIn("batch.delete(", source)
        self.assertIn("batch.update(", source)
        self.assertIn("LastUpdateOption", source)

    def test_refresh_full_verification_bulk_reads_every_expected_document(self):
        ids = [f"ZA5241__M{i:04d}__2026-06" for i in range(401)]
        dataset = monthly_dataset(ids)
        db = FakeDb(
            {
                document_id: FakeSnapshot(document_id, monthly_document(document_id))
                for document_id in ids
            }
        )
        with (
            mock.patch.object(STAGE04, "scope_query", return_value=object()),
            mock.patch.object(STAGE04, "query_count", return_value=len(ids)),
        ):
            verification = STAGE04.verify_refresh_post_upload(
                db,
                dataset,
                lm_pcode="ZA5241",
                month="2026-06",
            )

        self.assertEqual("PASS", verification["countVerification"])
        self.assertEqual("PASS", verification["fullDocumentVerification"])
        self.assertEqual(401, verification["documentsVerified"])
        self.assertEqual(2, verification["verificationReadWaves"])
        self.assertEqual([400, 1], [len(call) for call in db.get_all_calls])


if __name__ == "__main__":
    unittest.main()
