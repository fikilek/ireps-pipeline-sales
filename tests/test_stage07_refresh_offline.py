from __future__ import annotations

import argparse
import contextlib
import copy
import inspect
import io
import importlib.util
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from google.api_core import exceptions as google_exceptions


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "07_upload_meter_master_v3.py"
SPEC = importlib.util.spec_from_file_location("stage07_refresh", SCRIPT)
stage07 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage07
assert SPEC.loader is not None
SPEC.loader.exec_module(stage07)

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def row(master_id="0123456789", **changes):
    value = {
        "masterId": master_id,
        "lmPcode": "ZA7423",
        "meterNoRaw": master_id,
        "meterNoNormalized": master_id,
        "meterType": "electricity",
        "customerNo": "C100",
        "accountNo": "A100",
        "salesId": master_id,
        "salesProvider": "conlog",
        "astId": "",
    }
    value.update(changes)
    return value


def existing(value=None, *, ast_id="", sales=True):
    source = row() if value is None else value
    doc = stage07.build_create_doc(source, NOW)
    doc["refs"]["asts"]["id"] = ast_id
    if not sales:
        doc["refs"]["sales"] = {"id": "", "provider": ""}
    return doc


class Snapshot:
    def __init__(self, ref, payload, update_time):
        self.reference = ref
        self.id = ref.id
        self.exists = payload is not None
        self.payload = copy.deepcopy(payload)
        self.update_time = update_time

    def to_dict(self):
        return copy.deepcopy(self.payload)


class DocRef:
    def __init__(self, db, doc_id):
        self.db, self.id = db, doc_id
        self.path = f"meter_master/{doc_id}"

    def get(self, transaction=None):
        self.db.individual_get_calls += 1
        return self.db.snapshot(self)


class Collection:
    def __init__(self, db):
        self.db = db

    def document(self, doc_id):
        return DocRef(self.db, doc_id)

    def stream(self):
        return [self.db.snapshot(DocRef(self.db, key)) for key in sorted(self.db.docs)]


class Batch:
    def __init__(self, db):
        self.db = db
        self.operations = []

    def create(self, ref, payload):
        self.operations.append(("create", ref, copy.deepcopy(payload), None))

    def update(self, ref, updates, option=None):
        self.operations.append(("update", ref, copy.deepcopy(updates), option))

    def commit(self):
        self.db.commit_calls += 1
        self.db.batch_sizes.append(len(self.operations))
        self.db.batch_options.append([operation[3] for operation in self.operations])
        if self.db.on_commit is not None:
            callback = self.db.on_commit
            self.db.on_commit = None
            callback(self.db, self.operations)
        if self.db.commit_errors:
            raise self.db.commit_errors.pop(0)
        if self.db.write_error:
            raise self.db.write_error

        next_docs = copy.deepcopy(self.db.docs)
        for operation, ref, payload, _option in self.operations:
            if operation == "create":
                if ref.id in next_docs:
                    raise google_exceptions.AlreadyExists("concurrent create")
                next_docs[ref.id] = payload
            else:
                if ref.id not in next_docs:
                    raise google_exceptions.FailedPrecondition("missing update target")
                target = next_docs[ref.id]
                for path, value in payload.items():
                    current = target
                    parts = path.split(".")
                    for part in parts[:-1]:
                        current = current[part]
                    current[parts[-1]] = value
        self.db.docs = next_docs
        for operation, ref, payload, _option in self.operations:
            self.db.versions[ref.id] = self.db.versions.get(ref.id, 0) + 1
            if operation == "create":
                self.db.creates.append(ref.id)
            else:
                self.db.updates.append((ref.id, payload))
        return [object() for _ in self.operations]


class FakeDB:
    def __init__(
        self,
        docs=None,
        *,
        get_error=None,
        write_error=None,
        commit_errors=None,
        fail_get_on_call=None,
        on_commit=None,
    ):
        self.docs = copy.deepcopy(docs or {})
        self.versions = {doc_id: 1 for doc_id in self.docs}
        self.get_error = get_error
        self.write_error = write_error
        self.commit_errors = list(commit_errors or [])
        self.fail_get_on_call = fail_get_on_call
        self.on_commit = on_commit
        self.creates, self.updates = [], []
        self.get_all_calls = 0
        self.get_all_sizes = []
        self.individual_get_calls = 0
        self.commit_calls = 0
        self.batch_sizes = []
        self.batch_options = []

    def collection(self, unused_name):
        return Collection(self)

    def snapshot(self, ref):
        return Snapshot(ref, self.docs.get(ref.id), self.versions.get(ref.id))

    def get_all(self, refs):
        self.get_all_calls += 1
        refs = list(refs)
        self.get_all_sizes.append(len(refs))
        if self.get_error or self.fail_get_on_call == self.get_all_calls:
            raise self.get_error or google_exceptions.PermissionDenied("denied")
        return [self.snapshot(ref) for ref in refs]

    def batch(self):
        return Batch(self)


