# 04_upload_conlog_monthly_v2.py
import math
from pathlib import Path

import pandas as pd

import firebase_admin
from firebase_admin import credentials, firestore


BASE_DIR = Path(__file__).resolve().parent
SERVICE_ACCOUNT_FILE = BASE_DIR / "secrets" / "ireps2-e72fd9dc94de.json"

MONTHLY_DIR = BASE_DIR / "output" / "monthly"
MONTHLY_LM_DIR = BASE_DIR / "output" / "monthly_lm"
MONTHLY_LM_GROUPS_DIR = BASE_DIR / "output" / "monthly_lm_groups"

COLL_MONTHLY = "conlog_sales_monthly"
COLL_MONTHLY_LM = "conlog_sales_monthly_lm"
COLL_MONTHLY_LM_GROUPS = "conlog_sales_monthly_lm_groups"

BATCH_SIZE = 450
METER_LEN = 11  # must match stage 01 + stage 02 + stage 03


# -----------------------------
# Firestore init
# -----------------------------
def init_firestore():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise SystemExit(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")

    if not firebase_admin._apps:
        cred = credentials.Certificate(str(SERVICE_ACCOUNT_FILE))
        firebase_admin.initialize_app(cred)

    return firestore.client()


# -----------------------------
# Robust CSV read
# -----------------------------
def read_csv_robust(path: Path) -> pd.DataFrame:
    last = None
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            return pd.read_csv(path, dtype=str, encoding=enc)
        except Exception as e:
            last = e
    raise last


def normalize_meter_no(raw: str) -> str:
    if raw is None or pd.isna(raw):
        return ""
    s = str(raw).strip()

    # Excel artifact: "12345.0"
    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]

    if s.isdigit():
        s = s.zfill(METER_LEN)

    return s


def read_csv_required(path: Path, required_cols):
    df = read_csv_robust(path)
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} missing columns: {missing}")

    # trim all columns as strings
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()

    return df


