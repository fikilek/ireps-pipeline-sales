"""Shared monthly-source support for iREPS Sales Pipeline Stages 05 and 06.

This module is intentionally environment-neutral. It never connects to Firestore.
It adds a governed path for providers whose supplied source is already monthly
aggregated (for example Contour) without fabricating atomic transaction facts.

The existing atomic/Conlog path remains owned by the original Stage 05/06 code.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from sales_address_enrichment import (
    ADDRESS_STAGING_COLUMNS,
    build_enrichment_report,
    enrichment_contract,
    parse_physical_address,
    raw_address_mutation_count,
    raw_address_snapshot,
    write_json_atomic as write_address_json_atomic,
)

METER_RE = re.compile(r"^[A-Z0-9]+$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PROVIDER_RE = re.compile(r"^[a-z0-9_-]+$")
MONTHLY_SOURCE_FILENAME_RE = re.compile(
    r"^monthly__FULL__(?P<month>\d{4}-\d{2})__from_monthly_source\.csv$",
    re.IGNORECASE,
)
MASTER_COLUMNS = [
    "masterId",
    "lmPcode",
    "meterNoRaw",
    "meterNoNormalized",
    "meterType",
    "customerNo",
    "accountNo",
    "salesId",
    "salesProvider",
    "astId",
]

# Fields intentionally carried from the cleaned commercial source into Sales All.
# Operational allocation fields (tbRefs/geofenceRefs/etc.) are deliberately absent.
COMMERCIAL_SCALAR_FIELDS = [
    "sourceDocumentId",
    "sourceDocumentPath",
    "sourceEndRow",
    "accountNumberNormalized",
    "customerSurname",
    "addressLine1",
    "addressLine2",
    "town",
    "postalAddress1",
    "postalAddress2",
    "postalAddressTown",
    "standNumber",
    "tariffInstance",
    "installationDate",
    "previousMeterNumber",
    "previousInstallationDate",
    "leakageCategory",
    "riskTier",
    "riskScore",
    "salesPeriodFrom",
    "salesPeriodTo",
    "erfCandidateCount",
    "gpsMatchStatus",
    "hasUsableGps",
    "elmAccountMatched",
]
COMMERCIAL_JSON_FIELDS = ["erfCandidates", "erfNumbers", "missingErfNumbers", "elmSourceRows"]

MONTHLY_SOURCE_REQUIRED_COLUMNS = {
    "lmPcode",
    "meterNo",
    "ym",
    "provider",
    "amountTotalC",
    "unitsTotal",
    "sourceOrigin",
}


@dataclass(frozen=True)
class Snapshot:
    path: Path
    sha256: str
    rows: int


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def normalize_meter(value: Any) -> str:
    return "".join(safe_str(value).upper().split())


def normalize_provider(value: Any) -> str:
    return safe_str(value).lower()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def month_sequence(first: str, last: str) -> list[str]:
    if not MONTH_RE.fullmatch(first) or not MONTH_RE.fullmatch(last):
        raise ValueError("Monthly-source range must use YYYY-MM")
    a = pd.Period(first, freq="M")
    b = pd.Period(last, freq="M")
    if a > b:
        raise ValueError("--from-month may not be later than --to-month")
    return [str(p) for p in pd.period_range(a, b, freq="M")]


def require_provider(provider: str) -> str:
    value = normalize_provider(provider)
    if not value or not PROVIDER_RE.fullmatch(value):
        raise ValueError(
            "--provider must be non-blank lowercase alphanumeric text with optional _ or -"
        )
    return value


def require_sha256(actual: str, expected: str | None, label: str) -> None:
    if expected and actual.lower() != expected.lower():
        raise ValueError(f"{label} SHA256 mismatch: expected={expected}; actual={actual}")


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], Snapshot]:
    if not path.is_file():
        raise FileNotFoundError(f"Commercial source not found: {path}")
    payload = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(payload.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid JSONL at line {line_no}: {path}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Commercial source line {line_no} is not an object")
        rows.append(item)
    if not rows:
        raise ValueError("Commercial source contains zero records")
    return rows, Snapshot(path=path, sha256=sha256_bytes(payload), rows=len(rows))


def load_commercial_source(
    path: Path,
    *,
    expected_sha256: str | None,
    lm_pcode: str,
) -> tuple[dict[str, dict[str, Any]], Snapshot]:
    rows, snap = _read_jsonl(path)
    require_sha256(snap.sha256, expected_sha256, "Commercial source")
    by_meter: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        meter = normalize_meter(row.get("meterNoNormalized") or row.get("meterNo"))
        if not meter or not METER_RE.fullmatch(meter):
            raise ValueError(f"Commercial source row {index}: invalid meterNoNormalized")
        source_lm = safe_str(row.get("lmPcode")).upper()
        if source_lm != lm_pcode:
            raise ValueError(
                f"Commercial source row {index}: lmPcode={source_lm!r}, expected {lm_pcode!r}"
            )
        if meter in by_meter:
            raise ValueError(f"Commercial source duplicate meterNoNormalized: {meter}")
        copy = dict(row)
        copy["meterNoNormalized"] = meter
        by_meter[meter] = copy
    return by_meter, snap


def discover_monthly_source_files(
    monthly_dir: Path,
    months: Sequence[str],
) -> dict[str, Path]:
    if not monthly_dir.is_dir():
        raise FileNotFoundError(f"Monthly output directory not found: {monthly_dir}")
    wanted = set(months)
    found: dict[str, Path] = {}
    for path in monthly_dir.iterdir():
        if not path.is_file():
            continue
        match = MONTHLY_SOURCE_FILENAME_RE.fullmatch(path.name)
        if not match:
            continue
        month = match.group("month")
        if month not in wanted:
            continue
        if month in found:
            raise ValueError(f"Duplicate monthly-source file for {month}: {path}")
        found[month] = path.resolve()
    missing = [m for m in months if m not in found]
    if missing:
        raise FileNotFoundError(
            "Missing monthly-source outputs for: " + ", ".join(missing)
        )
    return {m: found[m] for m in months}


def _decimal(value: Any, label: str, row_number: int) -> Decimal:
    text = safe_str(value)
    if text == "":
        raise ValueError(f"{label} is blank at row {row_number}")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not numeric at row {row_number}: {text!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be finite and non-negative at row {row_number}")
    return result


def _int(value: Any, label: str, row_number: int) -> int:
    d = _decimal(value, label, row_number)
    if d != d.to_integral_value():
        raise ValueError(f"{label} must be an integer at row {row_number}: {value!r}")
    return int(d)


def validate_monthly_source_csv(
    path: Path,
    *,
    month: str,
    lm_pcode: str,
    provider: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    payload = path.read_bytes()
    try:
        df = pd.read_csv(io.BytesIO(payload), dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        raise ValueError(f"Invalid monthly-source CSV: {path}") from exc
    missing = sorted(MONTHLY_SOURCE_REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"{path.name}: missing required columns {missing}")
    if df.empty:
        raise ValueError(f"{path.name}: zero rows")

    seen: set[str] = set()
    amount_total = 0
    units_total = Decimal("0")
    zero_sales = 0
    for row_number, row in enumerate(df.to_dict("records"), start=2):
        lm = safe_str(row.get("lmPcode")).upper()
        ym = safe_str(row.get("ym"))
        meter = normalize_meter(row.get("meterNo"))
        row_provider = normalize_provider(row.get("provider"))
        origin = safe_str(row.get("sourceOrigin")).lower()
        if lm != lm_pcode:
            raise ValueError(f"{path.name}: LM mismatch at row {row_number}")
        if ym != month:
            raise ValueError(f"{path.name}: ym mismatch at row {row_number}")
        if row_provider != provider:
            raise ValueError(f"{path.name}: provider mismatch at row {row_number}")
        if origin != "monthly_source":
            raise ValueError(f"{path.name}: sourceOrigin must be monthly_source")
        if not meter or not METER_RE.fullmatch(meter):
            raise ValueError(f"{path.name}: invalid meterNo at row {row_number}")
        if meter in seen:
            raise ValueError(f"{path.name}: duplicate meterNo {meter}")
        seen.add(meter)
        amount = _int(row.get("amountTotalC"), "amountTotalC", row_number)
        units = _decimal(row.get("unitsTotal"), "unitsTotal", row_number)
        amount_total += amount
        units_total += units
        zero_sales += int(amount == 0)

        if "docId" in df.columns and safe_str(row.get("docId")):
            expected = f"{lm_pcode}__{meter}__{month}"
            if safe_str(row.get("docId")) != expected:
                raise ValueError(f"{path.name}: deterministic docId mismatch at row {row_number}")
        if "y" in df.columns and safe_str(row.get("y")):
            if _int(row.get("y"), "y", row_number) != int(month[:4]):
                raise ValueError(f"{path.name}: y mismatch at row {row_number}")
        if "m" in df.columns and safe_str(row.get("m")):
            if _int(row.get("m"), "m", row_number) != int(month[5:7]):
                raise ValueError(f"{path.name}: m mismatch at row {row_number}")

    return df, {
        "month": month,
        "path": str(path),
        "filename": path.name,
        "rows": len(df),
        "columns": list(df.columns),
        "sha256": sha256_bytes(payload),
        "amountTotalC": amount_total,
        "unitsTotal": format(units_total, "f"),
        "zeroSalesRows": zero_sales,
    }


def _write_csv(df: pd.DataFrame, path: Path) -> Snapshot:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = df.to_csv(index=False, lineterminator="\n").encode("utf-8")
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_bytes(payload)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()
    written = path.read_bytes()
    if written != payload:
        raise RuntimeError(f"Written CSV bytes differ from planned payload: {path}")
    return Snapshot(path=path, sha256=sha256_bytes(written), rows=len(df))


def _write_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            target.write("\n")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _commercial_account(row: Mapping[str, Any]) -> str:
    return safe_str(row.get("accountNo") or row.get("accountNumber"))


def build_stage05_monthly_source(
    *,
    lm_pcode: str,
    provider: str,
    meter_type: str,
    from_month: str,
    to_month: str,
    commercial_source: Path,
    expected_commercial_sha256: str | None,
    monthly_dir: Path,
    output_path: Path,
    manifest_path: Path,
    source_run_id: str,
) -> dict[str, Any]:
    provider = require_provider(provider)
    source_run_id = safe_str(source_run_id)
    if not source_run_id:
        raise ValueError("--source-run-id is required for monthly_source Stage 05")
    lm_pcode = safe_str(lm_pcode).upper()
    if not lm_pcode:
        raise ValueError("--lm-pcode may not be blank")
    months = month_sequence(from_month, to_month)
    commercial, commercial_snap = load_commercial_source(
        commercial_source,
        expected_sha256=expected_commercial_sha256,
        lm_pcode=lm_pcode,
    )
    files = discover_monthly_source_files(monthly_dir, months)
    monthly_evidence: list[dict[str, Any]] = []
    monthly_meters: set[str] = set()
    for month in months:
        df, evidence = validate_monthly_source_csv(
            files[month], month=month, lm_pcode=lm_pcode, provider=provider
        )
        monthly_meters.update(normalize_meter(v) for v in df["meterNo"].tolist())
        monthly_evidence.append(evidence)

    unknown_monthly = sorted(monthly_meters - set(commercial))
    if unknown_monthly:
        raise ValueError(
            "Monthly outputs contain meter(s) absent from commercial source. Examples: "
            + ", ".join(unknown_monthly[:10])
        )

    rows: list[dict[str, str]] = []
    for meter in sorted(commercial):
        source = commercial[meter]
        rows.append(
            {
                "masterId": meter,
                "lmPcode": lm_pcode,
                "meterNoRaw": safe_str(source.get("meterNo")) or meter,
                "meterNoNormalized": meter,
                "meterType": safe_str(meter_type).lower() or "electricity",
                "customerNo": safe_str(source.get("customerNo")),
                "accountNo": _commercial_account(source),
                "salesId": meter,
                "salesProvider": provider,
                "astId": "",
            }
        )
    df = pd.DataFrame(rows, columns=MASTER_COLUMNS)
    if df.empty or df["masterId"].duplicated().any():
        raise ValueError("Monthly-source Meter Master output identity failure")
    output_snap = _write_csv(df, output_path)

    stats = {
        "commercialMeters": len(commercial),
        "monthlyBackedMeters": len(monthly_meters),
        "commercialOnlyMeters": len(set(commercial) - monthly_meters),
        "totalMasterRows": len(df),
    }
    source_contract = {
        "sourceOrigin": "monthly_source",
        "sourceRunId": source_run_id,
        "lmPcode": lm_pcode,
        "scope": "FULL",
        "fromMonth": months[0],
        "toMonth": months[-1],
        "includedMonths": months,
        "provider": provider,
        "meterType": safe_str(meter_type).lower(),
        "commercialSource": {
            "path": str(commercial_snap.path),
            "filename": commercial_snap.path.name,
            "rows": commercial_snap.rows,
            "sha256": commercial_snap.sha256,
        },
        "monthlyInputs": monthly_evidence,
        "atomicFactsFabricated": 0,
    }
    output_contract = {
        "path": str(output_path),
        "filename": output_path.name,
        "rows": len(df),
        "columns": MASTER_COLUMNS,
        "sha256": output_snap.sha256,
    }
    fingerprint = {
        "sourceContract": source_contract,
        "outputContract": {
            "filename": output_contract["filename"],
            "rows": output_contract["rows"],
            "columns": output_contract["columns"],
            "sha256": output_contract["sha256"],
        },
        "stats": stats,
    }
    manifest = {
        "schemaVersion": 2,
        "stage": "05",
        "script": "05_build_meter_master_v3.py",
        "status": "PASS",
        "result": "BUILD_WRITTEN",
        "createdAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceContract": source_contract,
        "outputContract": output_contract,
        "stats": stats,
        "buildFingerprint": canonical_sha256(fingerprint),
    }
    _write_json(manifest, manifest_path)
    return manifest


def validate_stage05_monthly_source_manifest(
    manifest: Mapping[str, Any],
    *,
    master_path: Path,
    lm_pcode: str,
    provider: str,
    from_month: str,
    to_month: str,
    commercial_source: Path,
    expected_commercial_sha256: str | None,
) -> dict[str, Any]:
    if manifest.get("schemaVersion") != 2 or manifest.get("stage") != "05":
        raise ValueError("Expected Stage 05 schemaVersion 2 monthly-source manifest")
    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    stats = manifest.get("stats")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping) or not isinstance(stats, Mapping):
        raise ValueError("Stage 05 monthly-source manifest contract is incomplete")
    fingerprint = {
        "sourceContract": dict(source),
        "outputContract": {
            "filename": output.get("filename"),
            "rows": output.get("rows"),
            "columns": output.get("columns"),
            "sha256": output.get("sha256"),
        },
        "stats": dict(stats),
    }
    if safe_str(manifest.get("buildFingerprint")) != canonical_sha256(fingerprint):
        raise ValueError("Stage 05 monthly-source buildFingerprint is invalid")
    months = month_sequence(from_month, to_month)
    if source.get("sourceOrigin") != "monthly_source":
        raise ValueError("Stage 05 sourceOrigin mismatch")
    if source.get("lmPcode") != lm_pcode or source.get("provider") != provider:
        raise ValueError("Stage 05 LM/provider mismatch")
    if source.get("includedMonths") != months:
        raise ValueError("Stage 05 includedMonths mismatch")
    actual_master = master_path.read_bytes()
    if output.get("sha256") != sha256_bytes(actual_master):
        raise ValueError("Meter Master SHA does not match Stage 05 manifest")
    commercial_bytes = commercial_source.read_bytes()
    commercial_sha = sha256_bytes(commercial_bytes)
    require_sha256(commercial_sha, expected_commercial_sha256, "Commercial source")
    commercial_evidence = source.get("commercialSource") or {}
    if commercial_evidence.get("sha256") != commercial_sha:
        raise ValueError("Commercial source SHA does not match Stage 05 manifest")
    return {
        "buildFingerprint": manifest["buildFingerprint"],
        "manifestSha256": "",  # caller supplies exact manifest SHA if needed
        "sourceRunId": source.get("sourceRunId"),
        "monthlyInputs": list(source.get("monthlyInputs") or []),
        "commercialSource": dict(commercial_evidence),
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bool_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = safe_str(value).lower()
    if text in {"true", "1", "yes"}:
        return "true"
    if text in {"false", "0", "no", ""}:
        return "false"
    raise ValueError(f"Invalid boolean value: {value!r}")


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _commercial_monthly_int_map(source: Mapping[str, Any], field: str, months: Sequence[str]) -> dict[str, int]:
    value = source.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"Commercial source field {field} must be an object/map")
    result: dict[str, int] = {}
    for month in months:
        raw = value.get(month, 0)
        text = safe_str(raw)
        if text == "":
            result[month] = 0
            continue
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"Commercial {field}[{month}] is not numeric: {raw!r}") from exc
        if not dec.is_finite() or dec < 0 or dec != dec.to_integral_value():
            raise ValueError(f"Commercial {field}[{month}] must be a non-negative integer")
        result[month] = int(dec)
    return result


def _commercial_monthly_decimal_map(source: Mapping[str, Any], field: str, months: Sequence[str]) -> dict[str, Decimal]:
    value = source.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"Commercial source field {field} must be an object/map")
    result: dict[str, Decimal] = {}
    for month in months:
        raw = value.get(month, 0)
        text = safe_str(raw)
        if text == "":
            result[month] = Decimal("0")
            continue
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"Commercial {field}[{month}] is not numeric: {raw!r}") from exc
        if not dec.is_finite() or dec < 0:
            raise ValueError(f"Commercial {field}[{month}] must be finite and non-negative")
        result[month] = dec
    return result


def build_stage06_monthly_source(
    *,
    lm_pcode: str,
    provider: str,
    from_month: str,
    to_month: str,
    master_path: Path,
    master_manifest_path: Path,
    commercial_source: Path,
    expected_commercial_sha256: str | None,
    monthly_dir: Path,
    output_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    provider = require_provider(provider)
    lm_pcode = safe_str(lm_pcode).upper()
    months = month_sequence(from_month, to_month)
    stage05_payload = json.loads(master_manifest_path.read_text(encoding="utf-8"))
    stage05_evidence = validate_stage05_monthly_source_manifest(
        stage05_payload,
        master_path=master_path,
        lm_pcode=lm_pcode,
        provider=provider,
        from_month=from_month,
        to_month=to_month,
        commercial_source=commercial_source,
        expected_commercial_sha256=expected_commercial_sha256,
    )
    stage05_manifest_sha = sha256_bytes(master_manifest_path.read_bytes())

    master = pd.read_csv(master_path, dtype=str, encoding="utf-8-sig").fillna("")
    if list(master.columns) != MASTER_COLUMNS:
        raise ValueError("Meter Master does not match the ten-column contract")
    if master.empty or master["masterId"].duplicated().any():
        raise ValueError("Meter Master identity failure")
    if not master["salesProvider"].eq(provider).all():
        raise ValueError("Meter Master provider mismatch")
    if not master["lmPcode"].eq(lm_pcode).all():
        raise ValueError("Meter Master LM mismatch")

    commercial, commercial_snap = load_commercial_source(
        commercial_source,
        expected_sha256=expected_commercial_sha256,
        lm_pcode=lm_pcode,
    )
    master_ids = [normalize_meter(v) for v in master["masterId"].tolist()]
    if set(master_ids) != set(commercial):
        missing = sorted(set(master_ids) - set(commercial))
        extra = sorted(set(commercial) - set(master_ids))
        raise ValueError(
            f"Meter Master/commercial population mismatch: missing={missing[:10]}; extra={extra[:10]}"
        )

    files = discover_monthly_source_files(monthly_dir, months)
    amount_by_meter: dict[str, dict[str, int]] = {meter: {m: 0 for m in months} for meter in master_ids}
    units_by_meter: dict[str, dict[str, Decimal]] = {meter: {m: Decimal('0') for m in months} for meter in master_ids}
    monthly_evidence: list[dict[str, Any]] = []
    monthly_rows = 0
    for month in months:
        frame, evidence = validate_monthly_source_csv(
            files[month], month=month, lm_pcode=lm_pcode, provider=provider
        )
        monthly_evidence.append(evidence)
        for row_number, row in enumerate(frame.to_dict("records"), start=2):
            meter = normalize_meter(row.get("meterNo"))
            if meter not in amount_by_meter:
                raise ValueError(f"Monthly row meter absent from Master: {meter}")
            amount_by_meter[meter][month] = _int(row.get("amountTotalC"), "amountTotalC", row_number)
            units_by_meter[meter][month] = _decimal(row.get("unitsTotal"), "unitsTotal", row_number)
            monthly_rows += 1

    monthly_amount_columns = [f"amount_{m.replace('-', '_')}_C" for m in months]
    monthly_units_columns = [f"units_{m.replace('-', '_')}" for m in months]
    rich_columns = [
        "lmPcode",
        "accountNumber",
        "customerName",
        "sourceFileName",
        "sourceRow",
        "totalSalesC",
        "totalUnits",
        *COMMERCIAL_SCALAR_FIELDS,
        *COMMERCIAL_JSON_FIELDS,
    ]
    include_category_history = any("monthlyCategories" in record for record in commercial.values())
    if include_category_history:
        rich_columns.append("monthlyCategories")
    base_columns = [
        "masterId",
        "meterNo",
        "meterNoNormalized",
        "provider",
        "customerNo",
        "accountNo",
        "totalAmountC",
        "lastPurchaseAtISO",
        "daysSinceLastPurchase",
    ]
    output_columns = (
        base_columns
        + rich_columns
        + ADDRESS_STAGING_COLUMNS
        + monthly_amount_columns
        + monthly_units_columns
    )

    master_by_id = {
        normalize_meter(row["masterId"]): row
        for row in master.to_dict("records")
    }
    raw_before = raw_address_snapshot(commercial)
    out_rows: list[dict[str, Any]] = []
    enrichment_records: list[tuple[str, str, str, Any]] = []
    total_sales = 0
    total_units = Decimal("0")
    for meter in sorted(master_ids):
        master_row = master_by_id[meter]
        source = commercial[meter]
        meter_amounts = amount_by_meter[meter]
        meter_units = units_by_meter[meter]

        # Strong monthly-source boundary check: Stage 03B monthly outputs must
        # reconcile exactly back to the frozen cleaned commercial source.
        source_amounts = _commercial_monthly_int_map(source, "monthlySalesC", months)
        source_units = _commercial_monthly_decimal_map(source, "monthlyUnits", months)
        if meter_amounts != source_amounts:
            differing = [m for m in months if meter_amounts[m] != source_amounts[m]]
            raise ValueError(
                f"Monthly sales do not reconcile to commercial source for {meter}; "
                f"months={differing[:10]}"
            )
        differing_units = [m for m in months if meter_units[m] != source_units[m]]
        if differing_units:
            raise ValueError(
                f"Monthly units do not reconcile to commercial source for {meter}; "
                f"months={differing_units[:10]}"
            )

        meter_total_sales = sum(meter_amounts.values())
        meter_total_units = sum(meter_units.values(), Decimal("0"))
        declared_total_sales = safe_str(source.get("totalSalesC"))
        declared_total_units = safe_str(source.get("totalUnits"))
        if declared_total_sales:
            if _int(declared_total_sales, "commercial totalSalesC", 0) != meter_total_sales:
                raise ValueError(f"Commercial totalSalesC mismatch for {meter}")
        if declared_total_units:
            if _decimal(declared_total_units, "commercial totalUnits", 0) != meter_total_units:
                raise ValueError(f"Commercial totalUnits mismatch for {meter}")
        total_sales += meter_total_sales
        total_units += meter_total_units
        row: dict[str, Any] = {
            "masterId": meter,
            "meterNo": safe_str(master_row.get("meterNoRaw")) or meter,
            "meterNoNormalized": meter,
            "provider": provider,
            "customerNo": safe_str(master_row.get("customerNo")),
            "accountNo": safe_str(master_row.get("accountNo")),
            "totalAmountC": meter_total_sales,
            # Monthly-only source has no transaction timestamp truth.
            "lastPurchaseAtISO": "",
            "daysSinceLastPurchase": "",
            "lmPcode": lm_pcode,
            "accountNumber": safe_str(master_row.get("accountNo")),
            # Compatibility aliases consumed by the protected Web normalizer.
            "customerName": safe_str(source.get("customerName") or source.get("customerSurname")),
            "sourceFileName": Path(safe_str(source.get("sourceDocumentPath"))).name if safe_str(source.get("sourceDocumentPath")) else "",
            "sourceRow": safe_str(source.get("sourceEndRow")),
            "totalSalesC": meter_total_sales,
            "totalUnits": _decimal_text(meter_total_units),
        }
        for field in COMMERCIAL_SCALAR_FIELDS:
            value = source.get(field)
            if field == "hasUsableGps" or field == "elmAccountMatched":
                row[field] = _bool_text(value)
            elif field in {"sourceEndRow", "riskScore", "erfCandidateCount", "elmSourceRows"}:
                row[field] = safe_str(value)
            else:
                row[field] = safe_str(value)
        for field in COMMERCIAL_JSON_FIELDS:
            value = source.get(field)
            if value is None or value == "":
                value = []
            if not isinstance(value, (list, dict)):
                raise ValueError(f"Commercial field {field} must be list/object for meter {meter}")
            row[field] = _compact_json(value)

        if include_category_history:
            from sales_monthly_categories import validate_history
            history = source.get("monthlyCategories", {})
            validate_history(history)
            row["monthlyCategories"] = _compact_json(history)

        address_result = parse_physical_address(
            source.get("addressLine1"),
            source.get("addressLine2"),
        )
        row["strNo"] = address_result.strNo
        row["strName"] = address_result.strName
        row["strType"] = address_result.strType
        enrichment_records.append(
            (
                meter,
                safe_str(source.get("addressLine1")),
                safe_str(source.get("addressLine2")),
                address_result,
            )
        )

        for month in months:
            row[f"amount_{month.replace('-', '_')}_C"] = meter_amounts[month]
            row[f"units_{month.replace('-', '_')}"] = _decimal_text(meter_units[month])
        out_rows.append(row)

    output = pd.DataFrame(out_rows, columns=output_columns)
    if output.empty or output["masterId"].duplicated().any():
        raise ValueError("Sales All monthly-source output identity failure")

    raw_after_records = {
        safe_str(row.get("masterId")): row
        for row in output.to_dict("records")
    }
    raw_after = raw_address_snapshot(raw_after_records)
    raw_mutations = raw_address_mutation_count(raw_before, raw_after)
    if raw_mutations != 0:
        raise ValueError(
            "Monthly-source Stage 06 changed governed raw address fields: "
            f"count={raw_mutations}"
        )

    enrichment_report_path = output_path.with_suffix(".address_enrichment.json")
    enrichment_report = build_enrichment_report(
        enrichment_records,
        source_label=str(commercial_snap.path),
        source_sha256=commercial_snap.sha256,
        raw_address_mutation_count=raw_mutations,
    )
    enrichment_report_sha = write_address_json_atomic(
        enrichment_report, enrichment_report_path
    )
    address_contract = enrichment_contract(
        enrichment_report,
        report_path=enrichment_report_path,
        report_sha256=enrichment_report_sha,
    )

    output_snap = _write_csv(output, output_path)
    stats = {
        "masterRows": len(master),
        "monthlyRowsMerged": monthly_rows,
        "metersWithSales": int((output["totalAmountC"].astype("int64") > 0).sum()),
        "metersWithoutSales": int((output["totalAmountC"].astype("int64") == 0).sum()),
        "totalOutputRows": len(output),
        "totalUnits": _decimal_text(total_units),
        "addressEnrichedRows": address_contract["enrichedRows"],
        "addressUnresolvedRows": address_contract["unresolvedRows"],
    }
    source_contract = {
        "sourceOrigin": "monthly_source",
        "sourceRunId": (stage05_payload.get("sourceContract") or {}).get("sourceRunId"),
        "lmPcode": lm_pcode,
        "fromMonth": months[0],
        "toMonth": months[-1],
        "includedMonths": months,
        "provider": provider,
        "recencyFactsAvailable": False,
        "visibilityOwnership": "OPERATIONAL_WRITERS_ONLY",
        "stage05Manifest": {
            "path": str(master_manifest_path),
            "filename": master_manifest_path.name,
            "sha256": stage05_manifest_sha,
            "buildFingerprint": stage05_payload.get("buildFingerprint"),
        },
        "meterMaster": {
            "path": str(master_path),
            "filename": master_path.name,
            "rows": len(master),
            "columns": MASTER_COLUMNS,
            "sha256": sha256_bytes(master_path.read_bytes()),
            "documentIdsSha256": canonical_sha256(sorted(master_ids)),
        },
        "commercialSource": {
            "path": str(commercial_snap.path),
            "filename": commercial_snap.path.name,
            "rows": commercial_snap.rows,
            "sha256": commercial_snap.sha256,
        },
        "monthlyInputs": monthly_evidence,
        "atomicFactsFabricated": 0,
    }
    output_contract = {
        "path": str(output_path),
        "filename": output_path.name,
        "rows": len(output),
        "columns": output_columns,
        "sha256": output_snap.sha256,
        "documentIdsSha256": canonical_sha256(sorted(master_ids)),
        "months": months,
        "monthlyColumns": monthly_amount_columns,
        "monthlyUnitColumns": monthly_units_columns,
        "provider": provider,
        "totalAmountC": total_sales,
        "totalUnits": _decimal_text(total_units),
        "visibilityColumn": "ABSENT",
        "addressEnrichment": address_contract,
    }
    fingerprint = {
        "sourceContract": source_contract,
        "outputContract": {
            k: output_contract[k]
            for k in (
                "filename", "rows", "columns", "sha256", "documentIdsSha256",
                "months", "monthlyColumns", "monthlyUnitColumns", "provider",
                "totalAmountC", "totalUnits", "visibilityColumn",
                "addressEnrichment"
            )
        },
        "stats": stats,
    }
    manifest = {
        "schemaVersion": 2,
        "stage": "06",
        "script": "06_build_sales_all_meters.py",
        "status": "PASS",
        "result": "BUILD_WRITTEN",
        "createdAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceContract": source_contract,
        "outputContract": output_contract,
        "stats": stats,
        "buildFingerprint": canonical_sha256(fingerprint),
    }
    _write_json(manifest, manifest_path)
    return manifest
