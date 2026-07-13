"""
08_upload_sales_all_meters.py

Upload sales_all_meters CSV into Firestore collection: sales-all-meters
"""

from pathlib import Path

import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account
from tqdm import tqdm


# =========================================================
# CONFIG
# =========================================================

INPUT_PATH = Path("output/sales_all_meters/sales_all_meters__FULL__2025-09_to_2026-02.csv")
SERVICE_ACCOUNT_FILE = Path("secrets/ireps2-e72fd9dc94de.json")
PROJECT_ID = "ireps2"

COLLECTION_NAME = "sales-all-meters"
BATCH_SIZE = 450


# =========================================================
# HELPERS
# =========================================================

def require_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")


def safe_str(v) -> str:
    if v is None or pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def safe_optional_str(v):
    s = safe_str(v)
    return s if s else None


def safe_int(v) -> int:
    if v is None or pd.isna(v):
        return 0
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return 0
    return int(float(s))


def safe_optional_int(v):
    if v is None or pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return int(float(s))


# =========================================================
# LOAD
# =========================================================

def load_csv() -> pd.DataFrame:
    df = pd.read_csv(INPUT_PATH, dtype=str)
    return df.fillna("")


# =========================================================
# BUILD DOCUMENT
# =========================================================

def build_doc(row) -> dict:
    return {
        "master": {
            "id": safe_str(row.get("masterId")),
            "visibility": safe_str(row.get("visibility")) or "INVISIBLE",
        },
        "meterNo": safe_str(row.get("meterNo")),
        "meterNoNormalized": safe_str(row.get("meterNoNormalized")),
        "provider": safe_str(row.get("provider")) or "conlog",
        "customerNo": safe_str(row.get("customerNo")),
        "accountNo": safe_str(row.get("accountNo")),
        "totalAmountC": safe_int(row.get("totalAmountC")),
        "monthlyTotalsC": {
            "2025-09": safe_int(row.get("amount_2025_09_C")),
            "2025-10": safe_int(row.get("amount_2025_10_C")),
            "2025-11": safe_int(row.get("amount_2025_11_C")),
            "2025-12": safe_int(row.get("amount_2025_12_C")),
            "2026-01": safe_int(row.get("amount_2026_01_C")),
            "2026-02": safe_int(row.get("amount_2026_02_C")),
        },
        "lastPurchaseAtISO": safe_optional_str(row.get("lastPurchaseAtISO")),
        "daysSinceLastPurchase": safe_optional_int(row.get("daysSinceLastPurchase")),
    }


# =========================================================
# UPLOAD
# =========================================================

def upload(df: pd.DataFrame) -> None:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE
    )
    db = firestore.Client(project=PROJECT_ID, credentials=credentials)

    batch = db.batch()
    count = 0
    total = len(df)

    for _, row in tqdm(df.iterrows(), total=total):
        doc_id = safe_str(row.get("masterId"))
        if not doc_id:
            continue

        doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
        doc = build_doc(row)

        batch.set(doc_ref, doc, merge=True)
        count += 1

        if count % BATCH_SIZE == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    print(f"Uploaded {count} documents to '{COLLECTION_NAME}'")


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    require_file(INPUT_PATH)
    require_file(SERVICE_ACCOUNT_FILE)

    print("Loading CSV...")
    df = load_csv()

    print("Uploading to Firestore...")
    upload(df)

    print("Done.")


if __name__ == "__main__":
    main()