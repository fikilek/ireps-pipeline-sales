from __future__ import annotations

import argparse
import contextlib
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
    def __init__(self, doc_id, payload):
        self.id = doc_id
        self.exists = payload is not None
        self.payload = payload

    def to_dict(self):
        return self.payload


class DocRef:
    def __init__(self, db, doc_id):
        self.db, self.id = db, doc_id

    def get(self, transaction=None):
        if self.db.get_error:
            raise self.db.get_error
        return Snapshot(self.id, self.db.docs.get(self.id))


class Collection:
    def __init__(self, db):
        self.db = db

    def document(self, doc_id):
        return DocRef(self.db, doc_id)

    def stream(self):
        return [Snapshot(key, value) for key, value in self.db.docs.items()]


class Transaction:
    def __init__(self, db):
        self.db = db

    def create(self, ref, payload):
        if ref.id in self.db.docs:
            raise google_exceptions.AlreadyExists("concurrent create")
        if self.db.write_error:
            raise self.db.write_error
        self.db.docs[ref.id] = payload
        self.db.creates.append(ref.id)

    def update(self, ref, updates):
        if self.db.write_error:
            raise self.db.write_error
        target = self.db.docs[ref.id]
        for path, value in updates.items():
            current = target
            parts = path.split(".")
            for part in parts[:-1]:
                current = current[part]
            current[parts[-1]] = value
        self.db.updates.append((ref.id, dict(updates)))


class FakeDB:
    def __init__(self, docs=None, get_error=None, write_error=None):
        self.docs = dict(docs or {})
        self.get_error = get_error
        self.write_error = write_error
        self.creates, self.updates = [], []

    def collection(self, unused_name):
        return Collection(self)

    def transaction(self):
        return Transaction(self)


