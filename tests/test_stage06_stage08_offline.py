from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_script(module_name: str, filename: str):
    path = PROJECT_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


stage06 = load_script("stage06_offline", "06_build_sales_all_meters.py")
stage08 = load_script("stage08_offline", "08_upload_sales_all_meters.py")


class Stage06ContractTests(unittest.TestCase):
    def test_positive_monthly_sales_require_purchase_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive amountTotalC"):
            stage06.validate_monthly_purchase(
                amount=1,
                purchase=None,
                ym="2026-06",
                approved_months={"2026-06"},
                path=Path("monthly.csv"),
                row_number=2,
            )

    def test_purchase_date_must_belong_to_applicable_month(self) -> None:
        with self.assertRaisesRegex(ValueError, "applicable sales month"):
            stage06.validate_monthly_purchase(
                amount=1,
                purchase=datetime(2026, 5, 31, 22, 0, tzinfo=UTC),
                ym="2026-06",
                approved_months={"2026-06"},
                path=Path("monthly.csv"),
                row_number=2,
            )

    def test_valid_monthly_purchase_passes(self) -> None:
        stage06.validate_monthly_purchase(
            amount=1,
            purchase=datetime(2026, 6, 30, 22, 0, tzinfo=UTC),
            ym="2026-06",
            approved_months={"2026-06"},
            path=Path("monthly.csv"),
            row_number=2,
        )

    def test_zero_total_output_has_blank_recency(self) -> None:
        config = stage06.BuildConfig(
            lm_pcode="ZA7423",
            from_month="2026-06",
            to_month="2026-06",
            master_path=Path("unused.csv"),
            master_manifest_path=Path("unused.json"),
            monthly_dir=Path("."),
            output_path=Path("unused-output.csv"),
            manifest_path=Path("unused-output.manifest.json"),
            as_of_date=date(2026, 7, 16),
        )
        records = {
            "ABC123": {
                "masterId": "ABC123",
                "meterNo": "ABC123",
                "meterNoNormalized": "ABC123",
                "provider": "conlog",
                "customerNo": "",
                "accountNo": "",
                "months": {"2026-06": 0},
                "totalAmountC": 0,
                "lastPurchase": datetime(2026, 6, 30, tzinfo=UTC),
            }
        }
        frame = stage06.finalize_rows(
            config,
            ["2026-06"],
            records,
            stage06.BuildStats(),
        )
        self.assertEqual(frame.at[0, "lastPurchaseAtISO"], "")
        self.assertEqual(frame.at[0, "daysSinceLastPurchase"], "")

    def test_output_identity_is_exact(self) -> None:
        valid = (
            stage06.GOVERNED_OUTPUT_DIR
            / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.csv"
        )
        stage06.validate_output_identity(valid, "ZA7423", "2026-06", "2026-06")
        with self.assertRaisesRegex(ValueError, "governed output directory"):
            stage06.validate_output_identity(
                Path(tempfile.gettempdir()) / valid.name,
                "ZA7423",
                "2026-06",
                "2026-06",
            )
        with self.assertRaisesRegex(ValueError, "filename"):
            stage06.validate_output_identity(
                stage06.GOVERNED_OUTPUT_DIR / "wrong.csv",
                "ZA7423",
                "2026-06",
                "2026-06",
            )

    def test_csv_snapshot_hash_and_frame_share_one_byte_read(self) -> None:
        original = b"lmPcode,meterNo\nZA7423,ABC123\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            path.write_bytes(original)
            snapshot = stage06.read_csv_snapshot(path, "synthetic input")
            path.write_bytes(b"lmPcode,meterNo\nZA7423,CHANGED\n")

        self.assertEqual(snapshot.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(snapshot.frame.at[0, "meterNo"], "ABC123")


def sales_all_frame(*, provider: str = "conlog") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "masterId": "ABC123",
                "meterNo": "ABC123",
                "meterNoNormalized": "ABC123",
                "provider": provider,
                "customerNo": "C1",
                "accountNo": "A1",
                "totalAmountC": "100",
                "lastPurchaseAtISO": "2026-06-30T10:00:00Z",
                "daysSinceLastPurchase": "16",
                "amount_2026_06_C": "100",
            }
        ]
    )


