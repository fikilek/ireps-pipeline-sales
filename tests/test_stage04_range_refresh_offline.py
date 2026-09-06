from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STAGE04B = load_module(
    "stage04b_range_refresh_offline",
    "scripts/04b_preflight_monthly_source_range.py",
)
STAGE04C = load_module(
    "stage04c_range_refresh_offline",
    "scripts/04c_upload_monthly_source_range_dev.py",
)


DATASETS = ("monthly", "monthly_lm", "monthly_lm_groups")


def base_report(*, operation: str, result: str, mode: str) -> dict[str, object]:
    report: dict[str, object] = {
        "stage": "04",
        "script": "04_upload_conlog_monthly_v3.py",
        "status": "PASS",
        "result": result,
        "operation": operation,
        "mode": mode,
        "targetProject": "ireps2",
        "confirmProject": "ireps2",
        "credentialProject": "ireps2",
        "providerId": "vpr_test",
        "lmPcode": "ZA5241",
        "month": "2026-06",
        "sourceStage": "03B",
        "sourceOrigin": "monthly_source",
        "sourceProvider": "contour",
        "provider": {
            "providerId": "vpr_test",
            "providerCode": "CONTOUR",
            "status": "active",
        },
        "sourceContract": {"fingerprint": "f" * 64},
        "reconciliation": {"amountTotalC": 1},
        "inputs": {},
    }
    return report


def refresh_preflight_report() -> dict[str, object]:
    report = base_report(operation="preflight-only", result="PREFLIGHT_OK", mode="refresh")
    report["preflight"] = {}
    for dataset in DATASETS:
        report["inputs"][dataset] = {"rows": 1}
        report["preflight"][dataset] = {
            "collection": f"collection_{dataset}",
            "documentsBefore": 1,
            "matchingDocuments": 0,
            "unchangedDocuments": 0,
            "documentsPlanned": 1,
            "documentsPlannedCreate": 0,
            "documentsPlannedUpdate": 1,
            "conflictCount": 0,
            "extraDocumentCount": 0,
        }
    return report


def refresh_upload_report() -> dict[str, object]:
    report = base_report(operation="execute-upload", result="UPLOAD_VERIFIED", mode="refresh")
    report["documentsCreated"] = {dataset: 0 for dataset in DATASETS}
    report["documentsUpdated"] = {dataset: 1 for dataset in DATASETS}
    report["documentsUnchanged"] = {dataset: 0 for dataset in DATASETS}
    report["verification"] = {
        dataset: {
            "expectedCount": 1,
            "finalCount": 1,
            "countVerification": "PASS",
            "fullDocumentVerification": "PASS",
        }
        for dataset in DATASETS
    }
    for dataset in DATASETS:
        report["inputs"][dataset] = {"rows": 1}
    return report


class Stage04RangeRefreshOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_json(self, name: str, payload: dict[str, object]) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def test_stage04b_accepts_refresh_preflight_accounting(self):
        path = self.write_json("preflight.json", refresh_preflight_report())
        result = STAGE04B.validate_stage04_report(
            path,
            project_id="ireps2",
            lm_pcode="ZA5241",
            month="2026-06",
            provider="contour",
            vending_provider_id="vpr_test",
            mode="refresh",
        )
        self.assertEqual(3, result["plannedDocuments"])
        for dataset in DATASETS:
            self.assertEqual(0, result["datasets"][dataset]["create"])
            self.assertEqual(1, result["datasets"][dataset]["update"])
            self.assertEqual(0, result["datasets"][dataset]["unchanged"])

    def test_stage04c_accepts_refresh_preflight_and_full_upload_verification(self):
        preflight_path = self.write_json("preflight.json", refresh_preflight_report())
        preflight = STAGE04C.validate_preflight_report(
            preflight_path,
            project_id="ireps2",
            lm_pcode="ZA5241",
            month="2026-06",
            provider="contour",
            vending_provider_id="vpr_test",
            mode="refresh",
        )
        self.assertEqual(3, preflight["totalPlanned"])

        upload_path = self.write_json("upload.json", refresh_upload_report())
        upload = STAGE04C.validate_upload_report(
            upload_path,
            project_id="ireps2",
            lm_pcode="ZA5241",
            month="2026-06",
            provider="contour",
            vending_provider_id="vpr_test",
            mode="refresh",
        )
        self.assertEqual(0, upload["totalCreated"])
        self.assertEqual(3, upload["totalUpdated"])
        self.assertEqual(3, upload["totalWrites"])

    def test_stage04c_pins_the_exact_patched_stage04_sha256(self):
        actual = STAGE04C.sha256_file(ROOT / "scripts" / "04_upload_conlog_monthly_v3.py")
        self.assertEqual(STAGE04C.EXPECTED_STAGE04_SHA256, actual)

    def test_range_helpers_expose_only_create_only_or_refresh_not_resume(self):
        for relative in (
            "scripts/04b_preflight_monthly_source_range.py",
            "scripts/04c_upload_monthly_source_range_dev.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('choices=("create-only", "refresh")', text)
            self.assertNotIn('choices=("create-only", "refresh", "resume")', text)


if __name__ == "__main__":
    unittest.main()
