"""
07_upload_meter_master_v2.py

Upload the Meter Master staging CSV into the Firestore collection:
    meter_master

The uploader preserves the approved Meter Master data shape and enforces the
standard six-field iREPS metadata contract.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

PIPELINE_ACTOR_UID = "SYSTEM"
PIPELINE_ACTOR_USER = "METER MASTER PIPELINE"

REQUIRED_COLUMNS = [
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
# HELPERS
# =========================================================


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def utc_now_iso() -> str:
    """Return an iREPS-compatible UTC timestamp with millisecond precision."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def nested_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# =========================================================
# LOAD
# =========================================================


def load_csv() -> pd.DataFrame:
    df = pd.read_csv(INPUT_PATH, dtype=str).fillna("")

    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(
            f"{INPUT_PATH.name} is missing required columns: {', '.join(missing)}"
        )

    return df


# =========================================================
# BUILD DOCUMENT
# =========================================================


def build_metadata(existing_doc: dict, now_iso: str) -> dict:
    """
    Preserve immutable creation metadata on updates and refresh update metadata.

    Existing documents that pre-date the metadata rule are repaired by receiving
    creation metadata during their next pipeline upload.
    """
    existing_metadata = nested_dict(existing_doc.get("metadata"))

    return {
        "createdAt": safe_str(existing_metadata.get("createdAt")) or now_iso,
        "createdByUid": (
            safe_str(existing_metadata.get("createdByUid")) or PIPELINE_ACTOR_UID
        ),
        "createdByUser": (
            safe_str(existing_metadata.get("createdByUser")) or PIPELINE_ACTOR_USER
        ),
        "updatedAt": now_iso,
        "updatedByUid": PIPELINE_ACTOR_UID,
        "updatedByUser": PIPELINE_ACTOR_USER,
    }


def resolve_ast_id(row: pd.Series, existing_doc: dict) -> str:
    """
    Use the CSV AST id when supplied.

    As a safety guard, a blank staging astId must not erase a populated AST link
    that may already have been written by a field workflow.
    """
    staged_ast_id = safe_str(row.get("astId"))
    if staged_ast_id:
        return staged_ast_id

    existing_refs = nested_dict(existing_doc.get("refs"))
    existing_asts = nested_dict(existing_refs.get("asts"))
    return safe_str(existing_asts.get("id"))


def build_doc(row: pd.Series, existing_doc: dict, now_iso: str) -> dict:
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
                "id": resolve_ast_id(row, existing_doc),
            },
            "sales": {
                "id": safe_str(row.get("salesId")),
                "provider": safe_str(row.get("salesProvider")) or "conlog",
            },
        },
        "metadata": build_metadata(existing_doc, now_iso),
    }


# =========================================================
# UPLOAD
# =========================================================


def upload(df: pd.DataFrame) -> None:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE
    )
    db = firestore.Client(project=PROJECT_ID, credentials=credentials)

    total_rows = len(df)
    written = 0
    skipped_missing_master_id = 0

    for start in tqdm(range(0, total_rows, BATCH_SIZE), desc="Uploading batches"):
        end = min(start + BATCH_SIZE, total_rows)
        chunk = df.iloc[start:end]

        pending: list[tuple[str, pd.Series, Any]] = []
        refs = []

        for _, row in chunk.iterrows():
            doc_id = safe_str(row.get("masterId"))
            if not doc_id:
                skipped_missing_master_id += 1
                continue

            doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
            pending.append((doc_id, row, doc_ref))
            refs.append(doc_ref)

        if not pending:
            continue

        existing_by_id: dict[str, dict] = {}
        for snapshot in db.get_all(refs):
            if snapshot.exists:
                existing_by_id[snapshot.id] = snapshot.to_dict() or {}

        now_iso = utc_now_iso()
        batch = db.batch()

        for doc_id, row, doc_ref in pending:
            existing_doc = existing_by_id.get(doc_id, {})
            doc = build_doc(row, existing_doc, now_iso)
            batch.set(doc_ref, doc, merge=True)
            written += 1

        batch.commit()

    print(f"Uploaded {written} documents to '{COLLECTION_NAME}'")
    print(f"Skipped rows with no masterId: {skipped_missing_master_id}")


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
