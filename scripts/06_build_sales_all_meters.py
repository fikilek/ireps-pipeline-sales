"""
06_build_sales_all_meters.py

Build sales_all_meters.csv from:
- MASTER.csv
- monthly meter-level CSVs
"""

from pathlib import Path
from typing import Dict, Any
import pandas as pd
from datetime import datetime, UTC


# =========================================================
# CONFIG
# =========================================================

MASTER_FILEPATH = Path("output/meter_master/meter_master__FULL__2025-09_to_2026-02.csv")

MONTHLY_FILEPATHS = {
    "2025-09": Path("output/monthly/monthly__FULL__2025-09__from_atomic.csv"),
    "2025-10": Path("output/monthly/monthly__FULL__2025-10__from_atomic.csv"),
    "2025-11": Path("output/monthly/monthly__FULL__2025-11__from_atomic.csv"),
    "2025-12": Path("output/monthly/monthly__FULL__2025-12__from_atomic.csv"),
    "2026-01": Path("output/monthly/monthly__FULL__2026-01__from_atomic.csv"),
    "2026-02": Path("output/monthly/monthly__FULL__2026-02__from_atomic.csv"),
}

OUTPUT_PATH = Path("output/sales_all_meters/sales_all_meters__FULL__2025-09_to_2026-02.csv")


# =========================================================
# HELPERS
# =========================================================

def require_file(filepath: Path) -> None:
    if not filepath.exists():
        raise FileNotFoundError(f"Required file not found: {filepath}")


def normalize_meter_no(v):
    if v is None:
        return ""
    s = str(v).strip().replace(" ", "").upper()
    return "" if s.lower() == "nan" else s


def safe_str(v):
    if v is None:
        return ""
    if pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def safe_int(v):
    try:
        if v is None or pd.isna(v):
            return 0
        s = str(v).strip()
        if not s or s.lower() == "nan":
            return 0
        return int(float(s))
    except Exception:
        return 0


def parse_iso(v):
    try:
        s = safe_str(v)
        if not s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def days_since(date_obj):
    if not date_obj:
        return None
    return (datetime.now(UTC) - date_obj).days


# =========================================================
# LOADERS
# =========================================================

def load_master_csv():
    df = pd.read_csv(MASTER_FILEPATH, dtype=str)

    records = {}

    for _, row in df.iterrows():
        key = normalize_meter_no(row["meterNoNormalized"])
        if not key:
            continue

        records[key] = {
            "masterId": safe_str(row.get("masterId")),
            "meterNo": safe_str(row.get("meterNoRaw")),
            "meterNoNormalized": key,
            "provider": "conlog",
            "customerNo": safe_str(row.get("customerNo")),
            "accountNo": safe_str(row.get("accountNo")),
            "astId": safe_str(row.get("astId")),
            "months": {},
            "total": 0,
            "lastPurchase": None,
        }

    return records


def load_monthly_files(records):
    for ym, path in MONTHLY_FILEPATHS.items():
        df = pd.read_csv(path, dtype=str)

        for _, row in df.iterrows():
            meter = normalize_meter_no(row.get("meterNo"))
            if not meter:
                continue

            amount = safe_int(row.get("amountTotalC", 0))
            purchase_dt = parse_iso(row.get("lastPurchaseAtISO"))

            rec = records.get(meter)
            if not rec:
                continue  # skip unknown meters not present in MASTER

            rec["months"][ym] = amount
            rec["total"] += amount

            if purchase_dt:
                if not rec["lastPurchase"] or purchase_dt > rec["lastPurchase"]:
                    rec["lastPurchase"] = purchase_dt

    return records


# =========================================================
# FINALIZE
# =========================================================

def finalize_records(records):
    for rec in records.values():
        rec["lastPurchaseAtISO"] = (
            rec["lastPurchase"].isoformat().replace("+00:00", "Z")
            if rec["lastPurchase"]
            else ""
        )

        rec["daysSinceLastPurchase"] = (
            days_since(rec["lastPurchase"]) if rec["lastPurchase"] else None
        )

        rec["visibility"] = "VISIBLE" if safe_str(rec.get("astId")) else "INVISIBLE"

    return records


# =========================================================
# OUTPUT
# =========================================================

def build_row(rec):
    row = {
        "masterId": rec["masterId"],
        "visibility": rec["visibility"],
        "meterNo": rec["meterNo"],
        "meterNoNormalized": rec["meterNoNormalized"],
        "provider": rec["provider"],
        "customerNo": rec["customerNo"],
        "accountNo": rec["accountNo"],
        "totalAmountC": rec["total"],
        "lastPurchaseAtISO": rec["lastPurchaseAtISO"],
        "daysSinceLastPurchase": rec["daysSinceLastPurchase"],
    }

    for ym in MONTHLY_FILEPATHS.keys():
        row[f"amount_{ym.replace('-', '_')}_C"] = rec["months"].get(ym, 0)

    return row


def to_dataframe(records):
    rows = [build_row(r) for r in records.values()]
    df = pd.DataFrame(rows)

    if df.empty:
        return df

    column_order = [
        "masterId",
        "visibility",
        "meterNo",
        "meterNoNormalized",
        "provider",
        "customerNo",
        "accountNo",
        "totalAmountC",
        "lastPurchaseAtISO",
        "daysSinceLastPurchase",
        "amount_2025_09_C",
        "amount_2025_10_C",
        "amount_2025_11_C",
        "amount_2025_12_C",
        "amount_2026_01_C",
        "amount_2026_02_C",
    ]

    for col in column_order:
        if col not in df.columns:
            df[col] = ""

    return df[column_order].sort_values(
        by=["meterNoNormalized", "meterNo"],
        ascending=[True, True],
        kind="stable",
    ).reset_index(drop=True)


def write_csv(df):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)


# =========================================================
# MAIN
# =========================================================

def main():
    require_file(MASTER_FILEPATH)
    for path in MONTHLY_FILEPATHS.values():
        require_file(path)

    print("Loading MASTER...")
    records = load_master_csv()

    print("Merging monthly...")
    records = load_monthly_files(records)

    print("Finalizing...")
    records = finalize_records(records)

    df = to_dataframe(records)
    write_csv(df)

    print("Done.")
    print(f"Rows: {len(df)}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()