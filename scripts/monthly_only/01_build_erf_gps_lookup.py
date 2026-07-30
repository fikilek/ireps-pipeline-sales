"""
Stage M01: build an ERF-to-ward-to-GPS lookup from a verified cadastral ERF JSONL file.

This script is environment-neutral. It does not connect to Firebase or Firestore.

It reads one JSON object per line and extracts:
    erfId                    <- erfId
    erfNumber                <- sg.erfNo
    erfNumberNormalized      <- normalized sg.erfNo
    wardNumber               <- admin.ward.name
    wardPcode                <- admin.ward.pcode
    lmPcode                  <- admin.localMunicipality.pcode
    latitude                 <- centroid.lat
    longitude                <- centroid.lng
    geometry                 <- parsed geometry GeoJSON object

The source geometry may be stored as JSON text or as an object. The JSONL output
always stores geometry as an object. The CSV output stores the same geometry in
the geometryJson text column.

Duplicate displayed ERF numbers are allowed and preserved. Duplicate erfId values
are fatal because erfId is the unique cadastral identity.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "output" / "monthly_only" / "erf_gps_lookup"
)

CSV_FIELDS = [
    "erfId",
    "erfNumber",
    "erfNumberNormalized",
    "wardNumber",
    "wardPcode",
    "lmPcode",
    "latitude",
    "longitude",
    "geometryJson",
]

EXCEPTION_FIELDS = [
    "sourceLine",
    "erfId",
    "erfNumber",
    "wardNumber",
    "wardPcode",
    "lmPcode",
    "latitude",
    "longitude",
    "errorCodes",
    "errorMessage",
]


@dataclass(frozen=True)
class LookupRecord:
    source_line: int
    erf_id: str
    erf_number: str
    erf_number_normalized: str
    ward_number: str
    ward_pcode: str
    lm_pcode: str
    latitude: float
    longitude: float
    geometry: dict[str, Any]

    def as_json_document(self) -> dict[str, Any]:
        return {
            "erfId": self.erf_id,
            "erfNumber": self.erf_number,
            "erfNumberNormalized": self.erf_number_normalized,
            "wardNumber": self.ward_number,
            "wardPcode": self.ward_pcode,
            "lmPcode": self.lm_pcode,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "geometry": self.geometry,
        }

    def as_csv_row(self) -> dict[str, Any]:
        return {
            "erfId": self.erf_id,
            "erfNumber": self.erf_number,
            "erfNumberNormalized": self.erf_number_normalized,
            "wardNumber": self.ward_number,
            "wardPcode": self.ward_pcode,
            "lmPcode": self.lm_pcode,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "geometryJson": json.dumps(
                self.geometry,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a verified ERF/Ward/GPS lookup from one cadastral ERF JSONL file."
        )
    )
    parser.add_argument(
        "--source-erfs",
        type=Path,
        required=True,
        help="Authoritative B04_erf_documents.jsonl source file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory for the lookup JSONL, CSV, summary JSON, and exception CSV. "
            f"Default: {DEFAULT_OUTPUT_DIR}"
        ),
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Expected Local Municipality pCode, for example ZA5241.",
    )
    parser.add_argument(
        "--source-run-id",
        required=True,
        help="Authoritative cadastral import run ID bound to the source file.",
    )
    parser.add_argument(
        "--expected-source-sha256",
        required=True,
        help="Approved SHA-256 for the authoritative source file.",
    )
    parser.add_argument(
        "--expected-record-count",
        type=int,
        required=True,
        help="Expected number of ERF documents in the source JSONL.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress after this many source rows. Default: 1000.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Replace different existing lookup outputs only after successful validation. "
            "Identical existing outputs are left unchanged."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate the complete source and report the planned outputs without writing them.",
    )
    return parser.parse_args()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_erf_number(value: Any) -> str:
    text = clean_text(value).upper()
    text = re.sub(r"\s+", "", text)

    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    if text.isdigit():
        # Treat numeric ERF values from Excel and JSON consistently.
        return str(int(text))

    return text


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_nested(document: Any, *path: str) -> Any:
    current = document
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result if math.isfinite(result) else None


def parse_geometry(value: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "blank_geometry"

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"invalid_geometry_json:{exc.msg}"
    else:
        parsed = value

    if not isinstance(parsed, dict):
        return None, "geometry_not_object"

    geometry_type = clean_text(parsed.get("type"))
    coordinates = parsed.get("coordinates")

    if geometry_type not in {"Polygon", "MultiPolygon"}:
        return None, f"unsupported_geometry_type:{geometry_type or 'blank'}"

    if not isinstance(coordinates, list) or not coordinates:
        return None, "geometry_coordinates_missing"

    return parsed, None


def safe_filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value))
    token = token.strip("._-")
    if not token:
        raise ValueError("A safe filename token could not be created")
    return token


def build_output_paths(
    output_dir: Path,
    *,
    lm_pcode: str,
    source_run_id: str,
) -> dict[str, Path]:
    lm_token = safe_filename_token(lm_pcode)
    run_token = safe_filename_token(source_run_id)
    stem = f"erf_gps_lookup__{lm_token}__{run_token}"

    return {
        "jsonl": output_dir / f"{stem}.jsonl",
        "csv": output_dir / f"{stem}.csv",
        "summary": output_dir / f"{stem}__summary.json",
        "exceptions": output_dir / f"{stem}__exceptions.csv",
    }


def extract_record(
    document: Any,
    *,
    source_line: int,
    expected_lm_pcode: str,
) -> tuple[Optional[LookupRecord], Optional[dict[str, Any]]]:
    errors: list[str] = []

    if not isinstance(document, dict):
        return None, {
            "sourceLine": source_line,
            "erfId": "",
            "erfNumber": "",
            "wardNumber": "",
            "wardPcode": "",
            "lmPcode": "",
            "latitude": "",
            "longitude": "",
            "errorCodes": "document_not_object",
            "errorMessage": "The JSONL line did not contain a JSON object.",
        }

    erf_id = clean_text(document.get("erfId"))
    erf_number = clean_text(get_nested(document, "sg", "erfNo"))
    erf_number_normalized = normalize_erf_number(erf_number)
    ward_number = clean_text(get_nested(document, "admin", "ward", "name"))
    ward_pcode = clean_text(get_nested(document, "admin", "ward", "pcode"))
    lm_pcode = clean_text(
        get_nested(document, "admin", "localMunicipality", "pcode")
    ).upper()
    latitude_raw = get_nested(document, "centroid", "lat")
    longitude_raw = get_nested(document, "centroid", "lng")
    latitude = parse_finite_float(latitude_raw)
    longitude = parse_finite_float(longitude_raw)
    geometry, geometry_error = parse_geometry(document.get("geometry"))

    if not erf_id:
        errors.append("missing_erfId")
    if not erf_number:
        errors.append("missing_erfNumber")
    if not erf_number_normalized:
        errors.append("missing_normalized_erfNumber")
    if not ward_number:
        errors.append("missing_wardNumber")
    if not ward_pcode:
        errors.append("missing_wardPcode")
    if not lm_pcode:
        errors.append("missing_lmPcode")
    elif lm_pcode != expected_lm_pcode:
        errors.append("wrong_lmPcode")

    if latitude is None:
        errors.append("invalid_latitude")
    elif latitude < -90 or latitude > 90:
        errors.append("latitude_out_of_range")

    if longitude is None:
        errors.append("invalid_longitude")
    elif longitude < -180 or longitude > 180:
        errors.append("longitude_out_of_range")

    if geometry_error:
        errors.append(geometry_error)

    if errors:
        return None, {
            "sourceLine": source_line,
            "erfId": erf_id,
            "erfNumber": erf_number,
            "wardNumber": ward_number,
            "wardPcode": ward_pcode,
            "lmPcode": lm_pcode,
            "latitude": "" if latitude is None else latitude,
            "longitude": "" if longitude is None else longitude,
            "errorCodes": ";".join(errors),
            "errorMessage": "One or more required ERF lookup fields failed validation.",
        }

    assert latitude is not None
    assert longitude is not None
    assert geometry is not None

    return (
        LookupRecord(
            source_line=source_line,
            erf_id=erf_id,
            erf_number=erf_number,
            erf_number_normalized=erf_number_normalized,
            ward_number=ward_number,
            ward_pcode=ward_pcode,
            lm_pcode=lm_pcode,
            latitude=latitude,
            longitude=longitude,
            geometry=geometry,
        ),
        None,
    )


def read_and_validate_source(
    source_path: Path,
    *,
    expected_lm_pcode: str,
    progress_every: int,
) -> tuple[list[LookupRecord], list[dict[str, Any]], int]:
    records: list[LookupRecord] = []
    exceptions: list[dict[str, Any]] = []
    total_lines = 0

    with source_path.open("r", encoding="utf-8-sig") as source:
        for source_line, raw_line in enumerate(source, start=1):
            total_lines += 1

            if progress_every > 0 and total_lines % progress_every == 0:
                print(f"[PROGRESS] Processed {total_lines:,} source ERFs")

            if not raw_line.strip():
                exceptions.append(
                    {
                        "sourceLine": source_line,
                        "erfId": "",
                        "erfNumber": "",
                        "wardNumber": "",
                        "wardPcode": "",
                        "lmPcode": "",
                        "latitude": "",
                        "longitude": "",
                        "errorCodes": "blank_jsonl_line",
                        "errorMessage": "The source JSONL line is blank.",
                    }
                )
                continue

            try:
                document = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                exceptions.append(
                    {
                        "sourceLine": source_line,
                        "erfId": "",
                        "erfNumber": "",
                        "wardNumber": "",
                        "wardPcode": "",
                        "lmPcode": "",
                        "latitude": "",
                        "longitude": "",
                        "errorCodes": "invalid_json",
                        "errorMessage": f"{exc.msg} at column {exc.colno}.",
                    }
                )
                continue

            record, exception = extract_record(
                document,
                source_line=source_line,
                expected_lm_pcode=expected_lm_pcode,
            )

            if record is not None:
                records.append(record)
            if exception is not None:
                exceptions.append(exception)

    return records, exceptions, total_lines


def duplicate_groups(values: Iterable[str]) -> dict[str, int]:
    counts = Counter(values)
    return {
        value: count
        for value, count in sorted(counts.items())
        if count > 1
    }


def add_duplicate_erf_id_exceptions(
    records: list[LookupRecord],
    exceptions: list[dict[str, Any]],
) -> dict[str, int]:
    duplicate_ids = duplicate_groups(record.erf_id for record in records)
    if not duplicate_ids:
        return {}

    duplicate_id_set = set(duplicate_ids)
    for record in records:
        if record.erf_id not in duplicate_id_set:
            continue

        exceptions.append(
            {
                "sourceLine": record.source_line,
                "erfId": record.erf_id,
                "erfNumber": record.erf_number,
                "wardNumber": record.ward_number,
                "wardPcode": record.ward_pcode,
                "lmPcode": record.lm_pcode,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "errorCodes": "duplicate_erfId",
                "errorMessage": (
                    f"erfId appears {duplicate_ids[record.erf_id]} times in the source."
                ),
            }
        )

    return duplicate_ids


def json_bytes(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def build_jsonl_bytes(records: list[LookupRecord]) -> bytes:
    lines = [
        json.dumps(
            record.as_json_document(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for record in records
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def build_csv_bytes(records: list[LookupRecord]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(record.as_csv_row())
    return buffer.getvalue().encode("utf-8-sig")


def build_exceptions_csv_bytes(exceptions: list[dict[str, Any]]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=EXCEPTION_FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for exception in exceptions:
        writer.writerow(exception)
    return buffer.getvalue().encode("utf-8-sig")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def existing_file_matches(path: Path, payload: bytes) -> bool:
    return path.is_file() and sha256_file(path) == sha256_bytes(payload)


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def write_controlled_outputs(
    payloads: dict[str, bytes],
    output_paths: dict[str, Path],
    *,
    replace_existing: bool,
) -> dict[str, str]:
    results: dict[str, str] = {}
    planned_writes: list[tuple[str, Path, bytes]] = []

    # Inspect every output before writing so a blocked run cannot partially replace files.
    for key, payload in payloads.items():
        path = output_paths[key]

        if existing_file_matches(path, payload):
            results[key] = "unchanged"
            continue

        if path.exists() and not replace_existing:
            raise FileExistsError(
                f"Different existing output found: {path}. "
                "Review it, then rerun with --replace-existing only when approved."
            )

        planned_writes.append((key, path, payload))

    for key, path, payload in planned_writes:
        write_atomic(path, payload)
        results[key] = "written"

    return results


def main() -> None:
    args = parse_args()

    source_path = args.source_erfs.resolve()
    output_dir = args.output_dir.resolve()
    expected_lm_pcode = clean_text(args.lm_pcode).upper()
    source_run_id = clean_text(args.source_run_id)
    expected_sha256 = clean_text(args.expected_source_sha256).lower()

    if not source_path.is_file():
        raise FileNotFoundError(f"Source ERF JSONL file not found: {source_path}")
    if not expected_lm_pcode:
        raise ValueError("--lm-pcode cannot be blank")
    if not source_run_id:
        raise ValueError("--source-run-id cannot be blank")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("--expected-source-sha256 must be exactly 64 hexadecimal characters")
    if args.expected_record_count <= 0:
        raise ValueError("--expected-record-count must be greater than zero")
    if args.progress_every < 0:
        raise ValueError("--progress-every cannot be negative")

    source_sha256 = sha256_file(source_path)
    if source_sha256.lower() != expected_sha256:
        raise ValueError(
            "Source SHA-256 mismatch. "
            f"Expected {expected_sha256}, found {source_sha256}."
        )

    output_paths = build_output_paths(
        output_dir,
        lm_pcode=expected_lm_pcode,
        source_run_id=source_run_id,
    )

    mode = "preflight-only" if args.preflight_only else "write"
    print("[STAGE M01] CADASTRAL ERFS -> ERF/WARD/GPS LOOKUP")
    print(f"  source:                {source_path}")
    print(f"  source run ID:         {source_run_id}")
    print(f"  source SHA-256:        {source_sha256}")
    print(f"  expected LM:           {expected_lm_pcode}")
    print(f"  expected ERFs:         {args.expected_record_count:,}")
    print(f"  output directory:      {output_dir}")
    print(f"  mode:                  {mode}")

    records, exceptions, total_lines = read_and_validate_source(
        source_path,
        expected_lm_pcode=expected_lm_pcode,
        progress_every=args.progress_every,
    )

    duplicate_erf_ids = add_duplicate_erf_id_exceptions(records, exceptions)
    duplicate_erf_numbers = duplicate_groups(
        record.erf_number_normalized for record in records
    )

    ward_distribution = dict(
        sorted(Counter(record.ward_pcode for record in records).items())
    )
    geometry_type_distribution = dict(
        sorted(
            Counter(
                clean_text(record.geometry.get("type"))
                for record in records
            ).items()
        )
    )

    print("\n[VALIDATION SUMMARY]")
    print(f"  source JSONL lines:             {total_lines:,}")
    print(f"  valid lookup records:           {len(records):,}")
    print(f"  exceptions:                     {len(exceptions):,}")
    print(f"  unique ERF IDs:                 {len({r.erf_id for r in records}):,}")
    print(f"  duplicate ERF ID groups:        {len(duplicate_erf_ids):,}")
    print(
        "  unique normalized ERF numbers: "
        f"{len({r.erf_number_normalized for r in records}):,}"
    )
    print(f"  duplicate ERF number groups:    {len(duplicate_erf_numbers):,}")
    print(f"  wards represented:              {len(ward_distribution):,}")
    print(f"  geometry types represented:     {len(geometry_type_distribution):,}")

    for ward_pcode, count in ward_distribution.items():
        print(f"    {ward_pcode}: {count:,}")

    blocking_reasons: list[str] = []
    if total_lines != args.expected_record_count:
        blocking_reasons.append(
            "source_record_count_mismatch:"
            f"expected={args.expected_record_count},actual={total_lines}"
        )
    if len(records) != args.expected_record_count:
        blocking_reasons.append(
            "valid_lookup_record_count_mismatch:"
            f"expected={args.expected_record_count},actual={len(records)}"
        )
    if exceptions:
        blocking_reasons.append(f"validation_exceptions:{len(exceptions)}")
    if duplicate_erf_ids:
        blocking_reasons.append(
            f"duplicate_erf_id_groups:{len(duplicate_erf_ids)}"
        )

    status = "PASSED" if not blocking_reasons else "FAILED"

    jsonl_payload = build_jsonl_bytes(records)
    csv_payload = build_csv_bytes(records)
    exceptions_payload = build_exceptions_csv_bytes(exceptions)

    summary = {
        "stage": "M01",
        "status": status,
        "scriptVersion": "1.0.0",
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
        "source": {
            "path": str(source_path),
            "fileName": source_path.name,
            "runId": source_run_id,
            "sha256": source_sha256,
            "jsonlLines": total_lines,
        },
        "scope": {
            "lmPcode": expected_lm_pcode,
            "expectedRecordCount": args.expected_record_count,
        },
        "counts": {
            "validLookupRecords": len(records),
            "exceptions": len(exceptions),
            "uniqueErfIds": len({record.erf_id for record in records}),
            "duplicateErfIdGroups": len(duplicate_erf_ids),
            "uniqueNormalizedErfNumbers": len(
                {record.erf_number_normalized for record in records}
            ),
            "duplicateErfNumberGroups": len(duplicate_erf_numbers),
            "wardsRepresented": len(ward_distribution),
        },
        "wardDistribution": ward_distribution,
        "geometryTypeDistribution": geometry_type_distribution,
        "blockingReasons": blocking_reasons,
        "plannedOutputs": {
            key: str(path)
            for key, path in output_paths.items()
        },
        "plannedOutputSha256": {
            "jsonl": sha256_bytes(jsonl_payload),
            "csv": sha256_bytes(csv_payload),
            "exceptions": sha256_bytes(exceptions_payload),
        },
        "notes": [
            "Duplicate displayed ERF numbers are allowed and preserved.",
            "Duplicate erfId values are blocking.",
            "No GPS values were calculated; centroid.lat and centroid.lng were copied from the source.",
            "Source geometry JSON text was parsed and validated before output.",
        ],
    }
    summary_payload = json_bytes(summary)

    if args.preflight_only:
        if status == "PASSED":
            print("\n[PREFLIGHT OK] The source passed the complete ERF lookup validation.")
            print("No lookup files were written.")
            return

        print("\n[PREFLIGHT FAILED]")
        for reason in blocking_reasons:
            print(f"  - {reason}")
        print("No lookup files were written.")
        raise SystemExit(1)

    if status != "PASSED":
        output_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(output_paths["exceptions"], exceptions_payload)
        write_atomic(output_paths["summary"], summary_payload)
        print(f"\n[BLOCKED] {len(exceptions):,} exception(s) detected.")
        print(f"[EXCEPTIONS] {output_paths['exceptions']}")
        print(f"[SUMMARY] {output_paths['summary']}")
        print("No final JSONL or CSV lookup was written.")
        raise SystemExit(1)

    payloads = {
        "jsonl": jsonl_payload,
        "csv": csv_payload,
        "exceptions": exceptions_payload,
        "summary": summary_payload,
    }
    results = write_controlled_outputs(
        payloads,
        output_paths,
        replace_existing=args.replace_existing,
    )

    print("\n[OUTPUTS]")
    for key in ("jsonl", "csv", "exceptions", "summary"):
        path = output_paths[key]
        print(f"  [{results[key].upper()}] {path}")
        print(f"    SHA-256: {sha256_file(path)}")

    print("\n[OK] Stage M01 completed.")
    print("The cadastral source file was not modified.")
    print("No Firebase or Firestore reads or writes were performed.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FAILED] {exc}")
        raise SystemExit(1)
