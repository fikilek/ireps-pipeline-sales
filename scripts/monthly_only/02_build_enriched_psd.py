#!/usr/bin/env python3
"""
Stage M02: build the corrected Endumeni enriched PSD while preserving every END meter.

Primary source:
    END 2026-07-29.xlsx

One-to-one location linkage:
    END.AccountNumber = CsmValRollB.OWNER_ACCOUNT_NO
    CsmValRollB.GIS_KEY + one comparison-only trailing zero
        = B04 sg.prclKey after removing the K241 prefix

The PSD root shape and historic field names remain unchanged.
No geometry is included.
Unmatched END meters remain in the output.
No Firebase or Firestore access is performed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

from lib.psd_model import (
    clean_text,
    enrich_end_rows,
    read_bridge_workbook,
    read_end_workbook,
    read_erf_documents,
)
from lib.psd_outputs import (
    build_candidate_csv,
    build_jsonl,
    build_main_csv,
    build_unmatched_csv,
    json_bytes,
    output_paths,
    sha256_file,
    write_outputs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "output"
    / "monthly_only"
    / "ZA5241"
    / "02_enriched_psd_sg_fixed"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the corrected enriched PSD by linking END accounts to "
            "valuation-roll GIS keys and then to exact B04 ERF documents."
        )
    )
    parser.add_argument("--end-workbook", type=Path, required=True)
    parser.add_argument("--bridge-workbook", type=Path, required=True)
    parser.add_argument("--erf-documents", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--source-erf-run-id", required=True)
    parser.add_argument("--expected-end-sha256", required=True)
    parser.add_argument("--expected-bridge-sha256", required=True)
    parser.add_argument("--expected-erf-documents-sha256", required=True)
    parser.add_argument(
        "--expected-end-record-count",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--expected-bridge-record-count",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--expected-erf-record-count",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--expected-exact-gps-count",
        type=int,
        required=True,
    )
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def require_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(
                f"Required source file not found: {path}"
            )


def validate_sha256(path: Path, expected: str, label: str) -> str:
    expected_clean = clean_text(expected).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_clean):
        raise ValueError(
            f"{label} expected SHA-256 must be "
            "64 hexadecimal characters"
        )
    actual = sha256_file(path)
    if actual.lower() != expected_clean:
        raise ValueError(
            f"{label} SHA-256 mismatch. "
            f"Expected {expected_clean}, found {actual}."
        )
    return actual


def main() -> None:
    args = parse_args()

    end_path = args.end_workbook.resolve()
    bridge_path = args.bridge_workbook.resolve()
    erf_documents_path = args.erf_documents.resolve()
    output_dir = args.output_dir.resolve()
    lm_pcode = clean_text(args.lm_pcode).upper()
    source_erf_run_id = clean_text(args.source_erf_run_id)

    require_files([end_path, bridge_path, erf_documents_path])
    if not lm_pcode or not source_erf_run_id:
        raise ValueError(
            "LM pCode and source ERF run ID cannot be blank"
        )
    if args.progress_every < 0:
        raise ValueError("--progress-every cannot be negative")
    if args.expected_exact_gps_count <= 0:
        raise ValueError(
            "--expected-exact-gps-count must be positive"
        )

    end_sha = validate_sha256(
        end_path,
        args.expected_end_sha256,
        "END workbook",
    )
    bridge_sha = validate_sha256(
        bridge_path,
        args.expected_bridge_sha256,
        "Valuation bridge workbook",
    )
    erf_documents_sha = validate_sha256(
        erf_documents_path,
        args.expected_erf_documents_sha256,
        "ERF documents JSONL",
    )

    mode = "preflight-only" if args.preflight_only else "write"

    print("")
    print("============================================================")
    print("STAGE M02 — CORRECTED ENDUMENI ENRICHED PSD")
    print("============================================================")
    print(f"  primary END workbook:       {end_path}")
    print(f"  account/SG bridge:          {bridge_path}")
    print(f"  ERF documents JSONL:        {erf_documents_path}")
    print(f"  expected LM:                {lm_pcode}")
    print(f"  source ERF run ID:          {source_erf_run_id}")
    print(f"  output directory:           {output_dir}")
    print(f"  mode:                       {mode}")
    print("  location join:              Account -> GIS_KEY -> sg.prclKey")
    print("  ELM workbook used:          NO")
    print("  geometry included:          NO")
    print("  Firebase / Firestore:       NONE")

    print("\n[1/4] Reading primary END workbook")
    end_rows, end_stats = read_end_workbook(
        end_path,
        args.expected_end_record_count,
    )

    print("[2/4] Reading valuation-roll account-to-GIS bridge")
    bridge_by_account, bridge_stats = read_bridge_workbook(
        bridge_path,
        args.expected_bridge_record_count,
    )

    print("[3/4] Reading authoritative B04 ERF documents")
    lookup_by_comparison_key, erf_stats = read_erf_documents(
        erf_documents_path,
        lm_pcode,
        args.expected_erf_record_count,
        args.progress_every,
    )

    print("[4/4] Enriching every END meter")
    enriched, merge_stats = enrich_end_rows(
        end_rows,
        bridge_by_account,
        lookup_by_comparison_key,
        args.progress_every,
    )

    if len(enriched) != len(end_rows):
        raise ValueError("Primary END row preservation failed")
    if len(
        {record.identity["MeterNumber"] for record in enriched}
    ) != len(enriched):
        raise ValueError("MeterNumber uniqueness was not preserved")
    if merge_stats["metersWithUsableGps"] != args.expected_exact_gps_count:
        raise ValueError(
            "Exact-GPS count mismatch. "
            f"Expected {args.expected_exact_gps_count:,}, "
            f"found {merge_stats['metersWithUsableGps']:,}."
        )
    if merge_stats["endRowsWithMultipleGpsCandidates"] != 0:
        raise ValueError(
            "One-to-one gate failed: one or more meters have "
            "multiple GPS candidates"
        )

    print("\n[SOURCE SUMMARY]")
    print(
        f"  END records:                           "
        f"{end_stats['records']:,}"
    )
    print(
        f"  END unique MeterNumbers:               "
        f"{end_stats['uniqueMeterNumbers']:,}"
    )
    print(
        f"  END blank AccountNumbers:              "
        f"{end_stats['blankAccountNumbers']:,}"
    )
    print(
        f"  bridge records:                        "
        f"{bridge_stats['records']:,}"
    )
    print(
        f"  bridge unique accounts:                "
        f"{bridge_stats['uniqueAccounts']:,}"
    )
    print(
        f"  bridge accounts with GIS key:          "
        f"{bridge_stats['accountsWithGisKey']:,}"
    )
    print(
        f"  bridge accounts with multiple GIS:     "
        f"{bridge_stats['accountsWithMultipleDistinctGisKeys']:,}"
    )
    print(
        f"  ERF documents:                         "
        f"{erf_stats['records']:,}"
    )
    print(
        f"  ERF unique normalized parcel keys:     "
        f"{erf_stats['uniqueNormalizedParcelKeys']:,}"
    )

    print("\n[MERGE SUMMARY]")
    print(
        f"  PSD meter records preserved:           "
        f"{merge_stats['records']:,}"
    )
    print(
        f"  exact one-to-one GPS meters:           "
        f"{merge_stats['metersWithUsableGps']:,}"
    )
    print(
        f"  meters without exact GPS:              "
        f"{merge_stats['metersWithoutUsableGps']:,}"
    )
    print(
        f"  meters with multiple GPS candidates:   "
        f"{merge_stats['endRowsWithMultipleGpsCandidates']:,}"
    )
    print(
        f"  candidate location relationships:      "
        f"{merge_stats['candidateLocationRelationships']:,}"
    )

    print("\n[LINKAGE DETAIL DISTRIBUTION]")
    for status, count in merge_stats["linkageDetailCounts"].items():
        print(f"  {status}: {count:,}")

    print("\n[PSD STATUS DISTRIBUTION]")
    for status, count in merge_stats["statusCounts"].items():
        print(f"  {status}: {count:,}")

    print("\n[COMPARISON NORMALIZATION]")
    for rule, count in merge_stats[
        "comparisonNormalizationCounts"
    ].items():
        print(f"  {rule}: {count:,}")

    paths = output_paths(output_dir, lm_pcode)
    jsonl_payload, document_size_stats = build_jsonl(enriched)
    csv_payload = build_main_csv(enriched)
    candidates_payload = build_candidate_csv(enriched)
    unmatched_payload = build_unmatched_csv(enriched)

    print("\n[DOCUMENT SHAPE QA]")
    print(
        f"  maximum JSONL document bytes:          "
        f"{document_size_stats['maxJsonlDocumentBytes']:,}"
    )
    print(
        f"  documents over 900,000 bytes:          "
        f"{document_size_stats['jsonlDocumentsOver900000Bytes']:,}"
    )
    print(
        f"  maximum ERF candidates per meter:      "
        f"{document_size_stats['maxErfCandidateCount']:,}"
    )
    print(
        f"  documents with multiple candidates:    "
        f"{document_size_stats['documentsWithMultipleErfCandidates']:,}"
    )
    print(
        f"  forbidden geometry key occurrences:    "
        f"{document_size_stats['forbiddenGeometryKeyOccurrences']:,}"
    )

    summary = {
        "stage": "M02",
        "status": "PASSED",
        "scriptVersion": "2.0.0",
        "primarySourceRule": (
            "Every output record originates from END 2026-07-29.xlsx; "
            "MeterNumber remains sourced only from END."
        ),
        "linkage": [
            "END.AccountNumber = CsmValRollB.OWNER_ACCOUNT_NO",
            (
                "CsmValRollB.GIS_KEY + one comparison-only trailing zero "
                "= B04.sg.prclKey after removing K241"
            ),
        ],
        "oneToOneRule": (
            "One meter may receive at most one ERF candidate, one ward, "
            "and one centroid GPS location. Several meters may share one ERF."
        ),
        "sourceErfRunId": source_erf_run_id,
        "scope": {
            "lmPcode": lm_pcode,
            "salesPeriodStart": "2023-12",
            "salesPeriodEnd": "2026-06",
        },
        "sources": {
            "endWorkbook": {
                "path": str(end_path),
                "sha256": end_sha,
                **end_stats,
            },
            "valuationBridgeWorkbook": {
                "path": str(bridge_path),
                "sha256": bridge_sha,
                **bridge_stats,
            },
            "erfDocuments": {
                "path": str(erf_documents_path),
                "sha256": erf_documents_sha,
                **erf_stats,
            },
        },
        "merge": {
            **merge_stats,
            **document_size_stats,
        },
        "outputs": {
            key: str(path)
            for key, path in paths.items()
        },
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
        "notes": [
            "All END meters remain in the enriched PSD.",
            "Only exact one-to-one SG matches receive ERF, ward and GPS data.",
            "Unmatched meters are preserved without invented location data.",
            "No meter receives multiple ERF candidates or multiple wards.",
            "Geometry and GeometryJson content are excluded.",
            (
                "Legacy PSD field names ElmAccountMatched and ElmSourceRows "
                "are retained for frontend compatibility; they now describe "
                "the valuation-roll account bridge."
            ),
            (
                "PreviousInstallationDate values beginning 1900-01-01 "
                "are cleared."
            ),
            (
                "trnBatchIds is initialized as an empty array "
                "for every PSD record."
            ),
        ],
    }
    summary_payload = json_bytes(summary)

    if args.preflight_only:
        print("")
        print("============================================================")
        print("PREFLIGHT PASSED")
        print("============================================================")
        print("Every END meter was preserved.")
        print(
            f"Exact one-to-one GPS meters: "
            f"{merge_stats['metersWithUsableGps']:,}"
        )
        print("Meters with multiple wards: 0")
        print("Geometry included: NO")
        print("No PSD or QA output files were written.")
        print("No Firebase or Firestore reads or writes were performed.")
        return

    payloads = {
        "jsonl": jsonl_payload,
        "csv": csv_payload,
        "candidates": candidates_payload,
        "unmatched": unmatched_payload,
        "summary": summary_payload,
    }
    results = write_outputs(
        paths,
        payloads,
        args.replace_existing,
    )

    print("\n[OUTPUTS]")
    for key in (
        "jsonl",
        "csv",
        "candidates",
        "unmatched",
        "summary",
    ):
        path = paths[key]
        print(f"  [{results[key].upper()}] {path}")
        print(f"    SHA-256: {sha256_file(path)}")

    print("")
    print("============================================================")
    print("STAGE M02 COMPLETED")
    print("============================================================")
    print("Every END meter remains in the enriched PSD.")
    print(
        f"Exact one-to-one GPS meters: "
        f"{merge_stats['metersWithUsableGps']:,}"
    )
    print("Meters with multiple wards: 0")
    print("Geometry included: NO")
    print("No Firebase or Firestore reads or writes were performed.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FAILED] {exc}")
        raise SystemExit(1)
