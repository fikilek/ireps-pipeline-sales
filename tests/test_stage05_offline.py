from __future__ import annotations

import csv
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "05_build_meter_master_v3.py"
)
SPEC = importlib.util.spec_from_file_location("stage05_meter_master", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"Unable to import {SCRIPT_PATH}")
stage05 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage05
SPEC.loader.exec_module(stage05)

STAGE06_PATH = Path(__file__).resolve().parents[1] / "scripts" / "06_build_sales_all_meters.py"
STAGE06_SPEC = importlib.util.spec_from_file_location("stage06_sales_all", STAGE06_PATH)
if STAGE06_SPEC is None or STAGE06_SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"Unable to import {STAGE06_PATH}")
stage06 = importlib.util.module_from_spec(STAGE06_SPEC)
sys.modules[STAGE06_SPEC.name] = stage06
STAGE06_SPEC.loader.exec_module(stage06)


LM = "ZA7423"
MONTH = "2026-01"
CONFIG = stage05.BuildConfig(
    lm_pcode=LM,
    provider=stage05.GOVERNED_PROVIDER,
    meter_type=stage05.GOVERNED_METER_TYPE,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_rows(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def valid_monthly_row() -> dict[str, object]:
    return {
        "docId": f"{LM}__METER001__{MONTH}",
        "lmPcode": LM,
        "meterNo": "METER001",
        "ym": MONTH,
        "y": "2026",
        "m": "1",
        "purchasesCount": "2",
        "amountTotalC": "10000",
        "costC": "8000",
        "vatC": "2000",
        "firstPurchaseAtISO": "2026-01-05T10:00:00Z",
        "lastPurchaseAtISO": "2026-01-20T12:30:00Z",
        "firstPurchaseAtMs": "1767607200000",
        "lastPurchaseAtMs": "1768912200000",
        "salesGroupId": "GR2",
        "salesGroupLabel": "100-299.99",
    }


def reconciliation() -> dict[str, object]:
    return {
        "lmPcode": LM,
        "month": MONTH,
        "purchasesCount": 2,
        "metersCount": 1,
        "amountTotalC": 10000,
        "costC": 8000,
        "vatC": 2000,
    }


def stage03_manifest_payload(monthly_path: Path) -> dict[str, object]:
    monthly_lm_name = f"monthly_lm__FULL__{MONTH}__from_atomic.csv"
    groups_name = f"monthly_lm_groups__FULL__{MONTH}__from_atomic.csv"
    atomic_name = f"atomic__conlog_prepaid_sales__{LM}__{MONTH}__2.csv"
    return {
        "stage": "03",
        "script": stage05.STAGE03_SCRIPT,
        "status": "PASS",
        "result": "BUILD_WRITTEN",
        "operation": "build-write",
        "lmPcode": LM,
        "month": MONTH,
        "atomicFile": {
            "month": MONTH,
            "path": str(monthly_path.parent / atomic_name),
            "filename": atomic_name,
            "rows": 2,
            "sha256": "a" * 64,
        },
        "atomicRows": 2,
        "atomicUniqueMeters": 1,
        "monthlyRows": 1,
        "monthlyLmRows": 1,
        "monthlyLmGroupRows": 1,
        "reconciliation": [reconciliation()],
        "outputs": [
            {
                "dataset": "monthly",
                "month": MONTH,
                "path": str(monthly_path.resolve()),
                "filename": monthly_path.name,
                "rows": 1,
                "sha256": sha256(monthly_path),
                "existingState": "MISSING",
                "existingSha256": "",
            },
            {
                "dataset": "monthly_lm",
                "month": MONTH,
                "path": str(monthly_path.parent / monthly_lm_name),
                "filename": monthly_lm_name,
                "rows": 1,
                "sha256": "b" * 64,
                "existingState": "MISSING",
                "existingSha256": "",
            },
            {
                "dataset": "monthly_lm_groups",
                "month": MONTH,
                "path": str(monthly_path.parent / groups_name),
                "filename": groups_name,
                "rows": 1,
                "sha256": "c" * 64,
                "existingState": "MISSING",
                "existingSha256": "",
            },
        ],
        "writeSummary": {"written": 3, "unchanged": 0},
    }


def write_manifest(
    manifest_dir: Path,
    monthly_path: Path,
    payload: dict[str, object] | None = None,
) -> Path:
    path = manifest_dir / (
        f"stage03_monthly_build__{LM}__{MONTH}__20260716T000000Z.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload or stage03_manifest_payload(monthly_path), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class Stage05OfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.monthly_path = (
            self.root / f"monthly__FULL__{MONTH}__from_atomic.csv"
        )
        write_rows(
            self.monthly_path,
            stage05.MONTHLY_COLUMNS,
            [valid_monthly_row()],
        )
        self.manifest_dir = self.root / "manifests"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def monthly_input(self) -> object:
        return stage05.MonthlyInput(period=MONTH, path=self.monthly_path)

    def approve(self) -> object:
        write_manifest(self.manifest_dir, self.monthly_path)
        return stage05.approve_monthly_input(
            self.monthly_input(),
            manifest_dir=self.manifest_dir,
            config=CONFIG,
            scope="FULL",
        )

    def test_valid_monthly_and_manifest_evidence_uses_immutable_bytes(self) -> None:
        approved = self.approve()
        original_monthly_sha = approved.csv.sha256
        original_manifest_sha = approved.stage03_manifest.sha256

        self.monthly_path.write_text("changed after validation\n", encoding="utf-8")
        self.assertNotEqual(original_monthly_sha, sha256(self.monthly_path))
        self.assertEqual(approved.frame.loc[0, "meterNo"], "METER001")

        customer_path = self.root / "Customer_Details.csv"
        npr_path = self.root / "90_Days_No_Purchase_Report.csv"
        write_rows(
            customer_path,
            ["MeterNumber", "CustomerNo", "AccountNo", "AccountStatus", "LastPurchaseDate"],
            [],
        )
        write_rows(
            npr_path,
            ["MeterIdentifier", "CustomerNo1", "LastPurchaseDate"],
            [],
        )
        customer = stage05.read_csv_snapshot(customer_path, "Customer Details")
        npr = stage05.read_csv_snapshot(npr_path, "NPR")
        customer_path.write_text("mutated\n", encoding="utf-8")

        master_map = stage05.load_monthly_meter_universe([approved], CONFIG)
        stage05.finalize_master_records(master_map, CONFIG)
        master_frame = stage05.master_rows_to_dataframe(master_map)
        output = stage05.write_csv(master_frame, self.root / "meter_master.csv")
        stats = stage05.BuildStats(monthly_backed_meters=1, total_master_rows=1)
        result = stage05.build_manifest(
            config=CONFIG,
            scope="FULL",
            from_month=MONTH,
            to_month=MONTH,
            included_months=[MONTH],
            monthly_inputs=[approved],
            customer_details=customer,
            npr=npr,
            output=output,
            output_rows=1,
            stats=stats,
        )
        evidence = result["sourceContract"]["monthlyInputs"][0]
        self.assertEqual(evidence["sha256"], original_monthly_sha)
        self.assertEqual(
            evidence["stage03Manifest"]["sha256"], original_manifest_sha
        )
        self.assertEqual(
            result["sourceContract"]["customerDetails"]["sha256"],
            customer.sha256,
        )
        self.assertTrue(result["buildFingerprint"])
        self.assertEqual(
            result["buildFingerprint"],
            stage06.canonical_json_sha256(stage06.stage05_fingerprint_contract(result)),
        )

    def test_valid_cli_build_writes_frozen_csv_and_manifest(self) -> None:
        write_manifest(self.manifest_dir, self.monthly_path)
        customer_path = self.root / "Customer_Details.csv"
        npr_path = self.root / "90_Days_No_Purchase_Report.csv"
        output_path = self.root / f"meter_master__{LM}__FULL__{MONTH}_to_{MONTH}.csv"
        write_rows(
            customer_path,
            ["MeterNumber", "CustomerNo", "AccountNo", "AccountStatus", "LastPurchaseDate"],
            [],
        )
        write_rows(
            npr_path,
            ["MeterIdentifier", "CustomerNo1", "LastPurchaseDate"],
            [],
        )
        argv = [
            str(SCRIPT_PATH),
            "--lm-pcode",
            LM,
            "--from-month",
            MONTH,
            "--to-month",
            MONTH,
            "--monthly-dir",
            str(self.root),
            "--stage03-manifest-dir",
            str(self.manifest_dir),
            "--customer-details",
            str(customer_path),
            "--npr",
            str(npr_path),
            "--output",
            str(output_path),
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            stage05.main()

        manifest_path = output_path.with_suffix(".manifest.json")
        self.assertTrue(output_path.is_file())
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["result"], "BUILD_WRITTEN")
        self.assertEqual(
            manifest["sourceContract"]["monthlyInputs"][0]["stage03Manifest"]["sha256"],
            sha256(next(self.manifest_dir.glob("*.json"))),
        )

    def test_rejects_non_exact_monthly_schema(self) -> None:
        columns = stage05.MONTHLY_COLUMNS[:-1]
        row = valid_monthly_row()
        row.pop("salesGroupLabel")
        write_rows(self.monthly_path, columns, [row])
        write_manifest(self.manifest_dir, self.monthly_path)
        with self.assertRaisesRegex(ValueError, "monthly schema mismatch"):
            stage05.approve_monthly_input(
                self.monthly_input(),
                manifest_dir=self.manifest_dir,
                config=CONFIG,
                scope="FULL",
            )

    def test_rejects_invalid_monthly_identity_and_integer_types(self) -> None:
        for field, value, expected_error in (
            ("docId", "wrong", "deterministic docId mismatch"),
            ("amountTotalC", "10000.0", "integer text"),
        ):
            with self.subTest(field=field):
                row = valid_monthly_row()
                row[field] = value
                write_rows(self.monthly_path, stage05.MONTHLY_COLUMNS, [row])
                with self.assertRaisesRegex(ValueError, expected_error):
                    snapshot = stage05.read_csv_snapshot(self.monthly_path, "monthly")
                    stage05.validate_monthly_snapshot(
                        self.monthly_input(), snapshot, CONFIG
                    )

    def test_rejects_manifest_with_bad_reconciliation_or_output_set(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []
        bad_reconciliation = stage03_manifest_payload(self.monthly_path)
        bad_reconciliation["reconciliation"][0]["amountTotalC"] = 9999
        cases.append(("reconciliation", bad_reconciliation, "reconciliation"))

        bad_outputs = stage03_manifest_payload(self.monthly_path)
        bad_outputs["outputs"] = bad_outputs["outputs"][:2]
        cases.append(("outputs", bad_outputs, "exactly three outputs"))

        for name, payload, expected_error in cases:
            with self.subTest(name=name):
                manifest_path = write_manifest(
                    self.manifest_dir, self.monthly_path, payload
                )
                monthly_snapshot = stage05.read_csv_snapshot(
                    self.monthly_path, "monthly"
                )
                _, expected_reconciliation = stage05.validate_monthly_snapshot(
                    self.monthly_input(), monthly_snapshot, CONFIG
                )
                manifest = stage05.read_json_snapshot(manifest_path, "Stage 03")
                with self.assertRaisesRegex(ValueError, expected_error):
                    stage05.validate_stage03_manifest(
                        manifest,
                        monthly_input=self.monthly_input(),
                        monthly_snapshot=monthly_snapshot,
                        reconciliation=expected_reconciliation,
                        config=CONFIG,
                        scope="FULL",
                    )

    def test_blank_npr_customer_never_replaces_populated_identity(self) -> None:
        for populated in ("METER001", "REAL-CUSTOMER"):
            with self.subTest(populated=populated):
                existing = {
                    "meterNoRaw": "METER001",
                    "customerNo": populated,
                    "lastPurchaseDate": "2026-01-01",
                    "sourceRow": 2,
                }
                before = deepcopy(existing)
                candidate = {
                    "meterNoRaw": "METER001",
                    "customerNo": "",
                    "lastPurchaseDate": "2026-02-01",
                    "sourceRow": 3,
                }
                resolution = stage05.merge_npr_duplicate_record(
                    existing,
                    candidate,
                    source=Path("npr.csv"),
                    row_number=3,
                    normalized="METER001",
                )
                self.assertIsNone(resolution)
                self.assertEqual(existing, before)

    def test_nonblank_npr_customer_can_replace_placeholder(self) -> None:
        existing = {
            "meterNoRaw": "METER001",
            "customerNo": "METER001",
            "lastPurchaseDate": "2026-01-01",
            "sourceRow": 2,
        }
        candidate = {
            "meterNoRaw": "METER001",
            "customerNo": "REAL-CUSTOMER",
            "lastPurchaseDate": "2026-02-01",
            "sourceRow": 3,
        }
        resolution = stage05.merge_npr_duplicate_record(
            existing,
            candidate,
            source=Path("npr.csv"),
            row_number=3,
            normalized="METER001",
        )
        self.assertEqual(resolution, "placeholder")
        self.assertEqual(existing["customerNo"], "REAL-CUSTOMER")


if __name__ == "__main__":
    unittest.main()
