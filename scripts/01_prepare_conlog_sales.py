"""
Stage 01: prepare one Conlog RAW STAGING CSV into one upload-ready Atomic Sales CSV.

Run one LM and one month at a time.

Input filename contract:
    input/conlog_sales/conlog_prepaid_sales__<lmPcode>__YYYY-MM.csv

Output filename contract:
    output/atomic/atomic__conlog_prepaid_sales__<lmPcode>__YYYY-MM__<rows>.csv

RAW STAGING input columns (exact order):
    lmPcode, txAt, meterNo, amountTotalC, costC, vatC

The three RAW STAGING monetary fields contain decimal rand values. Stage 01 is
 the single controlled boundary that converts them to integer cents.

This script is environment-neutral. It does not connect to Firebase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input" / "conlog_sales"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "atomic"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs"

CONLOG_VENDING_PROVIDER_ID = "vpr_7f4d3c91a2b84e6f"
SOURCE_FILENAME_RE = re.compile(
    r"^conlog_prepaid_sales__(?P<lm_pcode>[A-Za-z0-9_-]+)__(?P<period>\d{4}-\d{2})\.csv$"
)

STAGING_COLUMNS = [
    "lmPcode",
    "txAt",
    "meterNo",
    "amountTotalC",
    "costC",
    "vatC",
]

ATOMIC_COLUMNS = [
    "atomicId",
    "vendingProviderId",
    "lmPcode",
    "meterNo",
    "txAtISO",
    "txAtMs",
    "ym",
    "y",
    "m",
    "amountTotalC",
    "costC",
    "vatC",
    "currency",
    "sourceFileId",
    "sourceRow",
    "ingestedAtISO",
    "ingestedAtMs",
]

# Used to compare reruns without treating a new ingestion timestamp as business-data drift.
ATOMIC_BUSINESS_COLUMNS = [
    column
    for column in ATOMIC_COLUMNS
    if column not in {"ingestedAtISO", "ingestedAtMs"}
]


@dataclass
class PreparedAtomic:
    source_path: Path
    output_path: Path
    lm_pcode: str
    period: str
    source_sha256: str
    business_sha256: str
    atomic: pd.DataFrame
    rejected: pd.DataFrame
    rows_read: int
    unique_meters: int
    duplicate_business_rows: int
    amount_total_cents: int
    cost_total_cents: int
    vat_total_cents: int
    meter_normalization_changes: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare one six-column Conlog RAW STAGING CSV into one "
            "upload-ready Atomic Sales CSV."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Directory containing Conlog RAW STAGING CSV files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for upload-ready Atomic Sales CSV files.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for Stage 01 summaries and rejected-row reports.",
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Expected LM pCode. It must match the source filename and every row.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Sales month to process in YYYY-MM format.",
    )
    parser.add_argument(
        "--vending-provider-id",
        default=CONLOG_VENDING_PROVIDER_ID,
        help=(
            "Stable Firestore document ID for Conlog in vending_providers. "
            f"Default: {CONLOG_VENDING_PROVIDER_ID}."
        ),
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Replace different existing Atomic output(s) only after successful validation. "
            "Semantically identical existing output is left unchanged automatically."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate and report the planned Atomic output without writing it.",
    )
    return parser.parse_args()


def validate_month(value: str, argument_name: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise SystemExit(f"{argument_name} must use YYYY-MM format: {value!r}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def clean_header(value: object) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(value)
        .replace("\ufeff", "")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip(),
    )


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def read_csv_robust(path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            frame = pd.read_csv(path, dtype=str, encoding=encoding)
            frame.columns = [clean_header(column) for column in frame.columns]
            return frame
        except Exception as exc:  # pragma: no cover - final error is re-raised
            last_error = exc

    if last_error is None:
        raise RuntimeError(f"Unable to read CSV: {path}")
    raise last_error


def parse_source_identity(path: Path) -> tuple[str, str]:
    match = SOURCE_FILENAME_RE.fullmatch(path.name)
    if not match:
        raise ValueError(
            "Invalid RAW STAGING filename. Expected "
            "conlog_prepaid_sales__<lmPcode>__YYYY-MM.csv: "
            f"{path.name}"
        )
    return match.group("lm_pcode").upper(), match.group("period")


def parse_transaction_datetime(value: object) -> pd.Timestamp:
    text = clean_text(value)
    if not text:
        return pd.NaT

    for date_format in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return pd.Timestamp(dt.datetime.strptime(text, date_format))
        except ValueError:
            pass

    return pd.NaT


def normalize_meter_no(value: object) -> str:
    text = "".join(clean_text(value).upper().split())
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def parse_rand_to_cents(value: object) -> Optional[int]:
    """Convert one RAW STAGING decimal-rand value to integer cents exactly once."""
    text = clean_text(value)
    if not text:
        return None

    text = text.replace(" ", "").replace("\u00a0", "")
    text = re.sub(r"^[Rr]", "", text)

    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None

    cents = (amount * Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(cents)


def legacy_wall_clock_epoch_ms(value: pd.Timestamp) -> int:
    """
    Preserve the existing conlog_sales_atomic time contract.

    txAtISO is stored without an offset. txAtMs is calculated from those same
    wall-clock components as UTC. This deliberately preserves compatibility
    with the existing Atomic documents; it is not a timezone conversion.
    """
    value_datetime = value.to_pydatetime().replace(tzinfo=dt.timezone.utc)
    return int(value_datetime.timestamp() * 1000)


def append_reason(reasons: pd.Series, mask: pd.Series, label: str) -> None:
    current = reasons.loc[mask]
    reasons.loc[mask] = current.where(current == "", current + ";") + label


def dataframe_csv_bytes(frame: pd.DataFrame, columns: list[str]) -> bytes:
    return frame[columns].to_csv(
        index=False,
        lineterminator="\n",
    ).encode("utf-8")


def business_sha256(frame: pd.DataFrame) -> str:
    return sha256_bytes(dataframe_csv_bytes(frame, ATOMIC_BUSINESS_COLUMNS))


def existing_business_sha256(path: Path) -> str:
    frame = read_csv_robust(path)
    missing = [column for column in ATOMIC_BUSINESS_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            f"Existing Atomic output {path.name} is missing columns: {missing}"
        )
    return business_sha256(frame)


def canonicalize(
    frame: pd.DataFrame,
    *,
    source_file_id: str,
    expected_lm_pcode: str,
    expected_period: str,
    vending_provider_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    if list(frame.columns) != STAGING_COLUMNS:
        raise ValueError(
            "RAW STAGING schema mismatch. Expected exact columns and order: "
            f"{STAGING_COLUMNS}. Found: {list(frame.columns)}"
        )

    working = frame.copy()
    source_rows = pd.Series(
        np.arange(1, len(working) + 1, dtype=np.int64), index=working.index
    )

    out = pd.DataFrame(index=working.index)
    out["sourceRow"] = source_rows
    out["lmPcode"] = working["lmPcode"].apply(clean_text).str.upper()
    out["txAt_dt"] = working["txAt"].apply(parse_transaction_datetime)
    out["meterNoSource"] = working["meterNo"].apply(clean_text)
    out["meterNo"] = working["meterNo"].apply(normalize_meter_no)
    out["amountTotalC"] = working["amountTotalC"].apply(parse_rand_to_cents)
    out["costC"] = working["costC"].apply(parse_rand_to_cents)
    out["vatC"] = working["vatC"].apply(parse_rand_to_cents)

    reasons = pd.Series("", index=working.index, dtype="object")

    append_reason(reasons, out["lmPcode"].eq(""), "blank_lmPcode")
    append_reason(
        reasons,
        out["lmPcode"].ne(expected_lm_pcode),
        "lmPcode_filename_or_argument_mismatch",
    )
    append_reason(reasons, out["txAt_dt"].isna(), "invalid_txAt")

    parsed_period = out["txAt_dt"].dt.strftime("%Y-%m")
    append_reason(
        reasons,
        out["txAt_dt"].notna() & parsed_period.ne(expected_period),
        "txAt_month_mismatch",
    )

    append_reason(reasons, out["meterNo"].eq(""), "blank_meterNo")
    append_reason(
        reasons,
        out["meterNo"].ne("") & ~out["meterNo"].str.fullmatch(r"[A-Z0-9]+"),
        "invalid_meter_characters",
    )
    append_reason(reasons, out["amountTotalC"].isna(), "invalid_amountTotal")
    append_reason(reasons, out["costC"].isna(), "invalid_cost")
    append_reason(reasons, out["vatC"].isna(), "invalid_vat")

    append_reason(
        reasons,
        out["amountTotalC"].notna() & (out["amountTotalC"] < 0),
        "negative_amountTotal",
    )
    append_reason(
        reasons,
        out["costC"].notna() & (out["costC"] < 0),
        "negative_cost",
    )
    append_reason(
        reasons,
        out["vatC"].notna() & (out["vatC"] < 0),
        "negative_vat",
    )

    money_valid = (
        out["amountTotalC"].notna()
        & out["costC"].notna()
        & out["vatC"].notna()
    )
    append_reason(
        reasons,
        money_valid
        & (out["amountTotalC"] != (out["costC"] + out["vatC"])),
        "amount_cost_vat_reconciliation_failed",
    )

    rejected_mask = reasons.ne("")
    rejected = pd.DataFrame(
        {
            "sourceFileId": source_file_id,
            "sourceRow": source_rows.loc[rejected_mask],
            "rejectReason": reasons.loc[rejected_mask],
            "lmPcode": working.loc[rejected_mask, "lmPcode"],
            "txAt": working.loc[rejected_mask, "txAt"],
            "meterNo": working.loc[rejected_mask, "meterNo"],
            "amountTotalC": working.loc[rejected_mask, "amountTotalC"],
            "costC": working.loc[rejected_mask, "costC"],
            "vatC": working.loc[rejected_mask, "vatC"],
        }
    )

    accepted = out.loc[~rejected_mask].copy()
    meter_normalization_changes = int(
        accepted["meterNoSource"].ne(accepted["meterNo"]).sum()
    )

    accepted["txAtISO"] = accepted["txAt_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    accepted["txAtMs"] = accepted["txAt_dt"].apply(legacy_wall_clock_epoch_ms)
    accepted["ym"] = accepted["txAt_dt"].dt.strftime("%Y-%m")
    accepted["y"] = accepted["txAt_dt"].dt.year.astype("int64")
    accepted["m"] = accepted["txAt_dt"].dt.month.astype("int64")
    accepted["amountTotalC"] = accepted["amountTotalC"].astype("int64")
    accepted["costC"] = accepted["costC"].astype("int64")
    accepted["vatC"] = accepted["vatC"].astype("int64")
    accepted["currency"] = "ZAR"
    accepted["sourceFileId"] = source_file_id
    accepted["vendingProviderId"] = vending_provider_id

    base_key = (
        accepted["vendingProviderId"].astype(str)
        + "|"
        + accepted["lmPcode"].astype(str)
        + "|"
        + accepted["meterNo"].astype(str)
        + "|"
        + accepted["txAtISO"].astype(str)
        + "|"
        + accepted["amountTotalC"].astype(str)
        + "|"
        + accepted["costC"].astype(str)
        + "|"
        + accepted["vatC"].astype(str)
    )

    accepted["baseHash"] = base_key.apply(sha1_text)
    accepted["duplicateIndex"] = (
        accepted.groupby("baseHash", sort=False).cumcount() + 1
    )
    accepted["atomicId"] = np.where(
        accepted["duplicateIndex"].eq(1),
        accepted["baseHash"],
        accepted["baseHash"] + "__" + accepted["duplicateIndex"].astype(str),
    )

    if accepted["atomicId"].duplicated().any():
        raise ValueError("Generated atomicId values are not unique")

    accepted = accepted.drop(
        columns=[
            "txAt_dt",
            "meterNoSource",
            "baseHash",
            "duplicateIndex",
        ]
    )

    return accepted, rejected, meter_normalization_changes


def prepare_atomic(source_path: Path, args: argparse.Namespace) -> PreparedAtomic:
    filename_lm, filename_period = parse_source_identity(source_path)
    expected_lm = clean_text(args.lm_pcode).upper()
    expected_period = args.month

    if filename_lm != expected_lm:
        raise ValueError(
            f"Source filename LM {filename_lm!r} does not match --lm-pcode {expected_lm!r}"
        )
    if filename_period != expected_period:
        raise ValueError(
            f"Source filename month {filename_period!r} does not match --month {expected_period!r}"
        )

    provider_id = clean_text(args.vending_provider_id)
    if not provider_id:
        raise ValueError("--vending-provider-id cannot be blank")
    if provider_id != CONLOG_VENDING_PROVIDER_ID:
        raise ValueError(
            "This Conlog Stage 01 script is governed for vending provider ID "
            f"{CONLOG_VENDING_PROVIDER_ID!r}. Received {provider_id!r}."
        )

    source_frame = read_csv_robust(source_path)
    atomic, rejected, meter_changes = canonicalize(
        source_frame,
        source_file_id=source_path.name,
        expected_lm_pcode=expected_lm,
        expected_period=expected_period,
        vending_provider_id=provider_id,
    )

    output_path = (
        args.output_dir.resolve()
        / f"atomic__{source_path.stem}__{len(atomic)}.csv"
    )

    duplicate_business_rows = int(
        atomic.duplicated(
            subset=[
                "vendingProviderId",
                "lmPcode",
                "meterNo",
                "txAtISO",
                "amountTotalC",
                "costC",
                "vatC",
            ]
        ).sum()
    )

    prepared = PreparedAtomic(
        source_path=source_path,
        output_path=output_path,
        lm_pcode=expected_lm,
        period=expected_period,
        source_sha256=sha256_file(source_path),
        business_sha256=business_sha256(atomic),
        atomic=atomic,
        rejected=rejected,
        rows_read=len(source_frame),
        unique_meters=int(atomic["meterNo"].nunique()),
        duplicate_business_rows=duplicate_business_rows,
        amount_total_cents=int(atomic["amountTotalC"].sum()),
        cost_total_cents=int(atomic["costC"].sum()),
        vat_total_cents=int(atomic["vatC"].sum()),
        meter_normalization_changes=meter_changes,
    )
    return prepared


def print_prepared(item: PreparedAtomic) -> None:
    print(f"\n[RAW STAGING FILE] {item.source_path.name}")
    print(f"  LM / period:             {item.lm_pcode} / {item.period}")
    print(f"  rows read:               {item.rows_read:,}")
    print(f"  rows prepared:           {len(item.atomic):,}")
    print(f"  rows rejected:           {len(item.rejected):,}")
    print(f"  unique meters:           {item.unique_meters:,}")
    print(
        "  duplicate Atomic business rows: "
        f"{item.duplicate_business_rows:,} (preserved with unique atomicId suffixes)"
    )
    print(
        "  meter normalisations:    "
        f"{item.meter_normalization_changes:,}"
    )
    print(f"  amount total:            {item.amount_total_cents:,} cents / R {item.amount_total_cents / 100:,.2f}")
    print(f"  cost total:              {item.cost_total_cents:,} cents / R {item.cost_total_cents / 100:,.2f}")
    print(f"  VAT total:               {item.vat_total_cents:,} cents / R {item.vat_total_cents / 100:,.2f}")
    print(f"  source SHA-256:          {item.source_sha256}")
    print(f"  Atomic business SHA-256: {item.business_sha256}")
    print(f"  planned output:          {item.output_path}")


def write_rejected_report(
    rejected: pd.DataFrame,
    *,
    source_path: Path,
    log_dir: Path,
    run_id: str,
) -> Optional[Path]:
    if rejected.empty:
        return None

    log_dir.mkdir(parents=True, exist_ok=True)
    report_path = log_dir / f"stage01_rejected__{source_path.stem}__{run_id}.csv"
    rejected.to_csv(report_path, index=False, lineterminator="\n")
    return report_path


def find_existing_outputs(output_dir: Path, source_stem: str) -> list[Path]:
    return sorted(output_dir.glob(f"atomic__{source_stem}__*.csv"))


def write_atomic_output(
    item: PreparedAtomic,
    *,
    replace_existing: bool,
    ingested_at: dt.datetime,
) -> tuple[str, str, list[Path]]:
    output_dir = item.output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_outputs = find_existing_outputs(output_dir, item.source_path.stem)
    matching_business: list[Path] = []
    different_business: list[Path] = []

    for existing in existing_outputs:
        try:
            existing_hash = existing_business_sha256(existing)
        except Exception:
            different_business.append(existing)
            continue

        if existing_hash == item.business_sha256:
            matching_business.append(existing)
        else:
            different_business.append(existing)

    if matching_business and not different_business and len(existing_outputs) == 1:
        existing = matching_business[0]
        return "unchanged", sha256_file(existing), []

    if existing_outputs and not replace_existing:
        existing_names = ", ".join(path.name for path in existing_outputs)
        raise FileExistsError(
            "Different or ambiguous existing Atomic output(s) found: "
            f"{existing_names}. Review them, then rerun with --replace-existing "
            "only when replacement is approved."
        )

    output_frame = item.atomic.copy()
    ingested_iso = (
        ingested_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    ingested_ms = int(ingested_at.timestamp() * 1000)
    output_frame["ingestedAtISO"] = ingested_iso
    output_frame["ingestedAtMs"] = ingested_ms
    output_frame = output_frame[ATOMIC_COLUMNS]

    temp_path = item.output_path.with_suffix(item.output_path.suffix + ".tmp")
    output_frame.to_csv(temp_path, index=False, lineterminator="\n")
    os.replace(temp_path, item.output_path)

    removed: list[Path] = []
    for existing in existing_outputs:
        if existing.resolve() == item.output_path.resolve():
            continue
        if existing.exists():
            existing.unlink()
            removed.append(existing)

    return "written", sha256_file(item.output_path), removed


def write_summary(
    item: PreparedAtomic,
    *,
    log_dir: Path,
    run_id: str,
    result: str,
    output_sha256: str,
    removed_outputs: list[Path],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / f"stage01_prep_summary__{run_id}.csv"

    row = {
        "stage": "01",
        "provider": "Conlog",
        "vendingProviderId": CONLOG_VENDING_PROVIDER_ID,
        "lmPcode": item.lm_pcode,
        "period": item.period,
        "sourceFile": str(item.source_path),
        "sourceSha256": item.source_sha256,
        "rowsRead": item.rows_read,
        "rowsPrepared": len(item.atomic),
        "rowsRejected": len(item.rejected),
        "uniqueMeters": item.unique_meters,
        "duplicateAtomicBusinessRowsPreserved": item.duplicate_business_rows,
        "meterNormalisations": item.meter_normalization_changes,
        "amountTotalC": item.amount_total_cents,
        "costC": item.cost_total_cents,
        "vatC": item.vat_total_cents,
        "atomicBusinessSha256": item.business_sha256,
        "atomicOutput": str(item.output_path),
        "atomicOutputSha256": output_sha256,
        "result": result,
        "obsoleteOutputsRemoved": len(removed_outputs),
    }
    pd.DataFrame([row]).to_csv(summary_path, index=False, lineterminator="\n")
    return summary_path


def main() -> None:
    args = parse_args()
    validate_month(args.month, "--month")

    args.input_dir = args.input_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.log_dir = args.log_dir.resolve()

    expected_lm = clean_text(args.lm_pcode).upper()
    if not expected_lm:
        raise SystemExit("--lm-pcode cannot be blank")

    expected_source = (
        args.input_dir
        / f"conlog_prepaid_sales__{expected_lm}__{args.month}.csv"
    )
    if not expected_source.is_file():
        raise FileNotFoundError(
            "Expected Conlog RAW STAGING file not found: "
            f"{expected_source}"
        )

    mode = "preflight-only" if args.preflight_only else "write"
    print("[STAGE 01] Conlog RAW STAGING -> ATOMIC SALES")
    print(f"  input:               {args.input_dir}")
    print(f"  output:              {args.output_dir}")
    print(f"  expected LM:         {expected_lm}")
    print(f"  month:               {args.month}")
    print(f"  vending provider ID: {args.vending_provider_id}")
    print(f"  selected files:      1")
    print(f"  mode:                {mode}")

    item = prepare_atomic(expected_source, args)
    print_prepared(item)

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if not item.rejected.empty:
        rejected_path = write_rejected_report(
            item.rejected,
            source_path=item.source_path,
            log_dir=args.log_dir,
            run_id=run_id,
        )
        print(f"\n[BLOCKED] {len(item.rejected):,} rejected row(s) detected.")
        if rejected_path:
            print(f"[REJECTED ROWS] {rejected_path}")
        print("No Atomic Sales file was written.")
        raise SystemExit(1)

    if args.preflight_only:
        print("\n[PREFLIGHT OK] The selected RAW STAGING file passed Stage 01 validation.")
        print("No Atomic Sales file was written.")
        return

    ingested_at = dt.datetime.now(dt.timezone.utc)
    result, output_sha, removed = write_atomic_output(
        item,
        replace_existing=args.replace_existing,
        ingested_at=ingested_at,
    )

    if result == "unchanged":
        print(f"[UNCHANGED] {item.output_path.name}")
        print("Existing Atomic business data is identical; original ingestion metadata was preserved.")
    else:
        print(f"[WRITTEN] {item.output_path.name}")

    for removed_path in removed:
        print(f"[REMOVED OBSOLETE] {removed_path.name}")

    print(f"  output SHA-256:       {output_sha}")

    summary_path = write_summary(
        item,
        log_dir=args.log_dir,
        run_id=run_id,
        result=result,
        output_sha256=output_sha,
        removed_outputs=removed,
    )
    print(f"\n[SUMMARY] {summary_path}")
    print("[OK] Stage 01 completed. RAW STAGING source file was not modified.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FAILED] {exc}")
        raise SystemExit(1)
