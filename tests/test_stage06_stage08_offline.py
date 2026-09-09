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
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


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
refresh = load_script("stage08_refresh_offline", "sales_pipeline_sales_all_refresh.py")
import sales_pipeline_monthly_source_support as monthly_support  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> str:
    payload = b"".join(
        (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        for row in rows
    )
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def synthetic_commercial_row(*, meter: str = "ABC123") -> dict:
    return {
        "lmPcode": "ZA7423",
        "meterNo": meter,
        "meterNoNormalized": meter,
        "customerNo": "C1",
        "accountNo": "A1",
        "accountNumber": "A1",
        "customerName": "CUSTOMER",
        "customerSurname": "SURNAME",
        "addressLine1": "42 MCKENZIE",
        "addressLine2": "STREET",
        "town": "DUNDEE",
        "postalAddress1": "PO BOX 1",
        "postalAddress2": "",
        "postalAddressTown": "DUNDEE",
        "standNumber": "STAND-42",
        "monthlySalesC": {"2026-06": 100},
        "monthlyUnits": {"2026-06": "5"},
        "totalSalesC": 100,
        "totalUnits": "5",
        "hasUsableGps": False,
        "elmAccountMatched": False,
        "erfCandidates": [],
        "erfNumbers": [],
        "missingErfNumbers": [],
        "elmSourceRows": [],
    }


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

    def test_atomic_enrichment_integration_preserves_existing_fields_and_binds_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commercial_path = root / "commercial.jsonl"
            source_row = synthetic_commercial_row()
            commercial_sha = write_jsonl(commercial_path, [source_row])
            original_source_bytes = commercial_path.read_bytes()

            base = pd.DataFrame(
                [
                    {
                        "masterId": "ABC123",
                        "meterNo": "ABC123",
                        "meterNoNormalized": "ABC123",
                        "provider": "conlog",
                        "customerNo": "C1",
                        "accountNo": "A1",
                        "totalAmountC": 100,
                        "lastPurchaseAtISO": "2026-06-30T10:00:00+00:00",
                        "daysSinceLastPurchase": 16,
                        "amount_2026_06_C": 100,
                    }
                ],
                columns=stage06.BASE_OUTPUT_COLUMNS + ["amount_2026_06_C"],
            )
            original = base.copy(deep=True)
            output_path = root / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.csv"

            enriched, evidence, contract = stage06.enrich_atomic_output(
                base,
                lm_pcode="ZA7423",
                commercial_source_path=commercial_path,
                expected_commercial_sha256=commercial_sha,
                output_path=output_path,
            )

            pd.testing.assert_frame_equal(
                enriched[stage06.BASE_OUTPUT_COLUMNS + ["amount_2026_06_C"]],
                original,
                check_dtype=False,
            )
            self.assertEqual(
                enriched.loc[0, ["strNo", "strName", "strType"]].tolist(),
                ["42", "Mckenzie", "Street"],
            )
            self.assertEqual(evidence["role"], "ADDRESS_EVIDENCE_ONLY")
            self.assertEqual(evidence["salesTruthAuthority"], "ATOMIC")
            self.assertEqual(contract["rawAddressMutationCount"], 0)
            report_path = root / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.address_enrichment.json"
            self.assertTrue(report_path.is_file())
            self.assertEqual(contract["reportSha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["rawAddressMutationCount"], 0)
            self.assertEqual(commercial_path.read_bytes(), original_source_bytes)

    def test_atomic_enrichment_identity_join_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commercial_path = root / "commercial.jsonl"
            commercial_sha = write_jsonl(
                commercial_path,
                [synthetic_commercial_row(), synthetic_commercial_row(meter="EXTRA999")],
            )
            base = pd.DataFrame(
                [
                    {
                        "masterId": "ABC123",
                        "meterNo": "ABC123",
                        "meterNoNormalized": "ABC123",
                        "provider": "conlog",
                        "customerNo": "C1",
                        "accountNo": "A1",
                        "totalAmountC": 100,
                        "lastPurchaseAtISO": "2026-06-30T10:00:00+00:00",
                        "daysSinceLastPurchase": 16,
                        "amount_2026_06_C": 100,
                    }
                ],
                columns=stage06.BASE_OUTPUT_COLUMNS + ["amount_2026_06_C"],
            )
            with self.assertRaisesRegex(ValueError, "population mismatch"):
                stage06.enrich_atomic_output(
                    base,
                    lm_pcode="ZA7423",
                    commercial_source_path=commercial_path,
                    expected_commercial_sha256=commercial_sha,
                    output_path=root / "out.csv",
                )

    def test_monthly_source_stage06_integration_preserves_raw_fields_and_binds_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            commercial_path = root / "commercial.jsonl"
            source_row = synthetic_commercial_row()
            commercial_sha = write_jsonl(commercial_path, [source_row])
            original_source_bytes = commercial_path.read_bytes()

            monthly_path = monthly_dir / "monthly__FULL__2026-06__from_monthly_source.csv"
            pd.DataFrame(
                [
                    {
                        "lmPcode": "ZA7423",
                        "meterNo": "ABC123",
                        "ym": "2026-06",
                        "provider": "contour",
                        "amountTotalC": "100",
                        "unitsTotal": "5",
                        "sourceOrigin": "monthly_source",
                    }
                ]
            ).to_csv(monthly_path, index=False, encoding="utf-8")

            master_path = root / "meter_master.csv"
            master_manifest_path = root / "meter_master.manifest.json"
            monthly_support.build_stage05_monthly_source(
                lm_pcode="ZA7423",
                provider="contour",
                meter_type="electricity",
                from_month="2026-06",
                to_month="2026-06",
                commercial_source=commercial_path,
                expected_commercial_sha256=commercial_sha,
                monthly_dir=monthly_dir,
                output_path=master_path,
                manifest_path=master_manifest_path,
                source_run_id="SYNTHETIC_RUN",
            )

            sales_path = root / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.csv"
            sales_manifest_path = root / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-06.manifest.json"
            manifest = monthly_support.build_stage06_monthly_source(
                lm_pcode="ZA7423",
                provider="contour",
                from_month="2026-06",
                to_month="2026-06",
                master_path=master_path,
                master_manifest_path=master_manifest_path,
                commercial_source=commercial_path,
                expected_commercial_sha256=commercial_sha,
                monthly_dir=monthly_dir,
                output_path=sales_path,
                manifest_path=sales_manifest_path,
            )

            output = pd.read_csv(sales_path, dtype=str, encoding="utf-8-sig").fillna("")
            row = output.to_dict("records")[0]
            for field in (
                "addressLine1",
                "addressLine2",
                "town",
                "postalAddress1",
                "postalAddress2",
                "postalAddressTown",
                "standNumber",
            ):
                self.assertEqual(row[field], str(source_row.get(field) or ""))
            self.assertEqual((row["strNo"], row["strName"], row["strType"]), ("42", "Mckenzie", "Street"))
            self.assertEqual(row["totalAmountC"], "100")
            self.assertEqual(row["provider"], "contour")
            address_contract = manifest["outputContract"]["addressEnrichment"]
            self.assertEqual(address_contract["rawAddressMutationCount"], 0)
            report_path = sales_path.with_suffix(".address_enrichment.json")
            self.assertEqual(address_contract["reportSha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["rawAddressMutationCount"], 0)
            self.assertEqual(commercial_path.read_bytes(), original_source_bytes)

    def test_monthly_source_stage06_advances_dense_coverage_period_through_zero_months(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            commercial_path = root / "commercial.jsonl"
            source_row = synthetic_commercial_row()
            source_row["salesPeriodFrom"] = "2026-06"
            source_row["salesPeriodTo"] = "2026-06"
            commercial_sha = write_jsonl(commercial_path, [source_row])

            for month, amount, units in (
                ("2026-06", "100", "5"),
                ("2026-07", "0", "0"),
                ("2026-08", "0", "0"),
            ):
                pd.DataFrame(
                    [
                        {
                            "lmPcode": "ZA7423",
                            "meterNo": "ABC123",
                            "ym": month,
                            "provider": "contour",
                            "amountTotalC": amount,
                            "unitsTotal": units,
                            "sourceOrigin": "monthly_source",
                        }
                    ]
                ).to_csv(
                    monthly_dir / f"monthly__FULL__{month}__from_monthly_source.csv",
                    index=False,
                    encoding="utf-8",
                )

            master_path = root / "meter_master.csv"
            master_manifest_path = root / "meter_master.manifest.json"
            monthly_support.build_stage05_monthly_source(
                lm_pcode="ZA7423",
                provider="contour",
                meter_type="electricity",
                from_month="2026-06",
                to_month="2026-08",
                commercial_source=commercial_path,
                expected_commercial_sha256=commercial_sha,
                monthly_dir=monthly_dir,
                output_path=master_path,
                manifest_path=master_manifest_path,
                source_run_id="SYNTHETIC_RUN",
            )

            sales_path = root / "sales_all_meters__ZA7423__FULL__2026-06_to_2026-08.csv"
            monthly_support.build_stage06_monthly_source(
                lm_pcode="ZA7423",
                provider="contour",
                from_month="2026-06",
                to_month="2026-08",
                master_path=master_path,
                master_manifest_path=master_manifest_path,
                commercial_source=commercial_path,
                expected_commercial_sha256=commercial_sha,
                monthly_dir=monthly_dir,
                output_path=sales_path,
                manifest_path=sales_path.with_suffix(".manifest.json"),
            )

            output = pd.read_csv(sales_path, dtype=str, encoding="utf-8-sig").fillna("")
            row = output.to_dict("records")[0]
            self.assertEqual(row["amount_2026_07_C"], "0")
            self.assertEqual(row["amount_2026_08_C"], "0")
            self.assertEqual(row["units_2026_07"], "0")
            self.assertEqual(row["units_2026_08"], "0")
            self.assertEqual(row["salesPeriodFrom"], "2026-06")
            self.assertEqual(row["salesPeriodTo"], "2026-08")

    def test_monthly_source_stage06_identity_join_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            commercial_path = root / "commercial.jsonl"
            commercial_sha = write_jsonl(commercial_path, [synthetic_commercial_row()])
            monthly_path = monthly_dir / "monthly__FULL__2026-06__from_monthly_source.csv"
            pd.DataFrame(
                [
                    {
                        "lmPcode": "ZA7423",
                        "meterNo": "ABC123",
                        "ym": "2026-06",
                        "provider": "contour",
                        "amountTotalC": "100",
                        "unitsTotal": "5",
                        "sourceOrigin": "monthly_source",
                    }
                ]
            ).to_csv(monthly_path, index=False, encoding="utf-8")

            master_path = root / "meter_master.csv"
            master_manifest_path = root / "meter_master.manifest.json"
            monthly_support.build_stage05_monthly_source(
                lm_pcode="ZA7423",
                provider="contour",
                meter_type="electricity",
                from_month="2026-06",
                to_month="2026-06",
                commercial_source=commercial_path,
                expected_commercial_sha256=commercial_sha,
                monthly_dir=monthly_dir,
                output_path=master_path,
                manifest_path=master_manifest_path,
                source_run_id="SYNTHETIC_RUN",
            )

            master = pd.read_csv(master_path, dtype=str, encoding="utf-8-sig").fillna("")
            extra = master.iloc[0].copy()
            extra["masterId"] = "EXTRA999"
            extra["meterNoRaw"] = "EXTRA999"
            extra["meterNoNormalized"] = "EXTRA999"
            extra["salesId"] = "EXTRA999"
            master = pd.concat([master, pd.DataFrame([extra])], ignore_index=True)
            master.to_csv(master_path, index=False, lineterminator="\n", encoding="utf-8")

            stage05_manifest = json.loads(master_manifest_path.read_text(encoding="utf-8"))
            stage05_manifest["outputContract"]["rows"] = 2
            stage05_manifest["outputContract"]["sha256"] = hashlib.sha256(master_path.read_bytes()).hexdigest()
            stage05_manifest["stats"]["totalMasterRows"] = 2
            fingerprint = {
                "sourceContract": stage05_manifest["sourceContract"],
                "outputContract": {
                    "filename": stage05_manifest["outputContract"]["filename"],
                    "rows": stage05_manifest["outputContract"]["rows"],
                    "columns": stage05_manifest["outputContract"]["columns"],
                    "sha256": stage05_manifest["outputContract"]["sha256"],
                },
                "stats": stage05_manifest["stats"],
            }
            stage05_manifest["buildFingerprint"] = monthly_support.canonical_sha256(fingerprint)
            master_manifest_path.write_text(
                json.dumps(stage05_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Meter Master/commercial population mismatch"):
                monthly_support.build_stage06_monthly_source(
                    lm_pcode="ZA7423",
                    provider="contour",
                    from_month="2026-06",
                    to_month="2026-06",
                    master_path=master_path,
                    master_manifest_path=master_manifest_path,
                    commercial_source=commercial_path,
                    expected_commercial_sha256=commercial_sha,
                    monthly_dir=monthly_dir,
                    output_path=root / "sales.csv",
                    manifest_path=root / "sales.manifest.json",
                )


def sales_all_frame(*, provider: str = "conlog", enriched: bool = False) -> pd.DataFrame:
    row = {
        "masterId": "ABC123",
        "meterNo": "ABC123",
        "meterNoNormalized": "ABC123",
        "provider": provider,
        "customerNo": "C1",
        "accountNo": "A1",
        "totalAmountC": "100",
        "lastPurchaseAtISO": "2026-06-30T10:00:00Z",
        "daysSinceLastPurchase": "16",
    }
    columns = list(stage08.BASE_COLUMNS)
    if enriched:
        row.update({"strNo": "42", "strName": "Mckenzie", "strType": "Street"})
        columns += list(stage08.ADDRESS_STAGING_COLUMNS)
    row["amount_2026_06_C"] = "100"
    columns.append("amount_2026_06_C")
    return pd.DataFrame([row], columns=columns)



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
                preflight_only=False,
            )
            snapshot = stage08.read_json_snapshot(manifest_path, "Stage 06 manifest")
            evidence = stage08.validate_stage06_manifest(
                config08,
                preflight,
                list(frame.columns),
                snapshot,
            )

        self.assertEqual(evidence["asOfDate"], "2026-07-16")

    def test_enriched_legacy_csv_builds_nested_adr_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(sales_all_frame(enriched=True), directory)
            frame, monthly_columns, preflight = stage08.load_and_validate_csv(path)
        self.assertTrue(preflight.address_enrichment_enabled)
        document = stage08.build_document(frame.to_dict("records")[0], monthly_columns)
        self.assertEqual(
            document["adr"],
            {"strNo": "42", "strName": "Mckenzie", "strType": "Street"},
        )
        self.assertNotIn("strNo", document)
        self.assertNotIn("strName", document)
        self.assertNotIn("strType", document)

    def test_unresolved_enriched_legacy_csv_builds_canonical_adr(self) -> None:
        frame = sales_all_frame(enriched=True)
        frame.loc[0, ["strNo", "strName", "strType"]] = ["", "", "-"]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(frame, directory)
            parsed, monthly_columns, preflight = stage08.load_and_validate_csv(path)
        self.assertEqual(preflight.address_unresolved_rows, 1)
        document = stage08.build_document(parsed.to_dict("records")[0], monthly_columns)
        self.assertEqual(document["adr"], {"strNo": "", "strName": "", "strType": "-"})

    def test_partial_address_staging_columns_are_rejected(self) -> None:
        frame = sales_all_frame()
        frame.insert(len(stage08.BASE_COLUMNS), "strNo", "42")
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(frame, directory)
            with self.assertRaisesRegex(ValueError, "present together"):
                stage08.load_and_validate_csv(path)

    def test_compare_existing_enriched_document_requires_exact_adr(self) -> None:
        frame = sales_all_frame(enriched=True)
        row = frame.to_dict("records")[0]
        expected = stage08.build_document(row, ["amount_2026_06_C"] )
        actual = dict(expected)
        actual["master"] = {"id": "ABC123", "visibility": "VISIBLE"}
        self.assertEqual(stage08.compare_existing_document(actual, expected), [])
        bad = dict(actual)
        bad["adr"] = {**actual["adr"], "extra": "x"}
        self.assertTrue(any("adr keys differ" in item for item in stage08.compare_existing_document(bad, expected)))

    def test_refresh_updates_adr_as_one_owned_map_and_preserves_other_roots(self) -> None:
        expected = {
            "master": {"id": "ABC123", "visibility": "INVISIBLE"},
            "meterNoNormalized": "ABC123",
            "provider": "contour",
            "lmPcode": "ZA5241",
            "adr": {"strNo": "42", "strName": "Mckenzie", "strType": "Street"},
        }
        existing = {
            **expected,
            "master": {"id": "ABC123", "visibility": "VISIBLE"},
            "adr": {"strNo": "41", "strName": "Mckenzie", "strType": "Street"},
            "tbRefs": [{"batchId": "TB1"}],
        }
        self.assertIsNone(refresh._conflict(existing, expected))
        updates = refresh._updates(existing, expected)
        self.assertEqual(updates, {"adr": expected["adr"]})
        self.assertEqual(refresh._preserved_projection(existing)["tbRefs"], [{"batchId": "TB1"}])
        self.assertEqual(refresh._preserved_projection(existing)["master"]["visibility"], "VISIBLE")

    def test_refresh_rejects_malformed_existing_adr_and_root_flat_address(self) -> None:
        expected = {
            "master": {"id": "ABC123", "visibility": "INVISIBLE"},
            "meterNoNormalized": "ABC123",
            "provider": "contour",
            "lmPcode": "ZA5241",
            "adr": {"strNo": "42", "strName": "Mckenzie", "strType": "Street"},
        }
        malformed = {**expected, "adr": {"strNo": "42", "strName": "Mckenzie", "strType": "Street", "extra": "x"}}
        self.assertIn("adr has unexpected/missing keys", refresh._conflict(malformed, expected))
        flat = {**expected, "strNo": "42"}
        self.assertIn("root strNo/strName/strType is prohibited", refresh._conflict(flat, expected))

    def test_rich_stage06_contract_projects_adr_for_initial_load_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {column: "" for column in refresh.BASE_COLUMNS + refresh.RICH_COLUMNS + refresh.ADDRESS_STAGING_COLUMNS}
            row.update(
                {
                    "masterId": "ABC123",
                    "meterNo": "ABC123",
                    "meterNoNormalized": "ABC123",
                    "provider": "contour",
                    "customerNo": "C1",
                    "accountNo": "A1",
                    "totalAmountC": "100",
                    "lmPcode": "ZA5241",
                    "accountNumber": "A1",
                    "totalSalesC": "100",
                    "totalUnits": "5",
                    "strNo": "42",
                    "strName": "Mckenzie",
                    "strType": "Street",
                }
            )
            for field in refresh.COMMERCIAL_JSON_FIELDS:
                row[field] = "[]"
            row["hasUsableGps"] = "false"
            row["elmAccountMatched"] = "false"
            row["amount_2026_06_C"] = "100"
            row["units_2026_06"] = "5"
            columns = (
                refresh.BASE_COLUMNS
                + refresh.RICH_COLUMNS
                + refresh.ADDRESS_STAGING_COLUMNS
                + ["amount_2026_06_C", "units_2026_06"]
            )
            frame = pd.DataFrame([row], columns=columns)
            csv_path = root / "sales_all_meters__ZA5241__FULL__2026-06_to_2026-06.csv"
            frame.to_csv(csv_path, index=False, encoding="utf-8")
            report_path = csv_path.with_suffix(".address_enrichment.json")
            report_path.write_text("{}\n", encoding="utf-8")
            report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
            address_contract = {
                "enabled": True,
                "stagingColumns": list(refresh.ADDRESS_STAGING_COLUMNS),
                "firestoreProjection": "adr",
                "enrichedRows": 1,
                "unresolvedRows": 0,
                "reasonCounts": {},
                "rawAddressMutationCount": 0,
                "fabricatedSpatialRelationshipCount": 0,
                "reportFilename": report_path.name,
                "reportSha256": report_sha,
            }
            source = {
                "sourceOrigin": "monthly_source",
                "sourceRunId": "RUN1",
                "lmPcode": "ZA5241",
                "fromMonth": "2026-06",
                "toMonth": "2026-06",
                "includedMonths": ["2026-06"],
                "provider": "contour",
                "recencyFactsAvailable": False,
                "visibilityOwnership": "OPERATIONAL_WRITERS_ONLY",
                "stage05Manifest": {},
                "meterMaster": {},
                "commercialSource": {},
                "monthlyInputs": [],
                "atomicFactsFabricated": 0,
            }
            output = {
                "filename": csv_path.name,
                "rows": 1,
                "columns": columns,
                "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
                "documentIdsSha256": refresh.canonical_sha256(["ABC123"]),
                "months": ["2026-06"],
                "monthlyColumns": ["amount_2026_06_C"],
                "monthlyUnitColumns": ["units_2026_06"],
                "provider": "contour",
                "totalAmountC": 100,
                "totalUnits": "5",
                "visibilityColumn": "ABSENT",
                "addressEnrichment": address_contract,
            }
            stats = {
                "masterRows": 1,
                "monthlyRowsMerged": 1,
                "metersWithSales": 1,
                "metersWithoutSales": 0,
                "totalOutputRows": 1,
                "totalUnits": "5",
                "addressEnrichedRows": 1,
                "addressUnresolvedRows": 0,
            }
            manifest = {
                "schemaVersion": 2,
                "stage": "06",
                "script": "06_build_sales_all_meters.py",
                "status": "PASS",
                "result": "BUILD_WRITTEN",
                "sourceContract": source,
                "outputContract": output,
                "stats": stats,
            }
            manifest["buildFingerprint"] = refresh._manifest_fingerprint(manifest)
            manifest_path = csv_path.with_suffix(".manifest.json")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            rows, evidence = refresh.load_and_validate(csv_path, manifest_path)

        self.assertEqual(rows[0]["expected"]["adr"], {"strNo": "42", "strName": "Mckenzie", "strType": "Street"})
        self.assertNotIn("strNo", rows[0]["expected"] )
        self.assertEqual(evidence["addressEnrichment"]["enrichedRows"], 1)

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


class Stage08RefreshGovernanceIntegrationTests(unittest.TestCase):
    def test_refresh_400_wave_partition_is_locked(self) -> None:
        waves = list(refresh._chunks(list(range(10216))))
        self.assertEqual([len(wave) for wave in waves], [400] * 25 + [216])

    def test_refresh_source_has_no_per_document_transaction_fallback(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "sales_pipeline_sales_all_refresh.py").read_text(encoding="utf-8")
        self.assertIn("FIRESTORE_BATCH_SIZE = 400", source)
        self.assertNotIn("db.transaction(", source)
        self.assertNotIn("firestore.transactional", source)
        self.assertIn('"perDocumentFallback": False', source)

    def test_refresh_batch_updates_use_last_update_preconditions(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "sales_pipeline_sales_all_refresh.py").read_text(encoding="utf-8")
        self.assertIn("LastUpdateOption", source)
        self.assertIn("batch.update(", source)
        self.assertIn("batch.create(", source)


if __name__ == "__main__":
    unittest.main()
