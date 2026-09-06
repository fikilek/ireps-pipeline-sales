#!/usr/bin/env python3
"""
Independent DEV June Sales sampler for iREPS.

READ-ONLY PURPOSE
-----------------
Interrogate the actual Firestore DEV state in:
  project    : ireps2
  collection : sales-all-meters
  lmPcode    : ZA5241
  month      : 2026-06

The script:
  1. scans the actual DEV Sales records for ZA5241;
  2. groups records by monthlyCategories["2026-06"].leakageCategory;
  3. randomly samples up to 3 meters from CAT1..CAT8;
  4. randomly samples up to 5 Normal meters;
  5. fetches the complete actual stored Firestore documents for those samples;
  6. writes timestamped JSON and Markdown evidence directly under output\\logs.

It performs NO Firestore writes, creates, updates, deletes, batch commits,
transactions, migrations, or Git operations.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account


PROJECT_ID = "ireps2"
COLLECTION = "sales-all-meters"
LM_PCODE = "ZA5241"
MONTH = "2026-06"
DEFAULT_SERVICE_ACCOUNT = r"C:\dev\secrets\ireps2-e72fd9dc94de.json"
DEFAULT_PER_CATEGORY = 3
DEFAULT_NORMAL_COUNT = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only random sample of actual June Sales records in DEV."
    )
    parser.add_argument(
        "--service-account",
        default=DEFAULT_SERVICE_ACCOUNT,
        help=f"DEV service account JSON. Default: {DEFAULT_SERVICE_ACCOUNT}",
    )
    parser.add_argument(
        "--confirm-project",
        required=True,
        help=f"Must be exactly {PROJECT_ID}. This is a DEV safety gate.",
    )
    parser.add_argument(
        "--per-category",
        type=int,
        default=DEFAULT_PER_CATEGORY,
        help="Samples requested for each CAT1..CAT8 bucket. Default: 3",
    )
    parser.add_argument(
        "--normal-count",
        type=int,
        default=DEFAULT_NORMAL_COUNT,
        help="Normal samples requested. Default: 5",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional reproducible random seed. A secure random seed is generated if omitted.",
    )
    return parser.parse_args()


class Progress:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> str:
        total = int(time.monotonic() - self.started)
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def stage(self, current: int, total: int, message: str) -> None:
        pct = round((current / total) * 100)
        print(
            f"\n[{current}/{total}] {pct}% | {message} | elapsed {self.elapsed()}",
            flush=True,
        )

    def heartbeat(self, message: str) -> None:
        print(f"    ... {message} | elapsed {self.elapsed()}", flush=True)


def fail(message: str, exit_code: int = 2) -> None:
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(exit_code)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime):
        return {
            "__type__": "timestamp",
            "iso": value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    if isinstance(value, date):
        return {"__type__": "date", "iso": value.isoformat()}
    if value.__class__.__name__ == "GeoPoint":
        return {
            "__type__": "geopoint",
            "latitude": getattr(value, "latitude", None),
            "longitude": getattr(value, "longitude", None),
        }
    if value.__class__.__name__ == "DocumentReference":
        return {
            "__type__": "document_reference",
            "value": str(value),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {
        "__type__": value.__class__.__name__,
        "value": str(value),
    }


def get_month_category(payload: dict[str, Any]) -> dict[str, Any] | None:
    monthly = payload.get("monthlyCategories")
    if not isinstance(monthly, dict):
        return None
    entry = monthly.get(MONTH)
    return entry if isinstance(entry, dict) else None


def classify_bucket(leakage_category: Any) -> str:
    if not isinstance(leakage_category, str) or not leakage_category.strip():
        return "MISSING"

    text = leakage_category.strip()
    upper = text.upper()

    for number in range(1, 9):
        if upper.startswith(f"CAT{number}"):
            return f"CAT{number}"

    if upper.startswith("NORMAL"):
        return "NORMAL"

    return "OTHER"


def iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def build_markdown(
    *,
    generated_at: str,
    random_seed: int,
    scanned_count: int,
    bucket_counts: dict[str, int],
    selected_ids: dict[str, list[str]],
    records: list[dict[str, Any]],
) -> str:
    lines: list[str] = [
        "# Independent DEV June Sales Sample",
        "",
        "**READ ONLY — actual documents fetched directly from Firestore.**",
        "",
        f"- Generated: `{generated_at}`",
        f"- Project: `{PROJECT_ID}`",
        f"- Collection: `{COLLECTION}`",
        f"- LM: `{LM_PCODE}`",
        f"- Month: `{MONTH}`",
        f"- ZA5241 Sales documents scanned: **{scanned_count:,}**",
        f"- Random seed: `{random_seed}`",
        "",
        "## Stored June category populations",
        "",
        "| Bucket | Actual stored count | Sampled |",
        "|---|---:|---:|",
    ]

    ordered_buckets = [*(f"CAT{n}" for n in range(1, 9)), "NORMAL", "OTHER", "MISSING"]
    for bucket in ordered_buckets:
        lines.append(
            f"| {bucket} | {bucket_counts.get(bucket, 0):,} | "
            f"{len(selected_ids.get(bucket, []))} |"
        )

    lines += ["", "## Actual sampled Firestore documents", ""]

    for record in records:
        entry = record.get("monthCategory") or {}
        lines += [
            f"### {record['bucket']} — `{record['documentId']}`",
            "",
            f"- Firestore createTime: `{record.get('createTime')}`",
            f"- Firestore updateTime: `{record.get('updateTime')}`",
            f"- leakageCategory: `{entry.get('leakageCategory')}`",
            f"- riskTier: `{entry.get('riskTier')}`",
            f"- riskScore: `{entry.get('riskScore')}`",
            "",
            "```json",
            json.dumps(record["document"], indent=2, ensure_ascii=False),
            "```",
            "",
        ]

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    progress = Progress()

    progress.stage(1, 5, "Validate DEV-only safety gates")

    if args.confirm_project != PROJECT_ID:
        fail(
            f"--confirm-project must be exactly {PROJECT_ID!r}. "
            f"Received {args.confirm_project!r}."
        )

    if args.per_category < 1:
        fail("--per-category must be at least 1.")

    if args.normal_count < 1:
        fail("--normal-count must be at least 1.")

    service_account_path = Path(args.service_account)
    if not service_account_path.is_file():
        fail(f"Service account not found: {service_account_path}")

    credentials = service_account.Credentials.from_service_account_file(
        str(service_account_path)
    )
    credential_project = getattr(credentials, "project_id", None)
    if credential_project and credential_project != PROJECT_ID:
        fail(
            f"Service-account project mismatch. "
            f"Expected {PROJECT_ID!r}, found {credential_project!r}."
        )

    db = firestore.Client(project=PROJECT_ID, credentials=credentials)
    if db.project != PROJECT_ID:
        fail(
            f"Firestore client project mismatch. "
            f"Expected {PROJECT_ID!r}, found {db.project!r}."
        )

    print(f"    project    : {db.project}", flush=True)
    print(f"    collection : {COLLECTION}", flush=True)
    print(f"    lmPcode    : {LM_PCODE}", flush=True)
    print(f"    month      : {MONTH}", flush=True)
    print("    mode       : READ ONLY", flush=True)

    progress.stage(2, 5, "Scan actual ZA5241 Sales records and classify June buckets")

    buckets: dict[str, list[str]] = defaultdict(list)
    raw_category_counts: Counter[str] = Counter()
    scanned_count = 0

    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("lmPcode", "==", LM_PCODE))
        .select(["monthlyCategories"])
    )

    for snapshot in query.stream():
        scanned_count += 1
        payload = snapshot.to_dict() or {}
        entry = get_month_category(payload)

        leakage_category = entry.get("leakageCategory") if entry else None
        bucket = classify_bucket(leakage_category)

        buckets[bucket].append(snapshot.id)
        raw_category_counts[str(leakage_category)] += 1

        if scanned_count % 250 == 0:
            progress.heartbeat(
                f"scanned {scanned_count:,} ZA5241 Sales documents"
            )

    progress.heartbeat(
        f"scan complete: {scanned_count:,} ZA5241 Sales documents"
    )

    progress.stage(3, 5, "Randomly sample CAT1..CAT8 plus Normal")

    seed = args.seed if args.seed is not None else secrets.randbits(64)
    rng = random.Random(seed)

    selected_ids: dict[str, list[str]] = {}

    for number in range(1, 9):
        bucket = f"CAT{number}"
        ids = sorted(buckets.get(bucket, []))
        take = min(args.per_category, len(ids))
        selected_ids[bucket] = rng.sample(ids, take) if take else []

    normal_ids = sorted(buckets.get("NORMAL", []))
    normal_take = min(args.normal_count, len(normal_ids))
    selected_ids["NORMAL"] = (
        rng.sample(normal_ids, normal_take) if normal_take else []
    )

    print(f"    random seed : {seed}", flush=True)

    for bucket in [*(f"CAT{n}" for n in range(1, 9)), "NORMAL"]:
        requested = args.normal_count if bucket == "NORMAL" else args.per_category
        available = len(buckets.get(bucket, []))
        sampled = len(selected_ids.get(bucket, []))
        print(
            f"    {bucket:6s} | available {available:5d} | "
            f"requested {requested:2d} | sampled {sampled:2d}",
            flush=True,
        )

    progress.stage(4, 5, "Fetch complete actual documents for selected meters")

    selected_pairs: list[tuple[str, str]] = []
    for bucket in [*(f"CAT{n}" for n in range(1, 9)), "NORMAL"]:
        for document_id in selected_ids.get(bucket, []):
            selected_pairs.append((bucket, document_id))

    records: list[dict[str, Any]] = []
    total_selected = len(selected_pairs)

    for index, (bucket, document_id) in enumerate(selected_pairs, start=1):
        snapshot = db.collection(COLLECTION).document(document_id).get()

        if not snapshot.exists:
            fail(
                f"Selected document no longer exists during full read: {document_id}"
            )

        payload = snapshot.to_dict() or {}
        month_category = get_month_category(payload)

        records.append(
            {
                "bucket": bucket,
                "documentId": document_id,
                "createTime": iso_timestamp(snapshot.create_time),
                "updateTime": iso_timestamp(snapshot.update_time),
                "monthCategory": json_safe(month_category),
                "document": json_safe(payload),
            }
        )

        progress.heartbeat(
            f"full-document read {index}/{total_selected}: "
            f"{bucket} {document_id}"
        )

    progress.stage(5, 5, "Write timestamped evidence directly under output\\logs")

    repo_root = Path(__file__).resolve().parents[3]
    output_logs = repo_root / "output" / "logs"

    if not output_logs.is_dir():
        fail(
            f"Expected existing output logs directory not found: {output_logs}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    json_path = output_logs / f"independent_dev_june_sales_sample__{stamp}.json"
    markdown_path = output_logs / f"independent_dev_june_sales_sample__{stamp}.md"

    bucket_counts = {
        bucket: len(ids)
        for bucket, ids in buckets.items()
    }

    result = {
        "readOnly": True,
        "generatedAt": generated_at,
        "projectId": PROJECT_ID,
        "firestoreClientProjectId": db.project,
        "collection": COLLECTION,
        "lmPcode": LM_PCODE,
        "month": MONTH,
        "scopeScanned": scanned_count,
        "randomSeed": seed,
        "requestedPerCategory": args.per_category,
        "requestedNormalCount": args.normal_count,
        "bucketCounts": bucket_counts,
        "rawLeakageCategoryCounts": dict(raw_category_counts.most_common()),
        "selectedIds": selected_ids,
        "sampleCount": len(records),
        "records": records,
        "firestoreWritesPerformed": 0,
    }

    json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    markdown_path.write_text(
        build_markdown(
            generated_at=generated_at,
            random_seed=seed,
            scanned_count=scanned_count,
            bucket_counts=bucket_counts,
            selected_ids=selected_ids,
            records=records,
        ),
        encoding="utf-8",
    )

    print("\n============================================================", flush=True)
    print("INDEPENDENT DEV JUNE SALES SAMPLE COMPLETE", flush=True)
    print("============================================================", flush=True)
    print(f"Project       : {PROJECT_ID}", flush=True)
    print(f"Collection    : {COLLECTION}", flush=True)
    print(f"LM            : {LM_PCODE}", flush=True)
    print(f"Month         : {MONTH}", flush=True)
    print(f"Scope scanned : {scanned_count:,}", flush=True)
    print(f"Samples       : {len(records)}", flush=True)
    print(f"Random seed   : {seed}", flush=True)
    print(f"JSON          : {json_path}", flush=True)
    print(f"Markdown      : {markdown_path}", flush=True)
    print("Firestore writes performed: 0", flush=True)
    print(f"Elapsed       : {progress.elapsed()}", flush=True)

    shortages: list[str] = []
    for number in range(1, 9):
        bucket = f"CAT{number}"
        sampled = len(selected_ids.get(bucket, []))
        if sampled < args.per_category:
            shortages.append(
                f"{bucket}: requested {args.per_category}, sampled {sampled}, "
                f"available {len(buckets.get(bucket, []))}"
            )

    if len(selected_ids.get("NORMAL", [])) < args.normal_count:
        shortages.append(
            f"NORMAL: requested {args.normal_count}, "
            f"sampled {len(selected_ids.get('NORMAL', []))}, "
            f"available {len(buckets.get('NORMAL', []))}"
        )

    if shortages:
        print("\nNOTE: Some requested buckets had fewer stored June records:", flush=True)
        for item in shortages:
            print(f"  - {item}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