class Stage08ContractTests(unittest.TestCase):
    def write_csv(self, frame: pd.DataFrame, directory: str) -> Path:
        path = (
            Path(directory)
            / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.csv"
        )
        frame.to_csv(path, index=False, encoding="utf-8")
        return path

    def test_valid_csv_and_recency_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(sales_all_frame(), directory)
            frame, _monthly_columns, preflight = stage08.load_and_validate_csv(path)
            stage08.validate_recency_contract(
                frame,
                preflight.months,
                date(2026, 7, 16),
            )
        self.assertEqual(preflight.providers, ["conlog"])

    def test_valid_stage06_manifest_chain_passes_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = self.write_csv(sales_all_frame(), directory)
            frame, _monthly_columns, preflight = stage08.load_and_validate_csv(csv_path)
            config06 = stage06.BuildConfig(
                lm_pcode="ZA7423",
                from_month="2026-06",
                to_month="2026-06",
                master_path=root / "meter_master.csv",
                master_manifest_path=root / "meter_master.manifest.json",
                monthly_dir=root,
                output_path=csv_path,
                manifest_path=csv_path.with_suffix(".manifest.json"),
                as_of_date=date(2026, 7, 16),
            )
            stage05_evidence = {
                "manifestPath": str(config06.master_manifest_path),
                "manifestFilename": config06.master_manifest_path.name,
                "manifestSha256": "a" * 64,
                "buildFingerprint": "b" * 64,
                "master": {
                    "path": str(config06.master_path),
                    "filename": config06.master_path.name,
                    "rows": 1,
                    "columns": stage06.MASTER_COLUMNS,
                    "sha256": "c" * 64,
                },
                "monthlyInputs": [
                    {
                        "month": "2026-06",
                        "path": str(root / "monthly__FULL__2026-06__from_atomic.csv"),
                        "filename": "monthly__FULL__2026-06__from_atomic.csv",
                        "rows": 1,
                        "columns": stage06.MONTHLY_COLUMNS,
                        "sha256": "d" * 64,
                    }
                ],
            }
            manifest = stage06.build_stage06_manifest(
                config06,
                ["2026-06"],
                frame,
                stage06.BuildStats(
                    master_rows=1,
                    monthly_rows=1,
                    meters_with_sales=1,
                    meters_without_sales=0,
                ),
                stage05_evidence,
            )
            manifest_path = config06.manifest_path
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            config08 = stage08.UploadConfig(
                project_id="ireps-test",
                service_account_path=root / "unused-service-account.json",
                input_path=csv_path,
                manifest_path=manifest_path,
                mode="create-only",
                resume_report_path=None,
                report_dir=root,
            )
            snapshot = stage08.read_json_snapshot(manifest_path, "Stage 06 manifest")
            evidence = stage08.validate_stage06_manifest(
                config08,
                preflight,
                list(frame.columns),
                snapshot,
            )

        self.assertEqual(evidence["asOfDate"], "2026-07-16")

    def test_blank_provider_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(sales_all_frame(provider=""), directory)
            with self.assertRaisesRegex(ValueError, "blank and alternate values"):
                stage08.load_and_validate_csv(path)

    def test_positive_sales_with_blank_recency_is_rejected(self) -> None:
        frame = sales_all_frame()
        frame.loc[0, "lastPurchaseAtISO"] = ""
        frame.loc[0, "daysSinceLastPurchase"] = ""
        with self.assertRaisesRegex(ValueError, "Positive totalAmountC"):
            stage08.validate_recency_contract(frame, ["2026-06"], date(2026, 7, 16))

    def test_wrong_recomputed_days_is_rejected(self) -> None:
        frame = sales_all_frame()
        frame.loc[0, "daysSinceLastPurchase"] = "15"
        with self.assertRaisesRegex(ValueError, "expected 16"):
            stage08.validate_recency_contract(frame, ["2026-06"], date(2026, 7, 16))

    def test_purchase_must_match_latest_positive_month(self) -> None:
        frame = sales_all_frame()
        frame["amount_2026_07_C"] = "50"
        frame.loc[0, "totalAmountC"] = "150"
        with self.assertRaisesRegex(ValueError, "latest positive sales month"):
            stage08.validate_recency_contract(
                frame,
                ["2026-06", "2026-07"],
                date(2026, 7, 16),
            )

    def test_csv_sha_is_from_same_bytes_as_parsed_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(sales_all_frame(), directory)
            original = path.read_bytes()
            frame, _monthly_columns, preflight = stage08.load_and_validate_csv(path)
            path.write_text("changed\n", encoding="utf-8")

        self.assertEqual(preflight.csv_sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(frame.at[0, "masterId"], "ABC123")


if __name__ == "__main__":
    unittest.main()
