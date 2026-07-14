"""
Create the governed Firestore vending_providers reference collection.

Creates:

    vending_providers/vpr_7f4d3c91a2b84e6f

The provider ID is a permanent opaque internal identifier.
The business code and display name are stored separately.

This script:
- requires an explicit Firebase project;
- verifies the service-account project;
- uses create-only Firestore writes;
- never overwrites an existing provider document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists


COLLECTION = "vending_providers"

PROVIDERS: list[dict[str, Any]] = [
    {
        "documentId": "vpr_7f4d3c91a2b84e6f",
        "providerId": "vpr_7f4d3c91a2b84e6f",
        "providerCode": "CONLOG",
        "providerName": "Conlog",
        "status": "active",
    }
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create governed vending-provider reference documents."
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="Firebase project ID, for example ireps-test.",
    )
    parser.add_argument(
        "--confirm-project",
        required=True,
        help="Must exactly match --project-id.",
    )
    parser.add_argument(
        "--service-account",
        required=True,
        type=Path,
        help="Path to the target project's service-account JSON.",
    )
    parser.add_argument(
        "--created-by-uid",
        default="SYSTEM",
        help="Audit actor UID. Default: SYSTEM.",
    )
    parser.add_argument(
        "--created-by-user",
        default="VENDING PROVIDER SEED",
        help="Audit actor display name.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate and print the planned write without connecting to Firestore.",
    )
    return parser.parse_args()


def load_service_account_project_id(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"Service-account file not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read service-account JSON: {exc}") from exc

    project_id = str(payload.get("project_id", "")).strip()
    if not project_id:
        raise SystemExit(
            f"Service-account JSON does not contain a valid project_id: {path}"
        )

    return project_id


def validate_args(args: argparse.Namespace) -> None:
    project_id = args.project_id.strip()
    confirmation = args.confirm_project.strip()

    if not project_id:
        raise SystemExit("--project-id may not be blank.")

    if confirmation != project_id:
        raise SystemExit(
            "[SAFETY] --confirm-project must exactly match --project-id. "
            f"Received project={project_id!r}, confirmation={confirmation!r}."
        )

    credential_project_id = load_service_account_project_id(args.service_account)
    if credential_project_id != project_id:
        raise SystemExit(
            "[SAFETY] Service-account project mismatch. "
            f"Requested={project_id!r}, credential={credential_project_id!r}."
        )


def validate_seed_data() -> None:
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()

    for provider in PROVIDERS:
        document_id = str(provider.get("documentId", "")).strip()
        provider_id = str(provider.get("providerId", "")).strip()
        provider_code = str(provider.get("providerCode", "")).strip().upper()
        provider_name = str(provider.get("providerName", "")).strip()
        status = str(provider.get("status", "")).strip().lower()

        if not document_id or "/" in document_id:
            raise SystemExit(f"Invalid provider document ID: {document_id!r}")

        if document_id != provider_id:
            raise SystemExit(
                "Provider identity mismatch: documentId must equal providerId. "
                f"documentId={document_id!r}, providerId={provider_id!r}"
            )

        if not provider_id.startswith("vpr_"):
            raise SystemExit(
                f"providerId must use the vpr_ prefix: {provider_id!r}"
            )

        if not provider_code:
            raise SystemExit("providerCode may not be blank.")

        if not provider_name:
            raise SystemExit("providerName may not be blank.")

        if status not in {"active", "inactive"}:
            raise SystemExit(
                f"Unsupported provider status for {provider_id}: {status!r}"
            )

        if provider_id in seen_ids:
            raise SystemExit(f"Duplicate providerId in seed data: {provider_id}")

        if provider_code in seen_codes:
            raise SystemExit(
                f"Duplicate providerCode in seed data: {provider_code}"
            )

        seen_ids.add(provider_id)
        seen_codes.add(provider_code)


def init_firestore(args: argparse.Namespace):
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(args.service_account))
        firebase_admin.initialize_app(
            cred,
            {"projectId": args.project_id},
        )

    return firestore.client()


def build_provider_document(
    provider: dict[str, Any],
    created_by_uid: str,
    created_by_user: str,
) -> dict[str, Any]:
    return {
        "providerId": provider["providerId"],
        "providerCode": provider["providerCode"],
        "providerName": provider["providerName"],
        "status": provider["status"],
        "metadata": {
            "createdAt": firestore.SERVER_TIMESTAMP,
            "createdByUid": created_by_uid,
            "createdByUser": created_by_user,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedByUid": created_by_uid,
            "updatedByUser": created_by_user,
        },
    }


def print_plan(args: argparse.Namespace) -> None:
    print("[TARGET]")
    print(f"  project:    {args.project_id}")
    print(f"  collection: {COLLECTION}")
    print(f"  providers:  {len(PROVIDERS)}")
    print("  mode:       create-only")

    for provider in PROVIDERS:
        print(
            "  planned:    "
            f"{COLLECTION}/{provider['documentId']} "
            f"({provider['providerCode']} - {provider['providerName']})"
        )


def main() -> None:
    args = parse_args()
    validate_args(args)
    validate_seed_data()
    print_plan(args)

    if args.preflight_only:
        print("[PREFLIGHT OK] No Firestore connection or write was performed.")
        return

    db = init_firestore(args)

    created = 0
    conflicts = 0

    for provider in PROVIDERS:
        document_id = provider["documentId"]
        document = build_provider_document(
            provider=provider,
            created_by_uid=args.created_by_uid,
            created_by_user=args.created_by_user,
        )

        ref = db.collection(COLLECTION).document(document_id)

        try:
            ref.create(document)
            created += 1
            print(f"[CREATED] {COLLECTION}/{document_id}")
        except AlreadyExists:
            conflicts += 1
            print(
                f"[CONFLICT] {COLLECTION}/{document_id} already exists. "
                "No overwrite was performed."
            )

    print("\n[RESULT]")
    print(f"  created:   {created}")
    print(f"  conflicts: {conflicts}")

    if conflicts:
        raise SystemExit(
            "[BLOCKED] One or more provider documents already existed. "
            "Review the existing document before making any controlled update."
        )

    print("[OK] Vending-provider seed completed.")


if __name__ == "__main__":
    main()
