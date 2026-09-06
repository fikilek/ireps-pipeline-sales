#!/usr/bin/env python3
"""
Independent read-only August DEV sampler for iREPS Sales All.

Reads actual Firestore state from:
  project    : ireps2
  collection : sales-all-meters
  lmPcode    : ZA5241
  month      : 2026-08

Sampling:
  - up to 3 from CAT1..CAT8
  - 5 Normal
  - both former July creates
  - 5 meters carrying June+July+August history
  - 3 outside-August historical docs
  - 04298092612 explicitly

Also reports actual June/July/August population counts and August category totals.

READ ONLY:
No Firestore set/update/create/delete/batch/transaction APIs are used.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

PROJECT_ID = "ireps2"
COLLECTION = "sales-all-meters"
LM_PCODE = "ZA5241"
JUNE = "2026-06"
JULY = "2026-07"
AUGUST = "2026-08"
DEFAULT_SERVICE_ACCOUNT = r"C:\dev\secrets\ireps2-e72fd9dc94de.json"

EXPECTED = {
    "juneActive": 10216,
    "julyActive": 10241,
    "augustActive": 10241,
    "continuingJulyAugust": 10241,
    "enteredAugust": 0,
    "exitedAfterJuly": 0,
    "knownThroughAugust": 10272,
}

EXPECTED_CATEGORY_COUNTS = {
    "CAT1": 32,
    "CAT2": 975,
    "CAT3": 2,
    "CAT4": 2291,
    "CAT5": 655,
    "CAT6": 303,
    "CAT7": 0,
    "CAT8": 1369,
    "NORMAL": 4614,
}

SPECIAL_IDS = ("04297839708", "04298618952", "04298092612")


class Progress:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def stage(self, current: int, total: int, message: str) -> None:
        pct = round((current / total) * 100)
        print(f"\n[{current}/{total}] {pct}% | {message} | elapsed {self.elapsed()}", flush=True)

    def heartbeat(self, message: str) -> None:
        print(f"    ... {message} | elapsed {self.elapsed()}", flush=True)


def fail(message: str, code: int = 2) -> None:
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def month_entry(payload: dict[str, Any], month: str) -> dict[str, Any] | None:
    monthly = payload.get("monthlyCategories")
    if not isinstance(monthly, dict):
        return None
    value = monthly.get(month)
    return value if isinstance(value, dict) else None


def category_bucket(value: Any) -> str:
    text = str(value or "").strip().upper()
    for number in range(1, 9):
        if text.startswith(f"CAT{number}"):
            return f"CAT{number}"
    if text.startswith("NORMAL"):
        return "NORMAL"
    return "MISSING"


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime):
        return {"__type__": "timestamp", "iso": value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if value.__class__.__name__ == "GeoPoint":
        return {"__type__": "geopoint", "latitude": getattr(value, "latitude", None), "longitude": getattr(value, "longitude", None)}
    if value.__class__.__name__ == "DocumentReference":
        return {"__type__": "document_reference", "value": str(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"__type__": value.__class__.__name__, "value": str(value)}


def iso_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only independent August DEV Sales sampler.")
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument("--per-category", type=int, default=3)
    parser.add_argument("--normal-count", type=int, default=5)
    parser.add_argument("--triple-history-count", type=int, default=5)
    parser.add_argument("--outside-august-count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    progress = Progress()

    progress.stage(1, 5, "Validate DEV-only target and open read-only client")

    if args.confirm_project != PROJECT_ID:
        fail(f"--confirm-project must be exactly {PROJECT_ID}")

    service_account_path = Path(args.service_account)
    if not service_account_path.is_file():
        fail(f"Service-account file not found: {service_account_path}")

    credentials = service_account.Credentials.from_service_account_file(str(service_account_path))
    credential_project = getattr(credentials, "project_id", None)
    if credential_project and credential_project != PROJECT_ID:
        fail(f"Service-account project mismatch: {credential_project!r} != {PROJECT_ID!r}")

    db = firestore.Client(project=PROJECT_ID, credentials=credentials)
    if db.project != PROJECT_ID:
        fail(f"Firestore client project mismatch: {db.project!r}")

    print(f"    project    : {db.project}", flush=True)
    print(f"    collection : {COLLECTION}", flush=True)
    print(f"    lmPcode    : {LM_PCODE}", flush=True)
    print(f"    month      : {AUGUST}", flush=True)
    print("    mode       : READ ONLY", flush=True)

    progress.stage(2, 5, "Scan actual ZA5241 month/category state")

    docs: dict[str, dict[str, Any]] = {}
    category_counts: Counter[str] = Counter()
    scanned = 0

    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("lmPcode", "==", LM_PCODE))
        .select(["monthlyCategories", "metadata", "previousMeterNumber"])
    )

    for snap in query.stream():
        scanned += 1
        payload = snap.to_dict() or {}
        docs[snap.id] = payload
        august_entry = month_entry(payload, AUGUST)
        if august_entry is not None:
            category_counts[category_bucket(august_entry.get("leakageCategory"))] += 1
        if scanned % 500 == 0:
            progress.heartbeat(f"scanned {scanned:,} ZA5241 Sales docs")

    june_ids = {meter_id for meter_id, d in docs.items() if month_entry(d, JUNE)}
    july_ids = {meter_id for meter_id, d in docs.items() if month_entry(d, JULY)}
    august_ids = {meter_id for meter_id, d in docs.items() if month_entry(d, AUGUST)}
    continuing = july_ids & august_ids
    entered = august_ids - july_ids
    exited = july_ids - august_ids
    known = june_ids | july_ids | august_ids
    outside_august = set(docs) - august_ids
    outside_known = set(docs) - known
    triple_history = june_ids & july_ids & august_ids

    progress.heartbeat(
        f"June={len(june_ids):,} July={len(july_ids):,} August={len(august_ids):,} Known={len(known):,}"
    )

    progress.stage(3, 5, "Select random August samples")

    seed = args.seed if args.seed is not None else secrets.randbits(64)
    rng = random.Random(seed)

    reasons: list[tuple[str, str]] = []
    bucket_ids: dict[str, list[str]] = defaultdict(list)

    for meter_id in sorted(august_ids):
        august_entry = month_entry(docs[meter_id], AUGUST)
        bucket_ids[category_bucket(august_entry.get("leakageCategory"))].append(meter_id)

    for number in range(1, 9):
        bucket = f"CAT{number}"
        ids = bucket_ids.get(bucket, [])
        take = min(args.per_category, len(ids))
        for meter_id in (rng.sample(ids, take) if take else []):
            reasons.append((f"{bucket} random", meter_id))

    normal_ids = bucket_ids.get("NORMAL", [])
    normal_take = min(args.normal_count, len(normal_ids))
    for meter_id in (rng.sample(normal_ids, normal_take) if normal_take else []):
        reasons.append(("NORMAL random", meter_id))

    for meter_id in SPECIAL_IDS[:2]:
        reasons.append(("FORMER JULY CREATE", meter_id))

    triple_ids = sorted(triple_history)
    triple_take = min(args.triple_history_count, len(triple_ids))
    for meter_id in rng.sample(triple_ids, triple_take):
        reasons.append(("JUNE+JULY+AUGUST history", meter_id))

    outside_august_ids = sorted(outside_august - {SPECIAL_IDS[2]})
    outside_take = min(args.outside_august_count, len(outside_august_ids))
    for meter_id in rng.sample(outside_august_ids, outside_take):
        reasons.append(("OUTSIDE AUGUST", meter_id))

    reasons.append(("OUTSIDE KNOWN predecessor", SPECIAL_IDS[2]))

    selected: dict[str, str] = {}
    for reason, meter_id in reasons:
        selected.setdefault(meter_id, reason)

    print(f"    random seed : {seed}", flush=True)
    for bucket in [*(f"CAT{n}" for n in range(1, 9)), "NORMAL"]:
        requested = args.normal_count if bucket == "NORMAL" else args.per_category
        sampled = sum(1 for reason in selected.values() if reason.startswith(bucket))
        print(f"    {bucket:6s} | available {len(bucket_ids.get(bucket, [])):5d} | requested {requested:2d} | sampled {sampled:2d}", flush=True)

    progress.stage(4, 5, "Fetch complete actual Firestore documents")

    sample_records: list[dict[str, Any]] = []
    total_selected = len(selected)

    for index, (meter_id, reason) in enumerate(selected.items(), start=1):
        snap = db.collection(COLLECTION).document(meter_id).get()
        if not snap.exists:
            fail(f"Selected document no longer exists: {meter_id}")
        payload = snap.to_dict() or {}
        sample_records.append({
            "sampleReason": reason,
            "documentId": meter_id,
            "createTime": iso_time(snap.create_time),
            "updateTime": iso_time(snap.update_time),
            "juneCategory": json_safe(month_entry(payload, JUNE)),
            "julyCategory": json_safe(month_entry(payload, JULY)),
            "augustCategory": json_safe(month_entry(payload, AUGUST)),
            "document": json_safe(payload),
        })
        progress.heartbeat(f"full read {index}/{total_selected}: {reason} {meter_id}")

    progress.stage(5, 5, "Write independent JSON and Markdown evidence")

    checks = {
        "juneActive": len(june_ids) == EXPECTED["juneActive"],
        "julyActive": len(july_ids) == EXPECTED["julyActive"],
        "augustActive": len(august_ids) == EXPECTED["augustActive"],
        "continuing": len(continuing) == EXPECTED["continuingJulyAugust"],
        "entered": len(entered) == EXPECTED["enteredAugust"],
        "exited": len(exited) == EXPECTED["exitedAfterJuly"],
        "known": len(known) == EXPECTED["knownThroughAugust"],
        "categoryCounts": all(category_counts.get(bucket, 0) == expected for bucket, expected in EXPECTED_CATEGORY_COUNTS.items()),
        "formerCreate1HasAugust": SPECIAL_IDS[0] in docs and month_entry(docs[SPECIAL_IDS[0]], AUGUST) is not None,
        "formerCreate2HasAugust": SPECIAL_IDS[1] in docs and month_entry(docs[SPECIAL_IDS[1]], AUGUST) is not None,
        "oldPredecessorOutsideKnown": SPECIAL_IDS[2] in outside_known,
    }

    verdict = "INDEPENDENT AUGUST DEV SAMPLE VERIFIED" if all(checks.values()) else "INDEPENDENT AUGUST DEV SAMPLE FAILED"

    result = {
        "verdict": verdict,
        "readOnly": True,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "projectId": PROJECT_ID,
        "collection": COLLECTION,
        "lmPcode": LM_PCODE,
        "scopeScanned": scanned,
        "juneActive": len(june_ids),
        "julyActive": len(july_ids),
        "augustActive": len(august_ids),
        "continuingJulyAugust": len(continuing),
        "enteredAugust": len(entered),
        "exitedAfterJuly": len(exited),
        "knownThroughAugust": len(known),
        "outsideAugustCount": len(outside_august),
        "outsideKnownCount": len(outside_known),
        "outsideKnownIds": sorted(outside_known),
        "tripleHistoryCount": len(triple_history),
        "categoryCounts": dict(category_counts),
        "randomSeed": seed,
        "sampleCount": len(sample_records),
        "sampleRecords": sample_records,
        "checks": checks,
        "firestoreWritesPerformed": 0,
    }

    repo_root = Path(__file__).resolve().parents[3]
    output_logs = repo_root / "output" / "logs"
    if not output_logs.is_dir():
        fail(f"Expected existing output logs directory missing: {output_logs}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_logs / f"independent_august_dev_sample__{stamp}.json"
    md_path = output_logs / f"independent_august_dev_sample__{stamp}.md"

    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# {verdict}",
        "",
        "**READ ONLY — actual Firestore documents.**",
        "",
        f"- Project: `{PROJECT_ID}`",
        f"- Collection: `{COLLECTION}`",
        f"- ZA5241 docs scanned: **{scanned:,}**",
        f"- June Active: **{len(june_ids):,}**",
        f"- July Active: **{len(july_ids):,}**",
        f"- August Active: **{len(august_ids):,}**",
        f"- Continuing July→August: **{len(continuing):,}**",
        f"- Entered August: **{len(entered)}**",
        f"- Exited after July: **{len(exited)}**",
        f"- Known through August: **{len(known):,}**",
        f"- Outside August: **{len(outside_august)}**",
        f"- Outside Known: **{len(outside_known)}** — `{', '.join(sorted(outside_known))}`",
        "",
        "## August category counts",
        "",
        "```json",
        json.dumps(dict(category_counts), indent=2),
        "```",
        "",
        "## Sampled actual documents",
        "",
    ]

    for record in sample_records:
        lines.extend([
            f"### {record['sampleReason']} — `{record['documentId']}`",
            "",
            f"- createTime: `{record['createTime']}`",
            f"- updateTime: `{record['updateTime']}`",
            "",
            "```json",
            json.dumps(record, indent=2, ensure_ascii=False),
            "```",
            "",
        ])

    lines.extend([
        "## Checks",
        "",
        "```json",
        json.dumps(checks, indent=2),
        "```",
        "",
        "Firestore writes performed: **0**",
    ])

    md_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n============================================================")
    print(verdict)
    print("============================================================")
    print(f"Scope scanned     : {scanned:,}")
    print(f"June Active       : {len(june_ids):,}")
    print(f"July Active       : {len(july_ids):,}")
    print(f"August Active     : {len(august_ids):,}")
    print(f"Continuing        : {len(continuing):,}")
    print(f"Entered August    : {len(entered)}")
    print(f"Exited after July : {len(exited)}")
    print(f"Known through Aug : {len(known):,}")
    print(f"Outside August    : {len(outside_august)}")
    print(f"Outside Known     : {len(outside_known)}")
    print(f"Samples           : {len(sample_records)}")
    print(f"JSON              : {json_path}")
    print(f"Markdown          : {md_path}")
    print("Firestore writes performed: 0")

    if not all(checks.values()):
        print("FAILED CHECKS:", [name for name, passed in checks.items() if not passed])
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
