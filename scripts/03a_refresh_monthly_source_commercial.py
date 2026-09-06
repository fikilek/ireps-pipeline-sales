#!/usr/bin/env python3
"""Stage 03A: refresh the cumulative monthly-source commercial population.

This adapter is local-only. It never connects to Firestore and never deletes an
existing meter identity.

Recurring monthly model:
- ``--baseline`` is the cumulative governed commercial JSONL (all meter identities
  ever legitimately ingested by the Sales Pipeline);
- ``--previous-snapshot`` is the prior supplier-month membership snapshot used to
  calculate month-to-month population movement;
- ``--workbook`` is the new supplier workbook;
- exact meter number remains the authoritative identity key;
- incoming meter identities absent from the cumulative baseline are appended;
- a newly appended meter is a replacement only when its ``PreviousMeterNumber``
  points to an already-known cumulative meter identity; otherwise it is new;
- old identities are retained even when absent from the new supplier snapshot;
- replacement/removal/new classifications are audit evidence in the run report and
  monthly snapshot, not new Firestore fields.

For the first governed recurring run, ``--bootstrap-previous-from-baseline`` may
be used instead of ``--previous-snapshot``. That mode is explicit and requires
every baseline record to end at the month immediately before ``--from-month``.
Subsequent runs consume the previous successful Stage 03A snapshot.

Safety:
- exact SHA-256 checks for cumulative baseline, workbook, and prior snapshot;
- exact LM/provider/run identity supplied by CLI;
- workbook meter numbers must be text and canonical;
- duplicate/blank meters fail;
- the two workbook monthly blocks must be structurally identical;
- monetary values are normalized to cents with <= 0.000001 source noise;
- units are normalized to one decimal with <= 0.000001 source noise;
- existing baseline non-purchase fields are byte-semantically preserved;
- existing sales history is never transferred to a replacement meter;
- cumulative output population is append-only: baseline + newly seen identities;
- no output JSONL or monthly snapshot is written unless ``--write`` is supplied;
- a JSON reconciliation report is always written to the explicit ``--report`` path.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET
from zipfile import ZipFile

METER_RE = re.compile(r"^[A-Z0-9]+$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
PROVIDER_RE = re.compile(r"^[a-z0-9_-]+$")

COMMERCIAL_WORKBOOK_HEADERS = (
    "Customer",
    "TariffInstance",
    "MeterNumber",
    "InstallationDate",
    "PreviousMeterNumber",
    "PreviousInstallationDate",
    "StandNumber",
    "Surname",
    "AddressLine1",
    "AddressLine2",
    "Town",
    "PostalAddress1",
    "PostalAddress2",
    "PostalAddressTown",
    "AccountNumber",
)
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_TYPE = "monthly_supplier_snapshot"

MUTABLE_FIELDS = frozenset(
    {
        "monthlySalesC",
        "monthlyUnits",
        "totalSalesC",
        "totalUnits",
        "salesPeriodFrom",
        "salesPeriodTo",
    }
)
FORBIDDEN_BASELINE_FIELDS = frozenset({"tbRefs", "batchFail", "trnBatchIds", "geofenceRefs"})

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class MonthValue:
    amount_c: int
    units: Decimal


@dataclass(frozen=True)
class WorkbookSnapshot:
    sha256: str
    filename: str
    rows: int
    months: list[str]
    meter_months: dict[str, dict[str, MonthValue]]
    commercial: dict[str, dict[str, str]]
    worksheet_rows: dict[str, int]
    totals: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SupplierSnapshot:
    sha256: str | None
    current_month: str
    meters: list[str]
    lm_pcode: str
    provider: str
    workbook_sha256: str | None
    source: str


@dataclass(frozen=True)
class BaselineSnapshot:
    sha256: str
    records: list[dict[str, Any]]
    by_meter: dict[str, dict[str, Any]]
    raw_lines: int
    nonpurchase_sha256: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_meter(value: Any) -> str:
    return "".join(clean_text(value).upper().split())


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return sha256_bytes(raw)


def validate_sha(value: str, label: str) -> str:
    normalized = clean_text(value).lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase 64-character SHA-256")
    return normalized


def month_range(first: str, last: str) -> list[str]:
    if not MONTH_RE.fullmatch(first) or not MONTH_RE.fullmatch(last):
        raise ValueError("Month range must use YYYY-MM")
    fy, fm = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    if (fy, fm) > (ly, lm):
        raise ValueError("--from-month may not be later than --to-month")
    values: list[str] = []
    y, m = fy, fm
    while (y, m) <= (ly, lm):
        values.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1
            m = 1
    return values


def previous_month(month: str) -> str:
    if not MONTH_RE.fullmatch(month):
        raise ValueError(f"Invalid month: {month!r}")
    year, month_no = (int(part) for part in month.split("-"))
    month_no -= 1
    if month_no == 0:
        year -= 1
        month_no = 12
    return f"{year:04d}-{month_no:02d}"


def _progress(label: str, done: int, total: int, started: float) -> None:
    elapsed = time.monotonic() - started
    percent = 100.0 if total <= 0 else min(100.0, (done / total) * 100.0)
    print(f"[{label}] {done:,}/{total:,} ({percent:5.1f}%) | elapsed {elapsed:,.1f}s", flush=True)


def _col_number(cell_ref: str) -> int:
    match = CELL_REF_RE.fullmatch(cell_ref)
    if not match:
        raise ValueError(f"Invalid XLSX cell reference: {cell_ref!r}")
    result = 0
    for char in match.group(1):
        result = result * 26 + (ord(char) - 64)
    return result


def _excel_month(raw: str) -> str | None:
    text = clean_text(raw)
    if not text:
        return None
    try:
        serial = Decimal(text)
    except InvalidOperation:
        return None
    if serial < 20_000 or serial > 80_000:
        return None
    day = datetime(1899, 12, 30) + timedelta(days=int(serial))
    return day.strftime("%Y-%m")


def _shared_strings(zf: ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    ns = {"m": NS_MAIN}
    return [
        "".join((node.text or "") for node in item.iter(f"{{{NS_MAIN}}}t"))
        for item in root.findall("m:si", ns)
    ]


def _sheet_path(zf: ZipFile, sheet_name: str) -> str:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    ns = {"m": NS_MAIN, "r": NS_REL_DOC}
    rel_id: str | None = None
    for sheet in wb.findall("m:sheets/m:sheet", ns):
        if sheet.attrib.get("name") == sheet_name:
            rel_id = sheet.attrib.get(f"{{{NS_REL_DOC}}}id")
            break
    if not rel_id:
        raise ValueError(f"Workbook has no sheet named {sheet_name!r}")

    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    target: str | None = None
    for rel in rels.findall(f"{{{NS_REL_PKG}}}Relationship"):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target")
            break
    if not target:
        raise ValueError(f"Unable to resolve workbook relationship for sheet {sheet_name!r}")

    if target.startswith("/"):
        return target.lstrip("/")
    return str(PurePosixPath("xl") / PurePosixPath(target))


def _cell_value(cell: ET.Element, shared: Sequence[str]) -> tuple[str, str]:
    cell_type = cell.attrib.get("t", "n")
    if cell_type == "inlineStr":
        text = "".join((node.text or "") for node in cell.iter(f"{{{NS_MAIN}}}t"))
        return text, cell_type
    value = cell.find(f"{{{NS_MAIN}}}v")
    raw = "" if value is None or value.text is None else value.text
    if cell_type == "s" and raw:
        index = int(raw)
        if index < 0 or index >= len(shared):
            raise ValueError(f"Shared-string index out of range: {index}")
        return shared[index], cell_type
    return raw, cell_type


def _header_month_blocks(header: Mapping[int, tuple[str, str]]) -> list[list[tuple[int, str]]]:
    month_by_col: dict[int, str] = {}
    for col, (raw, cell_type) in header.items():
        if cell_type not in {"n", ""}:
            continue
        month = _excel_month(raw)
        if month is not None:
            month_by_col[col] = month

    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    previous: int | None = None
    for col in sorted(month_by_col):
        if previous is None or col == previous + 1:
            current.append((col, month_by_col[col]))
        else:
            if current:
                blocks.append(current)
            current = [(col, month_by_col[col])]
        previous = col
    if current:
        blocks.append(current)
    return [block for block in blocks if len(block) >= 2]


def _decimal_from_cell(raw: str, *, label: str, row_number: int) -> Decimal:
    text = clean_text(raw)
    if not text:
        return Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not numeric at row {row_number}: {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and non-negative at row {row_number}")
    return value


def _money_to_cents(raw: str, *, row_number: int, month: str) -> int:
    value = _decimal_from_cell(raw, label=f"Sales {month}", row_number=row_number)
    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(value - normalized) > Decimal("0.000001"):
        raise ValueError(
            f"Sales {month} has more than two meaningful decimal places at row {row_number}: {value}"
        )
    return int((normalized * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _units_to_decimal(raw: str, *, row_number: int, month: str) -> Decimal:
    value = _decimal_from_cell(raw, label=f"Units {month}", row_number=row_number)
    normalized = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if abs(value - normalized) > Decimal("0.000001"):
        raise ValueError(
            f"Units {month} has more than one meaningful decimal place at row {row_number}: {value}"
        )
    return normalized


def read_workbook(
    path: Path,
    *,
    expected_sha256: str,
    sheet_name: str,
    target_months: Sequence[str],
    progress_every: int,
) -> WorkbookSnapshot:
    if not path.is_file():
        raise FileNotFoundError(f"Workbook not found: {path}")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha256:
        raise ValueError(
            f"Workbook SHA256 mismatch: expected={expected_sha256}; actual={actual_sha}"
        )

    started = time.monotonic()
    with ZipFile(path) as zf:
        shared = _shared_strings(zf)
        sheet_path = _sheet_path(zf, sheet_name)
        root = ET.fromstring(zf.read(sheet_path))
        rows = root.find(f"{{{NS_MAIN}}}sheetData")
        if rows is None:
            raise ValueError(f"Sheet {sheet_name!r} has no sheetData")
        row_nodes = rows.findall(f"{{{NS_MAIN}}}row")
        if not row_nodes:
            raise ValueError(f"Sheet {sheet_name!r} is empty")

        header: dict[int, tuple[str, str]] = {}
        for cell in row_nodes[0].findall(f"{{{NS_MAIN}}}c"):
            header[_col_number(cell.attrib["r"])] = _cell_value(cell, shared)

        header_positions: dict[str, int] = {}
        for name in COMMERCIAL_WORKBOOK_HEADERS:
            matches = [
                col for col, (raw, _type) in header.items() if clean_text(raw) == name
            ]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {name} header; found {matches}")
            header_positions[name] = matches[0]
        meter_col = header_positions["MeterNumber"]

        blocks = _header_month_blocks(header)
        if len(blocks) != 2:
            raise ValueError(
                "Expected exactly two contiguous monthly header blocks (Sales and Units); "
                f"found {len(blocks)}"
            )
        sales_block, units_block = blocks
        sales_months = [month for _, month in sales_block]
        units_months = [month for _, month in units_block]
        if sales_months != units_months:
            raise ValueError("Sales and Units monthly header blocks are not identical")
        if len(set(sales_months)) != len(sales_months):
            raise ValueError("Workbook monthly header block contains duplicate months")
        if sales_months != sorted(sales_months):
            raise ValueError("Workbook monthly header block is not chronological")

        sales_col = {month: col for col, month in sales_block}
        units_col = {month: col for col, month in units_block}
        missing = [m for m in target_months if m not in sales_col or m not in units_col]
        if missing:
            raise ValueError("Workbook is missing approved month(s): " + ", ".join(missing))
        target_end = max(target_months)
        usable_months = [month for month in sales_months if month <= target_end]
        if not usable_months or usable_months[-1] != target_end:
            raise ValueError(
                f"Workbook does not provide a continuous usable history through {target_end}"
            )

        meter_months: dict[str, dict[str, MonthValue]] = {}
        commercial: dict[str, dict[str, str]] = {}
        worksheet_rows: dict[str, int] = {}
        totals = {
            month: {
                "salesTotalC": 0,
                "unitsTotal": Decimal("0.0"),
                "purchasingMeters": 0,
                "noPurchaseMeters": 0,
            }
            for month in target_months
        }
        total_rows = len(row_nodes) - 1
        for index, row in enumerate(row_nodes[1:], start=1):
            excel_row = int(row.attrib.get("r", index + 1))
            values: dict[int, tuple[str, str]] = {}
            for cell in row.findall(f"{{{NS_MAIN}}}c"):
                values[_col_number(cell.attrib["r"])] = _cell_value(cell, shared)

            meter_raw, meter_type = values.get(meter_col, ("", ""))
            if not clean_text(meter_raw):
                raise ValueError(f"Blank MeterNumber at worksheet row {excel_row}")
            if meter_type not in {"s", "inlineStr", "str"}:
                raise ValueError(
                    f"MeterNumber must be stored as text to preserve identity at row {excel_row}; "
                    f"cellType={meter_type!r}"
                )
            meter = normalize_meter(meter_raw)
            if meter_raw != meter or not METER_RE.fullmatch(meter):
                raise ValueError(
                    f"MeterNumber is not canonical uppercase alphanumeric text at row {excel_row}: "
                    f"raw={meter_raw!r}, canonical={meter!r}"
                )
            if meter in meter_months:
                raise ValueError(f"Duplicate MeterNumber in workbook: {meter}")

            commercial_row = {
                name: clean_text(values.get(col, ("", ""))[0])
                for name, col in header_positions.items()
            }
            commercial_row["MeterNumber"] = meter
            commercial[meter] = commercial_row
            worksheet_rows[meter] = excel_row

            month_values: dict[str, MonthValue] = {}
            for month in usable_months:
                amount_raw = values.get(sales_col[month], ("", ""))[0]
                units_raw = values.get(units_col[month], ("", ""))[0]
                amount_c = _money_to_cents(amount_raw, row_number=excel_row, month=month)
                units = _units_to_decimal(units_raw, row_number=excel_row, month=month)
                month_values[month] = MonthValue(amount_c=amount_c, units=units)
                if month in totals:
                    totals[month]["salesTotalC"] += amount_c
                    totals[month]["unitsTotal"] += units
                    if amount_c > 0:
                        totals[month]["purchasingMeters"] += 1
                    else:
                        totals[month]["noPurchaseMeters"] += 1
            meter_months[meter] = month_values

            if progress_every > 0 and (index % progress_every == 0 or index == total_rows):
                _progress("WORKBOOK", index, total_rows, started)

    normalized_totals: dict[str, dict[str, Any]] = {}
    for month in target_months:
        item = totals[month]
        normalized_totals[month] = {
            "salesTotalC": int(item["salesTotalC"]),
            "unitsTotal": format(item["unitsTotal"], "f"),
            "purchasingMeters": int(item["purchasingMeters"]),
            "noPurchaseMeters": int(item["noPurchaseMeters"]),
        }
    return WorkbookSnapshot(
        sha256=actual_sha,
        filename=path.name,
        rows=len(meter_months),
        months=usable_months,
        meter_months=meter_months,
        commercial=commercial,
        worksheet_rows=worksheet_rows,
        totals=normalized_totals,
    )

def _parse_nonnegative_int(value: Any, label: str) -> int:
    if type(value) is bool:
        raise ValueError(f"{label} cannot be boolean")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not numeric: {value!r}") from exc
    if not decimal.is_finite() or decimal < 0 or decimal != decimal.to_integral_value():
        raise ValueError(f"{label} must be a non-negative integer: {value!r}")
    return int(decimal)


def _parse_units(value: Any, label: str) -> Decimal:
    if type(value) is bool:
        raise ValueError(f"{label} cannot be boolean")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not numeric: {value!r}") from exc
    normalized = decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if not decimal.is_finite() or decimal < 0 or decimal != normalized:
        raise ValueError(f"{label} must be finite, non-negative and normalized to one decimal")
    return normalized


def _nonpurchase_projection(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for record in records:
        meter = normalize_meter(record.get("meterNoNormalized") or record.get("meterNo"))
        payload = {key: value for key, value in record.items() if key not in MUTABLE_FIELDS}
        projected.append({"meter": meter, "payload": payload})
    projected.sort(key=lambda item: item["meter"])
    return projected


def read_baseline(
    path: Path,
    *,
    expected_sha256: str,
    lm_pcode: str,
    provider: str,
    progress_every: int,
) -> BaselineSnapshot:
    if not path.is_file():
        raise FileNotFoundError(f"Baseline JSONL not found: {path}")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha256:
        raise ValueError(
            f"Baseline SHA256 mismatch: expected={expected_sha256}; actual={actual_sha}"
        )

    raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not raw_lines:
        raise ValueError("Baseline JSONL is empty")
    records: list[dict[str, Any]] = []
    by_meter: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    total = len(raw_lines)

    for line_no, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            raise ValueError(f"Blank JSONL line at {line_no}")
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at baseline line {line_no}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Baseline line {line_no} is not an object")
        forbidden = sorted(FORBIDDEN_BASELINE_FIELDS.intersection(record))
        if forbidden:
            raise ValueError(
                f"Baseline line {line_no} contains operational field(s): {', '.join(forbidden)}"
            )
        meter_raw = clean_text(record.get("meterNo"))
        meter_normalized = clean_text(record.get("meterNoNormalized"))
        meter = normalize_meter(meter_raw)
        if not meter or not METER_RE.fullmatch(meter):
            raise ValueError(f"Baseline line {line_no}: invalid meterNo {meter_raw!r}")
        if meter_raw != meter or meter_normalized != meter:
            raise ValueError(
                f"Baseline line {line_no}: meter identity mismatch/noncanonical: "
                f"meterNo={meter_raw!r}, meterNoNormalized={meter_normalized!r}"
            )
        if meter in by_meter:
            raise ValueError(f"Duplicate baseline meter: {meter}")
        if clean_text(record.get("lmPcode")).upper() != lm_pcode:
            raise ValueError(
                f"Baseline line {line_no}: lmPcode mismatch; expected {lm_pcode!r}, "
                f"found {record.get('lmPcode')!r}"
            )
        record_provider = clean_text(record.get("provider")).lower()
        if record_provider and record_provider != provider:
            raise ValueError(
                f"Baseline line {line_no}: provider mismatch; expected {provider!r}, "
                f"found {record.get('provider')!r}"
            )

        sales = record.get("monthlySalesC")
        units = record.get("monthlyUnits")
        if not isinstance(sales, dict) or not isinstance(units, dict):
            raise ValueError(f"Baseline {meter}: monthlySalesC/monthlyUnits must both be maps")
        if set(sales) != set(units):
            raise ValueError(f"Baseline {meter}: Sales/Units month-key mismatch")
        if not sales:
            raise ValueError(f"Baseline {meter}: empty monthly history")
        for month in sales:
            if not MONTH_RE.fullmatch(str(month)):
                raise ValueError(f"Baseline {meter}: invalid month key {month!r}")
        sales_total = sum(_parse_nonnegative_int(value, f"{meter} monthlySalesC[{month}]") for month, value in sales.items())
        units_total = sum((_parse_units(value, f"{meter} monthlyUnits[{month}]") for month, value in units.items()), Decimal("0"))
        if _parse_nonnegative_int(record.get("totalSalesC"), f"{meter} totalSalesC") != sales_total:
            raise ValueError(f"Baseline {meter}: totalSalesC does not reconcile")
        if _parse_units(record.get("totalUnits"), f"{meter} totalUnits") != units_total:
            raise ValueError(f"Baseline {meter}: totalUnits does not reconcile")
        period_from = clean_text(record.get("salesPeriodFrom"))
        period_to = clean_text(record.get("salesPeriodTo"))
        if not MONTH_RE.fullmatch(period_from):
            raise ValueError(f"Baseline {meter}: salesPeriodFrom is not YYYY-MM")
        if not MONTH_RE.fullmatch(period_to):
            raise ValueError(f"Baseline {meter}: salesPeriodTo is not YYYY-MM")
        if period_from > period_to:
            raise ValueError(f"Baseline {meter}: salesPeriodFrom is after salesPeriodTo")
        outside_period = sorted(
            month for month in sales
            if str(month) < period_from or str(month) > period_to
        )
        if outside_period:
            raise ValueError(
                f"Baseline {meter}: monthly history contains key(s) outside "
                f"salesPeriodFrom/salesPeriodTo: {outside_period[:10]}"
            )

        records.append(record)
        by_meter[meter] = record
        if progress_every > 0 and (line_no % progress_every == 0 or line_no == total):
            _progress("BASELINE", line_no, total, started)

    nonpurchase_sha = canonical_sha256(_nonpurchase_projection(records))
    return BaselineSnapshot(
        sha256=actual_sha,
        records=records,
        by_meter=by_meter,
        raw_lines=len(raw_lines),
        nonpurchase_sha256=nonpurchase_sha,
    )


def _existing_record_with_extension(
    baseline_record: Mapping[str, Any],
    workbook: WorkbookSnapshot,
    meter: str,
    target_end: str,
    validation_months: Sequence[str],
) -> tuple[dict[str, Any], bool, int]:
    record = copy.deepcopy(dict(baseline_record))
    sales = dict(record["monthlySalesC"])
    units = dict(record["monthlyUnits"])
    period_to = clean_text(record.get("salesPeriodTo"))
    extension_months = [m for m in workbook.months if period_to < m <= target_end]
    changed = False
    idempotent = 0

    # If a requested target month is already present (for example an idempotent
    # rerun/recovery), it must agree exactly with the supplier workbook.
    for month in validation_months:
        if month in sales:
            update = workbook.meter_months[meter][month]
            existing_amount = _parse_nonnegative_int(sales[month], f"{meter} existing {month}")
            if existing_amount != update.amount_c:
                raise ValueError(
                    f"Existing monthlySalesC[{month}] conflicts with workbook for meter {meter}"
                )
            if month not in units or _parse_units(
                units[month], f"{meter} existing units {month}"
            ) != update.units:
                raise ValueError(
                    f"Existing monthlyUnits[{month}] conflicts with workbook for meter {meter}"
                )
            idempotent += 1

    # Existing keys are immutable evidence. If an extension key already exists,
    # it must agree exactly with the new workbook.
    for month in extension_months:
        update = workbook.meter_months[meter][month]
        if month in sales:
            existing_amount = _parse_nonnegative_int(sales[month], f"{meter} existing {month}")
            if existing_amount != update.amount_c:
                raise ValueError(
                    f"Existing monthlySalesC[{month}] conflicts with workbook for meter {meter}"
                )
            idempotent += 1
        if month in units:
            existing_units = _parse_units(units[month], f"{meter} existing units {month}")
            if existing_units != update.units:
                raise ValueError(
                    f"Existing monthlyUnits[{month}] conflicts with workbook for meter {meter}"
                )
        if sales.get(month) != update.amount_c:
            changed = True
        if month not in units or _parse_units(units[month], f"{meter} planned units {month}") != update.units:
            changed = True
        sales[month] = update.amount_c
        units[month] = float(update.units)

    ordered_months = sorted(sales)
    if set(ordered_months) != set(units):
        raise ValueError(f"Refreshed {meter}: Sales/Units month-key mismatch")
    record["monthlySalesC"] = {month: int(sales[month]) for month in ordered_months}
    record["monthlyUnits"] = {
        month: float(_parse_units(units[month], f"{meter} {month} units"))
        for month in ordered_months
    }
    record["totalSalesC"] = sum(record["monthlySalesC"].values())
    total_units = sum(
        (Decimal(str(record["monthlyUnits"][month])) for month in ordered_months),
        Decimal("0"),
    )
    record["totalUnits"] = float(total_units.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    record["salesPeriodFrom"] = clean_text(baseline_record.get("salesPeriodFrom"))
    if extension_months:
        record["salesPeriodTo"] = target_end
    return record, changed, idempotent


def _new_commercial_record(
    workbook: WorkbookSnapshot,
    *,
    meter: str,
    lm_pcode: str,
    provider: str,
    target_end: str,
) -> dict[str, Any]:
    source = workbook.commercial[meter]
    months = [month for month in workbook.months if month <= target_end]
    if not months:
        raise ValueError(f"New meter {meter}: workbook has no usable monthly history")
    sales = {month: workbook.meter_months[meter][month].amount_c for month in months}
    units_dec = {month: workbook.meter_months[meter][month].units for month in months}
    total_units = sum(units_dec.values(), Decimal("0")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    account = clean_text(source.get("AccountNumber"))
    customer_no = clean_text(source.get("Customer"))
    surname = clean_text(source.get("Surname"))
    excel_row = workbook.worksheet_rows[meter]

    # Only source-evidenced commercial fields are populated. Existing CAT/risk and
    # ERF/GPS enrichment are not fabricated for a newly introduced meter identity.
    return {
        "sourceDocumentId": meter,
        "sourceDocumentPath": "",
        "sourceEndRow": excel_row,
        "meterNo": meter,
        "meterNoNormalized": meter,
        "provider": provider,
        "lmPcode": lm_pcode,
        "accountNo": account,
        "accountNumber": account,
        "accountNumberNormalized": account,
        "customerNo": customer_no,
        "customerName": surname,
        "customerSurname": surname,
        "sourceFileName": workbook.filename,
        "sourceRow": excel_row,
        "addressLine1": clean_text(source.get("AddressLine1")),
        "addressLine2": clean_text(source.get("AddressLine2")),
        "town": clean_text(source.get("Town")),
        "postalAddress1": clean_text(source.get("PostalAddress1")),
        "postalAddress2": clean_text(source.get("PostalAddress2")),
        "postalAddressTown": clean_text(source.get("PostalAddressTown")),
        "standNumber": clean_text(source.get("StandNumber")),
        "tariffInstance": clean_text(source.get("TariffInstance")),
        "installationDate": clean_text(source.get("InstallationDate")),
        "previousMeterNumber": clean_text(source.get("PreviousMeterNumber")),
        "previousInstallationDate": clean_text(source.get("PreviousInstallationDate")),
        "leakageCategory": "",
        "riskTier": "",
        "riskScore": "",
        "salesPeriodFrom": min(months),
        "salesPeriodTo": target_end,
        "monthlySalesC": sales,
        "monthlyUnits": {month: float(units_dec[month]) for month in months},
        "totalSalesC": sum(sales.values()),
        "totalUnits": float(total_units),
        "elmAccountMatched": False,
        "elmSourceRows": [],
        "erfCandidateCount": 0,
        "erfCandidates": [],
        "erfNumbers": [],
        "missingErfNumbers": [],
        "gpsMatchStatus": "",
        "hasUsableGps": False,
    }


def _validate_previous_snapshot_payload(
    payload: Mapping[str, Any],
    *,
    lm_pcode: str,
    provider: str,
    expected_month: str,
) -> list[str]:
    if payload.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Previous snapshot schemaVersion mismatch")
    if payload.get("stage") != "03A" or payload.get("snapshotType") != SNAPSHOT_TYPE:
        raise ValueError("Previous snapshot is not a governed Stage 03A supplier snapshot")
    if clean_text(payload.get("lmPcode")).upper() != lm_pcode:
        raise ValueError("Previous snapshot lmPcode mismatch")
    if clean_text(payload.get("provider")).lower() != provider:
        raise ValueError("Previous snapshot provider mismatch")
    if clean_text(payload.get("currentMonth")) != expected_month:
        raise ValueError(
            f"Previous snapshot currentMonth must be {expected_month}; "
            f"found {payload.get('currentMonth')!r}"
        )
    meters = payload.get("meters")
    if not isinstance(meters, list) or not meters:
        raise ValueError("Previous snapshot meters must be a non-empty list")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(meters, start=1):
        meter = normalize_meter(raw)
        if clean_text(raw) != meter or not METER_RE.fullmatch(meter):
            raise ValueError(f"Previous snapshot meter {index} is not canonical: {raw!r}")
        if meter in seen:
            raise ValueError(f"Previous snapshot contains duplicate meter: {meter}")
        seen.add(meter)
        normalized.append(meter)
    if normalized != sorted(normalized):
        raise ValueError("Previous snapshot meter list must be sorted")
    return normalized


def read_previous_snapshot(
    path: Path,
    *,
    expected_sha256: str,
    lm_pcode: str,
    provider: str,
    expected_month: str,
) -> SupplierSnapshot:
    if not path.is_file():
        raise FileNotFoundError(f"Previous Stage 03A snapshot not found: {path}")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha256:
        raise ValueError(
            f"Previous snapshot SHA256 mismatch: expected={expected_sha256}; actual={actual_sha}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Previous snapshot is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Previous snapshot must contain one JSON object")
    meters = _validate_previous_snapshot_payload(
        payload,
        lm_pcode=lm_pcode,
        provider=provider,
        expected_month=expected_month,
    )
    workbook_sha = clean_text(payload.get("workbookSha256")) or None
    return SupplierSnapshot(
        sha256=actual_sha,
        current_month=expected_month,
        meters=meters,
        lm_pcode=lm_pcode,
        provider=provider,
        workbook_sha256=workbook_sha,
        source=str(path),
    )


def bootstrap_previous_snapshot(
    baseline: BaselineSnapshot,
    *,
    lm_pcode: str,
    provider: str,
    expected_month: str,
) -> SupplierSnapshot:
    period_ends = {clean_text(record.get("salesPeriodTo")) for record in baseline.records}
    if period_ends != {expected_month}:
        raise ValueError(
            "--bootstrap-previous-from-baseline requires every baseline record to have "
            f"salesPeriodTo={expected_month}; found={sorted(period_ends)}"
        )
    return SupplierSnapshot(
        sha256=None,
        current_month=expected_month,
        meters=sorted(baseline.by_meter),
        lm_pcode=lm_pcode,
        provider=provider,
        workbook_sha256=None,
        source="BOOTSTRAP_BASELINE",
    )


def build_refreshed_records(
    baseline: BaselineSnapshot,
    previous: SupplierSnapshot,
    workbook: WorkbookSnapshot,
    target_months: Sequence[str],
    *,
    lm_pcode: str,
    provider: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cumulative_ids = set(baseline.by_meter)
    previous_ids = set(previous.meters)
    current_ids = set(workbook.meter_months)
    target_end = max(target_months)
    target_start = min(target_months)

    if not previous_ids.issubset(cumulative_ids):
        unknown = sorted(previous_ids - cumulative_ids)
        raise ValueError(
            "Previous supplier snapshot contains meter(s) absent from cumulative baseline. "
            f"Examples: {unknown[:10]}"
        )

    unchanged = sorted(previous_ids & current_ids)
    current_additions = sorted(current_ids - previous_ids)
    previous_missing = sorted(previous_ids - current_ids)
    cumulative_creates = sorted(current_ids - cumulative_ids)
    known_cumulative_current_additions = sorted(
        (current_ids - previous_ids) & cumulative_ids
    )

    replacement_pairs: list[dict[str, str]] = []
    replacement_old_this_snapshot: set[str] = set()
    replacement_new_created: set[str] = set()
    unresolved_previous_on_created: list[dict[str, str]] = []
    previous_still_current_conflicts: list[dict[str, str]] = []
    previous_to_new: dict[str, str] = {}

    for meter in current_additions:
        raw_previous = clean_text(workbook.commercial[meter].get("PreviousMeterNumber"))
        previous_meter = normalize_meter(raw_previous) if raw_previous else ""
        if previous_meter and not METER_RE.fullmatch(previous_meter):
            raise ValueError(
                f"Workbook meter {meter}: PreviousMeterNumber is not canonicalizable: {raw_previous!r}"
            )
        if meter not in cumulative_ids:
            if previous_meter in cumulative_ids:
                if previous_meter == meter:
                    raise ValueError(f"Workbook meter {meter} cannot replace itself")
                if previous_meter in current_ids:
                    previous_still_current_conflicts.append(
                        {"previousMeterNumber": previous_meter, "replacementMeterNumber": meter}
                    )
                    continue
                if previous_meter in previous_to_new and previous_to_new[previous_meter] != meter:
                    raise ValueError(
                        "One cumulative meter is referenced as PreviousMeterNumber by more than "
                        f"one new meter: {previous_meter} -> "
                        f"{previous_to_new[previous_meter]}, {meter}"
                    )
                previous_to_new[previous_meter] = meter
                replacement_new_created.add(meter)
                if previous_meter in previous_ids:
                    replacement_old_this_snapshot.add(previous_meter)
                replacement_pairs.append(
                    {"previousMeterNumber": previous_meter, "replacementMeterNumber": meter}
                )
            elif previous_meter:
                unresolved_previous_on_created.append(
                    {"meterNumber": meter, "previousMeterNumber": previous_meter}
                )

    if previous_still_current_conflicts:
        examples = previous_still_current_conflicts[:10]
        raise ValueError(
            "New replacement meter(s) point to PreviousMeterNumber values that are still "
            f"present in the current supplier snapshot. Examples: {examples}"
        )

    new_created = sorted(set(cumulative_creates) - replacement_new_created)
    removed_not_carried = sorted(set(previous_missing) - replacement_old_this_snapshot)

    # Preserve every baseline record; update purchase history only for identities present
    # in the current supplier workbook. Append only newly seen cumulative identities.
    refreshed: list[dict[str, Any]] = []
    changed_existing = 0
    idempotent_extension_values = 0
    for baseline_record in baseline.records:
        meter = clean_text(baseline_record["meterNoNormalized"])
        if meter in current_ids:
            record, changed, idempotent = _existing_record_with_extension(
                baseline_record, workbook, meter, target_end, target_months
            )
            if changed:
                changed_existing += 1
            idempotent_extension_values += idempotent
            refreshed.append(record)
        else:
            refreshed.append(copy.deepcopy(baseline_record))

    for meter in sorted(cumulative_creates, key=lambda m: workbook.worksheet_rows[m]):
        refreshed.append(
            _new_commercial_record(
                workbook,
                meter=meter,
                lm_pcode=lm_pcode,
                provider=provider,
                target_end=target_end,
            )
        )

    output_ids = {
        normalize_meter(record.get("meterNoNormalized") or record.get("meterNo"))
        for record in refreshed
    }
    expected_output_ids = cumulative_ids | set(cumulative_creates)
    if output_ids != expected_output_ids:
        raise RuntimeError("Append-only cumulative output identity reconciliation failed")
    if len(refreshed) != len(expected_output_ids):
        raise RuntimeError("Append-only cumulative output contains duplicate identities")

    # The existing baseline records' non-purchase fields must remain unchanged.
    baseline_output_records = [
        record
        for record in refreshed
        if normalize_meter(record.get("meterNoNormalized") or record.get("meterNo")) in cumulative_ids
    ]
    output_baseline_nonpurchase_sha = canonical_sha256(
        _nonpurchase_projection(baseline_output_records)
    )
    if output_baseline_nonpurchase_sha != baseline.nonpurchase_sha256:
        raise RuntimeError("Existing baseline non-purchase projection changed during refresh")

    historical_months = [month for month in workbook.months if month < target_start]
    historical_positive_created: list[str] = []
    for meter in cumulative_creates:
        if any(
            workbook.meter_months[meter][month].amount_c > 0
            or workbook.meter_months[meter][month].units > 0
            for month in historical_months
        ):
            historical_positive_created.append(meter)

    reconciliation: dict[str, Any] = {
        # Compatibility/raw comparison fields retained for operator visibility.
        "baselineMeters": len(cumulative_ids),
        "workbookMeters": len(current_ids),
        "matchedMeters": len(cumulative_ids & current_ids),
        "baselineOnlyCount": len(cumulative_ids - current_ids),
        "workbookOnlyCount": len(current_ids - cumulative_ids),
        "baselineOnlyMeters": sorted(cumulative_ids - current_ids),
        "workbookOnlyMeters": sorted(current_ids - cumulative_ids),
        "exactPopulationParity": cumulative_ids == current_ids,
        # Governed recurring population model.
        "populationPolicy": "APPEND_ONLY_ZERO_DELETE",
        "previousSnapshotMonth": previous.current_month,
        "previousSnapshotMeters": len(previous_ids),
        "currentSnapshotMonth": target_end,
        "currentSnapshotMeters": len(current_ids),
        "unchangedFromPreviousCount": len(unchanged),
        "unchangedFromPreviousMeters": unchanged,
        "currentAdditionsCount": len(current_additions),
        "currentAdditionsMeters": current_additions,
        "previousMissingCount": len(previous_missing),
        "previousMissingMeters": previous_missing,
        "replacementPairsCount": len(replacement_pairs),
        "replacementPairs": sorted(
            replacement_pairs,
            key=lambda item: (item["previousMeterNumber"], item["replacementMeterNumber"]),
        ),
        "replacementOldThisSnapshotCount": len(replacement_old_this_snapshot),
        "replacementOldThisSnapshotMeters": sorted(replacement_old_this_snapshot),
        "removedNotCarriedForwardCount": len(removed_not_carried),
        "removedNotCarriedForwardMeters": removed_not_carried,
        "cumulativeBeforeMeters": len(cumulative_ids),
        "cumulativeCreatesCount": len(cumulative_creates),
        "cumulativeCreatesMeters": cumulative_creates,
        "replacementNewCreatedCount": len(replacement_new_created),
        "replacementNewCreatedMeters": sorted(replacement_new_created),
        "newCreatedCount": len(new_created),
        "newCreatedMeters": new_created,
        "knownCumulativeCurrentAdditionsCount": len(known_cumulative_current_additions),
        "knownCumulativeCurrentAdditionsMeters": known_cumulative_current_additions,
        "incomingCreatedWithUnresolvedPreviousMeterNumberCount": len(
            unresolved_previous_on_created
        ),
        "incomingCreatedWithUnresolvedPreviousMeterNumber": unresolved_previous_on_created,
        "cumulativeDeletesCount": 0,
        "cumulativeAfterMeters": len(expected_output_ids),
        "expectedCumulativeAfterMeters": len(cumulative_ids) + len(cumulative_creates),
        "changedExistingRecords": changed_existing,
        "idempotentExtensionMeterMonths": idempotent_extension_values,
        "existingBaselineNonPurchaseProjectionPreserved": True,
        "baselineNonPurchaseSha256": baseline.nonpurchase_sha256,
        "outputBaselineNonPurchaseSha256": output_baseline_nonpurchase_sha,
        "historicalBackfill": {
            "required": bool(cumulative_creates and historical_months),
            "monthsBeforeCurrentRefresh": historical_months,
            "createdMeters": len(cumulative_creates),
            "createdMetersWithHistoricalPositiveActivity": len(historical_positive_created),
            "createdMetersWithHistoricalPositiveActivityExamples": sorted(
                historical_positive_created
            )[:25],
        },
    }

    if reconciliation["cumulativeAfterMeters"] != reconciliation["expectedCumulativeAfterMeters"]:
        raise RuntimeError("Cumulative population arithmetic does not reconcile")
    if (
        reconciliation["unchangedFromPreviousCount"]
        + reconciliation["currentAdditionsCount"]
        != reconciliation["currentSnapshotMeters"]
    ):
        raise RuntimeError("Current supplier snapshot arithmetic does not reconcile")
    if (
        reconciliation["unchangedFromPreviousCount"]
        + reconciliation["previousMissingCount"]
        != reconciliation["previousSnapshotMeters"]
    ):
        raise RuntimeError("Previous supplier snapshot arithmetic does not reconcile")
    if (
        reconciliation["replacementNewCreatedCount"]
        + reconciliation["newCreatedCount"]
        != reconciliation["cumulativeCreatesCount"]
    ):
        raise RuntimeError("Created-identity classification does not reconcile")
    if (
        reconciliation["replacementOldThisSnapshotCount"]
        + reconciliation["removedNotCarriedForwardCount"]
        != reconciliation["previousMissingCount"]
    ):
        # This only holds when every replacement link points to an identity present in
        # the previous supplier snapshot. Historical replacement links are explicitly
        # allowed in later recurring runs, so account for them separately.
        historical_linked = {
            item["previousMeterNumber"]
            for item in replacement_pairs
            if item["previousMeterNumber"] not in previous_ids
        }
        if (
            reconciliation["replacementOldThisSnapshotCount"]
            + reconciliation["removedNotCarriedForwardCount"]
            + len(historical_linked & set(previous_missing))
            != reconciliation["previousMissingCount"]
        ):
            raise RuntimeError("Outgoing movement classification does not reconcile")
    return refreshed, reconciliation


def build_snapshot_payload(
    *,
    workbook: WorkbookSnapshot,
    lm_pcode: str,
    provider: str,
    current_month: str,
    source_run_id: str,
    cumulative_source_sha256: str,
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "stage": "03A",
        "snapshotType": SNAPSHOT_TYPE,
        "lmPcode": lm_pcode,
        "provider": provider,
        "currentMonth": current_month,
        "sourceRunId": source_run_id,
        "workbookSha256": workbook.sha256,
        "cumulativeSourceSha256": cumulative_source_sha256,
        "meters": sorted(workbook.meter_months),
        "population": {
            "currentSnapshotMeters": reconciliation["currentSnapshotMeters"],
            "cumulativeMeters": reconciliation["cumulativeAfterMeters"],
            "cumulativeCreates": reconciliation["cumulativeCreatesCount"],
            "cumulativeDeletes": 0,
        },
    }


def write_snapshot(payload: Mapping[str, Any], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    ).encode("utf-8")
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_bytes(raw)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()
    if path.read_bytes() != raw:
        raise RuntimeError(f"Written snapshot bytes differ from planned payload: {path}")
    return sha256_bytes(raw)

def write_jsonl(records: Sequence[Mapping[str, Any]], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        for record in records
    ).encode("utf-8")
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_bytes(payload)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()
    if path.read_bytes() != payload:
        raise RuntimeError(f"Written output bytes differ from planned payload: {path}")
    return sha256_bytes(payload)


def write_report(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh a cumulative governed monthly-source commercial JSONL from an XLSX "
            "supplier delivery using append-only population reconciliation."
        )
    )
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--expected-baseline-sha256", required=True)
    parser.add_argument("--previous-snapshot", type=Path)
    parser.add_argument("--expected-previous-snapshot-sha256")
    parser.add_argument("--bootstrap-previous-from-baseline", action="store_true")
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--expected-workbook-sha256", required=True)
    parser.add_argument("--sheet", default="Purchases")
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--from-month", required=True)
    parser.add_argument("--to-month", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--category-source", type=Path,
        help="Separate authoritative classification workbook for the exact target month.")
    parser.add_argument("--expected-category-source-sha256")
    parser.add_argument("--category-sheet")
    parser.add_argument("--category-identity-map", type=Path,
        help="Approved leading-zero reconciliation JSON array, if comparison aliases are needed.")
    parser.add_argument("--expected-category-identity-map-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = utc_now_iso()
    lm = clean_text(args.lm_pcode).upper()
    provider = clean_text(args.provider).lower()
    if not lm:
        raise ValueError("--lm-pcode may not be blank")
    if not PROVIDER_RE.fullmatch(provider):
        raise ValueError("--provider is not canonical")
    source_run_id = clean_text(args.source_run_id)
    if not source_run_id:
        raise ValueError("--source-run-id may not be blank")
    baseline_sha = validate_sha(args.expected_baseline_sha256, "--expected-baseline-sha256")
    workbook_sha = validate_sha(args.expected_workbook_sha256, "--expected-workbook-sha256")
    target_months = month_range(clean_text(args.from_month), clean_text(args.to_month))
    prior_month = previous_month(min(target_months))

    if bool(args.previous_snapshot) == bool(args.bootstrap_previous_from_baseline):
        raise ValueError(
            "Supply exactly one of --previous-snapshot or --bootstrap-previous-from-baseline"
        )
    previous_snapshot_sha: str | None = None
    if args.previous_snapshot is not None:
        if not args.expected_previous_snapshot_sha256:
            raise ValueError(
                "--previous-snapshot requires --expected-previous-snapshot-sha256"
            )
        previous_snapshot_sha = validate_sha(
            args.expected_previous_snapshot_sha256,
            "--expected-previous-snapshot-sha256",
        )
    elif args.expected_previous_snapshot_sha256:
        raise ValueError(
            "--expected-previous-snapshot-sha256 requires --previous-snapshot"
        )

    if args.write and args.output is None:
        raise ValueError("--write requires --output")
    if args.write and args.snapshot_output is None:
        raise ValueError("--write requires --snapshot-output for the next recurring run")

    report: dict[str, Any] = {
        "stage": "03A",
        "script": "03a_refresh_monthly_source_commercial.py",
        "operation": "append_only_monthly_source_commercial_refresh",
        "sourceOrigin": "monthly_source",
        "sourceRunId": source_run_id,
        "lmPcode": lm,
        "provider": provider,
        "targetMonths": target_months,
        "writeRequested": bool(args.write),
        "firestoreAccess": False,
        "firestoreWrites": 0,
        "firestoreDeletes": 0,
        "startedAt": started_at,
        "status": "STARTED",
        "result": "STARTED",
    }

    try:
        print("[1/5] Validate and read cumulative commercial baseline JSONL", flush=True)
        baseline = read_baseline(
            args.baseline.expanduser().resolve(),
            expected_sha256=baseline_sha,
            lm_pcode=lm,
            provider=provider,
            progress_every=max(0, args.progress_every),
        )
        report["baseline"] = {
            "path": str(args.baseline.expanduser().resolve()),
            "sha256": baseline.sha256,
            "rows": len(baseline.records),
            "nonPurchaseProjectionSha256": baseline.nonpurchase_sha256,
        }

        print("[2/5] Validate previous supplier membership snapshot", flush=True)
        if args.bootstrap_previous_from_baseline:
            previous = bootstrap_previous_snapshot(
                baseline,
                lm_pcode=lm,
                provider=provider,
                expected_month=prior_month,
            )
        else:
            assert args.previous_snapshot is not None
            assert previous_snapshot_sha is not None
            previous = read_previous_snapshot(
                args.previous_snapshot.expanduser().resolve(),
                expected_sha256=previous_snapshot_sha,
                lm_pcode=lm,
                provider=provider,
                expected_month=prior_month,
            )
        report["previousSnapshot"] = {
            "source": previous.source,
            "sha256": previous.sha256,
            "currentMonth": previous.current_month,
            "rows": len(previous.meters),
            "bootstrapFromBaseline": bool(args.bootstrap_previous_from_baseline),
        }

        print("[3/5] Validate workbook and extract commercial + monthly source facts", flush=True)
        workbook = read_workbook(
            args.workbook.expanduser().resolve(),
            expected_sha256=workbook_sha,
            sheet_name=args.sheet,
            target_months=target_months,
            progress_every=max(0, args.progress_every),
        )
        report["workbook"] = {
            "path": str(args.workbook.expanduser().resolve()),
            "sha256": workbook.sha256,
            "sheet": args.sheet,
            "rows": workbook.rows,
            "availableMonthsThroughTarget": workbook.months,
            "targetMonthTotals": workbook.totals,
        }

        print("[4/5] Reconcile movement and build append-only cumulative source in memory", flush=True)
        refreshed, reconciliation = build_refreshed_records(
            baseline,
            previous,
            workbook,
            target_months,
            lm_pcode=lm,
            provider=provider,
        )
        report["reconciliation"] = reconciliation
        if args.category_source:
            from sales_monthly_categories import ingest_workbook, append_to_commercial, sha
            if len(target_months) != 1:
                raise ValueError("Category ingestion requires one exact supplier target month")
            if not args.expected_category_source_sha256 or not args.category_sheet:
                raise ValueError("Category source requires hash and exact sheet")
            category_values, category_exceptions, category_aliases = ingest_workbook(
                args.category_source.resolve(), args.expected_category_source_sha256.lower(),
                args.category_sheet, target_months[0],
                {record["meterNoNormalized"] for record in refreshed})
            if category_aliases:
                if (not args.category_identity_map or
                        sha(args.category_identity_map) != args.expected_category_identity_map_sha256):
                    raise ValueError("Approved category identity reconciliation and matching SHA required")
                approved_aliases = json.loads(args.category_identity_map.read_text(encoding="utf-8"))
                if approved_aliases != category_aliases:
                    raise ValueError("Category identity reconciliation differs from approved evidence")
            refreshed = append_to_commercial(refreshed, target_months[0], category_values)
            report["categorySource"] = {"path": str(args.category_source.resolve()),
                "sha256": args.expected_category_source_sha256.lower(), "sheet": args.category_sheet,
                "month": target_months[0], "categoryCount": len(category_values),
                "exceptions": category_exceptions, "populationAuthority": False}
        elif any((args.expected_category_source_sha256, args.category_sheet,
                  args.category_identity_map, args.expected_category_identity_map_sha256)):
            raise ValueError("Category options require --category-source")
        report["status"] = "PASS"

        print(
            "[POPULATION] "
            f"previousSnapshot={reconciliation['previousSnapshotMeters']:,} | "
            f"currentSnapshot={reconciliation['currentSnapshotMeters']:,} | "
            f"unchanged={reconciliation['unchangedFromPreviousCount']:,} | "
            f"replacementPairs={reconciliation['replacementPairsCount']:,} | "
            f"removed={reconciliation['removedNotCarriedForwardCount']:,} | "
            f"creates={reconciliation['cumulativeCreatesCount']:,} "
            f"(replacement={reconciliation['replacementNewCreatedCount']:,}, "
            f"new={reconciliation['newCreatedCount']:,}) | "
            f"cumulative={reconciliation['cumulativeAfterMeters']:,} | deletes=0",
            flush=True,
        )

        if args.write:
            assert args.output is not None
            assert args.snapshot_output is not None
            print("[5/5] Write cumulative JSONL + next supplier snapshot", flush=True)
            output_path = args.output.expanduser().resolve()
            output_sha = write_jsonl(refreshed, output_path)
            snapshot_payload = build_snapshot_payload(
                workbook=workbook,
                lm_pcode=lm,
                provider=provider,
                current_month=max(target_months),
                source_run_id=source_run_id,
                cumulative_source_sha256=output_sha,
                reconciliation=reconciliation,
            )
            snapshot_path = args.snapshot_output.expanduser().resolve()
            snapshot_sha = write_snapshot(snapshot_payload, snapshot_path)
            report["output"] = {
                "path": str(output_path),
                "rows": len(refreshed),
                "sha256": output_sha,
            }
            report["currentSnapshot"] = {
                "path": str(snapshot_path),
                "rows": workbook.rows,
                "currentMonth": max(target_months),
                "sha256": snapshot_sha,
            }
            report["result"] = "CUMULATIVE_SOURCE_AND_SNAPSHOT_WRITTEN"
        else:
            print("[5/5] Dry run complete — no cumulative JSONL or snapshot written", flush=True)
            report["plannedOutputRows"] = len(refreshed)
            report["plannedSnapshotRows"] = workbook.rows
            report["result"] = "PREFLIGHT_PASS"
        return_code = 0
    except Exception as exc:
        report["status"] = "FAIL"
        report["result"] = "VALIDATION_FAILED"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        return_code = 2
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        report["finishedAt"] = utc_now_iso()
        write_report(report, args.report.expanduser().resolve())
        print(f"Report: {args.report.expanduser().resolve()}", flush=True)

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
