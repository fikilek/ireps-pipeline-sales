"""
Stage 00: prepare one original Conlog portal CSV export into one RAW STAGING CSV.

Run one LM and one month at a time.

Input filename contract:
    input/raw-sales/conlog_raw_sales__<lmPcode>__YYYY-MM.csv

Output filename contract:
    input/conlog_sales/conlog_prepaid_sales__<lmPcode>__YYYY-MM.csv

RAW STAGING output columns:
    lmPcode, txAt, meterNo, amountTotalC, costC, vatC

The original RAW files are read-only source evidence. This script never edits,
renames, moves, or deletes them. It does not connect to Firebase.

Meter identities use the shared normalization contract: remove all whitespace,
uppercase letters, preserve leading zeroes, and impose no universal fixed length.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input" / "raw-sales"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "input" / "conlog_sales"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs"

RAW_FILENAME_RE = re.compile(
    r"^conlog_raw_sales__(?P<lm_pcode>[A-Za-z0-9_-]+)__(?P<period>\d{4}-\d{2})\.csv$"
)

RAW_REQUIRED_COLUMNS = [
    "CDUToggle",
    "VendingNo_3",
    "TransactionDateTime_2",
    "Amount",
    "RefundAmount",
    "CostOfUnits_2",
    "VAT",
]

STAGING_COLUMNS = [
    "lmPcode",
    "txAt",
    "meterNo",
    "amountTotalC",
    "costC",
    "vatC",
]



@dataclass
class PreparedFile:
    raw_path: Path
    output_path: Path
    lm_pcode: str
    period: str
    raw_sha256: str
    output_sha256: str
    staging: pd.DataFrame
    rows_read: int
    unique_meters: int
    exact_duplicate_rows: int
    amount_total_cents: int
    cost_total_cents: int
    vat_total_cents: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare original Conlog portal exports into six-column RAW STAGING CSV files."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="RAW directory. Relative paths resolve from the repository root.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="RAW STAGING output directory. Relative paths resolve from the repository root.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Log directory. Relative paths resolve from the repository root.",
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Expected LM pCode. It must match every selected RAW filename.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Sales month to process in YYYY-MM format.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Replace a different existing RAW STAGING output after successful validation. "
            "Identical existing outputs are left unchanged automatically."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate and report planned outputs without writing RAW STAGING files.",
    )
    return parser.parse_args()


def validate_month(value: Optional[str], argument_name: str) -> None:
    if value is None:
        return
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


def resolve_project_path(value: Path) -> Path:
    """Resolve relative CLI paths from PROJECT_ROOT, never from the shell CWD."""
    return value if value.is_absolute() else PROJECT_ROOT / value


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
    match = RAW_FILENAME_RE.fullmatch(path.name)
    if not match:
        raise ValueError(
            "Invalid RAW filename. Expected "
            "conlog_raw_sales__<lmPcode>__YYYY-MM.csv: "
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


def normalize_raw_meter(value: object) -> str:
    """Apply the governed meter-number normalization without fixed-length rules."""
    text = re.sub(r"\s+", "", clean_text(value)).upper()

    # Conlog/CSV tools can render an integer identifier as text ending in ".0".
    # Remove only that numeric source artifact; preserve all other characters.
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    return text


def parse_money_to_cents(value: object) -> Optional[int]:
    text = clean_text(value)
    if not text:
        return None

    text = text.replace(" ", "").replace("\u00a0", "")
    text = re.sub(r"[Rr]", "", text)

    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None

    cents = (amount * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return int(cents)


def append_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    current = reasons.loc[mask]
    reasons.loc[mask] = current.where(current == "", current + ";") + reason


def canonicalize_raw(
    frame: pd.DataFrame,
    source_file: Path,
    expected_lm_pcode: str,
    expected_period: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    missing = [column for column in RAW_REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            f"{source_file.name} is missing required Conlog portal columns: {missing}. "
            f"Found: {list(frame.columns)}"
        )

    source_rows = pd.Series(frame.index + 2, index=frame.index, dtype="int64")
    reasons = pd.Series("", index=frame.index, dtype="object")

    tx_parsed = frame["TransactionDateTime_2"].apply(parse_transaction_datetime)
    append_reason(reasons, tx_parsed.isna(), "invalid_transaction_datetime")

    actual_period = tx_parsed.dt.strftime("%Y-%m")
    append_reason(
        reasons,
        tx_parsed.notna() & (actual_period != expected_period),
        "transaction_month_mismatch",
    )

    meter_no = frame["VendingNo_3"].apply(normalize_raw_meter)
    meter_group = frame["CDUToggle"].apply(normalize_raw_meter)

    append_reason(reasons, meter_no == "", "blank_meter_number")
    append_reason(
        reasons,
        (meter_group != "") & (meter_group != meter_no),
        "meter_group_mismatch",
    )

    amount_cents = frame["Amount"].apply(parse_money_to_cents)
    cost_cents = frame["CostOfUnits_2"].apply(parse_money_to_cents)
    vat_cents = frame["VAT"].apply(parse_money_to_cents)
    refund_cents = frame["RefundAmount"].apply(parse_money_to_cents)

    append_reason(reasons, amount_cents.isna(), "invalid_amount")
    append_reason(reasons, cost_cents.isna(), "invalid_cost")
    append_reason(reasons, vat_cents.isna(), "invalid_vat")
    append_reason(reasons, refund_cents.isna(), "invalid_refund_amount")

    append_reason(reasons, amount_cents.notna() & (amount_cents < 0), "negative_amount")
    append_reason(reasons, cost_cents.notna() & (cost_cents < 0), "negative_cost")
    append_reason(reasons, vat_cents.notna() & (vat_cents < 0), "negative_vat")
    append_reason(
        reasons,
        refund_cents.notna() & (refund_cents != 0),
        "non_zero_refund_requires_separate_design",
    )

    money_valid = amount_cents.notna() & cost_cents.notna() & vat_cents.notna()
    append_reason(
        reasons,
        money_valid & (amount_cents != (cost_cents + vat_cents)),
        "amount_cost_vat_reconciliation_failed",
    )

    rejected_mask = reasons != ""
    rejected = pd.DataFrame(
        {
            "sourceFileId": source_file.name,
            "sourceRow": source_rows.loc[rejected_mask],
            "rejectReason": reasons.loc[rejected_mask],
            "TransactionDateTime_2": frame.loc[
                rejected_mask, "TransactionDateTime_2"
            ],
            "CDUToggle": frame.loc[rejected_mask, "CDUToggle"],
            "VendingNo_3": frame.loc[rejected_mask, "VendingNo_3"],
            "Amount": frame.loc[rejected_mask, "Amount"],
            "RefundAmount": frame.loc[rejected_mask, "RefundAmount"],
            "CostOfUnits_2": frame.loc[rejected_mask, "CostOfUnits_2"],
            "VAT": frame.loc[rejected_mask, "VAT"],
        }
    ).reset_index(drop=True)

    valid_mask = ~rejected_mask
    staging = pd.DataFrame(
        {
            "lmPcode": expected_lm_pcode,
            "txAt": tx_parsed.loc[valid_mask].dt.strftime("%d/%m/%Y %H:%M"),
            "meterNo": meter_no.loc[valid_mask],
            "amountTotalC": frame.loc[valid_mask, "Amount"].apply(clean_text),
            "costC": frame.loc[valid_mask, "CostOfUnits_2"].apply(clean_text),
            "vatC": frame.loc[valid_mask, "VAT"].apply(clean_text),
        }
    ).reset_index(drop=True)

    numeric_totals = {
        "amountTotalCents": int(amount_cents.loc[valid_mask].sum()),
        "costTotalCents": int(cost_cents.loc[valid_mask].sum()),
        "vatTotalCents": int(vat_cents.loc[valid_mask].sum()),
    }

    return staging[STAGING_COLUMNS], rejected, numeric_totals


def discover_raw_files(args: argparse.Namespace) -> list[Path]:
    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"RAW input directory not found: {input_dir}")

    expected_lm = args.lm_pcode.strip().upper()
    expected_name = f"conlog_raw_sales__{expected_lm}__{args.month}.csv"
    expected_path = input_dir / expected_name

    if not expected_path.is_file():
        available = sorted(path.name for path in input_dir.glob("conlog_raw_sales__*.csv"))
        available_text = "\n".join(f"  - {name}" for name in available) or "  (none)"
        raise SystemExit(
            "[MISSING] Expected one monthly RAW Conlog file:\n"
            f"  {expected_path}\n"
            "Available RAW files:\n"
            f"{available_text}"
        )

    lm_pcode, period = parse_source_identity(expected_path)
    if lm_pcode != expected_lm or period != args.month:
        raise SystemExit(
            "[SAFETY] RAW filename identity mismatch. "
            f"Expected LM/month {expected_lm}/{args.month}, "
            f"found {lm_pcode}/{period}."
        )

    return [expected_path]


def csv_payload(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def prepare_file(path: Path, args: argparse.Namespace) -> tuple[PreparedFile, pd.DataFrame]:
    expected_lm, period = parse_source_identity(path)
    expected_argument_lm = args.lm_pcode.strip().upper()

    if expected_lm != expected_argument_lm:
        raise ValueError(
            f"LM mismatch for {path.name}: filename={expected_lm}, "
            f"argument={expected_argument_lm}"
        )

    raw_hash_before = sha256_file(path)
    frame = read_csv_robust(path)
    raw_hash_after = sha256_file(path)

    if raw_hash_before != raw_hash_after:
        raise RuntimeError(
            f"RAW source changed while it was being read: {path}. Stop and investigate."
        )

    staging, rejected, totals = canonicalize_raw(
        frame=frame,
        source_file=path,
        expected_lm_pcode=expected_lm,
        expected_period=period,
    )

    output_path = args.output_dir.resolve() / (
        f"conlog_prepaid_sales__{expected_lm}__{period}.csv"
    )
    output_hash = sha256_bytes(csv_payload(staging))

    exact_duplicates = int(staging.duplicated(subset=STAGING_COLUMNS).sum())

    prepared = PreparedFile(
        raw_path=path,
        output_path=output_path,
        lm_pcode=expected_lm,
        period=period,
        raw_sha256=raw_hash_before,
        output_sha256=output_hash,
        staging=staging,
        rows_read=int(len(frame)),
        unique_meters=int(staging["meterNo"].nunique()),
        exact_duplicate_rows=exact_duplicates,
        amount_total_cents=totals["amountTotalCents"],
        cost_total_cents=totals["costTotalCents"],
        vat_total_cents=totals["vatTotalCents"],
    )
    return prepared, rejected


def print_prepared(item: PreparedFile, rejected_rows: int) -> None:
    print(f"\n[RAW FILE] {item.raw_path.name}")
    print(f"  LM / period:          {item.lm_pcode} / {item.period}")
    print(f"  rows read:            {item.rows_read:,}")
    print(f"  rows prepared:        {len(item.staging):,}")
    print(f"  rows rejected:        {rejected_rows:,}")
    print(f"  unique meters:        {item.unique_meters:,}")
    print(
        "  duplicate six-field staging rows: "
        f"{item.exact_duplicate_rows:,} (preserved)"
    )
    print(f"  amount total:         R {item.amount_total_cents / 100:,.2f}")
    print(f"  cost total:           R {item.cost_total_cents / 100:,.2f}")
    print(f"  VAT total:            R {item.vat_total_cents / 100:,.2f}")
    print(f"  RAW SHA-256:          {item.raw_sha256}")
    print(f"  output SHA-256:       {item.output_sha256}")
    print(f"  planned output:       {item.output_path}")


def write_rejection_report(
    rejected: pd.DataFrame,
    raw_path: Path,
    log_dir: Path,
    run_id: str,
) -> Optional[Path]:
    if rejected.empty:
        return None

    log_dir.mkdir(parents=True, exist_ok=True)
    report_path = log_dir / f"stage00_rejected__{raw_path.stem}__{run_id}.csv"
    rejected.to_csv(report_path, index=False, lineterminator="\n")
    return report_path


def precheck_output_conflicts(
    prepared_files: list[PreparedFile],
    replace_existing: bool,
) -> None:
    for item in prepared_files:
        if not item.output_path.exists():
            continue

        existing_hash = sha256_file(item.output_path)
        if existing_hash == item.output_sha256:
            continue

        if not replace_existing:
            raise SystemExit(
                "[SAFETY] A different RAW STAGING output already exists:\n"
                f"  file:     {item.output_path}\n"
                f"  existing: {existing_hash}\n"
                f"  planned:  {item.output_sha256}\n"
                "Review the difference, then rerun with --replace-existing only if approved."
            )


def write_outputs(prepared_files: list[PreparedFile]) -> list[str]:
    statuses: list[str] = []

    for item in prepared_files:
        item.output_path.parent.mkdir(parents=True, exist_ok=True)

        if item.output_path.exists():
            existing_hash = sha256_file(item.output_path)
            if existing_hash == item.output_sha256:
                statuses.append("UNCHANGED")
                print(f"[UNCHANGED] {item.output_path.name}")
                continue

        temp_path = item.output_path.with_suffix(item.output_path.suffix + ".tmp")
        temp_path.write_bytes(csv_payload(item.staging))

        if sha256_file(temp_path) != item.output_sha256:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Output hash verification failed before finalising {item.output_path.name}."
            )

        temp_path.replace(item.output_path)
        statuses.append("WRITTEN")
        print(f"[WRITTEN] {item.output_path.name}")

    return statuses


def write_summary(
    prepared_files: list[PreparedFile],
    statuses: list[str],
    log_dir: Path,
    run_id: str,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for item, status in zip(prepared_files, statuses):
        rows.append(
            {
                "rawFile": item.raw_path.name,
                "rawSha256": item.raw_sha256,
                "lmPcode": item.lm_pcode,
                "period": item.period,
                "rowsRead": item.rows_read,
                "rowsPrepared": len(item.staging),
                "rowsRejected": 0,
                "uniqueMeters": item.unique_meters,
                "duplicateSixFieldStagingRowsPreserved": item.exact_duplicate_rows,
                "amountTotalCents": item.amount_total_cents,
                "costTotalCents": item.cost_total_cents,
                "vatTotalCents": item.vat_total_cents,
                "outputFile": item.output_path.name,
                "outputSha256": item.output_sha256,
                "status": status,
            }
        )

    summary = pd.DataFrame(rows)
    summary_path = log_dir / f"stage00_prep_summary__{run_id}.csv"
    summary.to_csv(summary_path, index=False, lineterminator="\n")
    return summary_path


def main() -> None:
    args = parse_args()
    validate_month(args.month, "--month")

    args.input_dir = resolve_project_path(args.input_dir).resolve()
    args.output_dir = resolve_project_path(args.output_dir).resolve()
    args.log_dir = resolve_project_path(args.log_dir).resolve()

    args.lm_pcode = args.lm_pcode.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]+", args.lm_pcode):
        raise SystemExit(f"Invalid --lm-pcode: {args.lm_pcode!r}")

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_files = discover_raw_files(args)

    print("[STAGE 00] Conlog RAW -> RAW STAGING")
    print(f"  input:          {args.input_dir.resolve()}")
    print(f"  output:         {args.output_dir.resolve()}")
    print(f"  expected LM:    {args.lm_pcode}")
    print(f"  month:          {args.month}")
    print(f"  selected files: {len(raw_files)}")
    print(f"  mode:           {'preflight-only' if args.preflight_only else 'write'}")

    prepared_files: list[PreparedFile] = []
    rejected_by_file: list[tuple[Path, pd.DataFrame]] = []

    for raw_path in raw_files:
        prepared, rejected = prepare_file(raw_path, args)
        prepared_files.append(prepared)
        rejected_by_file.append((raw_path, rejected))
        print_prepared(prepared, len(rejected))

    total_rejected = sum(len(rejected) for _, rejected in rejected_by_file)
    if total_rejected:
        print(f"\n[BLOCKED] {total_rejected:,} rejected row(s) detected.")
        for raw_path, rejected in rejected_by_file:
            report = write_rejection_report(
                rejected=rejected,
                raw_path=raw_path,
                log_dir=args.log_dir.resolve(),
                run_id=run_id,
            )
            if report:
                print(f"  rejection report: {report}")
        raise SystemExit(
            "No RAW STAGING files were written. Correct the Stage 00 rules or source issue, "
            "then rerun. Do not edit RAW STAGING manually."
        )

    precheck_output_conflicts(prepared_files, args.replace_existing)

    if args.preflight_only:
        print("\n[PREFLIGHT OK] All selected RAW files passed Stage 00 validation.")
        print("No RAW STAGING file was written.")
        return

    statuses = write_outputs(prepared_files)
    summary_path = write_summary(
        prepared_files=prepared_files,
        statuses=statuses,
        log_dir=args.log_dir.resolve(),
        run_id=run_id,
    )

    print(f"\n[SUMMARY] {summary_path}")
    print("[OK] Stage 00 completed. RAW source files were not modified.")


if __name__ == "__main__":
    main()
