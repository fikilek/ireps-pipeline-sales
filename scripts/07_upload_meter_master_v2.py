"""
07_upload_meter_master_v2.py

Upload meter_master CSV into Firestore collection: meter_master
"""

from pathlib import Path

import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account
from tqdm import tqdm


# =========================================================
# CONFIG
# =========================================================

INPUT_PATH = Path("output/meter_master/meter_master__FULL__2025-09_to_2026-02.csv")
SERVICE_ACCOUNT_FILE = Path("secrets/ireps2-e72fd9dc94de.json")
PROJECT_ID = "ireps2"

COLLECTION_NAME = "meter_master"
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
        "lmPcode": safe_str(row.get("lmPcode")),
        "meterNo": {
            "raw": safe_str(row.get("meterNoRaw")),
            "normalized": safe_str(row.get("meterNoNormalized")),
        },
        "meterType": safe_str(row.get("meterType")) or "electricity",
        "customerNo": safe_str(row.get("customerNo")),
        "accountNo": safe_str(row.get("accountNo")),
        "refs": {
            "asts": {
                "id": safe_str(row.get("astId")),
            },
            "sales": {
                "id": safe_str(row.get("salesId")),
                "provider": safe_str(row.get("salesProvider")) or "conlog",
            },
        },
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