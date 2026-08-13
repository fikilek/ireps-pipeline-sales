from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "02_upload_conlog_atomic_v2.py"
SPEC = importlib.util.spec_from_file_location("stage02_under_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
stage02 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage02
SPEC.loader.exec_module(stage02)


def epoch_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def atomic_row(*, meter_no: str = "00123") -> dict[str, str]:
    provider_id = stage02.CONLOG_VENDING_PROVIDER_ID
    lm_pcode = "ZA7423"
    period = "2026-06"
    tx_at_iso = "2026-06-01T00:00:00"
    amount = "115"
    cost = "100"
    vat = "15"
    identity = "|".join(
        (provider_id, lm_pcode, meter_no, tx_at_iso, amount, cost, vat)
    )
    ingested = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    tx_at = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    return {
        "atomicId": hashlib.sha1(identity.encode("utf-8")).hexdigest(),
        "vendingProviderId": provider_id,
        "lmPcode": lm_pcode,
        "meterNo": meter_no,
        "txAtISO": tx_at_iso,
        "txAtMs": str(epoch_ms(tx_at)),
        "ym": period,
        "y": "2026",
        "m": "6",
        "amountTotalC": amount,
        "costC": cost,
        "vatC": vat,
        "currency": "ZAR",
        "sourceFileId": "conlog_prepaid_sales__ZA7423__2026-06.csv",
        "sourceRow": "1",
        "ingestedAtISO": "2026-07-01T12:00:00Z",
        "ingestedAtMs": str(epoch_ms(ingested)),
    }


def csv_bytes(row: dict[str, str]) -> bytes:
    import io

    target = io.StringIO(newline="")
    writer = csv.DictWriter(
        target,
        fieldnames=stage02.ATOMIC_COLUMNS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerow(row)
    return target.getvalue().encode("utf-8")


class FakeSnapshot:
    def __init__(self, document_id: str, data: dict[str, object]) -> None:
        self.id = document_id
        self._data = data

    def to_dict(self) -> dict[str, object]:
        return self._data


class FakeQuery:
    def __init__(self, snapshots: list[FakeSnapshot]) -> None:
        self._snapshots = snapshots

    def stream(self):
        return iter(self._snapshots)


class Stage02OfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = (
            Path(self.temp_dir.name)
            / "atomic__conlog_prepaid_sales__ZA7423__2026-06__1.csv"
        )
        self.original_row = atomic_row()
        self.original_bytes = csv_bytes(self.original_row)
        self.path.write_bytes(self.original_bytes)

    def load_atomic(self):
        return stage02.validate_and_load_atomic(
            self.path,
            expected_lm_pcode="ZA7423",
            expected_period="2026-06",
            expected_provider_id=stage02.CONLOG_VENDING_PROVIDER_ID,
        )

    def test_atomic_sha_and_parsed_rows_use_one_immutable_byte_snapshot(self) -> None:
        replacement_bytes = csv_bytes(atomic_row(meter_no="00999"))
        original_reader = stage02.read_csv_robust

        def replace_path_then_parse(snapshot: bytes):
            self.path.write_bytes(replacement_bytes)
            return original_reader(snapshot)

        stage02.read_csv_robust = replace_path_then_parse
        self.addCleanup(setattr, stage02, "read_csv_robust", original_reader)

        atomic = self.load_atomic()

        self.assertEqual(atomic.frame.iloc[0]["meterNo"], "00123")
        self.assertEqual(
            atomic.file_sha256,
            hashlib.sha256(self.original_bytes).hexdigest(),
        )
        self.assertEqual(self.path.read_bytes(), replacement_bytes)

    def test_strict_document_comparison_rejects_float_bool_and_extra_fields(self) -> None:
        atomic = self.load_atomic()
        expected = stage02.row_to_document(atomic.frame.iloc[0])
        self.assertEqual(stage02.compare_atomic_document(dict(expected), expected), [])

        as_float = dict(expected)
        as_float["amountTotalC"] = float(expected["amountTotalC"])
        self.assertTrue(
            any("amountTotalC must be an integer" in item for item in stage02.compare_atomic_document(as_float, expected))
        )

        as_bool = dict(expected)
        as_bool["sourceRow"] = True
        self.assertTrue(
            any("sourceRow must be an integer" in item for item in stage02.compare_atomic_document(as_bool, expected))
        )

        with_extra = dict(expected)
        with_extra["unexpected"] = "value"
        self.assertTrue(
            any("unexpected fields" in item for item in stage02.compare_atomic_document(with_extra, expected))
        )

    def test_resume_existing_state_uses_strict_document_types(self) -> None:
        atomic = self.load_atomic()
        document_id = str(atomic.frame.iloc[0]["atomicId"])
        actual = stage02.row_to_document(atomic.frame.iloc[0])
        actual["amountTotalC"] = float(actual["amountTotalC"])

        with self.assertRaisesRegex(ValueError, "Firestore conflicts"):
            stage02.compare_existing_for_resume(
                FakeQuery([FakeSnapshot(document_id, actual)]),
                atomic,
            )

    def test_resume_requires_exact_failed_report_and_upload_contract(self) -> None:
        atomic = self.load_atomic()
        args = SimpleNamespace(
            project_id="ireps-test",
            vending_provider_id=stage02.CONLOG_VENDING_PROVIDER_ID,
        )
        contract = stage02.make_upload_contract(args=args, atomic=atomic)
        fingerprint = stage02.canonical_json_sha256(contract)
        report = {
            "stage": "02",
            "script": "02_upload_conlog_atomic_v2.py",
            "operation": "execute-upload",
            "mode": "create-only",
            "status": "FAIL",
            "result": "FAILED",
            "startedAt": "2026-07-16T10:00:00Z",
            "finishedAt": "2026-07-16T10:01:00Z",
            "uploadContract": contract,
            "uploadFingerprint": fingerprint,
        }
        report["reportFingerprint"] = stage02.canonical_json_sha256(report)
        report_path = Path(self.temp_dir.name) / "failed-stage02.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        evidence = stage02.validate_resume_report(
            report_path,
            current_contract=contract,
            current_fingerprint=fingerprint,
        )
        self.assertEqual(evidence["previousMode"], "create-only")

        changed_contract = dict(contract)
        changed_contract["csvSha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match the failed original"):
            stage02.validate_resume_report(
                report_path,
                current_contract=changed_contract,
                current_fingerprint=stage02.canonical_json_sha256(changed_contract),
            )

    def test_resume_rejects_edited_report(self) -> None:
        atomic = self.load_atomic()
        args = SimpleNamespace(
            project_id="ireps-test",
            vending_provider_id=stage02.CONLOG_VENDING_PROVIDER_ID,
        )
        contract = stage02.make_upload_contract(args=args, atomic=atomic)
        report = {
            "stage": "02",
            "script": "02_upload_conlog_atomic_v2.py",
            "operation": "execute-upload",
            "mode": "create-only",
            "status": "FAIL",
            "result": "FAILED",
            "uploadContract": contract,
            "uploadFingerprint": stage02.canonical_json_sha256(contract),
        }
        report["reportFingerprint"] = stage02.canonical_json_sha256(report)
        report["mode"] = "resume"
        report_path = Path(self.temp_dir.name) / "edited-stage02.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "report may be edited or corrupt"):
            stage02.validate_resume_report(
                report_path,
                current_contract=contract,
                current_fingerprint=stage02.canonical_json_sha256(contract),
            )

    def test_resume_mode_requires_report_and_create_only_prohibits_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --resume-report"):
            stage02.validate_resume_mode(
                argparse.Namespace(mode="resume", resume_report=None)
            )
        with self.assertRaisesRegex(ValueError, "only with --mode resume"):
            stage02.validate_resume_mode(
                argparse.Namespace(mode="create-only", resume_report=Path("failed.json"))
            )


class Stage02BatchGovernanceTests(unittest.TestCase):
    def test_sample_verification_uses_bulk_get_all(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "02_upload_conlog_atomic_v2.py").read_text(encoding="utf-8")
        self.assertIn("db.get_all(refs)", source)
        self.assertIn("BATCH_SIZE = 400", source)



if __name__ == "__main__":
    unittest.main()
