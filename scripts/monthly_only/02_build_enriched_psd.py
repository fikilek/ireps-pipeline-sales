"""
Stage M02: build one enriched PSD while preserving every END meter.

Primary source:
    END 2026-07-29.xlsx

Linkage:
    END.AccountNumber = ELM METERS.ACCOUNT NO
    ELM METERS.ERF NUMBER = Stage M01 erfNumberNormalized

MeterNumber is always taken from END. The ELM meter-number field is not read.
Unmatched END meters remain in the output and are reported.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Iterable

from lib.psd_model import (
    clean_text,
    enrich_end_rows,
    read_elm_workbook,
    read_end_workbook,
    read_erf_lookup,
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
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "monthly_only" / "ZA5241" / "02_enriched_psd"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a fully enriched PSD by linking END account numbers to ELM ERF "
            "numbers and then to the M01 ERF/GPS lookup."
        )
    )
    parser.add_argument("--end-workbook", type=Path, required=True)
    parser.add_argument("--elm-workbook", type=Path, required=True)
    parser.add_argument("--erf-lookup", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--source-erf-run-id", required=True)
    parser.add_argument("--expected-end-sha256", required=True)
    parser.add_argument("--expected-elm-sha256", required=True)
    parser.add_argument("--expected-erf-lookup-sha256", required=True)
    parser.add_argument("--expected-end-record-count", type=int, required=True)
    parser.add_argument("--expected-elm-record-count", type=int, required=True)
    parser.add_argument("--expected-erf-record-count", type=int, required=True)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def require_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Required source file not found: {path}")


def validate_sha256(path: Path, expected: str, label: str) -> str:
    expected_clean = clean_text(expected).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_clean):
        raise ValueError(f"{label} expected SHA-256 must be 64 hexadecimal characters")
    actual = sha256_file(path)
    if actual.lower() != expected_clean:
        raise ValueError(f"{label} SHA-256 mismatch. Expected {expected_clean}, found {actual}.")
    return actual


def main() -> None:
    args = parse_args()
    end_path = args.end_workbook.resolve()
    elm_path = args.elm_workbook.resolve()
    lookup_path = args.erf_lookup.resolve()
    output_dir = args.output_dir.resolve()
    lm_pcode = clean_text(args.lm_pcode).upper()
    source_erf_run_id = clean_text(args.source_erf_run_id)

    require_files([end_path, elm_path, lookup_path])
    if not lm_pcode or not source_erf_run_id:
        raise ValueError("LM pCode and source ERF run ID cannot be blank")
    if args.progress_every < 0:
        raise ValueError("--progress-every cannot be negative")

    end_sha = validate_sha256(end_path, args.expected_end_sha256, "END workbook")
    elm_sha = validate_sha256(elm_path, args.expected_elm_sha256, "ELM workbook")
    lookup_sha = validate_sha256(lookup_path, args.expected_erf_lookup_sha256, "ERF lookup")

    mode = "preflight-only" if args.preflight_only else "write"
    print("[STAGE M02] END + ELM ACCOUNT/ERF LINK + ERF/GPS LOOKUP -> ENRICHED PSD")
    print(f"  primary END workbook:    {end_path}")
    print(f"  linking ELM workbook:    {elm_path}")
    print(f"  ERF/GPS lookup:          {lookup_path}")
    print(f"  expected LM:             {lm_pcode}")
    print(f"  source ERF run ID:       {source_erf_run_id}")
    print(f"  output directory:        {output_dir}")
    print(f"  mode:                    {mode}")
    print("  ELM meter field used:    NO")
    print("  Firestore access:        NONE")

    print("\n[1/4] Reading primary END workbook")
    end_rows, end_stats = read_end_workbook(end_path, args.expected_end_record_count)

    print("[2/4] Reading ELM account-to-ERF links")
    elm_by_account, elm_stats = read_elm_workbook(elm_path, args.expected_elm_record_count)

    print("[3/4] Reading Stage M01 ERF/GPS lookup")
    lookup_by_erf_number, lookup_stats = read_erf_lookup(
        lookup_path, lm_pcode, args.expected_erf_record_count
    )

    print("[4/4] Enriching every END meter")
    enriched, merge_stats = enrich_end_rows(
        end_rows, elm_by_account, lookup_by_erf_number, args.progress_every
    )

    if len(enriched) != len(end_rows):
        raise ValueError("Primary END row preservation failed")
    if len({record.identity["MeterNumber"] for record in enriched}) != len(enriched):
        raise ValueError("MeterNumber uniqueness was not preserved")

    print("\n[SOURCE SUMMARY]")
    print(f"  END records:                         {end_stats['records']:,}")
    print(f"  END unique MeterNumbers:             {end_stats['uniqueMeterNumbers']:,}")
    print(f"  END blank AccountNumbers:            {end_stats['blankAccountNumbers']:,}")
    print(f"  END unique nonblank accounts:        {end_stats['uniqueNonblankAccountNumbers']:,}")
    print(f"  ELM records:                         {elm_stats['records']:,}")
    print(f"  ELM unique accounts:                 {elm_stats['uniqueAccounts']:,}")
    print(f"  ELM unique account/ERF pairs:        {elm_stats['uniqueAccountErfPairs']:,}")
    print(f"  ELM duplicate exact links removed:  {elm_stats['duplicateExactAccountErfRowsSuppressed']:,}")
    print(f"  ERF lookup records:                  {lookup_stats['records']:,}")
    print(f"  ERF lookup unique ERF numbers:       {lookup_stats['uniqueNormalizedErfNumbers']:,}")

    print("\n[MERGE SUMMARY]")
    print(f"  PSD meter records preserved:         {merge_stats['records']:,}")
    print(f"  END rows matched to ELM account:     {merge_stats['matchedEndRowsByAccount']:,}")
    print(f"  END rows not matched to ELM account: {merge_stats['unmatchedEndRowsByAccount']:,}")
    print(f"  meters with usable GPS:              {merge_stats['metersWithUsableGps']:,}")
    print(f"  meters without usable GPS:           {merge_stats['metersWithoutUsableGps']:,}")
    print(f"  meters with multiple GPS candidates: {merge_stats['endRowsWithMultipleGpsCandidates']:,}")
    print(f"  GPS candidate relationships:         {merge_stats['candidateLocationRelationships']:,}")

    print("\n[STATUS DISTRIBUTION]")
    for status, count in merge_stats["statusCounts"].items():
        print(f"  {status}: {count:,}")

    paths = output_paths(output_dir, lm_pcode)
    jsonl_payload, document_size_stats = build_jsonl(enriched)
    csv_payload = build_main_csv(enriched)
    candidates_payload = build_candidate_csv(enriched)
    unmatched_payload = build_unmatched_csv(enriched)

    print("\n[DOCUMENT SIZE QA]")
    print(f"  maximum JSONL document bytes:        {document_size_stats['maxJsonlDocumentBytes']:,}")
    print(f"  documents over 900,000 bytes:        {document_size_stats['jsonlDocumentsOver900000Bytes']:,}")

    summary = {
        "stage": "M02",
        "status": "PASSED",
        "scriptVersion": "1.0.0",
        "primarySourceRule": (
            "Every output record originates from END 2026-07-29.xlsx; "
            "MeterNumber is never sourced from ELM METERS.xlsx."
        ),
        "linkage": [
            "END.AccountNumber = ELM.ACCOUNT NO",
            "ELM.ERF NUMBER = M01.erfNumberNormalized",
        ],
        "sourceErfRunId": source_erf_run_id,
        "scope": {
            "lmPcode": lm_pcode,
            "salesPeriodStart": "2023-12",
            "salesPeriodEnd": "2026-06",
        },
        "sources": {
            "endWorkbook": {"path": str(end_path), "sha256": end_sha, **end_stats},
            "elmWorkbook": {"path": str(elm_path), "sha256": elm_sha, **elm_stats},
            "erfGpsLookup": {"path": str(lookup_path), "sha256": lookup_sha, **lookup_stats},
        },
        "merge": {**merge_stats, **document_size_stats},
        "outputs": {key: str(path) for key, path in paths.items()},
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
        "notes": [
            "Unmatched END meters remain in the enriched PSD.",
            "Exact duplicate ELM account/ERF links are suppressed.",
            "Multiple ERF and GPS candidates are preserved, not arbitrarily reduced to one.",
            "The ELM meter-number field is not read or used.",
            "PreviousInstallationDate values beginning 1900-01-01 are cleared.",
            "trnBatchIds is initialized as an empty array for every PSD record.",
        ],
    }
    summary_payload = json_bytes(summary)

    if args.preflight_only:
        print("\n[PREFLIGHT OK] All three sources passed and every END meter was preserved.")
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
    results = write_outputs(paths, payloads, args.replace_existing)

    print("\n[OUTPUTS]")
    for key in ("jsonl", "csv", "candidates", "unmatched", "summary"):
        path = paths[key]
        print(f"  [{results[key].upper()}] {path}")
        print(f"    SHA-256: {sha256_file(path)}")

    print("\n[OK] Stage M02 completed.")
    print("Every END meter remains in the enriched PSD.")
    print("No Firebase or Firestore reads or writes were performed.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FAILED] {exc}")
        raise SystemExit(1)
