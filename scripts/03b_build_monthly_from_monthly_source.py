"""
Stage 03B: build governed monthly-origin sales outputs from a cleaned meter snapshot.

This is the non-Atomic companion to Stage 03. It does NOT invent Atomic facts.

Input:
    JSONL with one cleaned meter record per line. Required commercial fields:
      meterNo, meterNoNormalized, lmPcode, monthlySalesC, monthlyUnits,
      totalSalesC, totalUnits, sourceDocumentId, sourceEndRow.

Outputs (one set per month):
    output/monthly/monthly__FULL__YYYY-MM__from_monthly_source.csv
    output/monthly_lm/monthly_lm__FULL__YYYY-MM__from_monthly_source.csv
    output/monthly_lm_groups/monthly_lm_groups__FULL__YYYY-MM__from_monthly_source.csv
    output/logs/monthly_source_build/
        stage03b_monthly_source_build__<LM>__YYYY-MM__<run>.json

Important:
    - no Firestore access;
    - no Atomic documents are created;
    - purchasesCount, cost/vat split, and purchase timestamps are intentionally absent;
    - zero-sales meter-months are retained when the source contains Units for that month;
    - existing output files are never overwritten with different bytes.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MONTHLY_DIR = PROJECT_ROOT / "output" / "monthly"
DEFAULT_MONTHLY_LM_DIR = PROJECT_ROOT / "output" / "monthly_lm"
DEFAULT_MONTHLY_LM_GROUPS_DIR = PROJECT_ROOT / "output" / "monthly_lm_groups"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_source_build"

RUN_TAG = "FULL"
SOURCE_ORIGIN = "monthly_source"
METER_RE = re.compile(r"^[A-Z0-9]+$")
LM_RE = re.compile(r"^[A-Z0-9_-]+$")
PROVIDER_RE = re.compile(r"^[a-z0-9_-]+$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FORBIDDEN_INPUT_FIELDS = {
    "tbRefs",
    "batchFail",
    "trnBatchIds",
    "loadedAt",
    "geofenceRefs",
}

MONTHLY_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "meterNo",
    "ym",
    "y",
    "m",
    "amountTotalC",
    "unitsTotal",
    "salesGroupId",
    "salesGroupLabel",
    "sourceDocumentId",
    "sourceEndRow",
]

MONTHLY_LM_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "ym",
    "y",
    "m",
    "metersCount",
    "amountTotalC",
    "unitsTotal",
    "zeroSalesMetersCount",
]

MONTHLY_LM_GROUP_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "ym",
    "y",
    "m",
    "salesGroupId",
    "salesGroupLabel",
    "metersCount",
    "amountTotalC",
    "unitsTotal",
    "zeroSalesMetersCount",
]


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    sha256: str
    rows: int


@dataclass
class Stats:
    input_records: int = 0
    generated_meter_month_rows: int = 0
    zero_sales_meter_month_rows: int = 0
    total_sales_c: int = 0
    total_units: Decimal = Decimal("0.0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build monthly-origin meter, LM, and LM-group CSVs from a cleaned JSONL "
            "snapshot without manufacturing Atomic facts."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--from-month", required=True)
    parser.add_argument("--to-month", required=True)
    parser.add_argument("--expected-input-sha256")
    parser.add_argument("--monthly-dir", type=Path, default=DEFAULT_MONTHLY_DIR)
    parser.add_argument("--monthly-lm-dir", type=Path, default=DEFAULT_MONTHLY_LM_DIR)
    parser.add_argument(
        "--monthly-lm-groups-dir",
        type=Path,
        default=DEFAULT_MONTHLY_LM_GROUPS_DIR,
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write governed CSV/manifests. Without --write, perform a full dry run only.",
    )
    parser.add_argument("--progress-every", type=int, default=1000)
    return parser.parse_args()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_meter_no(value: Any) -> str:
    return "".join(clean_text(value).upper().split())


def validate_month(value: str, label: str) -> None:
    if not MONTH_RE.fullmatch(value):
        raise ValueError(f"{label} must be YYYY-MM; found {value!r}")
    year, month = (int(part) for part in value.split("-"))
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise ValueError(f"{label} is outside the governed range: {value!r}")


def month_range(first: str, last: str) -> list[str]:
    validate_month(first, "--from-month")
    validate_month(last, "--to-month")
    if first > last:
        raise ValueError("--from-month cannot be later than --to-month")
    year, month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    values: list[str] = []
    while (year, month) <= (end_year, end_month):
        values.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return values


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def parse_money_cents(value: Any, *, label: str) -> int:
    if type(value) is bool or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer cents value; found {value!r}")
    if value < 0:
        raise ValueError(f"{label} cannot be negative; found {value!r}")
    return value


def parse_units(value: Any, *, label: str) -> Decimal:
    if type(value) is bool:
        raise ValueError(f"{label} cannot be boolean")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be numeric; found {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be finite and non-negative; found {value!r}")
    normalized = amount.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if amount != normalized:
        raise ValueError(
            f"{label} must already be normalized to one decimal place; found {value!r}"
        )
    return normalized


def sales_group_from_amount_total_c(amount_total_c: int) -> str:
    if amount_total_c <= 9_999:
        return "GR1"
    if amount_total_c <= 29_999:
        return "GR2"
    if amount_total_c <= 49_999:
        return "GR3"
    if amount_total_c <= 99_999:
        return "GR4"
    return "GR5"


def sales_group_label(group_id: str) -> str:
    return {
        "GR1": "<=99.99",
        "GR2": "100-299.99",
        "GR3": "300-499.99",
        "GR4": "500-999.99",
        "GR5": ">=1000",
    }[group_id]


def read_source(
    path: Path,
    *,
    expected_sha256: str | None,
    lm_pcode: str,
    provider: str,
    months: set[str],
    progress_every: int,
) -> tuple[SourceSnapshot, dict[str, list[dict[str, Any]]], Stats]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Input JSONL not found: {resolved}")

    actual_sha = sha256_file(resolved)
    if expected_sha256:
        expected = expected_sha256.strip().lower()
        if not SHA256_RE.fullmatch(expected):
            raise ValueError("--expected-input-sha256 must be a lowercase SHA-256")
        if actual_sha != expected:
            raise ValueError(
                f"Input SHA-256 mismatch: expected {expected}, found {actual_sha}"
            )

    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stats = Stats()
    seen_source_ids: set[str] = set()
    seen_meters: set[str] = set()
    seen_meter_months: set[tuple[str, str]] = set()

    with resolved.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                raise ValueError(f"Blank JSONL line at {line_number}")
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_number}: {exc}") from exc
            if type(record) is not dict:
                raise ValueError(f"JSONL line {line_number} is not an object")

            forbidden = sorted(FORBIDDEN_INPUT_FIELDS.intersection(record))
            if forbidden:
                raise ValueError(
                    f"Clean input line {line_number} still contains forbidden field(s): "
                    + ", ".join(forbidden)
                )

            source_id = clean_text(record.get("sourceDocumentId"))
            if not source_id:
                raise ValueError(f"Line {line_number}: sourceDocumentId is blank")
            if source_id in seen_source_ids:
                raise ValueError(f"Duplicate sourceDocumentId: {source_id!r}")
            seen_source_ids.add(source_id)

            meter_raw = clean_text(record.get("meterNo"))
            meter_normalized = normalize_meter_no(record.get("meterNoNormalized"))
            meter = normalize_meter_no(meter_raw)
            if not meter or not METER_RE.fullmatch(meter):
                raise ValueError(f"Line {line_number}: invalid meterNo {meter_raw!r}")
            if meter_raw != meter or meter_normalized != meter:
                raise ValueError(
                    f"Line {line_number}: meter identity is not canonical/equal: "
                    f"meterNo={meter_raw!r}, meterNoNormalized={record.get('meterNoNormalized')!r}"
                )
            if meter in seen_meters:
                raise ValueError(f"Duplicate meterNo: {meter!r}")
            seen_meters.add(meter)

            row_lm = clean_text(record.get("lmPcode")).upper()
            if row_lm != lm_pcode:
                raise ValueError(
                    f"Line {line_number}: lmPcode mismatch; expected {lm_pcode!r}, found {row_lm!r}"
                )

            sales = record.get("monthlySalesC")
            units = record.get("monthlyUnits")
            if type(sales) is not dict or type(units) is not dict:
                raise ValueError(
                    f"Line {line_number}: monthlySalesC and monthlyUnits must both be objects"
                )
            if set(sales) != set(units):
                missing_units = sorted(set(sales) - set(units))[:10]
                missing_sales = sorted(set(units) - set(sales))[:10]
                raise ValueError(
                    f"Line {line_number}: Sales/Units month-key mismatch; "
                    f"missingUnits={missing_units}, missingSales={missing_sales}"
                )

            total_sales = 0
            total_units = Decimal("0.0")
            source_end_row_raw = record.get("sourceEndRow")
            if source_end_row_raw in (None, ""):
                source_end_row: int | str = ""
            elif type(source_end_row_raw) is int and source_end_row_raw >= 0:
                source_end_row = source_end_row_raw
            else:
                raise ValueError(
                    f"Line {line_number}: sourceEndRow must be blank or a non-negative integer"
                )

            for ym in sorted(sales):
                validate_month(ym, f"line {line_number} month key")
                if ym not in months:
                    raise ValueError(
                        f"Line {line_number}: month {ym!r} is outside the approved range"
                    )
                amount_c = parse_money_cents(
                    sales[ym], label=f"line {line_number} monthlySalesC[{ym}]"
                )
                unit_value = parse_units(
                    units[ym], label=f"line {line_number} monthlyUnits[{ym}]"
                )
                key = (meter, ym)
                if key in seen_meter_months:
                    raise ValueError(f"Duplicate meter-month generated: {meter}/{ym}")
                seen_meter_months.add(key)

                year, month_no = (int(part) for part in ym.split("-"))
                group_id = sales_group_from_amount_total_c(amount_c)
                by_month[ym].append(
                    {
                        "docId": f"{lm_pcode}__{meter}__{ym}",
                        "sourceOrigin": SOURCE_ORIGIN,
                        "provider": provider,
                        "lmPcode": lm_pcode,
                        "meterNo": meter,
                        "ym": ym,
                        "y": year,
                        "m": month_no,
                        "amountTotalC": amount_c,
                        "unitsTotal": unit_value,
                        "salesGroupId": group_id,
                        "salesGroupLabel": sales_group_label(group_id),
                        "sourceDocumentId": source_id,
                        "sourceEndRow": source_end_row,
                    }
                )
                total_sales += amount_c
                total_units += unit_value
                stats.generated_meter_month_rows += 1
                stats.total_sales_c += amount_c
                stats.total_units += unit_value
                if amount_c == 0:
                    stats.zero_sales_meter_month_rows += 1

            expected_total_sales = parse_money_cents(
                record.get("totalSalesC"), label=f"line {line_number} totalSalesC"
            )
            expected_total_units = parse_units(
                record.get("totalUnits"), label=f"line {line_number} totalUnits"
            )
            if total_sales != expected_total_sales:
                raise ValueError(
                    f"Line {line_number}: totalSalesC does not reconcile; "
                    f"map={total_sales}, declared={expected_total_sales}"
                )
            if total_units != expected_total_units:
                raise ValueError(
                    f"Line {line_number}: totalUnits does not reconcile; "
                    f"map={total_units}, declared={expected_total_units}"
                )

            stats.input_records += 1
            if progress_every > 0 and stats.input_records % progress_every == 0:
                print(
                    f"[PROGRESS] input {stats.input_records:,} | "
                    f"meter-month {stats.generated_meter_month_rows:,} | "
                    f"zero-sales {stats.zero_sales_meter_month_rows:,}"
                )

    if not by_month:
        raise ValueError("Input produced zero meter-month rows")
    missing_source_months = sorted(months - set(by_month))
    if missing_source_months:
        raise ValueError(
            "Approved range contains month(s) with no source meter-month rows: "
            + ", ".join(missing_source_months)
        )

    for ym in by_month:
        by_month[ym].sort(key=lambda row: row["meterNo"])

    return SourceSnapshot(resolved, actual_sha, stats.input_records), by_month, stats


def format_units(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1')):.1f}"


def csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        output: dict[str, Any] = {}
        for column in columns:
            value = row.get(column, "")
            if isinstance(value, Decimal):
                value = format_units(value)
            output[column] = value
        writer.writerow(output)
    return buffer.getvalue().encode("utf-8")


def build_lm_row(
    rows: list[dict[str, Any]], *, provider: str, lm_pcode: str, ym: str
) -> dict[str, Any]:
    amount = sum(int(row["amountTotalC"]) for row in rows)
    units = sum((row["unitsTotal"] for row in rows), Decimal("0.0"))
    zero_sales = sum(1 for row in rows if int(row["amountTotalC"]) == 0)
    year, month_no = (int(part) for part in ym.split("-"))
    return {
        "docId": f"{lm_pcode}__{ym}",
        "sourceOrigin": SOURCE_ORIGIN,
        "provider": provider,
        "lmPcode": lm_pcode,
        "ym": ym,
        "y": year,
        "m": month_no,
        "metersCount": len(rows),
        "amountTotalC": amount,
        "unitsTotal": units,
        "zeroSalesMetersCount": zero_sales,
    }


def build_group_rows(
    rows: list[dict[str, Any]], *, provider: str, lm_pcode: str, ym: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["salesGroupId"])].append(row)
    year, month_no = (int(part) for part in ym.split("-"))
    result: list[dict[str, Any]] = []
    for group_id in sorted(grouped):
        group_rows = grouped[group_id]
        result.append(
            {
                "docId": f"{lm_pcode}__{ym}__{group_id}",
                "sourceOrigin": SOURCE_ORIGIN,
                "provider": provider,
                "lmPcode": lm_pcode,
                "ym": ym,
                "y": year,
                "m": month_no,
                "salesGroupId": group_id,
                "salesGroupLabel": sales_group_label(group_id),
                "metersCount": len(group_rows),
                "amountTotalC": sum(int(row["amountTotalC"]) for row in group_rows),
                "unitsTotal": sum(
                    (row["unitsTotal"] for row in group_rows), Decimal("0.0")
                ),
                "zeroSalesMetersCount": sum(
                    1 for row in group_rows if int(row["amountTotalC"]) == 0
                ),
            }
        )
    return result


def reconcile_month(
    monthly: list[dict[str, Any]],
    lm_row: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    if len({row["docId"] for row in monthly}) != len(monthly):
        raise ValueError("monthly output has duplicate docId values")
    if len({row["meterNo"] for row in monthly}) != len(monthly):
        raise ValueError("monthly output has duplicate meterNo values")
    if len({row["docId"] for row in groups}) != len(groups):
        raise ValueError("monthly_lm_groups output has duplicate docId values")

    amount = sum(int(row["amountTotalC"]) for row in monthly)
    units = sum((row["unitsTotal"] for row in monthly), Decimal("0.0"))
    zero_sales = sum(1 for row in monthly if int(row["amountTotalC"]) == 0)

    if int(lm_row["metersCount"]) != len(monthly):
        raise ValueError("monthly_lm metersCount does not reconcile")
    if int(lm_row["amountTotalC"]) != amount:
        raise ValueError("monthly_lm amountTotalC does not reconcile")
    if lm_row["unitsTotal"] != units:
        raise ValueError("monthly_lm unitsTotal does not reconcile")
    if int(lm_row["zeroSalesMetersCount"]) != zero_sales:
        raise ValueError("monthly_lm zeroSalesMetersCount does not reconcile")

    if sum(int(row["metersCount"]) for row in groups) != len(monthly):
        raise ValueError("monthly_lm_groups metersCount does not reconcile")
    if sum(int(row["amountTotalC"]) for row in groups) != amount:
        raise ValueError("monthly_lm_groups amountTotalC does not reconcile")
    if sum((row["unitsTotal"] for row in groups), Decimal("0.0")) != units:
        raise ValueError("monthly_lm_groups unitsTotal does not reconcile")
    if sum(int(row["zeroSalesMetersCount"]) for row in groups) != zero_sales:
        raise ValueError("monthly_lm_groups zeroSalesMetersCount does not reconcile")

    return {
        "metersCount": len(monthly),
        "amountTotalC": amount,
        "unitsTotal": format_units(units),
        "zeroSalesMetersCount": zero_sales,
    }


def guarded_write(path: Path, payload: bytes, *, write: bool) -> str:
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise ValueError(
                f"Refusing to overwrite existing different file: {path}\n"
                f"existingSha256={sha256_bytes(existing)}\n"
                f"plannedSha256={sha256_bytes(payload)}"
            )
        return "UNCHANGED"
    if not write:
        return "PLANNED"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise ValueError(f"Temporary output already exists: {temporary}")
    try:
        temporary.write_bytes(payload)
        if temporary.read_bytes() != payload:
            raise ValueError(f"Temporary output verification failed: {temporary}")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "WRITTEN"


def run_id(now: dt.datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def main() -> int:
    args = parse_args()
    started = dt.datetime.now(dt.timezone.utc)

    lm_pcode = clean_text(args.lm_pcode).upper()
    provider = clean_text(args.provider).lower()
    if not LM_RE.fullmatch(lm_pcode):
        raise ValueError(f"Invalid --lm-pcode: {lm_pcode!r}")
    if not PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"Invalid --provider: {provider!r}")

    approved_months = month_range(args.from_month, args.to_month)
    approved_set = set(approved_months)

    print("=" * 72)
    print("iREPS SALES PIPELINE — STAGE 03B MONTHLY-SOURCE ADAPTER")
    print("=" * 72)
    print(f"Input       : {args.input.expanduser().resolve()}")
    print(f"LM          : {lm_pcode}")
    print(f"Provider    : {provider}")
    print(f"Range       : {args.from_month} -> {args.to_month}")
    print(f"Mode        : {'BUILD-WRITE' if args.write else 'DRY-RUN'}")
    print("Firestore   : NONE")
    print("Atomic facts: NEVER FABRICATED")
    print("=" * 72)

    source, by_month, stats = read_source(
        args.input,
        expected_sha256=args.expected_input_sha256,
        lm_pcode=lm_pcode,
        provider=provider,
        months=approved_set,
        progress_every=args.progress_every,
    )

    plans: list[dict[str, Any]] = []
    month_manifests: list[tuple[Path, bytes]] = []
    state_counts = defaultdict(int)

    for ym in approved_months:
        month_state_counts = defaultdict(int)
        monthly_rows = by_month[ym]
        lm_row = build_lm_row(monthly_rows, provider=provider, lm_pcode=lm_pcode, ym=ym)
        group_rows = build_group_rows(
            monthly_rows, provider=provider, lm_pcode=lm_pcode, ym=ym
        )
        reconciliation = reconcile_month(monthly_rows, lm_row, group_rows)

        output_specs = [
            (
                "monthly",
                args.monthly_dir.expanduser().resolve()
                / f"monthly__{RUN_TAG}__{ym}__from_monthly_source.csv",
                monthly_rows,
                MONTHLY_COLUMNS,
            ),
            (
                "monthly_lm",
                args.monthly_lm_dir.expanduser().resolve()
                / f"monthly_lm__{RUN_TAG}__{ym}__from_monthly_source.csv",
                [lm_row],
                MONTHLY_LM_COLUMNS,
            ),
            (
                "monthly_lm_groups",
                args.monthly_lm_groups_dir.expanduser().resolve()
                / f"monthly_lm_groups__{RUN_TAG}__{ym}__from_monthly_source.csv",
                group_rows,
                MONTHLY_LM_GROUP_COLUMNS,
            ),
        ]

        outputs: list[dict[str, Any]] = []
        for dataset, path, rows, columns in output_specs:
            payload = csv_bytes(rows, columns)
            state = guarded_write(path, payload, write=args.write)
            state_counts[state] += 1
            month_state_counts[state] += 1
            outputs.append(
                {
                    "dataset": dataset,
                    "month": ym,
                    "path": str(path),
                    "filename": path.name,
                    "rows": len(rows),
                    "columns": columns,
                    "sha256": sha256_bytes(payload),
                    "writeState": state,
                }
            )

        month_manifest = {
            "schemaVersion": 1,
            "stage": "03B",
            "script": "03b_build_monthly_from_monthly_source.py",
            "status": "PASS",
            "result": "BUILD_WRITTEN" if args.write else "DRY_RUN_PASS",
            "operation": "build-write" if args.write else "dry-run",
            "sourceOrigin": SOURCE_ORIGIN,
            "provider": provider,
            "lmPcode": lm_pcode,
            "month": ym,
            "sourceInput": {
                "path": str(source.path),
                "filename": source.path.name,
                "sha256": source.sha256,
                "rows": source.rows,
            },
            "sourceFacts": {
                "purchasesCountAvailable": False,
                "costVatBreakdownAvailable": False,
                "purchaseTimestampsAvailable": False,
                "atomicTransactionsAvailable": False,
            },
            "reconciliation": {
                "lmPcode": lm_pcode,
                "month": ym,
                **reconciliation,
            },
            "outputs": outputs,
            "writeSummary": dict(sorted(month_state_counts.items())),
            "builtAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        manifest_core = dict(month_manifest)
        month_manifest["buildFingerprint"] = sha256_bytes(
            canonical_json_bytes(manifest_core)
        )
        manifest_path = (
            args.log_dir.expanduser().resolve()
            / f"stage03b_monthly_source_build__{lm_pcode}__{ym}__{run_id(started)}.json"
        )
        month_manifests.append((manifest_path, canonical_json_bytes(month_manifest)))
        plans.append(
            {
                "month": ym,
                "meterRows": len(monthly_rows),
                "amountTotalC": reconciliation["amountTotalC"],
                "unitsTotal": reconciliation["unitsTotal"],
                "zeroSalesMetersCount": reconciliation["zeroSalesMetersCount"],
                "groups": len(group_rows),
            }
        )
        print(
            f"[MONTH {ym}] meters {len(monthly_rows):,} | "
            f"salesC {reconciliation['amountTotalC']:,} | "
            f"units {reconciliation['unitsTotal']} | "
            f"zero-sales {reconciliation['zeroSalesMetersCount']:,}"
        )

    if args.write:
        for manifest_path, manifest_payload in month_manifests:
            state = guarded_write(manifest_path, manifest_payload, write=True)
            if state not in {"WRITTEN", "UNCHANGED"}:
                raise ValueError(f"Unexpected manifest write state: {state}")

    summary = {
        "schemaVersion": 1,
        "stage": "03B",
        "script": "03b_build_monthly_from_monthly_source.py",
        "status": "PASS",
        "result": "BUILD_WRITTEN" if args.write else "DRY_RUN_PASS",
        "sourceOrigin": SOURCE_ORIGIN,
        "provider": provider,
        "lmPcode": lm_pcode,
        "fromMonth": args.from_month,
        "toMonth": args.to_month,
        "sourceInput": {
            "path": str(source.path),
            "sha256": source.sha256,
            "rows": source.rows,
        },
        "stats": {
            "inputRecords": stats.input_records,
            "meterMonthRows": stats.generated_meter_month_rows,
            "zeroSalesMeterMonthRows": stats.zero_sales_meter_month_rows,
            "totalSalesC": stats.total_sales_c,
            "totalUnits": format_units(stats.total_units),
            "months": len(approved_months),
        },
        "months": plans,
        "sourceFacts": {
            "purchasesCountAvailable": False,
            "costVatBreakdownAvailable": False,
            "purchaseTimestampsAvailable": False,
            "atomicTransactionsAvailable": False,
        },
        "finishedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firestoreAccess": "NONE",
        "firestoreWrites": 0,
    }
    summary["buildFingerprint"] = sha256_bytes(canonical_json_bytes(summary))

    if args.write:
        summary_path = (
            args.log_dir.expanduser().resolve()
            / f"stage03b_monthly_source_summary__{lm_pcode}__{args.from_month}_to_{args.to_month}__{run_id(started)}.json"
        )
        guarded_write(summary_path, canonical_json_bytes(summary), write=True)

    print("=" * 72)
    print("STAGE 03B COMPLETE")
    print("=" * 72)
    print(f"Status                 : PASS")
    print(f"Result                 : {summary['result']}")
    print(f"Input records          : {stats.input_records:,}")
    print(f"Months                 : {len(approved_months):,}")
    print(f"Meter-month rows       : {stats.generated_meter_month_rows:,}")
    print(f"Zero-sales meter-month : {stats.zero_sales_meter_month_rows:,}")
    print(f"Total sales cents      : {stats.total_sales_c:,}")
    print(f"Total units            : {format_units(stats.total_units)}")
    print(f"Input SHA256           : {source.sha256}")
    print(f"Firestore writes       : 0")
    print(f"Atomic facts fabricated: 0")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
