#!/usr/bin/env python3
"""
Build a deterministic 20-document Firestore pilot from exact-GPS PSD records only.

Eligibility:
- HasUsableGps is true
- GpsMatchStatus is MATCHED_SINGLE_GPS
- ErfCandidateCount is exactly 1
- exactly one ERF candidate, ward and GPS point
- no Geometry or GeometryJson keys anywhere

The output upload JSONL preserves the established Firestore ID contract:
    {"docId": "<MeterNumber>", "data": <unchanged PSD document>}

No Firebase or Firestore access is performed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict, deque
from io import StringIO
from pathlib import Path
from typing import Any


EXPECTED_PILOT_COUNT = 20
FORBIDDEN_GEOMETRY_KEYS = {"geometry", "geometryjson"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-psd", required=True, type=Path)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument(
        "--expected-total-record-count",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--expected-eligible-count",
        required=True,
        type=int,
    )
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--replace-existing", action="store_true")
    return parser.parse_args()


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: str, label: str) -> str:
    cleaned = clean_text(value).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(
            f"{label} must be a 64-character SHA-256"
        )
    return cleaned


def find_forbidden_geometry_keys(
    value: Any,
    path: str = "",
) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_GEOMETRY_KEYS:
                found.append(child_path)
            found.extend(
                find_forbidden_geometry_keys(child, child_path)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                find_forbidden_geometry_keys(
                    child,
                    f"{path}[{index}]",
                )
            )
    return found


def finite_coordinate(value: Any, low: float, high: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and low <= number <= high


def validate_exact_gps_record(
    record: dict[str, Any],
    line_number: int,
    lm_pcode: str,
) -> tuple[str, str, str]:
    forbidden = find_forbidden_geometry_keys(record)
    if forbidden:
        raise ValueError(
            f"Geometry found at PSD line {line_number}: "
            f"{forbidden[:5]}"
        )

    meter_number = clean_text(record.get("MeterNumber"))
    if not meter_number:
        raise ValueError(
            f"Blank MeterNumber at PSD line {line_number}"
        )

    if record.get("HasUsableGps") is not True:
        raise ValueError(
            f"Pilot candidate {meter_number} has no usable GPS"
        )
    if clean_text(record.get("GpsMatchStatus")) != "MATCHED_SINGLE_GPS":
        raise ValueError(
            f"Pilot candidate {meter_number} is not MATCHED_SINGLE_GPS"
        )
    if record.get("ErfCandidateCount") != 1:
        raise ValueError(
            f"Pilot candidate {meter_number} does not declare one candidate"
        )

    candidates = record.get("ErfCandidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError(
            f"Pilot candidate {meter_number} does not contain one candidate"
        )

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ValueError(
            f"Invalid ERF candidate for meter {meter_number}"
        )

    expected_candidate_fields = {
        "ErfNumber",
        "ErfId",
        "WardNumber",
        "WardPcode",
        "LmPcode",
        "Latitude",
        "Longitude",
    }
    if set(candidate) != expected_candidate_fields:
        raise ValueError(
            f"Unexpected ERF candidate shape for meter {meter_number}. "
            f"Expected {sorted(expected_candidate_fields)}, "
            f"found {sorted(candidate)}"
        )

    erf_id = clean_text(candidate.get("ErfId"))
    ward_pcode = clean_text(candidate.get("WardPcode"))
    candidate_lm = clean_text(candidate.get("LmPcode")).upper()

    if not erf_id or not ward_pcode:
        raise ValueError(
            f"Blank ERF/ward identity for meter {meter_number}"
        )
    if candidate_lm != lm_pcode:
        raise ValueError(
            f"Wrong LM for pilot meter {meter_number}: {candidate_lm!r}"
        )
    if not finite_coordinate(candidate.get("Latitude"), -90, 90):
        raise ValueError(
            f"Invalid latitude for pilot meter {meter_number}"
        )
    if not finite_coordinate(candidate.get("Longitude"), -180, 180):
        raise ValueError(
            f"Invalid longitude for pilot meter {meter_number}"
        )

    return meter_number, ward_pcode, erf_id


def load_psd(
    path: Path,
    expected_total: int,
    expected_eligible: int,
    lm_pcode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    meter_numbers: set[str] = set()
    status_counts = Counter()

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON at PSD line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"PSD line {line_number} is not a JSON object"
                )

            meter_number = clean_text(record.get("MeterNumber"))
            if not meter_number:
                raise ValueError(
                    f"Blank MeterNumber at PSD line {line_number}"
                )
            if meter_number in meter_numbers:
                raise ValueError(
                    f"Duplicate MeterNumber in PSD: {meter_number}"
                )
            meter_numbers.add(meter_number)

            forbidden = find_forbidden_geometry_keys(record)
            if forbidden:
                raise ValueError(
                    f"Geometry found at PSD line {line_number}: "
                    f"{forbidden[:5]}"
                )

            status = clean_text(record.get("GpsMatchStatus"))
            status_counts[status] += 1
            records.append(record)

            if (
                record.get("HasUsableGps") is True
                and status == "MATCHED_SINGLE_GPS"
                and record.get("ErfCandidateCount") == 1
            ):
                validate_exact_gps_record(
                    record,
                    line_number,
                    lm_pcode,
                )
                eligible.append(record)

            if len(records) % 2000 == 0:
                print(
                    f"[PROGRESS] Read {len(records):,} PSD records; "
                    f"eligible exact-GPS records {len(eligible):,}"
                )

    if len(records) != expected_total:
        raise ValueError(
            f"PSD record-count mismatch. Expected {expected_total:,}, "
            f"found {len(records):,}."
        )
    if len(eligible) != expected_eligible:
        raise ValueError(
            f"Eligible exact-GPS count mismatch. "
            f"Expected {expected_eligible:,}, found {len(eligible):,}."
        )

    return records, eligible, dict(sorted(status_counts.items()))


def select_round_robin_by_ward(
    eligible: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, deque[dict[str, Any]]] = defaultdict(deque)

    for record in sorted(
        eligible,
        key=lambda item: (
            clean_text(item["ErfCandidates"][0]["WardPcode"]),
            clean_text(item["MeterNumber"]),
        ),
    ):
        ward_pcode = clean_text(
            record["ErfCandidates"][0]["WardPcode"]
        )
        groups[ward_pcode].append(record)

    ward_order = sorted(groups)
    selected: list[dict[str, Any]] = []

    while len(selected) < EXPECTED_PILOT_COUNT:
        selected_in_round = 0
        for ward_pcode in ward_order:
            if len(selected) >= EXPECTED_PILOT_COUNT:
                break
            if groups[ward_pcode]:
                selected.append(groups[ward_pcode].popleft())
                selected_in_round += 1
        if selected_in_round == 0:
            break

    if len(selected) != EXPECTED_PILOT_COUNT:
        raise ValueError(
            f"Could not select exactly {EXPECTED_PILOT_COUNT} "
            "eligible pilot records"
        )

    return selected


def write_atomic(path: Path, payload: bytes, replace_existing: bool) -> str:
    if path.is_file() and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(payload).digest():
        return "unchanged"
    if path.exists() and not replace_existing:
        raise FileExistsError(
            f"Different existing output found: {path}. "
            "Use --replace-existing only after review."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    return "written"


def main() -> None:
    args = parse_args()

    input_path = args.input_psd.resolve()
    output_dir = args.output_dir.resolve()
    lm_pcode = clean_text(args.lm_pcode).upper()

    if not input_path.is_file():
        raise FileNotFoundError(f"Input PSD not found: {input_path}")

    expected_sha = require_sha256(
        args.expected_input_sha256,
        "Expected input PSD SHA-256",
    )
    actual_sha = sha256_file(input_path)
    if actual_sha.lower() != expected_sha:
        raise ValueError(
            f"Input PSD SHA-256 mismatch. "
            f"Expected {expected_sha}, found {actual_sha}."
        )

    print("")
    print("============================================================")
    print("BUILD EXACT-GPS SALES PILOT — READ ONLY")
    print("============================================================")
    print(f"Input PSD:           {input_path}")
    print(f"Input SHA-256:       {actual_sha}")
    print(f"Expected total:      {args.expected_total_record_count:,}")
    print(f"Expected eligible:   {args.expected_eligible_count:,}")
    print(f"Pilot count:         {EXPECTED_PILOT_COUNT}")
    print(f"LM:                  {lm_pcode}")
    print("Eligibility:          exact one-to-one GPS only")
    print("Firestore access:     NONE")

    records, eligible, status_counts = load_psd(
        input_path,
        args.expected_total_record_count,
        args.expected_eligible_count,
        lm_pcode,
    )

    selected = select_round_robin_by_ward(eligible)

    wrappers = []
    manifest_rows = []
    ward_distribution = Counter()

    for index, record in enumerate(selected, start=1):
        meter_number = clean_text(record["MeterNumber"])
        candidate = record["ErfCandidates"][0]
        doc_id = meter_number
        ward_pcode = clean_text(candidate["WardPcode"])
        ward_distribution[ward_pcode] += 1

        wrappers.append(
            {
                "docId": doc_id,
                "data": record,
            }
        )
        manifest_rows.append(
            {
                "pilotOrder": index,
                "docId": doc_id,
                "MeterNumber": meter_number,
                "AccountNumber": clean_text(record.get("AccountNumber")),
                "ErfId": clean_text(candidate.get("ErfId")),
                "ErfNumber": clean_text(candidate.get("ErfNumber")),
                "WardNumber": clean_text(candidate.get("WardNumber")),
                "WardPcode": ward_pcode,
                "Latitude": candidate.get("Latitude"),
                "Longitude": candidate.get("Longitude"),
                "GpsMatchStatus": clean_text(record.get("GpsMatchStatus")),
                "ErfCandidateCount": record.get("ErfCandidateCount"),
            }
        )

    upload_jsonl = (
        "\n".join(
            json.dumps(
                wrapper,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for wrapper in wrappers
        )
        + "\n"
    ).encode("utf-8")

    manifest_buffer = StringIO(newline="")
    manifest_writer = csv.DictWriter(
        manifest_buffer,
        fieldnames=list(manifest_rows[0]),
        lineterminator="\n",
    )
    manifest_writer.writeheader()
    manifest_writer.writerows(manifest_rows)
    manifest_csv = manifest_buffer.getvalue().encode("utf-8-sig")

    upload_sha = hashlib.sha256(upload_jsonl).hexdigest()
    manifest_sha = hashlib.sha256(manifest_csv).hexdigest()

    summary = {
        "status": "PASSED",
        "sourcePsd": {
            "path": str(input_path),
            "sha256": actual_sha,
            "records": len(records),
            "eligibleExactGpsRecords": len(eligible),
        },
        "pilot": {
            "count": len(selected),
            "selectionRule": (
                "Deterministic round-robin across sorted WardPcode groups, "
                "then sorted MeterNumber within each ward"
            ),
            "documentIdRule": "Plain MeterNumber",
            "eligibility": {
                "HasUsableGps": True,
                "GpsMatchStatus": "MATCHED_SINGLE_GPS",
                "ErfCandidateCount": 1,
                "geometryAllowed": False,
            },
            "wardDistribution": dict(sorted(ward_distribution.items())),
            "uniqueDocumentIds": len(
                {wrapper["docId"] for wrapper in wrappers}
            ),
            "uniqueMeterNumbers": len(
                {row["MeterNumber"] for row in manifest_rows}
            ),
        },
        "psdStatusCounts": status_counts,
        "outputs": {
            "uploadJsonlSha256": upload_sha,
            "manifestCsvSha256": manifest_sha,
        },
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
    }
    summary_payload = (
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    upload_path = output_dir / "pilot_20_exact_gps_upload.jsonl"
    manifest_path = output_dir / "pilot_20_exact_gps_manifest.csv"
    summary_path = output_dir / "pilot_20_exact_gps_summary.json"

    results = {
        "upload": write_atomic(
            upload_path,
            upload_jsonl,
            args.replace_existing,
        ),
        "manifest": write_atomic(
            manifest_path,
            manifest_csv,
            args.replace_existing,
        ),
        "summary": write_atomic(
            summary_path,
            summary_payload,
            args.replace_existing,
        ),
    }

    print("")
    print("============================================================")
    print("PILOT BUILD PASSED")
    print("============================================================")
    print(f"PSD records checked:       {len(records):,}")
    print(f"Exact-GPS eligible:        {len(eligible):,}")
    print(f"Pilot documents selected:  {len(selected):,}")
    print(f"Unique document IDs:       {len({w['docId'] for w in wrappers}):,}")
    print(f"Wards represented:         {len(ward_distribution):,}")
    print("Geometry occurrences:      0")
    print("")
    print(f"[{results['upload'].upper()}] {upload_path}")
    print(f"  SHA-256: {upload_sha}")
    print(f"[{results['manifest'].upper()}] {manifest_path}")
    print(f"  SHA-256: {manifest_sha}")
    print(f"[{results['summary'].upper()}] {summary_path}")
    print("Firestore reads performed:  NO")
    print("Firestore writes performed: NO")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FAILED] {exc}")
        raise SystemExit(1)
