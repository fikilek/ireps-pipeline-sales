from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


address_stub = types.ModuleType("sales_address_enrichment")
address_stub.ADDRESS_MAP_FIELDS = ("strNo", "strName", "strType")
address_stub.ADDRESS_STAGING_COLUMNS = ("strNo", "strName", "strType")
address_stub.address_map_from_row = lambda row: {
    key: row.get(key, "") for key in address_stub.ADDRESS_MAP_FIELDS
}
address_stub.validate_address_values = lambda *args, **kwargs: None
_old_address = sys.modules.get("sales_address_enrichment")
sys.modules["sales_address_enrichment"] = address_stub

monthly_stub = types.ModuleType("sales_pipeline_monthly_source_support")
monthly_stub.COMMERCIAL_JSON_FIELDS = ()
monthly_stub.COMMERCIAL_SCALAR_FIELDS = ()
monthly_stub.canonical_sha256 = lambda payload: "stub-sha"
monthly_stub.normalize_meter = lambda value: str(value or "").replace(" ", "").upper()
monthly_stub.safe_str = lambda value: "" if value is None else str(value).strip()
_old_monthly = sys.modules.get("sales_pipeline_monthly_source_support")
sys.modules["sales_pipeline_monthly_source_support"] = monthly_stub

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "sales_pipeline_sales_all_refresh.py"
SPEC = importlib.util.spec_from_file_location("stage08_global_preflight", SCRIPT)
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
assert SPEC.loader is not None
try:
    SPEC.loader.exec_module(refresh)
finally:
    if _old_address is None:
        sys.modules.pop("sales_address_enrichment", None)
    else:
        sys.modules["sales_address_enrichment"] = _old_address
    if _old_monthly is None:
        sys.modules.pop("sales_pipeline_monthly_source_support", None)
    else:
        sys.modules["sales_pipeline_monthly_source_support"] = _old_monthly


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
        self.db.commit_calls += 1
        self.db.events.append(f"commit:{len(self.operations)}")
        self.db.batch_sizes.append(len(self.operations))
        failure = self.db.commit_failures.get(self.db.commit_calls)
        if failure is not None:
            raise failure
        return [object() for _ in self.operations]


class DB:
    def __init__(self, docs=None, commit_failures=None):
        self.docs = dict(docs or {})
        self.versions = {key: 1 for key in self.docs}
        self.commit_failures = dict(commit_failures or {})
        self.get_all_sizes = []
        self.batch_sizes = []
        self.commit_calls = 0
        self.events = []

    def collection(self, unused):
        return Collection(self)

    def get_all(self, refs):
        refs = list(refs)
        self.events.append(f"read:{len(refs)}")
        self.get_all_sizes.append(len(refs))
        return [
            Snapshot(ref, self.docs.get(ref.id), self.versions.get(ref.id))
            for ref in refs
        ]

    def batch(self):
        return Batch(self)

    def close(self):
        self.events.append("close")


def expected(doc_id):
    return {"master": {"id": doc_id, "visibility": "INVISIBLE"}}


def item(doc_id):
    return {"masterId": doc_id, "expected": expected(doc_id)}


def classify_and_gate(rows, db):
    stats = refresh.RefreshStats(rows=len(rows))
    preserved_before = {}
    plan = refresh.classify_all(
        db=db,
        collection=db.collection(refresh.COLLECTION),
        rows=rows,
        stats=stats,
        preserved_before=preserved_before,
    )
    refresh.evaluate_global_gate(rows=rows, plan=plan, stats=stats)
    return stats, plan, preserved_before


