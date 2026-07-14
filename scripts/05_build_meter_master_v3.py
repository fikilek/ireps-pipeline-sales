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
    meter_master__FULL__<first-month>_to_<last-month>.csv
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

import pandas as pd


MONTHLY_FILENAME_RE = re.compile(
    r"^monthly__(?P<scope>[A-Za-z0-9_-]+)__(?P<period>\d{4}-\d{2})__from_atomic\.csv$"
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


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


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
        df = pd.read_csv(monthly_input.path, dtype=str)
        require_columns(df, ["meterNo"], monthly_input.path)

        for meter_no_raw in df["meterNo"]:
            normalized = normalize_meter_no(meter_no_raw)
            if not normalized:
                continue

            rec = master_map.setdefault(normalized, make_base_master_record(config))
            rec["meterNoRaw"] = choose_best_raw_meter_no(
                rec.get("meterNoRaw"), meter_no_raw, "monthly"
            )
            rec["meterNoNormalized"] = normalized
            rec["sources"]["monthly"] = True

    return master_map


def load_customer_details(filepath: Path) -> CustomerMap:
    df = pd.read_csv(filepath, dtype=str)
    require_columns(df, ["MeterNumber", "CustomerNo", "AccountNo"], filepath)

    customer_map: CustomerMap = {}
    for _, row in df.iterrows():
        normalized = normalize_meter_no(row.get("MeterNumber"))
        if not normalized:
            continue
        customer_map[normalized] = {
            "meterNoRaw": safe_str(row.get("MeterNumber")),
            "customerNo": safe_str(row.get("CustomerNo")),
            "accountNo": safe_str(row.get("AccountNo")),
        }
    return customer_map


def load_npr(filepath: Path) -> NprMap:
    df = pd.read_csv(filepath, dtype=str)
    require_columns(df, ["MeterIdentifier", "CustomerNo1"], filepath)

    npr_map: NprMap = {}
    for _, row in df.iterrows():
        normalized = normalize_meter_no(row.get("MeterIdentifier"))
        if not normalized:
            continue
        npr_map[normalized] = {
            "meterNoRaw": safe_str(row.get("MeterIdentifier")),
            "customerNo": safe_str(row.get("CustomerNo1")),
        }
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


def write_csv(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8")


def main() -> None:
    args = parse_args()
    validate_month(args.from_month, "--from-month")
    validate_month(args.to_month, "--to-month")
    if args.from_month and args.to_month and args.from_month > args.to_month:
        raise ValueError("--from-month cannot be later than --to-month")

    require_file(args.customer_details)
    require_file(args.npr)

    monthly_inputs = discover_monthly_inputs(
        args.monthly_dir,
        args.scope,
        args.from_month,
        args.to_month,
    )
    validate_month_continuity(monthly_inputs, args.allow_gaps)
    first_month = monthly_inputs[0].period
    last_month = monthly_inputs[-1].period

    config = BuildConfig(
        lm_pcode=args.lm_pcode.strip().upper(),
        provider=args.provider.strip().lower(),
        meter_type=args.meter_type.strip().lower(),
    )
    output_path = resolve_output_path(
        args.output,
        args.output_dir,
        config.lm_pcode,
        args.scope,
        first_month,
        last_month,
    )
    stats = BuildStats()

    print("=== METER MASTER BUILD ===")
    print(f"LM/workbase: {config.lm_pcode}")
    print(f"Provider: {config.provider}")
    print(f"Months discovered: {len(monthly_inputs)} ({first_month} to {last_month})")
    for item in monthly_inputs:
        print(f"  - {item.period}: {item.path}")

    master_map = load_monthly_meter_universe(monthly_inputs, config)
    stats.monthly_backed_meters = len(master_map)

    customer_map = load_customer_details(args.customer_details)
    npr_map = load_npr(args.npr)
    merge_customer_details(master_map, customer_map, config, stats)
    merge_npr(master_map, npr_map, config, stats)
    finalize_master_records(master_map, config)

    df = master_rows_to_dataframe(master_map)
    write_csv(df, output_path)
    stats.total_master_rows = len(df)

    print("=== BUILD SUMMARY ===")
    print(f"Monthly-backed meters: {stats.monthly_backed_meters}")
    print(f"Customer-only seeded meters: {stats.customer_only_seeded_meters}")
    print(f"NPR-only seeded meters: {stats.npr_only_seeded_meters}")
    print(f"Total meter master rows: {stats.total_master_rows}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
