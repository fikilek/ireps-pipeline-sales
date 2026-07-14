"""
05_build_meter_master_v3.py

Build an environment-neutral meter master staging CSV from:
- every available meter-level monthly sales CSV in output/monthly
- Customer_Details.csv
- 90_Days_No_Purchase_Report.csv

The script does not connect to Firebase and does not choose a Firebase project.
The generated CSV can later be uploaded to ireps-test, ireps-trials, or
ireps-production by an environment-aware uploader.

Default monthly filename format:
    monthly__FULL__YYYY-MM__from_atomic.csv

Default output filename:
    meter_master__<lmPcode>__FULL__<first-month>_to_<last-month>.csv

Governance controls:
- every project path resolves from this script's repository root
- monthly rows must match the requested LM and filename month
- meter identifiers must be non-empty uppercase alphanumeric values after normalisation
- Customer Details duplicates prefer the dominant identity pattern where CustomerNo equals AccountNo and differs from MeterNumber
- an Active duplicate may replace a Block Purchases duplicate without consulting ERF, address, or customer-name fields
- competing dominant identities may resolve only by the latest valid LastPurchaseDate
- tied or missing purchase dates still stop the build
- 90-day report duplicates use the same placeholder preference and may resolve competing real customer numbers by latest valid LastPurchaseDate
- tied or missing NPR purchase dates still stop genuine competing non-placeholder identities
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONTHLY_FILENAME_RE = re.compile(
    r"^monthly__(?P<scope>[A-Za-z0-9_-]+)__(?P<period>\d{4}-\d{2})__from_atomic\.csv$"
)
METER_NO_RE = re.compile(r"^[A-Z0-9]+$")

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

MeterKey = str
MasterMap = Dict[MeterKey, Dict[str, Any]]
CustomerMap = Dict[MeterKey, Dict[str, Any]]
NprMap = Dict[MeterKey, Dict[str, Any]]


@dataclass(frozen=True)
class MonthlyInput:
    period: str
    path: Path


@dataclass(frozen=True)
class BuildConfig:
    lm_pcode: str
    provider: str
    meter_type: str


@dataclass
class BuildStats:
    monthly_backed_meters: int = 0
    customer_only_seeded_meters: int = 0
    npr_only_seeded_meters: int = 0
    customer_placeholder_duplicates_resolved: int = 0
    customer_active_status_duplicates_resolved: int = 0
    customer_latest_purchase_duplicates_resolved: int = 0
    npr_placeholder_duplicates_resolved: int = 0
    npr_latest_purchase_duplicates_resolved: int = 0
    total_master_rows: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build meter_master from every available monthly sales file."
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Local municipality/workbase code, for example ZA7423.",
    )
    parser.add_argument(
        "--monthly-dir",
        type=Path,
        default=Path("output/monthly"),
        help="Directory containing monthly meter-level CSV files.",
    )
    parser.add_argument(
        "--scope",
        default="FULL",
        help="Monthly filename scope to consume. Default: FULL.",
    )
    parser.add_argument(
        "--from-month",
        help="Optional inclusive first month in YYYY-MM format.",
    )
    parser.add_argument(
        "--to-month",
        help="Optional inclusive last month in YYYY-MM format.",
    )
    parser.add_argument(
        "--customer-details",
        type=Path,
        default=Path("input/reference/Customer_Details.csv"),
        help="Customer details reference CSV.",
    )
    parser.add_argument(
        "--npr",
        type=Path,
        default=Path("input/reference/90_Days_No_Purchase_Report.csv"),
        help="90-days-no-purchase reference CSV.",
    )
    parser.add_argument(
        "--provider",
        default="conlog",
        help="Sales provider written to the master output. Default: conlog.",
    )
    parser.add_argument(
        "--meter-type",
        default="electricity",
        help="Default meter type. Default: electricity.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/meter_master"),
        help="Directory for the generated meter master CSV.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional exact output CSV path. Overrides --output-dir.",
    )
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="Allow missing months inside the discovered date range.",
    )
    return parser.parse_args()


def validate_month(value: Optional[str], argument_name: str) -> None:
    if value is None:
        return
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"{argument_name} must use YYYY-MM format: {value}")


def resolve_project_path(path: Path) -> Path:
    """Resolve relative runtime paths from the repository root, never the shell CWD."""
    return path if path.is_absolute() else PROJECT_ROOT / path


def require_file(filepath: Path) -> None:
    if not filepath.is_file():
        raise FileNotFoundError(f"Required file not found: {filepath}")


def require_columns(df: pd.DataFrame, required: Sequence[str], source: Path) -> None:
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")


def discover_monthly_inputs(
    monthly_dir: Path,
    scope: str,
    from_month: Optional[str] = None,
    to_month: Optional[str] = None,
) -> list[MonthlyInput]:
    if not monthly_dir.is_dir():
        raise FileNotFoundError(f"Monthly directory not found: {monthly_dir}")

    discovered: dict[str, Path] = {}

    for path in monthly_dir.glob("monthly__*__????-??__from_atomic.csv"):
        match = MONTHLY_FILENAME_RE.fullmatch(path.name)
        if not match or match.group("scope") != scope:
            continue

        period = match.group("period")
        validate_month(period, "Monthly filename period")

        if from_month and period < from_month:
            continue
        if to_month and period > to_month:
            continue

        if period in discovered:
            raise ValueError(
                f"Duplicate monthly files found for {period}: "
                f"{discovered[period]} and {path}"
            )
        discovered[period] = path

    inputs = [MonthlyInput(period, discovered[period]) for period in sorted(discovered)]
    if not inputs:
        raise FileNotFoundError(
            f"No monthly files found in {monthly_dir} for scope={scope!r}"
        )

    return inputs


def month_sequence(first_month: str, last_month: str) -> list[str]:
    first = pd.Period(first_month, freq="M")
    last = pd.Period(last_month, freq="M")
    return [str(period) for period in pd.period_range(first, last, freq="M")]


def validate_month_continuity(
    monthly_inputs: Sequence[MonthlyInput],
    allow_gaps: bool,
) -> None:
    discovered = [item.period for item in monthly_inputs]
    expected = month_sequence(discovered[0], discovered[-1])
    missing = [period for period in expected if period not in discovered]
    if missing and not allow_gaps:
        raise ValueError(
            "Missing monthly files inside the selected range: "
            + ", ".join(missing)
            + ". Add the files or rerun with --allow-gaps only when intentional."
        )


def normalize_meter_no(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return "".join(str(value).strip().upper().split())


def validate_meter_no(value: Any, source: Path, row_number: int) -> str:
    """Normalise one meter identifier and reject blank or unsafe characters."""
    normalized = normalize_meter_no(value)
    if not normalized:
        raise ValueError(f"{source} row {row_number} has a blank meter identifier.")
    if not METER_NO_RE.fullmatch(normalized):
        raise ValueError(
            f"{source} row {row_number} has invalid meter identifier {value!r}; "
            "after normalisation, only A-Z and 0-9 are allowed."
        )
    return normalized


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def merge_duplicate_reference_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
    conflict_fields: Sequence[str],
) -> None:
    """Merge a duplicate reference row only when populated values do not conflict."""
    for field in conflict_fields:
        current = safe_str(existing.get(field))
        incoming = safe_str(candidate.get(field))
        if current and incoming and current != incoming:
            raise ValueError(
                f"{source} contains conflicting duplicate meter {normalized!r}: "
                f"{field} is {current!r} and {incoming!r} (conflict at row {row_number})."
            )
        if not current and incoming:
            existing[field] = incoming

    if not safe_str(existing.get("meterNoRaw")) and safe_str(candidate.get("meterNoRaw")):
        existing["meterNoRaw"] = safe_str(candidate.get("meterNoRaw"))


def customer_identity_pattern(record: Dict[str, Any], normalized: str) -> str:
    """Classify a Customer Details identity using the approved Lesedi source pattern."""
    customer_no = safe_str(record.get("customerNo"))
    account_no = safe_str(record.get("accountNo"))

    if customer_no and account_no and customer_no == account_no:
        if customer_no == normalized:
            return "METER_EQUALS_CUSTOMER_AND_ACCOUNT"
        return "CUSTOMER_EQUALS_ACCOUNT_NOT_METER"

    if not customer_no or not account_no:
        return "INCOMPLETE"

    return "MIXED_OR_OTHER"


def parse_customer_purchase_date(value: Any) -> Optional[pd.Timestamp]:
    """Parse one Customer Details purchase timestamp, returning None for blank or invalid values."""
    text = safe_str(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def replace_customer_record(existing: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    """Replace the stored duplicate candidate while preserving the normalized map key."""
    existing.clear()
    existing.update(candidate)


def merge_customer_duplicate_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
) -> Optional[str]:
    """
    Resolve one duplicate Customer Details row.

    Returns:
    - ``"placeholder"`` when a dominant identity replaces or defeats a meter-number placeholder;
    - ``"active_status"`` when an Active row replaces a Block Purchases row;
    - ``"latest_purchase"`` when two dominant identities resolve by latest valid purchase;
    - ``None`` for identical or complementary duplicates.

    ERF, address, customer name, and other descriptive fields are deliberately excluded
    from duplicate resolution because they are not part of the locked Meter Master schema.
    Competing dominant identities remain a hard error when both purchase dates are
    missing/invalid or when the dates tie.
    """
    conflict_fields = [
        field
        for field in ("customerNo", "accountNo")
        if safe_str(existing.get(field))
        and safe_str(candidate.get(field))
        and safe_str(existing.get(field)) != safe_str(candidate.get(field))
    ]

    if not conflict_fields:
        merge_duplicate_reference_record(
            existing,
            candidate,
            source=source,
            row_number=row_number,
            normalized=normalized,
            conflict_fields=["customerNo", "accountNo"],
        )
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            existing["lastPurchaseDate"] = safe_str(candidate.get("lastPurchaseDate"))
            existing["sourceRow"] = row_number
        return None

    existing_pattern = customer_identity_pattern(existing, normalized)
    candidate_pattern = customer_identity_pattern(candidate, normalized)
    dominant = "CUSTOMER_EQUALS_ACCOUNT_NOT_METER"
    placeholder = "METER_EQUALS_CUSTOMER_AND_ACCOUNT"

    # Resolve the approved source-identity pattern before considering status or
    # descriptive fields. A meter-number placeholder is weaker than a row where
    # CustomerNo = AccountNo and that identity differs from the meter number.
    # Placeholder ERF/address data may be stale and is not part of Meter Master.
    if existing_pattern == placeholder and candidate_pattern == dominant:
        replace_customer_record(existing, candidate)
        return "placeholder"

    if existing_pattern == dominant and candidate_pattern == placeholder:
        return "placeholder"

    existing_status = safe_str(existing.get("accountStatus")).upper()
    candidate_status = safe_str(candidate.get("accountStatus")).upper()
    status_pair = {existing_status, candidate_status}

    if status_pair == {"ACTIVE", "BLOCK PURCHASES"}:
        # AccountStatus is used only as an identity-resolution signal.
        # ERF, address, customer name, and purchase-date equality are not checked here
        # because those descriptive fields are outside the Meter Master contract.
        if existing_status != "ACTIVE":
            replace_customer_record(existing, candidate)
        return "active_status"

    if existing_pattern == dominant and candidate_pattern == dominant:
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        existing_row = safe_str(existing.get("sourceRow")) or "unknown"

        if existing_date is None and candidate_date is None:
            raise ValueError(
                f"{source} contains unresolved duplicate meter {normalized!r}. "
                f"Rows {existing_row} and {row_number} both match the dominant customer/account pattern, "
                "but neither has a valid LastPurchaseDate."
            )
        if existing_date is not None and candidate_date is not None and existing_date == candidate_date:
            raise ValueError(
                f"{source} contains unresolved duplicate meter {normalized!r}. "
                f"Rows {existing_row} and {row_number} both match the dominant customer/account pattern "
                f"and share the same LastPurchaseDate {existing_date.isoformat()!r}."
            )

        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            replace_customer_record(existing, candidate)
        return "latest_purchase"

    existing_row = safe_str(existing.get("sourceRow")) or "unknown"
    raise ValueError(
        f"{source} contains unresolved duplicate meter {normalized!r}. "
        f"Existing row {existing_row}: customerNo={safe_str(existing.get('customerNo'))!r}, "
        f"accountNo={safe_str(existing.get('accountNo'))!r}, pattern={existing_pattern}; "
        f"row {row_number}: customerNo={safe_str(candidate.get('customerNo'))!r}, "
        f"accountNo={safe_str(candidate.get('accountNo'))!r}, pattern={candidate_pattern}. "
        "Only the approved placeholder rule or latest-purchase rule may resolve duplicates."
    )


def make_base_master_record(config: BuildConfig) -> Dict[str, Any]:
    return {
        "masterId": "",
        "lmPcode": config.lm_pcode,
        "meterNoRaw": "",
        "meterNoNormalized": "",
        "meterType": config.meter_type,
        "customerNo": "",
        "accountNo": "",
        "salesId": "",
        "salesProvider": config.provider,
        "astId": "",
        "sources": {"monthly": False, "customer": False, "npr": False},
    }


def choose_best_raw_meter_no(
    current_raw: Optional[str],
    candidate_raw: Optional[str],
    source_name: str,
) -> str:
    current = safe_str(current_raw)
    candidate = safe_str(candidate_raw)

    if not candidate:
        return current
    if not current:
        return candidate
    if source_name == "customer":
        return candidate
    return current


def load_monthly_meter_universe(
    monthly_inputs: Iterable[MonthlyInput],
    config: BuildConfig,
) -> MasterMap:
    master_map: MasterMap = {}

    for monthly_input in monthly_inputs:
        df = pd.read_csv(monthly_input.path, dtype=str).fillna("")
        require_columns(df, ["lmPcode", "meterNo", "ym"], monthly_input.path)

        lm_values = df["lmPcode"].map(safe_str).str.upper()
        wrong_lm = lm_values != config.lm_pcode
        if wrong_lm.any():
            examples = sorted(set(lm_values[wrong_lm].head(10).tolist()))
            raise ValueError(
                f"{monthly_input.path} contains {int(wrong_lm.sum())} row(s) outside "
                f"LM {config.lm_pcode}. Examples: {examples}"
            )

        ym_values = df["ym"].map(safe_str)
        wrong_month = ym_values != monthly_input.period
        if wrong_month.any():
            examples = sorted(set(ym_values[wrong_month].head(10).tolist()))
            raise ValueError(
                f"{monthly_input.path} contains {int(wrong_month.sum())} row(s) whose ym "
                f"does not match filename month {monthly_input.period}. Examples: {examples}"
            )

        for index, row in df.iterrows():
            meter_no_raw = row.get("meterNo")
            normalized = validate_meter_no(
                meter_no_raw, monthly_input.path, int(index) + 2
            )

            rec = master_map.setdefault(normalized, make_base_master_record(config))
            rec["meterNoRaw"] = choose_best_raw_meter_no(
                rec.get("meterNoRaw"), meter_no_raw, "monthly"
            )
            rec["meterNoNormalized"] = normalized
            rec["sources"]["monthly"] = True

    return master_map


def load_customer_details(filepath: Path, stats: BuildStats) -> CustomerMap:
    df = pd.read_csv(filepath, dtype=str).fillna("")
    require_columns(
        df,
        [
            "MeterNumber",
            "CustomerNo",
            "AccountNo",
            "AccountStatus",
            "LastPurchaseDate",
        ],
        filepath,
    )

    customer_map: CustomerMap = {}
    for index, row in df.iterrows():
        row_number = int(index) + 2
        normalized = validate_meter_no(row.get("MeterNumber"), filepath, row_number)
        candidate = {
            "meterNoRaw": safe_str(row.get("MeterNumber")),
            "customerNo": safe_str(row.get("CustomerNo")),
            "accountNo": safe_str(row.get("AccountNo")),
            "lastPurchaseDate": safe_str(row.get("LastPurchaseDate")),
            "accountStatus": safe_str(row.get("AccountStatus")),
            "sourceRow": row_number,
        }

        existing = customer_map.get(normalized)
        if existing is None:
            customer_map[normalized] = candidate
        else:
            resolution = merge_customer_duplicate_record(
                existing,
                candidate,
                source=filepath,
                row_number=row_number,
                normalized=normalized,
            )
            if resolution == "placeholder":
                stats.customer_placeholder_duplicates_resolved += 1
            elif resolution == "active_status":
                stats.customer_active_status_duplicates_resolved += 1
            elif resolution == "latest_purchase":
                stats.customer_latest_purchase_duplicates_resolved += 1

    return customer_map


def merge_npr_duplicate_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
) -> Optional[str]:
    """Resolve one duplicate 90-day report row using governed identity evidence.

    Returns:
    - ``"placeholder"`` when a real customer number replaces or defeats a
      customer number equal to the meter number;
    - ``"latest_purchase"`` when two different real customer numbers resolve
      by the latest valid LastPurchaseDate;
    - ``None`` for identical or complementary duplicates.

    Two different non-placeholder customer numbers remain a hard error when
    both purchase dates are missing/invalid or when the dates tie.
    """
    current_customer = safe_str(existing.get("customerNo"))
    incoming_customer = safe_str(candidate.get("customerNo"))

    if not current_customer or current_customer == incoming_customer:
        merge_duplicate_reference_record(
            existing,
            candidate,
            source=source,
            row_number=row_number,
            normalized=normalized,
            conflict_fields=["customerNo"],
        )
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            existing["lastPurchaseDate"] = safe_str(candidate.get("lastPurchaseDate"))
            existing["sourceRow"] = row_number
        return None

    existing_placeholder = current_customer == normalized
    candidate_placeholder = incoming_customer == normalized

    if existing_placeholder and not candidate_placeholder:
        replace_customer_record(existing, candidate)
        return "placeholder"

    if not existing_placeholder and candidate_placeholder:
        return "placeholder"

    existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
    candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
    existing_row = safe_str(existing.get("sourceRow")) or "unknown"

    if existing_date is None and candidate_date is None:
        raise ValueError(
            f"{source} contains unresolved duplicate meter {normalized!r}. "
            f"Rows {existing_row} and {row_number} contain different non-placeholder "
            "CustomerNo1 values, but neither has a valid LastPurchaseDate."
        )
    if existing_date is not None and candidate_date is not None and existing_date == candidate_date:
        raise ValueError(
            f"{source} contains unresolved duplicate meter {normalized!r}. "
            f"Rows {existing_row} and {row_number} contain different non-placeholder "
            f"CustomerNo1 values and share the same LastPurchaseDate {existing_date.isoformat()!r}."
        )

    if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
        replace_customer_record(existing, candidate)
    return "latest_purchase"


def load_npr(filepath: Path, stats: BuildStats) -> NprMap:
    df = pd.read_csv(filepath, dtype=str).fillna("")
    require_columns(
        df,
        ["MeterIdentifier", "CustomerNo1", "LastPurchaseDate"],
        filepath,
    )

    npr_map: NprMap = {}
    for index, row in df.iterrows():
        row_number = int(index) + 2
        normalized = validate_meter_no(row.get("MeterIdentifier"), filepath, row_number)
        candidate = {
            "meterNoRaw": safe_str(row.get("MeterIdentifier")),
            "customerNo": safe_str(row.get("CustomerNo1")),
            "lastPurchaseDate": safe_str(row.get("LastPurchaseDate")),
            "sourceRow": row_number,
        }

        existing = npr_map.get(normalized)
        if existing is None:
            npr_map[normalized] = candidate
        else:
            resolution = merge_npr_duplicate_record(
                existing,
                candidate,
                source=filepath,
                row_number=row_number,
                normalized=normalized,
            )
            if resolution == "placeholder":
                stats.npr_placeholder_duplicates_resolved += 1
            elif resolution == "latest_purchase":
                stats.npr_latest_purchase_duplicates_resolved += 1

    return npr_map


def merge_customer_details(
    master_map: MasterMap,
    customer_map: CustomerMap,
    config: BuildConfig,
    stats: BuildStats,
) -> MasterMap:
    for normalized, customer_rec in customer_map.items():
        rec = master_map.get(normalized)
        if rec is None:
            rec = make_base_master_record(config)
            rec["meterNoNormalized"] = normalized
            master_map[normalized] = rec
            stats.customer_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"), customer_rec.get("meterNoRaw"), "customer"
        )
        if customer_rec.get("customerNo"):
            rec["customerNo"] = customer_rec["customerNo"]
        if customer_rec.get("accountNo"):
            rec["accountNo"] = customer_rec["accountNo"]
        rec["sources"]["customer"] = True

    return master_map


def merge_npr(
    master_map: MasterMap,
    npr_map: NprMap,
    config: BuildConfig,
    stats: BuildStats,
) -> MasterMap:
    for normalized, npr_rec in npr_map.items():
        rec = master_map.get(normalized)
        if rec is None:
            rec = make_base_master_record(config)
            rec["meterNoNormalized"] = normalized
            master_map[normalized] = rec
            stats.npr_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"), npr_rec.get("meterNoRaw"), "npr"
        )
        if not rec.get("customerNo") and npr_rec.get("customerNo"):
            rec["customerNo"] = npr_rec["customerNo"]
        rec["sources"]["npr"] = True

    return master_map


def finalize_master_records(
    master_map: MasterMap,
    config: BuildConfig,
) -> MasterMap:
    for normalized, rec in master_map.items():
        rec["masterId"] = normalized
        rec["lmPcode"] = config.lm_pcode
        rec["meterNoNormalized"] = normalized
        rec["salesId"] = normalized
        rec["salesProvider"] = config.provider
        rec["meterType"] = safe_str(rec.get("meterType")) or config.meter_type
        rec["astId"] = safe_str(rec.get("astId"))
        rec["meterNoRaw"] = safe_str(rec.get("meterNoRaw")) or normalized
        rec["customerNo"] = safe_str(rec.get("customerNo"))
        rec["accountNo"] = safe_str(rec.get("accountNo"))
    return master_map


def master_rows_to_dataframe(master_map: MasterMap) -> pd.DataFrame:
    rows = [
        {column: safe_str(record.get(column)) for column in MASTER_COLUMNS}
        for record in master_map.values()
    ]
    if not rows:
        return pd.DataFrame(columns=MASTER_COLUMNS)

    return (
        pd.DataFrame(rows)[MASTER_COLUMNS]
        .sort_values(
            by=["meterNoNormalized", "meterNoRaw"],
            ascending=[True, True],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def resolve_output_path(
    explicit_output: Optional[Path],
    output_dir: Path,
    lm_pcode: str,
    scope: str,
    first_month: str,
    last_month: str,
) -> Path:
    if explicit_output:
        return explicit_output
    return output_dir / (
        f"meter_master__{lm_pcode}__{scope}__{first_month}_to_{last_month}.csv"
    )


def validate_staging_dataframe(df: pd.DataFrame, config: BuildConfig) -> None:
    actual_columns = list(df.columns)
    if actual_columns != MASTER_COLUMNS:
        raise ValueError(
            f"Meter Master output columns do not match the approved contract: {actual_columns}"
        )
    if df.empty:
        raise ValueError("Meter Master build produced zero rows.")
    if df["masterId"].duplicated().any():
        examples = df.loc[df["masterId"].duplicated(keep=False), "masterId"].head(10).tolist()
        raise ValueError(f"Meter Master output contains duplicate masterId values: {examples}")
    if not df["masterId"].map(lambda value: bool(METER_NO_RE.fullmatch(safe_str(value)))).all():
        raise ValueError("Meter Master output contains invalid masterId characters.")
    if not (df["masterId"] == df["meterNoNormalized"]).all():
        raise ValueError("Meter Master identity failure: masterId must equal meterNoNormalized.")
    if not (df["salesId"] == df["meterNoNormalized"]).all():
        raise ValueError("Meter Master identity failure: salesId must equal meterNoNormalized.")
    if not (df["lmPcode"] == config.lm_pcode).all():
        raise ValueError("Meter Master output contains an unexpected lmPcode.")
    if not (df["salesProvider"] == config.provider).all():
        raise ValueError("Meter Master output contains an unexpected salesProvider.")


def write_csv(df: pd.DataFrame, output_path: Path) -> None:
    """Write through a temporary file so a failed write cannot leave partial output."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        df.to_csv(temp_path, index=False, encoding="utf-8")
        temp_path.replace(output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    args = parse_args()
    validate_month(args.from_month, "--from-month")
    validate_month(args.to_month, "--to-month")
    if args.from_month and args.to_month and args.from_month > args.to_month:
        raise ValueError("--from-month cannot be later than --to-month")

    config = BuildConfig(
        lm_pcode=safe_str(args.lm_pcode).upper(),
        provider=safe_str(args.provider).lower(),
        meter_type=safe_str(args.meter_type).lower(),
    )
    if not config.lm_pcode:
        raise ValueError("--lm-pcode may not be blank.")
    if not re.fullmatch(r"[A-Z0-9_-]+", config.lm_pcode):
        raise ValueError("--lm-pcode may contain only A-Z, 0-9, underscore, and hyphen.")
    if not config.provider:
        raise ValueError("--provider may not be blank.")
    if not config.meter_type:
        raise ValueError("--meter-type may not be blank.")

    monthly_dir = resolve_project_path(args.monthly_dir)
    customer_details_path = resolve_project_path(args.customer_details)
    npr_path = resolve_project_path(args.npr)
    output_dir = resolve_project_path(args.output_dir)
    explicit_output = (
        resolve_project_path(args.output) if args.output is not None else None
    )

    require_file(customer_details_path)
    require_file(npr_path)

    monthly_inputs = discover_monthly_inputs(
        monthly_dir,
        args.scope,
        args.from_month,
        args.to_month,
    )
    validate_month_continuity(monthly_inputs, args.allow_gaps)
    first_month = monthly_inputs[0].period
    last_month = monthly_inputs[-1].period

    output_path = resolve_output_path(
        explicit_output,
        output_dir,
        config.lm_pcode,
        args.scope,
        first_month,
        last_month,
    )
    stats = BuildStats()

    print("=== METER MASTER BUILD ===")
    print(f"Repository root: {PROJECT_ROOT}")
    print(f"LM/workbase: {config.lm_pcode}")
    print(f"Provider: {config.provider}")
    print(f"Customer details: {customer_details_path}")
    print(f"90-day report: {npr_path}")
    print(f"Months discovered: {len(monthly_inputs)} ({first_month} to {last_month})")
    for item in monthly_inputs:
        print(f"  - {item.period}: {item.path}")

    master_map = load_monthly_meter_universe(monthly_inputs, config)
    stats.monthly_backed_meters = len(master_map)

    customer_map = load_customer_details(customer_details_path, stats)
    npr_map = load_npr(npr_path, stats)
    merge_customer_details(master_map, customer_map, config, stats)
    merge_npr(master_map, npr_map, config, stats)
    finalize_master_records(master_map, config)

    df = master_rows_to_dataframe(master_map)
    validate_staging_dataframe(df, config)
    write_csv(df, output_path)
    stats.total_master_rows = len(df)

    print("=== BUILD SUMMARY ===")
    print(f"Monthly-backed meters: {stats.monthly_backed_meters}")
    print(f"Customer-only seeded meters: {stats.customer_only_seeded_meters}")
    print(f"NPR-only seeded meters: {stats.npr_only_seeded_meters}")
    print(
        "Customer placeholder duplicates resolved: "
        f"{stats.customer_placeholder_duplicates_resolved}"
    )
    print(
        "Customer Active-status duplicates resolved: "
        f"{stats.customer_active_status_duplicates_resolved}"
    )
    print(
        "Customer latest-purchase duplicates resolved: "
        f"{stats.customer_latest_purchase_duplicates_resolved}"
    )
    print(
        "NPR placeholder duplicates resolved: "
        f"{stats.npr_placeholder_duplicates_resolved}"
    )
    print(
        "NPR latest-purchase duplicates resolved: "
        f"{stats.npr_latest_purchase_duplicates_resolved}"
    )
    print(f"Total meter master rows: {stats.total_master_rows}")
    print("Validation: PASS")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
