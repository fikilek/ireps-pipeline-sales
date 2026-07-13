"""
05_build_meter_master_v2.py

Purpose
-------
Build MASTER.csv from:
- output/monthly/monthly__FULL__*.csv
- Customer_Details.csv
- 90_Days_No_Purchase_Report.csv

This script does NOT upload to Firestore.
It only produces the flat CSV staging file for later upload.

Output
------
MASTER.csv with columns:
    masterId
    lmPcode
    meterNoRaw
    meterNoNormalized
    meterType
    customerNo
    accountNo
    salesId
    salesProvider
    astId
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd


# =========================================================
# CONFIG
# =========================================================

PROVIDER = "conlog"
DEFAULT_METER_TYPE = "electricity"
LM_PCODE = "ZA7423"

MONTHLY_FILEPATHS = [
    Path("output/monthly/monthly__FULL__2025-09__from_atomic.csv"),
    Path("output/monthly/monthly__FULL__2025-10__from_atomic.csv"),
    Path("output/monthly/monthly__FULL__2025-11__from_atomic.csv"),
    Path("output/monthly/monthly__FULL__2025-12__from_atomic.csv"),
    Path("output/monthly/monthly__FULL__2026-01__from_atomic.csv"),
    Path("output/monthly/monthly__FULL__2026-02__from_atomic.csv"),
]

CUSTOMER_DETAILS_FILEPATH = Path("input/Customer_Details.csv")
NPR_FILEPATH = Path("input/90_Days_No_Purchase_Report.csv")

OUTPUT_DIR = Path("output/meter_master")
OUTPUT_FILEPATH = OUTPUT_DIR / "meter_master__FULL__2025-09_to_2026-02.csv"

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


# =========================================================
# TYPES
# =========================================================

MeterKey = str
MasterMap = Dict[MeterKey, Dict[str, Any]]
CustomerMap = Dict[MeterKey, Dict[str, Any]]
NprMap = Dict[MeterKey, Dict[str, Any]]


@dataclass
class BuildStats:
    monthly_backed_meters: int = 0
    customer_only_seeded_meters: int = 0
    npr_only_seeded_meters: int = 0
    total_master_rows: int = 0


# =========================================================
# HELPERS
# =========================================================

def require_file(filepath: Path) -> None:
    """
    Fail early with a clean message if a required input file is missing.
    """
    if not filepath.exists():
        raise FileNotFoundError(f"Required file not found: {filepath}")


def normalize_meter_no(value: Any) -> str:
    """
    Create canonical meter key used across monthly, customer, and NPR inputs.

    Rules:
    - cast to string
    - trim
    - remove all whitespace
    - uppercase
    - preserve leading zeroes
    - do not strip letters
    """
    if value is None:
        return ""

    s = str(value).strip().upper()
    s = "".join(s.split())
    return s


def safe_str(value: Any) -> str:
    """
    Safely convert values to trimmed string or empty string.
    """
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def make_base_master_record() -> Dict[str, Any]:
    """
    Create empty in-memory master record for one meter.
    """
    return {
        "masterId": "",
        "lmPcode": LM_PCODE,
        "meterNoRaw": "",
        "meterNoNormalized": "",
        "meterType": DEFAULT_METER_TYPE,
        "customerNo": "",
        "accountNo": "",
        "salesId": "",
        "salesProvider": PROVIDER,
        "astId": "",
        "sources": {
            "monthly": False,
            "customer": False,
            "npr": False,
        },
    }


def choose_best_raw_meter_no(
    current_raw: Optional[str],
    candidate_raw: Optional[str],
    source_name: str,
) -> str:
    """
    Choose best raw/display meter number across sources.

    Suggested precedence:
        1. customer
        2. monthly
        3. npr
    """
    current_raw = safe_str(current_raw)
    candidate_raw = safe_str(candidate_raw)

    if not candidate_raw:
        return current_raw

    if not current_raw:
        return candidate_raw

    if source_name == "customer":
        return candidate_raw

    if source_name == "monthly":
        return current_raw

    if source_name == "npr":
        return current_raw

    return current_raw


# =========================================================
# LOADERS
# =========================================================

def load_monthly_meter_universe(monthly_filepaths: Iterable[Path]) -> MasterMap:
    """
    Read prepared monthly meter-level CSVs and create initial master map
    from sales-backed meters.

    Uses:
    - meterNo

    Important:
    - this script uses monthly files only to identify sales-side meter existence
    - month totals are NOT handled here
    """
    master_map: MasterMap = {}

    for filepath in monthly_filepaths:
        df = pd.read_csv(filepath, dtype=str)

        for _, row in df.iterrows():
            meter_no_raw = row.get("meterNo")
            meter_no_normalized = normalize_meter_no(meter_no_raw)

            if not meter_no_normalized:
                continue

            rec = master_map.setdefault(
                meter_no_normalized,
                make_base_master_record(),
            )

            rec["meterNoRaw"] = choose_best_raw_meter_no(
                rec.get("meterNoRaw"),
                meter_no_raw,
                source_name="monthly",
            )
            rec["meterNoNormalized"] = meter_no_normalized
            rec["sources"]["monthly"] = True

    return master_map


def load_customer_details(filepath: Path) -> CustomerMap:
    """
    Read Customer_Details.csv and return lookup keyed by normalized meter number.

    Uses:
    - MeterNumber
    - CustomerNo
    - AccountNo
    """
    customer_map: CustomerMap = {}

    df = pd.read_csv(filepath, dtype=str)

    for _, row in df.iterrows():
        meter_no_raw = row.get("MeterNumber")
        meter_no_normalized = normalize_meter_no(meter_no_raw)

        if not meter_no_normalized:
            continue

        customer_map[meter_no_normalized] = {
            "meterNoRaw": safe_str(meter_no_raw),
            "customerNo": safe_str(row.get("CustomerNo")),
            "accountNo": safe_str(row.get("AccountNo")),
        }

    return customer_map


def load_npr(filepath: Path) -> NprMap:
    """
    Read 90_Days_No_Purchase_Report.csv and return lookup keyed by normalized meter number.

    Uses:
    - MeterIdentifier
    - CustomerNo1
    """
    npr_map: NprMap = {}

    df = pd.read_csv(filepath, dtype=str)

    for _, row in df.iterrows():
        meter_no_raw = row.get("MeterIdentifier")
        meter_no_normalized = normalize_meter_no(meter_no_raw)

        if not meter_no_normalized:
            continue

        npr_map[meter_no_normalized] = {
            "meterNoRaw": safe_str(meter_no_raw),
            "customerNo": safe_str(row.get("CustomerNo1")),
        }

    return npr_map


# =========================================================
# MERGERS
# =========================================================

def merge_customer_details(
    master_map: MasterMap,
    customer_map: CustomerMap,
    stats: Optional[BuildStats] = None,
) -> MasterMap:
    """
    Enrich existing sales-backed meters with customer/account info,
    or seed customer-only meters if not already present.
    """
    for meter_no_normalized, customer_rec in customer_map.items():
        rec = master_map.get(meter_no_normalized)

        is_new = rec is None
        if is_new:
            rec = make_base_master_record()
            rec["meterNoNormalized"] = meter_no_normalized
            master_map[meter_no_normalized] = rec
            if stats is not None:
                stats.customer_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"),
            customer_rec.get("meterNoRaw"),
            source_name="customer",
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
    stats: Optional[BuildStats] = None,
) -> MasterMap:
    """
    Enrich existing meters with NPR fallback customer number,
    or seed NPR-only meters if not already present.
    """
    for meter_no_normalized, npr_rec in npr_map.items():
        rec = master_map.get(meter_no_normalized)

        is_new = rec is None
        if is_new:
            rec = make_base_master_record()
            rec["meterNoNormalized"] = meter_no_normalized
            master_map[meter_no_normalized] = rec
            if stats is not None:
                stats.npr_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"),
            npr_rec.get("meterNoRaw"),
            source_name="npr",
        )

        if not rec.get("customerNo") and npr_rec.get("customerNo"):
            rec["customerNo"] = npr_rec["customerNo"]

        rec["sources"]["npr"] = True

    return master_map


# =========================================================
# FINALIZE
# =========================================================

def finalize_master_records(master_map: MasterMap) -> MasterMap:
    """
    Apply final defaults and cleanup before CSV output.

    Work:
    - ensure masterId = meterNoNormalized
    - ensure lmPcode = ZA7423
    - ensure salesId = meterNoNormalized
    - ensure salesProvider = "conlog"
    - ensure meterType = "electricity"
    - ensure astId = "" for now
    - fallback meterNoRaw to normalized if empty
    """
    for meter_no_normalized, rec in master_map.items():
        rec["masterId"] = meter_no_normalized
        rec["lmPcode"] = LM_PCODE
        rec["meterNoNormalized"] = meter_no_normalized
        rec["salesId"] = meter_no_normalized
        rec["salesProvider"] = PROVIDER
        rec["meterType"] = rec.get("meterType") or DEFAULT_METER_TYPE
        rec["astId"] = safe_str(rec.get("astId"))

        if not safe_str(rec.get("meterNoRaw")):
            rec["meterNoRaw"] = meter_no_normalized

        rec["customerNo"] = safe_str(rec.get("customerNo"))
        rec["accountNo"] = safe_str(rec.get("accountNo"))

    return master_map


# =========================================================
# OUTPUT BUILDERS
# =========================================================

def build_master_row(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten one in-memory master record into CSV row shape.
    """
    return {
        "masterId": safe_str(record.get("masterId")),
        "lmPcode": safe_str(record.get("lmPcode")),
        "meterNoRaw": safe_str(record.get("meterNoRaw")),
        "meterNoNormalized": safe_str(record.get("meterNoNormalized")),
        "meterType": safe_str(record.get("meterType")),
        "customerNo": safe_str(record.get("customerNo")),
        "accountNo": safe_str(record.get("accountNo")),
        "salesId": safe_str(record.get("salesId")),
        "salesProvider": safe_str(record.get("salesProvider")),
        "astId": safe_str(record.get("astId")),
    }