class Stage07RefreshTests(unittest.TestCase):
    def run_refresh(self, db, rows):
        state = stage07.RefreshRunState("run", len(rows))
        with mock.patch.object(stage07, "LastUpdateOption", lambda value: ("lastUpdate", value)):
            stage07.refresh_documents(db, rows, NOW, state)
        return state

    def test_preflight_and_main_report_include_approved_months(self):
        config = stage07.UploadConfig(
            "project", "project", Path("service.json"), Path("input.csv"),
            Path("manifest.json"), "refresh", None, Path("reports"), False,
        )
        preflight = stage07.PreflightResult(
            1, 1, "csv", "ids", ["ZA7423"], ["conlog"], ["electricity"],
            "2025-09", "2025-11", ["2025-09", "2025-10", "2025-11"],
            "manifest", "fingerprint",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stage07.print_preflight(config, preflight)
        self.assertIn("Included months:      2025-09, 2025-10, 2025-11", output.getvalue())
        self.assertIn('"includedMonths": preflight.included_months', inspect.getsource(stage07.main))

    def test_cli_accepts_refresh_and_rejects_resume_report(self):
        argv = ["stage07", "--project-id", "p", "--confirm-project", "p",
                "--service-account", "s.json", "--input", "i.csv", "--manifest", "m.json",
                "--mode", "refresh"]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(stage07.parse_args().mode, "refresh")
        args = argparse.Namespace(project_id="p", confirm_project="p", service_account=Path("s"),
            input=Path("i"), manifest=Path("m"), mode="refresh", resume_report=Path("r"),
            report_dir=Path("reports"))
        with self.assertRaisesRegex(ValueError, "only with --mode resume"):
            stage07.build_config(args)

    def test_create_only_and_resume_cli_protections_remain(self):
        base = dict(project_id="p", confirm_project="p", service_account=Path("s"),
                    input=Path("i"), manifest=Path("m"), report_dir=Path("reports"))
        with self.assertRaisesRegex(ValueError, "requires --resume-report"):
            stage07.build_config(argparse.Namespace(**base, mode="resume", resume_report=None))
        with self.assertRaisesRegex(ValueError, "only with --mode resume"):
            stage07.build_config(argparse.Namespace(**base, mode="create-only", resume_report=Path("r")))

    def test_missing_document_is_strictly_created_in_batch(self):
        db = FakeDB()
        state = self.run_refresh(db, [row()])
        self.assertEqual((state.created_count, state.write_success_count), (1, 1))
        self.assertEqual(db.creates, ["0123456789"])
        self.assertEqual(db.batch_sizes, [1])
        self.assertEqual(db.individual_get_calls, 0)
        self.assertEqual(db.docs["0123456789"]["refs"]["sales"],
                         {"id": "0123456789", "provider": "conlog"})

    def test_field_only_document_is_updated_preserving_ast_and_created_metadata(self):
        doc = existing(ast_id="AST100", sales=False)
        doc["customerNo"] = ""
        doc["accountNo"] = ""
        created = {key: doc["metadata"][key] for key in
                   ("createdAt", "createdByUid", "createdByUser")}
        db = FakeDB({"0123456789": doc})
        state = self.run_refresh(db, [row()])
        self.assertEqual(state.updated_count, 1)
        self.assertEqual(db.docs["0123456789"]["refs"]["asts"]["id"], "AST100")
        self.assertEqual({key: db.docs["0123456789"]["metadata"][key] for key in created}, created)
        self.assertEqual(set(db.updates[0][1]), set(stage07.REFRESH_UPDATE_PATHS))
        self.assertEqual(db.batch_options[0][0][0], "lastUpdate")

    def test_exact_values_are_unchanged_and_not_written(self):
        db = FakeDB({"0123456789": existing()})
        state = self.run_refresh(db, [row()])
        self.assertEqual(state.unchanged_count, 1)
        self.assertFalse(db.updates)
        self.assertEqual(db.commit_calls, 0)

    def assert_conflict(self, payload, code, incoming=None, doc_id="0123456789"):
        decision = stage07.classify_refresh_document(doc_id, payload, incoming or row(), NOW)
        self.assertEqual((decision.classification, decision.code), ("CONFLICT", code))

    def test_identity_lm_and_meter_type_conflicts(self):
        doc = existing(); doc["meterNo"]["normalized"] = "9999999999"
        self.assert_conflict(doc, "MM_NORMALIZED_IDENTITY_CONFLICT")
        doc = existing(); doc["lmPcode"] = "ZA9999"
        self.assert_conflict(doc, "MM_LM_CONFLICT")
        doc = existing(); doc["meterType"] = "water"
        self.assert_conflict(doc, "MM_METER_TYPE_CONFLICT")
        self.assert_conflict(existing(), "MM_DOCUMENT_ID_NONCANONICAL", doc_id="bad-id")

    def test_sales_reference_and_provider_conflicts(self):
        doc = existing(); doc["refs"]["sales"]["id"] = "9999999999"
        self.assert_conflict(doc, "MM_SALES_REFERENCE_CONFLICT")
        doc = existing(); doc["refs"]["sales"]["provider"] = "other"
        self.assert_conflict(doc, "MM_SALES_PROVIDER_CONFLICT")

    def test_unsafe_shape_wrong_types_ast_and_creation_metadata_conflict(self):
        doc = existing(); doc["refs"] = "unsafe"
        self.assert_conflict(doc, "MM_DOCUMENT_SHAPE_UNSAFE")
        doc = existing(); doc["customerNo"] = 100
        self.assert_conflict(doc, "MM_GOVERNED_FIELD_TYPE_INVALID")
        doc = existing(); doc["refs"]["asts"]["id"] = 123
        self.assert_conflict(doc, "MM_AST_REFERENCE_CONFLICT")
        doc = existing(); doc["metadata"]["createdByUid"] = ""
        self.assert_conflict(doc, "MM_CREATED_METADATA_INVALID")
        doc = existing(); doc["meterNo"]["raw"] = ""
        self.assert_conflict(doc, "MM_CANONICAL_FIELD_MISSING")

    def test_blank_customer_and_account_preserve_existing_values(self):
        db = FakeDB({"0123456789": existing()})
        state = self.run_refresh(db, [row(customerNo="", accountNo="")])
        self.assertEqual(state.unchanged_count, 1)
        self.assertEqual((db.docs["0123456789"]["customerNo"], db.docs["0123456789"]["accountNo"]),
                         ("C100", "A100"))

    def test_existing_operational_ast_is_reclassified_without_overwrite(self):
        doc = existing(ast_id="AST200")
        db = FakeDB({"0123456789": doc})
        state = self.run_refresh(db, [row()])
        self.assertEqual(state.unchanged_count, 1)
        self.assertEqual(db.docs["0123456789"]["refs"]["asts"]["id"], "AST200")

    def test_concurrent_create_race_becomes_conflict_without_per_document_fallback(self):
        def race(db, operations):
            target = operations[0][1].id
            db.docs[target] = existing()
            db.versions[target] = 1
        db = FakeDB(commit_errors=[google_exceptions.AlreadyExists("race")], on_commit=race)
        state = self.run_refresh(db, [row()])
        self.assertEqual((state.conflict_count, state.precondition_conflict_count), (1, 1))
        self.assertEqual(state.write_success_count, 0)
        self.assertEqual(db.individual_get_calls, 0)
        self.assertEqual(db.get_all_calls, 2)

    def test_conflict_does_not_hide_safe_write_in_same_wave(self):
        conflict_doc = existing(row("1111111111", meterNoNormalized="1111111111", salesId="1111111111"))
        conflict_doc["lmPcode"] = "ZA9999"
        db = FakeDB({"1111111111": conflict_doc})
        incoming = [row(), row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")]
        state = self.run_refresh(db, incoming)
        self.assertEqual((state.created_count, state.conflict_count), (1, 1))
        self.assertIn("0123456789", db.docs)
        self.assertEqual(db.batch_sizes, [1])

    def test_non_concurrency_batch_failure_fails_immediately(self):
        db = FakeDB(write_error=RuntimeError("batch transport failed"))
        with self.assertRaisesRegex(RuntimeError, "batch transport failed"):
            self.run_refresh(db, [row()])
        self.assertEqual(db.commit_calls, 1)
        self.assertEqual(db.individual_get_calls, 0)

    def test_systemic_firestore_read_failure_fails_refresh(self):
        db = FakeDB(get_error=google_exceptions.PermissionDenied("denied"))
        with self.assertRaises(google_exceptions.PermissionDenied):
            self.run_refresh(db, [row()])
        self.assertEqual(db.commit_calls, 0)

    def test_absent_document_reapply_is_unchanged_and_other_docs_untouched(self):
        other_row = row("2222222222", meterNoNormalized="2222222222", salesId="2222222222")
        absent = existing(other_row)
        db = FakeDB({"2222222222": absent})
        first = self.run_refresh(db, [row()])
        second = self.run_refresh(db, [row()])
        self.assertEqual((first.created_count, second.unchanged_count), (1, 1))
        self.assertEqual(db.docs["2222222222"], absent)
        self.assertEqual(len(db.docs), 2)

    def test_extra_fields_at_every_governed_level_are_shape_unsafe(self):
        mutations = (
            lambda doc: doc.update({"extra": "x"}),
            lambda doc: doc["meterNo"].update({"extra": "x"}),
            lambda doc: doc["refs"].update({"extra": {}}),
            lambda doc: doc["refs"]["asts"].update({"extra": "x"}),
            lambda doc: doc["refs"]["sales"].update({"extra": "x"}),
            lambda doc: doc["metadata"].update({"extra": "x"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                doc = existing()
                mutate(doc)
                self.assert_conflict(doc, "MM_DOCUMENT_SHAPE_UNSAFE")

    def test_raw_identity_mismatch_is_conflict_and_never_updated(self):
        doc = existing()
        incoming = row(meterNoRaw="0000000000")
        decision = stage07.classify_refresh_document("0123456789", doc, incoming, NOW)
        self.assertEqual((decision.classification, decision.code),
                         ("CONFLICT", "MM_RAW_IDENTITY_CONFLICT"))
        self.assertEqual(decision.updates, {})

    def test_exact_accounting_and_write_accounting(self):
        state = stage07.RefreshRunState("run", 5, records_inspected=5,
            created_count=1, updated_count=1, unchanged_count=1,
            conflict_count=1, failed_count=1, write_attempt_count=3,
            write_success_count=2)
        evidence = stage07.validate_refresh_accounting(state)
        self.assertTrue(evidence["balanced"])
        self.assertTrue(evidence["writesBalanced"])
        state.records_inspected = 4
        with self.assertRaisesRegex(RuntimeError, "accounting failed"):
            stage07.validate_refresh_accounting(state)

    def test_bounded_recovery_retries_safe_subset_once_and_counts_attempts(self):
        update_doc = existing()
        update_doc["customerNo"] = "OLD"

        def race(db, operations):
            # Make the CREATE participant stale while leaving UPDATE participant unchanged.
            db.docs["1111111111"] = existing(
                row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")
            )
            db.versions["1111111111"] = 1

        db = FakeDB(
            {"0123456789": update_doc},
            commit_errors=[google_exceptions.AlreadyExists("race")],
            on_commit=race,
        )
        incoming = [row(), row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")]
        state = self.run_refresh(db, incoming)
        self.assertEqual((state.updated_count, state.conflict_count), (1, 1))
        self.assertEqual((state.write_attempt_count, state.write_success_count), (3, 1))
        self.assertEqual((state.write_waves_attempted, state.write_waves_committed), (2, 1))
        self.assertEqual(db.batch_sizes, [2, 1])
        self.assertEqual(db.individual_get_calls, 0)

    def test_second_concurrency_failure_stops_without_individual_fallback(self):
        db = FakeDB(commit_errors=[
            google_exceptions.Aborted("first"),
            google_exceptions.Aborted("second"),
        ])
        with self.assertRaisesRegex(RuntimeError, "no per-document fallback"):
            self.run_refresh(db, [row()])
        self.assertEqual(db.commit_calls, 2)
        self.assertEqual(db.individual_get_calls, 0)

    def test_10216_write_partition_is_25x400_plus_216(self):
        waves = list(stage07.chunks(list(range(10216)), stage07.FIRESTORE_BATCH_SIZE))
        self.assertEqual([len(wave) for wave in waves], [400] * 25 + [216])
        self.assertLessEqual(max(map(len, waves)), 400)

    def test_preflight_bulk_reads_and_performs_zero_writes(self):
        values = [row(f"{index:010d}", meterNoNormalized=f"{index:010d}", salesId=f"{index:010d}") for index in range(1, 802)]
        db = FakeDB()
        state = stage07.RefreshRunState("preflight", len(values))
        stage07.preflight_refresh_documents(db, values, NOW, state)
        self.assertEqual(db.get_all_sizes, [400, 400, 1])
        self.assertEqual(db.commit_calls, 0)
        self.assertEqual(db.individual_get_calls, 0)
        self.assertEqual(state.created_count, 801)

    def test_governed_final_results(self):
        report = {}
        state = stage07.RefreshRunState("run", 1, records_inspected=1, unchanged_count=1)
        stage07.complete_refresh_report(report, state, {"balanced": True}, {"status": "PASS"})
        self.assertEqual((report["status"], report["result"]), ("PASS", "COMPLETED"))
        state.conflict_count = 1
        state.unchanged_count = 0
        report = {}
        stage07.complete_refresh_report(report, state, {"balanced": True}, {"status": "PASS"})
        self.assertEqual((report["status"], report["result"]),
                         ("PASS", "COMPLETED_WITH_CONFLICTS"))
        stage07.fail_refresh_report(report, state, RuntimeError("verification failed"))
        self.assertEqual((report["status"], report["result"]), ("FAIL", "FAILED"))

    def test_conflict_report_has_required_investigation_fields(self):
        state = stage07.RefreshRunState("run123", 1, records_inspected=1, conflict_count=1)
        state.conflicts.append({
            "runId": "run123", "masterId": "0123456789", "lmPcode": "ZA7423",
            "sourceRow": row(), "code": "MM_LM_CONFLICT", "conflictingPaths": ["lmPcode"],
            "existingValues": {"lmPcode": "ZA9999"}, "incomingValues": {"lmPcode": "ZA7423"},
            "message": "lmPcode differs", "detectedAt": NOW.isoformat(),
            "writeAttempted": False, "investigationRecommendation": "Review source",
        })
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp_dir:
            path = Path(temp_dir) / "conflicts.json"
            stage07.write_refresh_conflict_report(path, state, "project")
            payload = stage07.read_json(path, "conflict report")
        record = payload["records"][0]
        required = {"runId", "masterId", "lmPcode", "sourceRow", "code", "conflictingPaths",
                    "existingValues", "incomingValues", "message", "detectedAt",
                    "writeAttempted", "investigationRecommendation"}
        self.assertTrue(required.issubset(record))

    def test_refresh_deterministic_verification_uses_bulk_read_and_records_wave(self):
        doc1 = existing()
        second_row = row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")
        doc2 = existing(second_row)
        db = FakeDB({"0123456789": doc1, "1111111111": doc2})
        successful = [dict(row(), _expectedAstId=""), dict(second_row, _expectedAstId="")]
        state = stage07.RefreshRunState("run", 2)
        evidence = stage07.verify_refresh_post_write(db, successful, 2, 2, 0, state)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(state.verification_read_waves, 1)
        self.assertEqual(db.individual_get_calls, 0)
        db.docs["1111111111"]["metadata"]["extra"] = "unsafe"
        db.versions["1111111111"] += 1
        with self.assertRaisesRegex(RuntimeError, "verification failed") as caught:
            stage07.verify_refresh_post_write(db, successful, 2, 2, 0, state)
        report = {}
        stage07.fail_refresh_report(report, stage07.RefreshRunState("run", 2), caught.exception)
        self.assertEqual(report["result"], "FAILED")

    def test_systemic_failure_on_second_read_wave_preserves_first_wave_accounting(self):
        values = []
        docs = {}
        for index in range(1, 402):
            doc_id = f"{index:010d}"
            value = row(doc_id, meterNoNormalized=doc_id, salesId=doc_id)
            values.append(value)
            docs[doc_id] = existing(value)
        db = FakeDB(docs, fail_get_on_call=2)
        state = stage07.RefreshRunState("run", len(values))
        with self.assertRaises(google_exceptions.PermissionDenied):
            stage07.refresh_documents(db, values, NOW, state)
        self.assertEqual((state.records_inspected, state.unchanged_count), (400, 400))
        self.assertEqual(db.get_all_sizes, [400, 1])
        self.assertEqual(db.individual_get_calls, 0)


if __name__ == "__main__":
    unittest.main()
