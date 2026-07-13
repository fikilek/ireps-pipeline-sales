# 02_upload_conlog_atomic_v2.py
import math
import time
from pathlib import Path

import pandas as pd

import firebase_admin
from firebase_admin import credentials, firestore


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ATOMIC_DIR = BASE_DIR / "output" / "atomic"
SERVICE_ACCOUNT_FILE = BASE_DIR / "secrets" / "ireps2-e72fd9dc94de.json"

COLLECTION = "conlog_sales_atomic"
BATCH_SIZE = 450   # keep under 500
METER_LEN = 11     # Lesedi appears to use 11 digits

# -----------------------------
# SAFETY SWITCHES
# -----------------------------
UPLOAD_DRYRUN_FILES = False   # keep False for production safety
MAX_COMMIT_RETRIES = 3        # retries for transient errors
RETRY_SLEEP_SEC = 2


def init_firestore():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise SystemExit(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")

    if not firebase_admin._apps:
        cred = credentials.Certificate(str(SERVICE_ACCOUNT_FILE))
        firebase_admin.initialize_app(cred)

    return firestore.client()


def read_atomic_csv(path: Path) -> pd.DataFrame:
    # robust encoding + keep everything as string initially
    df = None
    last = None
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            break
        except Exception as e:
            last = e
    if df is None:
        raise last

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
        raise ValueError(f"{path.name} missing columns: {missing}")

    # numeric columns (do NOT touch meterNo)
    for c in ["txAtMs", "amountTotalC", "costC", "vatC", "sourceRow", "ingestedAtMs", "y", "m"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

    # trim key strings
    for c in ["atomicId", "lmPcode", "meterNo", "ym", "txAtISO", "currency", "sourceFileId", "ingestedAtISO"]:
        df[c] = df[c].astype(str).str.strip()

    # drop obviously bad rows
    df = df[
        (df["atomicId"] != "")
        & (df["lmPcode"] != "")
        & (df["meterNo"] != "")
        & (df["ym"] != "")
        & (df["txAtISO"] != "")
    ].copy()

    # light sanity warning (does not fail upload)
    s = df["meterNo"].astype(str).str.strip()
    bad_len = (s.str.len() != METER_LEN).sum()
    if bad_len:
        print(f"[WARN] {path.name}: {bad_len} rows have meterNo length != {METER_LEN} (will be skipped during upload)")

    return df


def normalize_meter_no(raw: str) -> str:
    """
    Ensure meterNo is always a string and preserve leading zeros.
    Also removes common Excel artifacts like trailing .0.
    """
    if raw is None or pd.isna(raw):
        return ""

    s = str(raw).strip()

    # Excel artifact: "12345.0"
    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]

    # If digits-only, enforce known fixed length
    if s.isdigit():
        s = s.zfill(METER_LEN)

    return s


def commit_with_retry(batch, attempt_tag=""):
    last_err = None
    for attempt in range(1, MAX_COMMIT_RETRIES + 1):
        try:
            batch.commit()
            return
        except Exception as e:
            last_err = e
            print(f"[WARN] batch commit failed {attempt}/{MAX_COMMIT_RETRIES} {attempt_tag}: {e}")
            if attempt < MAX_COMMIT_RETRIES:
                time.sleep(RETRY_SLEEP_SEC * attempt)
    raise last_err


def upload_file(db, csv_path: Path) -> dict:
    df = read_atomic_csv(csv_path)
    total = len(df)
    if total == 0:
        print(f"[SKIP] {csv_path.name} has 0 rows.")
        return {"file": csv_path.name, "total": 0, "written": 0, "skipped_meter": 0, "skipped_ym": 0}

    print(f"\n[FILE] {csv_path.name} -> {COLLECTION} (rows={total})")

    written = 0
    skipped_meter = 0
    skipped_ym = 0

    chunks = math.ceil(total / BATCH_SIZE)

    for i in range(chunks):
        start = i * BATCH_SIZE
        end = min((i + 1) * BATCH_SIZE, total)

        batch = db.batch()
        ops = 0  # ✅ track actual writes in this batch

        for _, row in df.iloc[start:end].iterrows():
            doc_id = row["atomicId"]
            meter_no = normalize_meter_no(row["meterNo"])

            # hard validations
            if not meter_no or len(meter_no) != METER_LEN:
                skipped_meter += 1
                continue

            ym_expected = str(row["txAtISO"])[:7]
            if row["ym"] != ym_expected:
                skipped_ym += 1
                continue

            doc = {
                "lmPcode": row["lmPcode"],
                "meterNo": meter_no,
                "txAtISO": row["txAtISO"],
                "txAtMs": int(row["txAtMs"]),
                "ym": row["ym"],
                "y": int(row["y"]),
                "m": int(row["m"]),
                "amountTotalC": int(row["amountTotalC"]),
                "costC": int(row["costC"]),
                "vatC": int(row["vatC"]),
                "currency": row["currency"],
                "sourceFileId": row["sourceFileId"],
                "sourceRow": int(row["sourceRow"]),
                "ingestedAtISO": row["ingestedAtISO"],
                "ingestedAtMs": int(row["ingestedAtMs"]),
            }

            doc_ref = db.collection(COLLECTION).document(doc_id)
            batch.set(doc_ref, doc, merge=True)
            ops += 1

        # ✅ commit only if we have writes
        if ops > 0:
            commit_with_retry(batch, attempt_tag=f"(file={csv_path.name}, batch={i+1}/{chunks})")

        written += ops
        print(f"  - batch {i+1}/{chunks}: wrote {ops} (total {written}/{total})")

    print(
        f"[OK] Uploaded {written}/{total} rows from {csv_path.name} "
        f"(skipped_meter={skipped_meter}, skipped_ym={skipped_ym})"
    )

    return {
        "file": csv_path.name,
        "total": int(total),
        "written": int(written),
        "skipped_meter": int(skipped_meter),
        "skipped_ym": int(skipped_ym),
    }


def main():
    if not OUTPUT_ATOMIC_DIR.exists():
        raise SystemExit(f"Atomic output dir not found: {OUTPUT_ATOMIC_DIR}")

    db = init_firestore()

    files = sorted(OUTPUT_ATOMIC_DIR.glob("atomic__*.csv"))
    if not files:
        raise SystemExit(f"No atomic__*.csv found in {OUTPUT_ATOMIC_DIR}")

    # ✅ Safety: block dryrun files unless explicitly allowed
    if not UPLOAD_DRYRUN_FILES:
        before = len(files)
        files = [f for f in files if "DRYRUN" not in f.name.upper()]
        removed = before - len(files)
        if removed:
            print(f"[SAFETY] Ignored {removed} DRYRUN file(s). Set UPLOAD_DRYRUN_FILES=True to allow.")
    if not files:
        raise SystemExit("[SAFETY] No eligible atomic files to upload (only DRYRUN files found).")

    grand_total = 0
    grand_written = 0
    grand_skipped_meter = 0
    grand_skipped_ym = 0

    for f in files:
        res = upload_file(db, f)
        grand_total += res["total"]
        grand_written += res["written"]
        grand_skipped_meter += res["skipped_meter"]
        grand_skipped_ym += res["skipped_ym"]

    print("\n[DONE] Atomic upload complete")
    print(f"  - total rows read:     {grand_total}")
    print(f"  - total rows written:  {grand_written}")
    print(f"  - skipped bad meter:   {grand_skipped_meter}")
    print(f"  - skipped bad ym:      {grand_skipped_ym}")


if __name__ == "__main__":
    main()