def master_rows_to_dataframe(master_map: MasterMap) -> pd.DataFrame:
    """
    Convert finalized master_map into pandas DataFrame in exact output column order.
    """
    rows = [build_master_row(rec) for rec in master_map.values()]
    df = pd.DataFrame(rows)

    if df.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)

    df = df.sort_values(
        by=["meterNoNormalized", "meterNoRaw"],
        ascending=[True, True],
        kind="stable",
    ).reset_index(drop=True)

    return df[MASTER_COLUMNS]


def write_master_csv(df: pd.DataFrame, output_path: Path) -> None:
    """
    Write final MASTER.csv output to disk.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8")


# =========================================================
# SUMMARY
# =========================================================

def print_summary(master_map: MasterMap, stats: BuildStats) -> None:
    """
    Print useful pipeline summary to terminal.
    """
    stats.total_master_rows = len(master_map)

    print("=== MASTER BUILD SUMMARY ===")
    print(f"Monthly-backed meters: {stats.monthly_backed_meters}")
    print(f"Customer-only seeded meters: {stats.customer_only_seeded_meters}")
    print(f"NPR-only seeded meters: {stats.npr_only_seeded_meters}")
    print(f"Total MASTER rows: {stats.total_master_rows}")
    print(f"Output: {OUTPUT_FILEPATH}")


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    """
    Orchestrate full MASTER.csv build.
    """
    for fp in MONTHLY_FILEPATHS:
        require_file(fp)
    require_file(CUSTOMER_DETAILS_FILEPATH)
    require_file(NPR_FILEPATH)

    stats = BuildStats()

    master_map = load_monthly_meter_universe(MONTHLY_FILEPATHS)
    stats.monthly_backed_meters = len(master_map)

    customer_map = load_customer_details(CUSTOMER_DETAILS_FILEPATH)
    npr_map = load_npr(NPR_FILEPATH)

    master_map = merge_customer_details(master_map, customer_map, stats=stats)
    master_map = merge_npr(master_map, npr_map, stats=stats)

    master_map = finalize_master_records(master_map)

    df = master_rows_to_dataframe(master_map)
    write_master_csv(df, OUTPUT_FILEPATH)

    print_summary(master_map, stats)


if __name__ == "__main__":
    main()