def google_module_stubs(db):
    google = types.ModuleType("google")
    google.__path__ = []
    cloud = types.ModuleType("google.cloud")
    cloud.__path__ = []
    firestore = types.ModuleType("google.cloud.firestore")
    firestore.Client = lambda project, credentials: db
    firestore_v1 = types.ModuleType("google.cloud.firestore_v1")
    firestore_v1.LastUpdateOption = lambda value: ("lastUpdate", value)

    api_core = types.ModuleType("google.api_core")
    api_core.__path__ = []
    exceptions = types.ModuleType("google.api_core.exceptions")

    class AlreadyExists(Exception):
        pass

    class Aborted(Exception):
        pass

    class FailedPrecondition(Exception):
        pass

    class Unauthenticated(Exception):
        pass

    class PermissionDenied(Exception):
        pass

    class ServiceUnavailable(Exception):
        pass

    class DeadlineExceeded(Exception):
        pass

    exceptions.AlreadyExists = AlreadyExists
    exceptions.Aborted = Aborted
    exceptions.FailedPrecondition = FailedPrecondition
    exceptions.Unauthenticated = Unauthenticated
    exceptions.PermissionDenied = PermissionDenied
    exceptions.ServiceUnavailable = ServiceUnavailable
    exceptions.DeadlineExceeded = DeadlineExceeded
    api_core.exceptions = exceptions

    oauth2 = types.ModuleType("google.oauth2")
    oauth2.__path__ = []
    service_account = types.ModuleType("google.oauth2.service_account")

    class Credentials:
        @staticmethod
        def from_service_account_file(path):
            return object()

    service_account.Credentials = Credentials
    oauth2.service_account = service_account
    cloud.firestore = firestore

    return {
        "google": google,
        "google.cloud": cloud,
        "google.cloud.firestore": firestore,
        "google.cloud.firestore_v1": firestore_v1,
        "google.api_core": api_core,
        "google.api_core.exceptions": exceptions,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": service_account,
    }, exceptions