class Stage07RefreshTests(unittest.TestCase):
    def apply_one(self, db, incoming):
        with mock.patch.object(stage07.firestore, "transactional", lambda function: function):
            return stage07.refresh_one_document(db, incoming, NOW)

    def test_preflight_and_main_report_include_approved_months(self):
        config = stage07.UploadConfig(
            "project", "project", Path("service.json"), Path("input.csv"),
            Path("manifest.json"), "refresh", None, Path("reports"),
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

    def test_missing_document_is_strictly_created(self):
        db = FakeDB()
        decision = self.apply_one(db, row())
        self.assertEqual(decision.classification, "CREATED")
        self.assertEqual(db.creates, ["0123456789"])
        self.assertEqual(db.docs["0123456789"]["refs"]["sales"],
                         {"id": "0123456789", "provider": "conlog"})

    def test_field_only_document_is_updated_preserving_ast_and_created_metadata(self):
        doc = existing(ast_id="AST100", sales=False)
        doc["customerNo"] = ""
        doc["accountNo"] = ""
        created = {key: doc["metadata"][key] for key in
                   ("createdAt", "createdByUid", "createdByUser")}
        db = FakeDB({"0123456789": doc})
        decision = self.apply_one(db, row())
        self.assertEqual(decision.classification, "UPDATED")
        self.assertEqual(db.docs["0123456789"]["refs"]["asts"]["id"], "AST100")
        self.assertEqual({key: doc["metadata"][key] for key in created}, created)
        self.assertEqual(set(db.updates[0][1]), set(stage07.REFRESH_UPDATE_PATHS))

    def test_exact_values_are_unchanged_and_not_written(self):
        db = FakeDB({"0123456789": existing()})
        decision = self.apply_one(db, row())
        self.assertEqual(decision.classification, "UNCHANGED")
        self.assertFalse(db.updates)

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
        decision = self.apply_one(db, row(customerNo="", accountNo=""))
        self.assertEqual(decision.classification, "UNCHANGED")
        self.assertEqual((db.docs["0123456789"]["customerNo"], db.docs["0123456789"]["accountNo"]),
                         ("C100", "A100"))

    def test_transaction_reread_reclassifies_concurrent_operational_change(self):
        doc = existing(ast_id="AST200")
        db = FakeDB({"0123456789": doc})
        decision = self.apply_one(db, row())
        self.assertEqual(decision.classification, "UNCHANGED")
        self.assertEqual(doc["refs"]["asts"]["id"], "AST200")

    def test_concurrent_strict_create_becomes_conflict(self):
        class RaceTransaction(Transaction):
            def create(self, ref, payload):
                raise google_exceptions.AlreadyExists("race")
        db = FakeDB()
        db.transaction = lambda: RaceTransaction(db)
        decision = self.apply_one(db, row())
        self.assertEqual((decision.classification, decision.code),
                         ("CONFLICT", "MM_TRANSACTION_PRECONDITION_CHANGED"))

    def test_isolated_failure_does_not_hide_success(self):
        good = row(); bad = row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")
        db = FakeDB()
        original = stage07.refresh_one_document
        def operation(db_arg, value, timestamp):
            if value["masterId"] == "1111111111":
                return stage07.RefreshDecision("FAILED", "MM_RECORD_WRITE_FAILED", {}, {"masterId": value["masterId"]})
            return original(db_arg, value, timestamp)
        with mock.patch.object(stage07.firestore, "transactional", lambda function: function), \
             mock.patch.object(stage07, "refresh_one_document", operation):
            state = stage07.RefreshRunState("run", 2)
            stage07.refresh_documents(db, [good, bad], NOW, state)
        self.assertEqual((state.created_count, state.failed_count, len(state.failures)), (1, 1, 1))

    def test_individual_write_exception_is_failed(self):
        db = FakeDB(write_error=RuntimeError("one write failed"))
        decision = self.apply_one(db, row())
        self.assertEqual((decision.classification, decision.code),
                         ("FAILED", "MM_RECORD_WRITE_FAILED"))

    def test_systemic_firestore_failure_fails_refresh(self):
        db = FakeDB(get_error=google_exceptions.PermissionDenied("denied"))
        with self.assertRaises(google_exceptions.PermissionDenied):
            self.apply_one(db, row())

    def test_absent_documents_are_untouched_and_reapply_is_unchanged(self):
        absent = existing(row("2222222222", meterNoNormalized="2222222222", salesId="2222222222"))
        db = FakeDB({"2222222222": absent})
        first = self.apply_one(db, row())
        second = self.apply_one(db, row())
        self.assertEqual((first.classification, second.classification), ("CREATED", "UNCHANGED"))
        self.assertIs(db.docs["2222222222"], absent)
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

    def test_write_attempt_and_success_counts_include_failed_attempt(self):
        decisions = iter((
            stage07.RefreshDecision("CREATED", "MM_DOCUMENT_CREATED", {}, {}, True, True),
            stage07.RefreshDecision("FAILED", "MM_RECORD_WRITE_FAILED", {}, {}, True, False),
        ))
        state = stage07.RefreshRunState("run", 2)
        with mock.patch.object(stage07, "refresh_one_document", side_effect=lambda *args: next(decisions)):
            stage07.refresh_documents(FakeDB(), [row(), row("1111111111")], NOW, state)
        self.assertEqual((state.write_attempt_count, state.write_success_count), (2, 1))

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

    def test_refresh_deterministic_verification_and_failure(self):
        doc1 = existing()
        second_row = row("1111111111", meterNoNormalized="1111111111", salesId="1111111111")
        doc2 = existing(second_row)
        db = FakeDB({"0123456789": doc1, "1111111111": doc2})
        successful = [dict(row(), _expectedAstId=""), dict(second_row, _expectedAstId="")]
        evidence = stage07.verify_refresh_post_write(db, successful, 2, 2, 0)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["sampleIds"], stage07.deterministic_sample_ids(["0123456789", "1111111111"]))
        doc2["metadata"]["extra"] = "unsafe"
        with self.assertRaisesRegex(RuntimeError, "verification failed") as caught:
            stage07.verify_refresh_post_write(db, successful, 2, 2, 0)
        report = {}
        stage07.fail_refresh_report(report, stage07.RefreshRunState("run", 2), caught.exception)
        self.assertEqual(report["result"], "FAILED")

    def test_systemic_mid_run_preserves_partial_state_and_conflict_evidence(self):
        state = stage07.RefreshRunState("run", 2)
        first = stage07.RefreshDecision(
            "CONFLICT", "MM_LM_CONFLICT", {},
            {"masterId": "0123456789", "detail": "lm differs"},
        )
        calls = iter((first, google_exceptions.PermissionDenied("denied")))
        def operation(*unused):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(stage07, "refresh_one_document", side_effect=operation):
            with self.assertRaises(google_exceptions.PermissionDenied):
                stage07.refresh_documents(FakeDB(), [row(), row("1111111111")], NOW, state)
        self.assertEqual((state.records_inspected, state.conflict_count, len(state.conflicts)), (1, 1, 1))
        report = {}
        stage07.fail_refresh_report(report, state, google_exceptions.PermissionDenied("denied"))
        self.assertEqual((report["recordsInspected"], report["conflictCount"], report["result"]),
                         (1, 1, "FAILED"))


if __name__ == "__main__":
    unittest.main()
