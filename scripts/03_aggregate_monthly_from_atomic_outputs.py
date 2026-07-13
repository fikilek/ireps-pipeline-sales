# 03_aggregate_monthly_from_atomic_outputs.py
from pathlib import Path
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
ATOMIC_DIR = BASE_DIR / "output" / "atomic"
MONTHLY_DIR = BASE_DIR / "output" / "monthly"
MONTHLY_LM_DIR = BASE_DIR / "output" / "monthly_lm"
MONTHLY_LM_GROUPS_DIR = BASE_DIR / "output" / "monthly_lm_groups"

METER_LEN = 11  # must match stage 01 + uploaders

# -----------------------------
# MODE TAG (prevents confusion)
# -----------------------------
RUN_TAG = "FULL"  # set to "DRYRUN500" etc if you ever do limited runs


def ensure_dirs():
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    MONTHLY_LM_DIR.mkdir(parents=True, exist_ok=True)
    MONTHLY_LM_GROUPS_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Defensive meter normalization
# -----------------------------
def normalize_meter_no(raw: str) -> str:
    s = str(raw).strip()

    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]

    if s.isdigit():
        s = s.zfill(METER_LEN)

    return s


def read_csv_robust(path: Path) -> pd.DataFrame:
    last = None
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            return pd.read_csv(path, dtype=str, encoding=enc)
        except Exception as e:
            last = e
    raise last


def read_all_atomic() -> pd.DataFrame:
    files = sorted(ATOMIC_DIR.glob("atomic__*.csv"))
    if not files:
        raise SystemExit(f"No atomic__*.csv found in {ATOMIC_DIR}")

    parts = []
    for f in files:
        df = read_csv_robust(f)

        required = [
            "atomicId",
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
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"{f.name} missing columns: {missing}")

        # numeric columns
        num_cols = [
            "txAtMs",
            "amountTotalC",
            "costC",
            "vatC",
            "sourceRow",
            "ingestedAtMs",
            "y",
            "m",
        ]
        for c in num_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

        # trim strings
        str_cols = [
            "atomicId",
            "lmPcode",
            "meterNo",
            "ym",
            "txAtISO",
            "currency",
            "sourceFileId",
            "ingestedAtISO",
        ]
        for c in str_cols:
            df[c] = df[c].astype(str).str.strip()

        # normalize meter
        df["meterNo"] = df["meterNo"].apply(normalize_meter_no)

        # drop bad
        df = df[
            (df["atomicId"] != "")
            & (df["lmPcode"] != "")
            & (df["meterNo"] != "")
            & (df["ym"] != "")
        ].copy()

        # hard sanity check
        bad_len = (df["meterNo"].str.len() != METER_LEN).sum()
        if bad_len:
            raise ValueError(f"[BAD METER LENGTH] {f.name}: {bad_len} rows have meterNo length != {METER_LEN}")

        parts.append(df)
        print(f"[OK] Loaded {f.name} rows={len(df)}")

    all_df = pd.concat(parts, ignore_index=True)
    print(f"\n[ALL ATOMIC] rows={len(all_df)} uniqueMeters={all_df['meterNo'].nunique()} months={sorted(all_df['ym'].unique().tolist())}")
    return all_df


# -----------------------------
# Monthly Sales Grouping
# -----------------------------
# GR1: <=  99.99
# GR2: 100.00 - 299.99
# GR3: 300.00 - 499.99
# GR4: 500.00 - 999.99
# GR5: >= 1000.00
# -----------------------------
def sales_group_from_amount_total_c(total_cents: int) -> str:
    if total_cents <= 9_999:
        return "GR1"
    if total_cents <= 29_999:
        return "GR2"
    if total_cents <= 49_999:
        return "GR3"
    if total_cents <= 99_999:
        return "GR4"
    return "GR5"


def sales_group_label(group_id: str) -> str:
    return {
        "GR1": "<=99.99",
        "GR2": "100-299.99",
        "GR3": "300-499.99",
        "GR4": "500-999.99",
        "GR5": ">=1000",
    }.get(group_id, "")