class Stage08GlobalPreflightTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate global admission/batching. The new authoritative
        # metadata loader and typed recovery encoding have their own integration
        # tests; admit the synthetic, intentionally minimal historical fixtures.
        for patcher in (
            mock.patch("sales_monthly_categories.load_metadata_contract", return_value={}),
            mock.patch("sales_monthly_categories.encode_before_image",
                side_effect=lambda item, snapshot: {"masterId": item["masterId"], "exists": bool(snapshot and snapshot.exists)}),
            mock.patch("sales_monthly_categories.encode_write_plan",
                side_effect=lambda decision, item: {"masterId": decision["masterId"], "classification": decision["classification"]}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
    def test_conflict_at_row_9800_of_10000_blocks_before_any_write(self):
        rows = [item(f"M{index:05d}") for index in range(1, 10001)]
        db = DB(
            {
                "M09800": {
                    "master": {"id": "M09800", "visibility": "BROKEN"}
                }
            }
        )
        stats = refresh.RefreshStats(rows=len(rows))
        preserved_before = {}

        plan = refresh.classify_all(
            db=db,
            collection=db.collection(refresh.COLLECTION),
            rows=rows,
            stats=stats,
            preserved_before=preserved_before,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"writeAttemptCount=0; firestoreWrites=0",
        ):
            refresh.evaluate_global_gate(rows=rows, plan=plan, stats=stats)

        self.assertTrue(stats.global_preflight_complete)
        self.assertFalse(stats.global_preflight_gate_passed)
        self.assertEqual(stats.inspected, 10000)
        self.assertEqual(stats.conflicts, 1)
        self.assertEqual(stats.writes_attempted, 0)
        self.assertEqual(stats.writes_succeeded, 0)
        self.assertEqual(stats.write_waves_attempted, 0)
        self.assertEqual(stats.write_waves_committed, 0)
        self.assertEqual(db.commit_calls, 0)
        self.assertEqual(db.get_all_sizes, [400] * 25)
        self.assertEqual(refresh._failure_result(stats), "REFRESH_ABORTED_NO_WRITE")

    def test_clean_1000_reads_all_waves_before_first_write(self):
        rows = [item(f"N{index:05d}") for index in range(1, 1001)]
        db = DB()

        stats, plan, _ = classify_and_gate(rows, db)

        self.assertTrue(stats.global_preflight_complete)
        self.assertTrue(stats.global_preflight_gate_passed)
        self.assertEqual(db.events, ["read:400", "read:400", "read:200"])
        self.assertEqual(stats.created, 1000)
        self.assertEqual(stats.inspected, 1000)

        refresh.execute_global_plan(
            db=db,
            collection=db.collection(refresh.COLLECTION),
            rows=rows,
            plan=plan,
            stats=stats,
            last_update_option_cls=lambda value: ("lastUpdate", value),
            concurrency_exceptions=(RuntimeError,),
        )

        self.assertEqual(
            db.events,
            [
                "read:400",
                "read:400",
                "read:200",
                "commit:400",
                "commit:400",
                "commit:200",
            ],
        )
        self.assertEqual(stats.writes_attempted, 1000)
        self.assertEqual(stats.writes_succeeded, 1000)
        self.assertEqual(stats.write_waves_committed, 3)

    def test_preflight_only_path_has_complete_accounting_and_zero_writes(self):
        rows = [item(f"P{index:05d}") for index in range(1, 801)]
        db = DB()

        stats, plan, _ = classify_and_gate(rows, db)

        self.assertEqual(len(plan), 2)
        self.assertEqual(stats.inspected, 800)
        self.assertEqual(stats.created, 800)
        self.assertEqual(stats.writes_attempted, 0)
        self.assertEqual(stats.writes_succeeded, 0)
        self.assertEqual(db.commit_calls, 0)
        self.assertEqual(refresh._batch_evidence(stats)["globalPreflightGatePassed"], True)

    def test_accounting_imbalance_fails_in_global_gate_before_writes(self):
        rows = [item(f"A{index:05d}") for index in range(1, 801)]
        db = DB()
        stats = refresh.RefreshStats(rows=len(rows))
        plan = refresh.classify_all(
            db=db,
            collection=db.collection(refresh.COLLECTION),
            rows=rows,
            stats=stats,
            preserved_before={},
        )
        stats.inspected -= 1

        with self.assertRaisesRegex(RuntimeError, "accounting imbalance"):
            refresh.evaluate_global_gate(rows=rows, plan=plan, stats=stats)

        self.assertEqual(stats.writes_attempted, 0)
        self.assertEqual(stats.writes_succeeded, 0)
        self.assertEqual(db.commit_calls, 0)

    def test_plan_for_25000_rows_retains_no_snapshots_or_duplicate_expected_docs(self):
        rows = [item(f"Z{index:05d}") for index in range(1, 25001)]
        db = DB()
        stats = refresh.RefreshStats(rows=len(rows))

        plan = refresh.classify_all(
            db=db,
            collection=db.collection(refresh.COLLECTION),
            rows=rows,
            stats=stats,
            preserved_before={},
        )

        self.assertEqual(sum(len(wave["decisions"]) for wave in plan), 25000)
        self.assertEqual(len(plan), 63)
        for wave in plan:
            self.assertEqual(set(wave), {"waveNumber", "decisions"})
            for decision in wave["decisions"]:
                self.assertNotIn("snapshot", decision)
                self.assertNotIn("expected", decision)
        self.assertEqual(stats.writes_attempted, 0)
        self.assertEqual(db.commit_calls, 0)

    def test_concurrency_failure_on_wave_5_stops_with_exact_partial_evidence(self):
        class ConcurrencyError(Exception):
            pass

        rows = [item(f"Q{index:05d}") for index in range(1, 4001)]
        db = DB(commit_failures={5: ConcurrencyError("stale")})
        stats, plan, _ = classify_and_gate(rows, db)

        with self.assertRaisesRegex(
            RuntimeError,
            r"failedWriteWave=5; committedWriteWaves=\[1, 2, 3, 4\]",
        ):
            refresh.execute_global_plan(
                db=db,
                collection=db.collection(refresh.COLLECTION),
                rows=rows,
                plan=plan,
                stats=stats,
                last_update_option_cls=lambda value: ("lastUpdate", value),
                concurrency_exceptions=(ConcurrencyError,),
            )

        self.assertEqual(stats.write_waves_attempted, 5)
        self.assertEqual(stats.write_waves_committed, 4)
        self.assertEqual(stats.writes_attempted, 2000)
        self.assertEqual(stats.writes_succeeded, 1600)
        self.assertEqual(
            [wave["waveNumber"] for wave in stats.committed_write_waves],
            [1, 2, 3, 4],
        )
        self.assertEqual(stats.failed_write_wave["waveNumber"], 5)
        self.assertEqual(refresh._failure_result(stats), "REFRESH_ABORTED_PARTIAL")

    def test_compact_update_plan_preserves_last_update_precondition(self):
        doc_id = "U00001"
        existing = expected(doc_id)
        existing["customerNo"] = "OLD"
        expected_doc = expected(doc_id)
        expected_doc["customerNo"] = "NEW"
        rows = [{"masterId": doc_id, "expected": expected_doc}]
        db = DB({doc_id: existing})
        db.versions[doc_id] = 77

        stats, plan, _ = classify_and_gate(rows, db)
        decision = plan[0]["decisions"][0]

        self.assertEqual(decision["classification"], "UPDATED")
        self.assertEqual(decision["updateTime"], 77)
        self.assertNotIn("snapshot", decision)

        refresh.execute_global_plan(
            db=db,
            collection=db.collection(refresh.COLLECTION),
            rows=rows,
            plan=plan,
            stats=stats,
            last_update_option_cls=lambda value: ("lastUpdate", value),
            concurrency_exceptions=(RuntimeError,),
        )

        self.assertEqual(db.commit_calls, 1)

    def test_run_refresh_conflict_report_proves_zero_attempts_and_zero_writes(self):
        rows = [item(f"R{index:05d}") for index in range(1, 10001)]
        db = DB(
            {
                "R09800": {
                    "master": {"id": "R09800", "visibility": "BROKEN"}
                }
            }
        )
        modules, _ = google_module_stubs(db)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service_account_path = root / "sa.json"
            service_account_path.write_text(
                json.dumps({"project_id": "ireps-dev"}),
                encoding="utf-8",
            )
            input_path = root / "input.csv"
            manifest_path = root / "manifest.json"
            input_path.write_text("unused", encoding="utf-8")
            manifest_path.write_text("{}", encoding="utf-8")
            report_dir = root / "reports"

            with (
                mock.patch.dict(sys.modules, modules, clear=False),
                mock.patch.object(
                    refresh,
                    "load_and_validate",
                    return_value=(rows, {"rows": len(rows)}),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"writeAttemptCount=0; firestoreWrites=0",
                ):
                    refresh.run_refresh(
                        project_id="ireps-dev",
                        confirm_project="ireps-dev",
                        service_account_path=service_account_path,
                        input_path=input_path,
                        manifest_path=manifest_path,
                        report_dir=report_dir,
                        preflight_only=False,
                        metadata_contract_path=manifest_path,
                    )

            reports = list(report_dir.glob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["recordsInspected"], 10000)
            self.assertEqual(report["conflictCount"], 1)
            self.assertEqual(report["writeAttemptCount"], 0)
            self.assertEqual(report["writeSuccessCount"], 0)
            self.assertEqual(report["firestoreWrites"], 0)
            self.assertEqual(report["result"], "REFRESH_ABORTED_NO_WRITE")
            self.assertEqual(
                report["globalPreflight"],
                {"complete": True, "gatePassed": False},
            )
            self.assertEqual(report["batchEvidence"]["writeWavesAttempted"], 0)
            self.assertEqual(report["batchEvidence"]["writeWavesCommitted"], 0)

    def test_run_refresh_preflight_only_report_classifies_every_row_and_writes_zero(self):
        rows = [item(f"S{index:05d}") for index in range(1, 801)]
        db = DB()
        modules, _ = google_module_stubs(db)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service_account_path = root / "sa.json"
            service_account_path.write_text(
                json.dumps({"project_id": "ireps-dev"}),
                encoding="utf-8",
            )
            input_path = root / "input.csv"
            manifest_path = root / "manifest.json"
            input_path.write_text("unused", encoding="utf-8")
            manifest_path.write_text("{}", encoding="utf-8")
            report_dir = root / "reports"

            with (
                mock.patch.dict(sys.modules, modules, clear=False),
                mock.patch.object(
                    refresh,
                    "load_and_validate",
                    return_value=(rows, {"rows": len(rows)}),
                ),
            ):
                report_path = refresh.run_refresh(
                    project_id="ireps-dev",
                    confirm_project="ireps-dev",
                    service_account_path=service_account_path,
                    input_path=input_path,
                    manifest_path=manifest_path,
                    report_dir=report_dir,
                    preflight_only=True,
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["recordsInspected"], 800)
            self.assertEqual(report["createdCount"], 800)
            self.assertEqual(report["writeAttemptCount"], 0)
            self.assertEqual(report["writeSuccessCount"], 0)
            self.assertEqual(report["firestoreWrites"], 0)
            self.assertEqual(report["result"], "PREFLIGHT_PASS")
            self.assertEqual(
                report["globalPreflight"],
                {"complete": True, "gatePassed": True},
            )
            self.assertEqual(db.commit_calls, 0)

    def test_run_refresh_concurrency_wave_5_reports_exact_partial_commit(self):
        rows = [item(f"T{index:05d}") for index in range(1, 4001)]
        db = DB()
        modules, exceptions = google_module_stubs(db)
        db.commit_failures[5] = exceptions.Aborted("stale precondition")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service_account_path = root / "sa.json"
            service_account_path.write_text(
                json.dumps({"project_id": "ireps-dev"}),
                encoding="utf-8",
            )
            input_path = root / "input.csv"
            manifest_path = root / "manifest.json"
            input_path.write_text("unused", encoding="utf-8")
            manifest_path.write_text("{}", encoding="utf-8")
            report_dir = root / "reports"

            with (
                mock.patch.dict(sys.modules, modules, clear=False),
                mock.patch.object(
                    refresh,
                    "load_and_validate",
                    return_value=(rows, {"rows": len(rows)}),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"failedWriteWave=5; committedWriteWaves=\[1, 2, 3, 4\]",
                ):
                    refresh.run_refresh(
                        project_id="ireps-dev",
                        confirm_project="ireps-dev",
                        service_account_path=service_account_path,
                        input_path=input_path,
                        manifest_path=manifest_path,
                        report_dir=report_dir,
                        preflight_only=False,
                        metadata_contract_path=manifest_path,
                    )

            reports = list(report_dir.glob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "REFRESH_ABORTED_PARTIAL")
            self.assertEqual(report["writeAttemptCount"], 2000)
            self.assertEqual(report["writeSuccessCount"], 1600)
            self.assertEqual(report["firestoreWrites"], 1600)
            self.assertEqual(
                [wave["waveNumber"] for wave in report["batchEvidence"]["committedWriteWaves"]],
                [1, 2, 3, 4],
            )
            self.assertEqual(report["batchEvidence"]["failedWriteWave"]["waveNumber"], 5)

    def test_run_refresh_uses_global_classify_gate_then_optional_execute(self):
        source = SCRIPT.read_text(encoding="utf-8")
        run_source = source[source.index("def run_refresh("):]

        classify_pos = run_source.index("global_plan = classify_all(")
        gate_pos = run_source.index("evaluate_global_gate(")
        execute_pos = run_source.index("execute_global_plan(")

        self.assertLess(classify_pos, gate_pos)
        self.assertLess(gate_pos, execute_pos)
        self.assertIn("if not preflight_only:", run_source)
        self.assertNotIn(
            "processed = 0\n        for wave_items in _chunks(rows):",
            run_source,
        )


if __name__ == "__main__":
    unittest.main()
