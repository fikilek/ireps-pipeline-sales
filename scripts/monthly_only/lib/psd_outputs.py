"""Output serialization and controlled file writing for Stage M02."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from io import StringIO
from pathlib import Path
from typing import Any

from lib.psd_model import END_IDENTITY_COLUMNS
from lib.psd_model import EnrichedMeter, SALES_MONTH_COLUMNS, UNITS_MONTH_COLUMNS
from lib.psd_model import compact_json, format_number, join_unique


ENRICHMENT_COLUMNS = [
    "AccountNumberNormalized", "ElmAccountMatched", "ErfNumber",
    "ErfNumberCount", "ErfCandidateCount", "HasUsableGps", "GpsMatchStatus",
    "ErfId", "WardNumber", "WardPcode", "LmPcode", "Latitude", "Longitude",
    "Geometry", "ErfCandidatesJson", "ElmSourceRows", "trnBatchIds",
]

UNMATCHED_COLUMNS = [
    "SourceEndRow", "MeterNumber", "AccountNumber", "AccountNumberNormalized",
    "GpsMatchStatus", "ErfNumber", "MissingErfNumbers", "ElmSourceRows",
]

CANDIDATE_COLUMNS = [
    "SourceEndRow", "MeterNumber", "AccountNumber", "AccountNumberNormalized",
    "ErfNumber", "ErfId", "WardNumber", "WardPcode", "LmPcode", "Latitude",
    "Longitude", "GeometryJson",
]


def output_paths(output_dir: Path, lm_pcode: str) -> dict[str, Path]:
    stem = f"enriched_psd__{lm_pcode}__2023-12_to_2026-06"
    return {
        "jsonl": output_dir / f"{stem}.jsonl",
        "csv": output_dir / f"{stem}.csv",
        "candidates": output_dir / f"{stem}__gps_candidates.csv",
        "unmatched": output_dir / f"{stem}__unmatched_meters.csv",
        "summary": output_dir / f"{stem}__summary.json",
    }


def build_jsonl(records: list[EnrichedMeter]) -> tuple[bytes, dict[str, int]]:
    lines: list[str] = []
    max_document_bytes = 0
    documents_over_900kb = 0
    for record in records:
        line = compact_json(record.as_json_document())
        line_bytes = len(line.encode("utf-8"))
        max_document_bytes = max(max_document_bytes, line_bytes)
        documents_over_900kb += line_bytes > 900_000
        lines.append(line)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return payload, {
        "maxJsonlDocumentBytes": max_document_bytes,
        "jsonlDocumentsOver900000Bytes": documents_over_900kb,
    }


def build_main_csv(records: list[EnrichedMeter]) -> bytes:
    columns = (
        ["SourceEndRow"]
        + [header for _column, header in END_IDENTITY_COLUMNS]
        + ENRICHMENT_COLUMNS
        + [f"Sales_{month}" for _column, month in SALES_MONTH_COLUMNS]
        + [f"Units_{month}" for _column, month in UNITS_MONTH_COLUMNS]
    )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(record.as_csv_row())
    return buffer.getvalue().encode("utf-8-sig")


def build_candidate_csv(records: list[EnrichedMeter]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CANDIDATE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        for candidate in record.candidates:
            writer.writerow(
                {
                    "SourceEndRow": record.source_end_row,
                    "MeterNumber": record.identity["MeterNumber"],
                    "AccountNumber": record.identity["AccountNumber"],
                    "AccountNumberNormalized": record.account_number_normalized,
                    "ErfNumber": candidate.erf_number,
                    "ErfId": candidate.erf_id,
                    "WardNumber": candidate.ward_number,
                    "WardPcode": candidate.ward_pcode,
                    "LmPcode": candidate.lm_pcode,
                    "Latitude": format_number(candidate.latitude),
                    "Longitude": format_number(candidate.longitude),
                    "GeometryJson": compact_json(candidate.geometry),
                }
            )
    return buffer.getvalue().encode("utf-8-sig")


def build_unmatched_csv(records: list[EnrichedMeter]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=UNMATCHED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        if record.has_usable_gps:
            continue
        writer.writerow(
            {
                "SourceEndRow": record.source_end_row,
                "MeterNumber": record.identity["MeterNumber"],
                "AccountNumber": record.identity["AccountNumber"],
                "AccountNumberNormalized": record.account_number_normalized,
                "GpsMatchStatus": record.gps_match_status,
                "ErfNumber": join_unique(record.erf_numbers),
                "MissingErfNumbers": join_unique(record.missing_erf_numbers),
                "ElmSourceRows": join_unique(str(row) for row in record.elm_source_rows),
            }
        )
    return buffer.getvalue().encode("utf-8-sig")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_outputs(
    paths: dict[str, Path],
    payloads: dict[str, bytes],
    replace_existing: bool,
) -> dict[str, str]:
    results: dict[str, str] = {}
    planned: list[tuple[str, Path, bytes]] = []

    for key, payload in payloads.items():
        path = paths[key]
        if path.is_file() and sha256_file(path) == sha256_bytes(payload):
            results[key] = "unchanged"
            continue
        if path.exists() and not replace_existing:
            raise FileExistsError(
                f"Different existing output found: {path}. Review it, then rerun "
                "with --replace-existing only when replacement is approved."
            )
        planned.append((key, path, payload))

    for key, path, payload in planned:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        results[key] = "written"

    return results