# -----------------------------
# Upload: conlog_sales_monthly (meter-month)
# -----------------------------
def upload_monthly_file(db, csv_path: Path) -> dict:
    required = [
        "lmPcode",
        "meterNo",
        "ym",
        "y",
        "m",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtISO",
        "lastPurchaseAtISO",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
        "salesGroupId",
        "salesGroupLabel",
    ]

    df = read_csv_required(csv_path, required)

    # numeric columns
    for c in [
        "y",
        "m",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

    # cleanup
    df = df[(df["lmPcode"] != "") & (df["meterNo"] != "") & (df["ym"] != "")].copy()

    total = len(df)
    if total == 0:
        print(f"[SKIP] {csv_path.name} has 0 rows after cleanup.")
        return {"read": 0, "written": 0, "skipped_meter": 0, "skipped_ym": 0}

    print(f"\n[MONTHLY FILE] {csv_path.name} -> {COLL_MONTHLY} (rows={total})")

    written = 0
    skipped_meter = 0
    skipped_ym = 0

    chunks = math.ceil(total / BATCH_SIZE)
    for i in range(chunks):
        start = i * BATCH_SIZE
        end = min((i + 1) * BATCH_SIZE, total)
        batch = db.batch()

        batch_written = 0

        for _, row in df.iloc[start:end].iterrows():
            lm_pcode = row["lmPcode"]
            ym = row["ym"]
            meter_no = normalize_meter_no(row["meterNo"])

            # hard validations
            if (not meter_no) or (len(meter_no) != METER_LEN):
                skipped_meter += 1
                continue

            # ym must match y/m
            ym_expected = f"{int(row['y']):04d}-{int(row['m']):02d}"
            if ym != ym_expected:
                skipped_ym += 1
                continue

            doc_id = f"{lm_pcode}__{meter_no}__{ym}"

            doc = {
                "lmPcode": lm_pcode,
                "meterNo": meter_no,
                "ym": ym,
                "y": int(row["y"]),
                "m": int(row["m"]),
                "purchasesCount": int(row["purchasesCount"]),
                "amountTotalC": int(row["amountTotalC"]),
                "costC": int(row["costC"]),
                "vatC": int(row["vatC"]),
                "firstPurchaseAtISO": row["firstPurchaseAtISO"],
                "lastPurchaseAtISO": row["lastPurchaseAtISO"],
                "firstPurchaseAtMs": int(row["firstPurchaseAtMs"]),
                "lastPurchaseAtMs": int(row["lastPurchaseAtMs"]),
                "salesGroupId": row["salesGroupId"],
                "salesGroupLabel": row["salesGroupLabel"],
            }

            doc_ref = db.collection(COLL_MONTHLY).document(doc_id)
            batch.set(doc_ref, doc, merge=True)
            batch_written += 1

        batch.commit()
        written += batch_written
        print(f"  - batch {i+1}/{chunks}: wrote {batch_written} (total {written}/{total})")

    print(
        f"[OK] Uploaded {written}/{total} monthly docs from {csv_path.name} "
        f"(skipped_meter={skipped_meter}, skipped_ym={skipped_ym})"
    )
    return {"read": total, "written": written, "skipped_meter": skipped_meter, "skipped_ym": skipped_ym}


# -----------------------------
# Upload: conlog_sales_monthly_lm (lm-month)
# -----------------------------
def upload_monthly_lm_file(db, csv_path: Path) -> dict:
    required = [
        "lmPcode",
        "ym",
        "y",
        "m",
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtISO",
        "lastPurchaseAtISO",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]

    df = read_csv_required(csv_path, required)

    for c in [
        "y",
        "m",
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

    df = df[(df["lmPcode"] != "") & (df["ym"] != "")].copy()

    total = len(df)
    if total == 0:
        print(f"[SKIP] {csv_path.name} has 0 rows.")
        return {"read": 0, "written": 0, "skipped_ym": 0}

    print(f"\n[MONTHLY_LM FILE] {csv_path.name} -> {COLL_MONTHLY_LM} (rows={total})")

    written = 0
    skipped_ym = 0

    chunks = math.ceil(total / BATCH_SIZE)
    for i in range(chunks):
        start = i * BATCH_SIZE
        end = min((i + 1) * BATCH_SIZE, total)
        batch = db.batch()

        batch_written = 0

        for _, row in df.iloc[start:end].iterrows():
            lm_pcode = row["lmPcode"]
            ym = row["ym"]

            ym_expected = f"{int(row['y']):04d}-{int(row['m']):02d}"
            if ym != ym_expected:
                skipped_ym += 1
                continue

            # rebuild docId (do not trust CSV)
            doc_id = f"{lm_pcode}__{ym}"

            doc = {
                "lmPcode": lm_pcode,
                "ym": ym,
                "y": int(row["y"]),
                "m": int(row["m"]),
                "purchasesCount": int(row["purchasesCount"]),
                "metersCount": int(row["metersCount"]),
                "amountTotalC": int(row["amountTotalC"]),
                "costC": int(row["costC"]),
                "vatC": int(row["vatC"]),
                "firstPurchaseAtISO": row["firstPurchaseAtISO"],
                "lastPurchaseAtISO": row["lastPurchaseAtISO"],
                "firstPurchaseAtMs": int(row["firstPurchaseAtMs"]),
                "lastPurchaseAtMs": int(row["lastPurchaseAtMs"]),
            }

            doc_ref = db.collection(COLL_MONTHLY_LM).document(doc_id)
            batch.set(doc_ref, doc, merge=True)
            batch_written += 1

        batch.commit()
        written += batch_written
        print(f"  - batch {i+1}/{chunks}: wrote {batch_written} (total {written}/{total})")

    print(f"[OK] Uploaded {written}/{total} monthly_lm docs from {csv_path.name} (skipped_ym={skipped_ym})")
    return {"read": total, "written": written, "skipped_ym": skipped_ym}


# -----------------------------
# Upload: conlog_sales_monthly_lm_groups (lm-month-group)
# -----------------------------
def upload_monthly_lm_groups_file(db, csv_path: Path) -> dict:
    required = [
        "lmPcode",
        "ym",
        "y",
        "m",
        "salesGroupId",
        "salesGroupLabel",
        "metersCount",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtISO",
        "lastPurchaseAtISO",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]

    df = read_csv_required(csv_path, required)

    for c in [
        "y",
        "m",
        "metersCount",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

    df = df[(df["lmPcode"] != "") & (df["ym"] != "") & (df["salesGroupId"] != "")].copy()

    total = len(df)
    if total == 0:
        print(f"[SKIP] {csv_path.name} has 0 rows.")
        return {"read": 0, "written": 0, "skipped_ym": 0}

    print(f"\n[MONTHLY_LM_GROUPS FILE] {csv_path.name} -> {COLL_MONTHLY_LM_GROUPS} (rows={total})")

    written = 0
    skipped_ym = 0

    chunks = math.ceil(total / BATCH_SIZE)
    for i in range(chunks):
        start = i * BATCH_SIZE
        end = min((i + 1) * BATCH_SIZE, total)
        batch = db.batch()

        batch_written = 0

        for _, row in df.iloc[start:end].iterrows():
            lm_pcode = row["lmPcode"]
            ym = row["ym"]
            group_id = row["salesGroupId"]

            ym_expected = f"{int(row['y']):04d}-{int(row['m']):02d}"
            if ym != ym_expected:
                skipped_ym += 1
                continue

            # rebuild docId
            doc_id = f"{lm_pcode}__{ym}__{group_id}"

            doc = {
                "lmPcode": lm_pcode,
                "ym": ym,
                "y": int(row["y"]),
                "m": int(row["m"]),
                "salesGroupId": group_id,
                "salesGroupLabel": row["salesGroupLabel"],
                "metersCount": int(row["metersCount"]),
                "purchasesCount": int(row["purchasesCount"]),
                "amountTotalC": int(row["amountTotalC"]),
                "costC": int(row["costC"]),
                "vatC": int(row["vatC"]),
                "firstPurchaseAtISO": row["firstPurchaseAtISO"],
                "lastPurchaseAtISO": row["lastPurchaseAtISO"],
                "firstPurchaseAtMs": int(row["firstPurchaseAtMs"]),
                "lastPurchaseAtMs": int(row["lastPurchaseAtMs"]),
            }

            doc_ref = db.collection(COLL_MONTHLY_LM_GROUPS).document(doc_id)
            batch.set(doc_ref, doc, merge=True)
            batch_written += 1

        batch.commit()
        written += batch_written
        print(f"  - batch {i+1}/{chunks}: wrote {batch_written} (total {written}/{total})")

    print(f"[OK] Uploaded {written}/{total} monthly_lm_groups docs from {csv_path.name} (skipped_ym={skipped_ym})")
    return {"read": total, "written": written, "skipped_ym": skipped_ym}


def main():
    db = init_firestore()

    # IMPORTANT: your stage 03 files are named with FULL tag
    monthly_files = sorted(MONTHLY_DIR.glob("monthly__FULL__????-??__from_atomic.csv"))
    if not monthly_files:
        raise SystemExit(
            f"No per-month monthly files found in {MONTHLY_DIR}. "
            f"Expected pattern monthly__FULL__YYYY-MM__from_atomic.csv"
        )

    monthly_lm_files = sorted(MONTHLY_LM_DIR.glob("monthly_lm__FULL__????-??__from_atomic.csv"))
    if not monthly_lm_files:
        raise SystemExit(
            f"No per-month monthly_lm files found in {MONTHLY_LM_DIR}. "
            f"Expected pattern monthly_lm__FULL__YYYY-MM__from_atomic.csv"
        )

    monthly_lm_groups_files = sorted(
        MONTHLY_LM_GROUPS_DIR.glob("monthly_lm_groups__FULL__????-??__from_atomic.csv")
    )
    if not monthly_lm_groups_files:
        raise SystemExit(
            f"No per-month monthly_lm_groups files found in {MONTHLY_LM_GROUPS_DIR}. "
            f"Expected pattern monthly_lm_groups__FULL__YYYY-MM__from_atomic.csv"
        )

    tot_m_read = tot_m_written = tot_m_skip_meter = tot_m_skip_ym = 0
    for f in monthly_files:
        res = upload_monthly_file(db, f)
        tot_m_read += res["read"]
        tot_m_written += res["written"]
        tot_m_skip_meter += res["skipped_meter"]
        tot_m_skip_ym += res["skipped_ym"]

    tot_lm_read = tot_lm_written = tot_lm_skip_ym = 0
    for f in monthly_lm_files:
        res = upload_monthly_lm_file(db, f)
        tot_lm_read += res["read"]
        tot_lm_written += res["written"]
        tot_lm_skip_ym += res["skipped_ym"]

    tot_g_read = tot_g_written = tot_g_skip_ym = 0
    for f in monthly_lm_groups_files:
        res = upload_monthly_lm_groups_file(db, f)
        tot_g_read += res["read"]
        tot_g_written += res["written"]
        tot_g_skip_ym += res["skipped_ym"]

    print("\n[DONE] Monthly uploads complete")
    print(f"  - {COLL_MONTHLY}: read={tot_m_read} written={tot_m_written} skipped_meter={tot_m_skip_meter} skipped_ym={tot_m_skip_ym}")
    print(f"  - {COLL_MONTHLY_LM}: read={tot_lm_read} written={tot_lm_written} skipped_ym={tot_lm_skip_ym}")
    print(f"  - {COLL_MONTHLY_LM_GROUPS}: read={tot_g_read} written={tot_g_written} skipped_ym={tot_g_skip_ym}")


if __name__ == "__main__":
    main()