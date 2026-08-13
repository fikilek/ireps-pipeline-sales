from __future__ import annotations

import csv
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "04_upload_conlog_monthly_v3.py"
)
SPEC = importlib.util.spec_from_file_location("stage04_offline", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
STAGE04 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STAGE04
SPEC.loader.exec_module(STAGE04)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def epoch_ms(value: str) -> int:
    parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=dt.timezone.utc
    )
    return int(parsed.timestamp() * 1000)


class Stage04OfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project_root_patch = mock.patch.object(STAGE04, "PROJECT_ROOT", self.root)
        self.project_root_patch.start()
        self.addCleanup(self.project_root_patch.stop)
        self.manifest_path, self.manifest = self._make_valid_build()

    @staticmethod
    def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path.read_bytes()

    def _write_manifest(self, manifest: dict[str, object]) -> Path:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.manifest_path

    def _make_valid_build(self) -> tuple[Path, dict[str, object]]:
        lm = "ZA7423"
        month = "2026-06"
        first_tx = "2026-06-01T10:00:00"
        last_tx = "2026-06-02T11:00:00"
        first_ms = epoch_ms(first_tx)
        last_ms = epoch_ms(last_tx)

        atomic_rows = [
            {
                "atomicId": "atomic-1",
                "vendingProviderId": STAGE04.CONLOG_VENDING_PROVIDER_ID,
                "lmPcode": lm,
                "meterNo": "METER01",
                "txAtISO": first_tx,
                "txAtMs": first_ms,
                "ym": month,
                "y": 2026,
                "m": 6,
                "amountTotalC": 100,
                "costC": 80,
                "vatC": 20,
                "currency": "ZAR",
                "sourceFileId": "source.csv",
                "sourceRow": 1,
                "ingestedAtISO": "2026-07-16T10:00:00Z",
                "ingestedAtMs": 1784196000000,
            },
            {
                "atomicId": "atomic-2",
                "vendingProviderId": STAGE04.CONLOG_VENDING_PROVIDER_ID,
                "lmPcode": lm,
                "meterNo": "METER01",
                "txAtISO": last_tx,
                "txAtMs": last_ms,
                "ym": month,
                "y": 2026,
                "m": 6,
                "amountTotalC": 200,
                "costC": 170,
                "vatC": 30,
                "currency": "ZAR",
                "sourceFileId": "source.csv",
                "sourceRow": 2,
                "ingestedAtISO": "2026-07-16T10:00:00Z",
                "ingestedAtMs": 1784196000000,
            },
        ]
        atomic_path = (
            self.root
            / "output"
            / "atomic"
            / f"atomic__conlog_prepaid_sales__{lm}__{month}__2.csv"
        )
        atomic_payload = self._write_csv(
            atomic_path, STAGE04.ATOMIC_COLUMNS, atomic_rows
        )

        monthly_rows = [
            {
                "docId": f"{lm}__METER01__{month}",
                "lmPcode": lm,
                "meterNo": "METER01",
                "ym": month,
                "y": 2026,
                "m": 6,
                "purchasesCount": 2,
                "amountTotalC": 300,
                "costC": 250,
                "vatC": 50,
                "firstPurchaseAtISO": first_tx + "Z",
                "lastPurchaseAtISO": last_tx + "Z",
                "firstPurchaseAtMs": first_ms,
                "lastPurchaseAtMs": last_ms,
                "salesGroupId": "GR1",
                "salesGroupLabel": "<=99.99",
            }
        ]
        monthly_lm_rows = [
            {
                "docId": f"{lm}__{month}",
                "lmPcode": lm,
                "ym": month,
                "y": 2026,
                "m": 6,
                "purchasesCount": 2,
                "metersCount": 1,
                "amountTotalC": 300,
                "costC": 250,
                "vatC": 50,
                "firstPurchaseAtISO": first_tx + "Z",
                "lastPurchaseAtISO": last_tx + "Z",
                "firstPurchaseAtMs": first_ms,
                "lastPurchaseAtMs": last_ms,
            }
        ]
        monthly_group_rows = [
            {
                "docId": f"{lm}__{month}__GR1",
                "lmPcode": lm,
                "ym": month,
                "y": 2026,
                "m": 6,
                "salesGroupId": "GR1",
                "salesGroupLabel": "<=99.99",
                "metersCount": 1,
                "purchasesCount": 2,
                "amountTotalC": 300,
                "costC": 250,
                "vatC": 50,
                "firstPurchaseAtISO": first_tx + "Z",
                "lastPurchaseAtISO": last_tx + "Z",
                "firstPurchaseAtMs": first_ms,
                "lastPurchaseAtMs": last_ms,
            }
        ]

        output_rows = {
            "monthly": monthly_rows,
            "monthly_lm": monthly_lm_rows,
            "monthly_lm_groups": monthly_group_rows,
        }
        outputs: list[dict[str, object]] = []
        for dataset in STAGE04.COLLECTIONS:
            directory, template = STAGE04.APPROVED_OUTPUT_LOCATIONS[dataset]
            output_path = (
                self.root
                / "output"
                / directory
                / template.format(month=month)
            )
            payload = self._write_csv(
                output_path,
                STAGE04.EXPECTED_COLUMNS[dataset],
                output_rows[dataset],
            )
            outputs.append(
                {
                    "dataset": dataset,
                    "existingSha256": "",
                    "existingState": "MISSING",
                    "filename": output_path.name,
                    "month": month,
                    "path": str(output_path.resolve()),
                    "rows": len(output_rows[dataset]),
                    "sha256": sha256_bytes(payload),
                }
            )

        manifest: dict[str, object] = {
            "atomicFile": {
                "filename": atomic_path.name,
                "month": month,
                "path": str(atomic_path.resolve()),
                "rows": 2,
                "sha256": sha256_bytes(atomic_payload),
            },
            "atomicRows": 2,
            "atomicUniqueMeters": 1,
            "finishedAt": "2026-07-16T10:00:02Z",
            "lmPcode": lm,
            "month": month,
            "monthlyLmGroupRows": 1,
            "monthlyLmRows": 1,
            "monthlyRows": 1,
            "operation": STAGE04.STAGE03_OPERATION,
            "outputs": outputs,
            "reconciliation": [
                {
                    "amountTotalC": 300,
                    "costC": 250,
                    "lmPcode": lm,
                    "metersCount": 1,
                    "month": month,
                    "purchasesCount": 2,
                    "vatC": 50,
                }
            ],
            "result": "BUILD_WRITTEN",
            "script": STAGE04.STAGE03_SCRIPT,
            "stage": "03",
            "startedAt": "2026-07-16T10:00:00Z",
            "status": "PASS",
            "writeSummary": {"unchanged": 0, "written": 3},
        }
        self.manifest_path = (
            self.root
            / "output"
            / "logs"
            / "monthly_build"
            / f"stage03_monthly_build__{lm}__{month}__20260716T100000Z.json"
        )
        self._write_manifest(manifest)
        return self.manifest_path, manifest

    def _load_validated(self):
        manifest, selected, manifest_sha = STAGE04.load_manifest_outputs(
            self.manifest_path,
            expected_lm_pcode="ZA7423",
            expected_month="2026-06",
        )
        source_origin, provider = STAGE04.manifest_source_contract(manifest)
        datasets = {
            dataset: STAGE04.validate_dataset(
                dataset,
                selected[dataset],
                expected_lm_pcode="ZA7423",
                expected_month="2026-06",
                source_origin=source_origin,
                expected_provider=provider,
            )
            for dataset in STAGE04.COLLECTIONS
        }
        reconciliation = STAGE04.reconcile_datasets(
            datasets,
            source_origin=source_origin,
        )
        return manifest, selected, manifest_sha, datasets, reconciliation

    def test_valid_stage03_manifest_and_evidence_pass(self) -> None:
        manifest, _, manifest_sha, datasets, reconciliation = self._load_validated()
        source_origin, _provider = STAGE04.manifest_source_contract(manifest)
        evidence = STAGE04.validate_manifest_evidence(
            manifest,
            datasets,
            reconciliation,
            expected_lm_pcode="ZA7423",
            expected_month="2026-06",
            source_origin=source_origin,
        )
        self.assertEqual("PASS", evidence["verification"])
        self.assertEqual(2, evidence["atomic"]["purchasesCount"])
        self.assertRegex(manifest_sha, r"^[0-9a-f]{64}$")

    def test_manifest_rejects_extra_output_entry(self) -> None:
        self.manifest["outputs"].append(dict(self.manifest["outputs"][0]))
        self._write_manifest(self.manifest)
        with self.assertRaisesRegex(ValueError, "exactly three"):
            STAGE04.load_manifest_outputs(
                self.manifest_path,
                expected_lm_pcode="ZA7423",
                expected_month="2026-06",
            )

    def test_manifest_rejects_wrong_identity(self) -> None:
        for field, value in (
            ("script", "03_wrong.py"),
            ("operation", "preflight-only"),
            ("status", "FAIL"),
            ("result", "PREFLIGHT_OK"),
        ):
            with self.subTest(field=field):
                changed = dict(self.manifest)
                changed[field] = value
                self._write_manifest(changed)
                with self.assertRaises(ValueError):
                    STAGE04.load_manifest_outputs(
                        self.manifest_path,
                        expected_lm_pcode="ZA7423",
                        expected_month="2026-06",
                    )

    def test_manifest_rejects_reconciliation_not_proven_by_csvs(self) -> None:
        manifest, _, _, datasets, reconciliation = self._load_validated()
        manifest["reconciliation"][0]["amountTotalC"] = 301
        source_origin, _provider = STAGE04.manifest_source_contract(manifest)
        with self.assertRaisesRegex(ValueError, "reconciliation amountTotalC mismatch"):
            STAGE04.validate_manifest_evidence(
                manifest,
                datasets,
                reconciliation,
                expected_lm_pcode="ZA7423",
                expected_month="2026-06",
                source_origin=source_origin,
            )

    def test_csv_sha_and_parser_use_one_byte_snapshot(self) -> None:
        _, selected, _, _, _ = self._load_validated()
        entry = selected["monthly"]
        original_reader = STAGE04.read_csv_robust_bytes

        def mutate_after_snapshot(payload: bytes, path: Path):
            path.write_bytes(b"changed-after-snapshot\n")
            return original_reader(payload, path)

        with mock.patch.object(
            STAGE04,
            "read_csv_robust_bytes",
            side_effect=mutate_after_snapshot,
        ):
            source_origin, provider = STAGE04.manifest_source_contract(self.manifest)
            dataset = STAGE04.validate_dataset(
                "monthly",
                entry,
                expected_lm_pcode="ZA7423",
                expected_month="2026-06",
                source_origin=source_origin,
                expected_provider=provider,
            )

        self.assertEqual(entry["sha256"], dataset.file_sha256)
        self.assertEqual(1, len(dataset.frame))
        self.assertNotEqual(entry["sha256"], sha256_bytes(Path(entry["path"]).read_bytes()))

    def test_strict_firestore_comparison_rejects_bool_float_and_shape_drift(self) -> None:
        expected = {"lmPcode": "ZA7423", "purchasesCount": 1}
        self.assertEqual([], STAGE04.strict_document_differences(dict(expected), expected))
        self.assertEqual(
            ["purchasesCount"],
            STAGE04.strict_document_differences(
                {"lmPcode": "ZA7423", "purchasesCount": True}, expected
            ),
        )
        self.assertEqual(
            ["purchasesCount"],
            STAGE04.strict_document_differences(
                {"lmPcode": "ZA7423", "purchasesCount": 1.0}, expected
            ),
        )
        self.assertEqual(
            ["unexpected"],
            STAGE04.strict_document_differences(
                {**expected, "unexpected": "value"}, expected
            ),
        )


class Stage04BatchGovernanceTests(unittest.TestCase):
    def test_sample_verification_uses_bulk_get_all(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "04_upload_conlog_monthly_v3.py").read_text(encoding="utf-8")
        self.assertIn("db.get_all(refs)", source)
        self.assertIn("BATCH_SIZE = 400", source)



if __name__ == "__main__":
    unittest.main()