def aggregate_monthly(df: pd.DataFrame) -> pd.DataFrame:
    g = (
        df.groupby(["lmPcode", "meterNo", "ym", "y", "m"], as_index=False)
        .agg(
            purchasesCount=("atomicId", "count"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("txAtMs", "min"),
            lastPurchaseAtMs=("txAtMs", "max"),
        )
    )

    g["firstPurchaseAtISO"] = pd.to_datetime(g["firstPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    g["lastPurchaseAtISO"] = pd.to_datetime(g["lastPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    g["salesGroupId"] = g["amountTotalC"].apply(sales_group_from_amount_total_c)
    g["salesGroupLabel"] = g["salesGroupId"].apply(sales_group_label)

    g["docId"] = g["lmPcode"] + "__" + g["meterNo"] + "__" + g["ym"]
    return g


def aggregate_monthly_lm(df: pd.DataFrame) -> pd.DataFrame:
    g = (
        df.groupby(["lmPcode", "ym", "y", "m"], as_index=False)
        .agg(
            purchasesCount=("atomicId", "count"),
            metersCount=("meterNo", pd.Series.nunique),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("txAtMs", "min"),
            lastPurchaseAtMs=("txAtMs", "max"),
        )
    )

    g["firstPurchaseAtISO"] = pd.to_datetime(g["firstPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    g["lastPurchaseAtISO"] = pd.to_datetime(g["lastPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    g["docId"] = g["lmPcode"] + "__" + g["ym"]
    return g


def aggregate_monthly_lm_groups(monthly_meter: pd.DataFrame) -> pd.DataFrame:
    g = (
        monthly_meter.groupby(["lmPcode", "ym", "y", "m", "salesGroupId", "salesGroupLabel"], as_index=False)
        .agg(
            metersCount=("meterNo", "count"),
            purchasesCount=("purchasesCount", "sum"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("firstPurchaseAtMs", "min"),
            lastPurchaseAtMs=("lastPurchaseAtMs", "max"),
        )
    )

    g["firstPurchaseAtISO"] = pd.to_datetime(g["firstPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    g["lastPurchaseAtISO"] = pd.to_datetime(g["lastPurchaseAtMs"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    g["docId"] = g["lmPcode"] + "__" + g["ym"] + "__" + g["salesGroupId"]
    return g


def reconcile(atomic: pd.DataFrame, monthly_lm: pd.DataFrame):
    # Compare atomic vs monthly_lm per (lmPcode, ym)
    a = (
        atomic.groupby(["lmPcode", "ym"], as_index=False)
        .agg(
            a_purchases=("atomicId", "count"),
            a_meters=("meterNo", pd.Series.nunique),
            a_amount=("amountTotalC", "sum"),
            a_cost=("costC", "sum"),
            a_vat=("vatC", "sum"),
        )
    )

    m = monthly_lm.rename(
        columns={
            "purchasesCount": "m_purchases",
            "metersCount": "m_meters",
            "amountTotalC": "m_amount",
            "costC": "m_cost",
            "vatC": "m_vat",
        }
    )[["lmPcode", "ym", "m_purchases", "m_meters", "m_amount", "m_cost", "m_vat"]]

    j = a.merge(m, on=["lmPcode", "ym"], how="left")

    # differences
    j["d_purchases"] = j["a_purchases"] - j["m_purchases"]
    j["d_meters"] = j["a_meters"] - j["m_meters"]
    j["d_amount"] = j["a_amount"] - j["m_amount"]
    j["d_cost"] = j["a_cost"] - j["m_cost"]
    j["d_vat"] = j["a_vat"] - j["m_vat"]

    bad = j[
        (j["d_purchases"] != 0)
        | (j["d_meters"] != 0)
        | (j["d_amount"] != 0)
        | (j["d_cost"] != 0)
        | (j["d_vat"] != 0)
    ].copy()

    print("\n[RECONCILE] atomic vs monthly_lm by lmPcode+ym")
    print(j[["lmPcode","ym","a_purchases","m_purchases","d_purchases","a_meters","m_meters","d_meters","d_amount"]])

    if len(bad):
        raise ValueError(f"[RECONCILE FAILED] Differences found in {len(bad)} lmPcode+ym rows.\n{bad.to_string(index=False)}")

    print("[RECONCILE OK] Monthly LM totals match atomic totals for all months.")


def write_outputs(monthly, monthly_lm, monthly_lm_groups):
    for ym, part in monthly.groupby("ym"):
        out = MONTHLY_DIR / f"monthly__{RUN_TAG}__{ym}__from_atomic.csv"
        part.to_csv(out, index=False)
        print(f"[OK] Wrote {out.name} rows={len(part)}")

    for ym, part in monthly_lm.groupby("ym"):
        out = MONTHLY_LM_DIR / f"monthly_lm__{RUN_TAG}__{ym}__from_atomic.csv"
        part.to_csv(out, index=False)
        print(f"[OK] Wrote {out.name} rows={len(part)}")

    for ym, part in monthly_lm_groups.groupby("ym"):
        out = MONTHLY_LM_GROUPS_DIR / f"monthly_lm_groups__{RUN_TAG}__{ym}__from_atomic.csv"
        part.to_csv(out, index=False)
        print(f"[OK] Wrote {out.name} rows={len(part)}")


def main():
    ensure_dirs()
    atomic = read_all_atomic()

    monthly = aggregate_monthly(atomic)
    monthly_lm = aggregate_monthly_lm(atomic)
    monthly_lm_groups = aggregate_monthly_lm_groups(monthly)

    print(f"\n[MONTHLY] rows={len(monthly)}")
    print(f"[MONTHLY_LM] rows={len(monthly_lm)}")
    print(f"[MONTHLY_LM_GROUPS] rows={len(monthly_lm_groups)}")

    # ✅ fail fast if any mismatch
    reconcile(atomic, monthly_lm)

    write_outputs(monthly, monthly_lm, monthly_lm_groups)

    # combined outputs
    monthly.to_csv(MONTHLY_DIR / f"monthly__{RUN_TAG}__ALL__from_atomic.csv", index=False)
    monthly_lm.to_csv(MONTHLY_LM_DIR / f"monthly_lm__{RUN_TAG}__ALL__from_atomic.csv", index=False)
    monthly_lm_groups.to_csv(MONTHLY_LM_GROUPS_DIR / f"monthly_lm_groups__{RUN_TAG}__ALL__from_atomic.csv", index=False)

    print("\n[OK] Combined files written")


if __name__ == "__main__":
    main()