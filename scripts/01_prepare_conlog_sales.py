# 01_prepare_conlog_sales.py
import re
import hashlib
import datetime as dt
from pathlib import Path

import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

METER_LEN = 11  # must match uploaders + monthly aggregation expectations

# -----------------------------
# FULL RUN (NO LIMIT)
# -----------------------------
DRYRUN_LIMIT = None  # None = FULL PRODUCTION RUN

# Optional: set this to a meter to trace, or set to None to disable
DEBUG_METER = "04242618025"  # or None


# -----------------------------
# Helpers
# -----------------------------
def clean_header(h: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(h).replace("\ufeff", "").replace("\n", " ").replace("\r", " ").strip(),
    )


def read_csv_robust(path: Path) -> pd.DataFrame:
    last = None
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            df.columns = [clean_header(c) for c in df.columns]
            return df
        except Exception as e:
            last = e
    raise last


def parse_ddmmyyyy_hhmm(s: str):
    if s is None or pd.isna(s):
        return pd.NaT
    s = str(s).strip()
    if not s:
        return pd.NaT

    for fmt in ["%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"]:
        try:
            return pd.to_datetime(s, format=fmt, dayfirst=True)
        except Exception:
            pass

    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def money_to_cents(x) -> int:
    if x is None or pd.isna(x):
        return 0

    s = str(x).strip()
    if not s:
        return 0

    s = s.replace(" ", "").replace("\u00a0", "")
    s = re.sub(r"[Rr]", "", s)

    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")

    try:
        val = float(s)
    except Exception:
        return 0

    return int(round(val * 100))


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def normalize_meter_no(raw: str) -> str:
    if raw is None or pd.isna(raw):
        return ""

    s = str(raw).strip()

    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]

    if s.isdigit():
        s = s.zfill(METER_LEN)

    return s


CANON = {
    "lmPcode": ["lmPcode", "LMPCODE", "LmPcode"],
    "txAt": ["txAt", "TransactionDateTime_2", "Transaction Date Time"],
    "meterNo": ["meterNo", "Meter Mo", "Meter No"],
    "amountTotal": ["amountTotalC", "Amount Total", "Total", "Cash Tendered"],
    "cost": ["costC", "Cost (excl Vat)", "Total (excl vat)", "Cost Of Units"],
    "vat": ["vatC", "Vat", "VAT"],
}


def find_col(df_cols, candidates):
    lower = {c.lower(): c for c in df_cols}
    for cand in candidates:
        k = cand.lower()
        if k in lower:
            return lower[k]
    return None


# -----------------------------
# Canonicalize to atomic rows
# -----------------------------
def canonicalize(df: pd.DataFrame, sourceFileId: str) -> pd.DataFrame:
    cols = list(df.columns)

    col_lm = find_col(cols, CANON["lmPcode"])
    col_tx = find_col(cols, CANON["txAt"])
    col_meter = find_col(cols, CANON["meterNo"])
    col_amt = find_col(cols, CANON["amountTotal"])
    col_cost = find_col(cols, CANON["cost"])
    col_vat = find_col(cols, CANON["vat"])

    missing = [
        name
        for name, col in [
            ("lmPcode", col_lm),
            ("txAt", col_tx),
            ("meterNo", col_meter),
            ("amountTotal", col_amt),
            ("cost", col_cost),
            ("vat", col_vat),
        ]
        if col is None
    ]

    if missing:
        raise ValueError(f"{sourceFileId} missing columns: {missing}. Found: {cols}")

    out = pd.DataFrame()
    out["lmPcode"] = df[col_lm].astype(str).str.strip()
    out["meterNo"] = df[col_meter].apply(normalize_meter_no)
    out["txAt_dt"] = df[col_tx].apply(parse_ddmmyyyy_hhmm)

    out = out[(out["lmPcode"] != "") & (out["meterNo"] != "") & (~out["txAt_dt"].isna())].copy()

    out["ym"] = out["txAt_dt"].dt.strftime("%Y-%m")
    out["y"] = out["txAt_dt"].dt.year.astype("Int64")
    out["m"] = out["txAt_dt"].dt.month.astype("Int64")

    out["amountTotalC"] = df.loc[out.index, col_amt].apply(money_to_cents).astype("int64")
    out["costC"] = df.loc[out.index, col_cost].apply(money_to_cents).astype("int64")
    out["vatC"] = df.loc[out.index, col_vat].apply(money_to_cents).astype("int64")

    out["currency"] = "ZAR"
    out["sourceFileId"] = sourceFileId
    out["sourceRow"] = np.arange(1, len(out) + 1, dtype=np.int64)

    out["txAtISO"] = out["txAt_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    out["txAtMs"] = (out["txAt_dt"].astype("int64") // 1_000_000).astype("int64")

    out = out.drop(columns=["txAt_dt"])

    # deterministic atomicId
    base_key = (
        out["lmPcode"].astype(str)
        + "|"
        + out["meterNo"].astype(str)
        + "|"
        + pd.to_datetime(out["txAtISO"]).dt.strftime("%d/%m/%Y %H:%M")
        + "|"
        + out["amountTotalC"].astype(str)
        + "|"
        + out["costC"].astype(str)
        + "|"
        + out["vatC"].astype(str)
    )

    out["baseHash"] = base_key.apply(sha1_hex)
    out["dupIndex"] = out.groupby(["sourceFileId", "baseHash"]).cumcount() + 1
    out["atomicId"] = np.where(
        out["dupIndex"] == 1,
        out["baseHash"],
        out["baseHash"] + "__" + out["dupIndex"].astype(str),
    )

    return out.drop(columns=["baseHash", "dupIndex"])


def ensure_dirs():
    for p in [
        OUTPUT_DIR / "atomic",
        OUTPUT_DIR / "monthly",
        OUTPUT_DIR / "monthly_lm",
        OUTPUT_DIR / "monthly_lm_groups",
        OUTPUT_DIR / "logs",
    ]:
        p.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Main
# -----------------------------
def main(limit_per_file=None):
    ensure_dirs()
    input_files = sorted(INPUT_DIR.glob("conlog_prepaid_sales__*.csv"))
    if not input_files:
        raise SystemExit(f"No files found in {INPUT_DIR}")

    ingested_at_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    ingested_at_ms = int(dt.datetime.utcnow().timestamp() * 1000)

    prep_rows = []

    print("[MODE] FULL RUN: exporting ALL rows per file")

    for f in input_files:
        df = read_csv_robust(f)
        canon = canonicalize(df, sourceFileId=f.name)

        canon["ingestedAtISO"] = ingested_at_iso
        canon["ingestedAtMs"] = ingested_at_ms

        out_atomic = OUTPUT_DIR / "atomic" / f"atomic__{f.stem}__{len(canon)}.csv"
        canon.to_csv(out_atomic, index=False)

        prep_rows.append(
            {
                "sourceFileId": f.name,
                "rows_out": int(len(canon)),
                "ym_min": canon["ym"].min(),
                "ym_max": canon["ym"].max(),
                "uniqueMeters": int(canon["meterNo"].nunique()),
                "amountTotalR": float(canon["amountTotalC"].sum() / 100.0),
            }
        )

        print(f"[OK] Prepared {out_atomic.name}")

    summary_df = pd.DataFrame(prep_rows)
    summary_csv = OUTPUT_DIR / "logs" / "prep_summary.csv"
    summary_df.to_csv(summary_csv, index=False)

    print(f"\n[SUMMARY] {summary_csv}")
    print(summary_df)


if __name__ == "__main__":
    main(limit_per_file=DRYRUN_LIMIT)