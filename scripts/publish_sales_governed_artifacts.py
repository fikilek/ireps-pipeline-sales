"""Validate or publish an approved DEV artifact package, with publication last.

Default operation is offline validation. Remote execution requires --execute and
separately approved release evidence. No Firestore client is used. Existing blobs
are never overwritten and publication uses an exact generation precondition.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sales_release_publication import build_publication, require

BUCKET = "ireps2.appspot.com"
SERVICE_ACCOUNT = Path(r"C:\dev\secrets\ireps2-e72fd9dc94de.json")


def read_evidence(evidence):
    path = Path(evidence["path"])
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == evidence["sha256"], "Artifact SHA mismatch: " + str(path))
    return raw, json.loads(raw)


def validate_package(package):
    require(package.get("schemaVersion") == 1 and package.get("projectId") == "ireps2"
            and package.get("bucket") == BUCKET, "Only approved DEV project/bucket supported")
    require(package.get("lmPcode") == "ZA5241" and package.get("provider") == "contour"
            and package.get("baselineMonth") == "2026-06", "Release scope mismatch")
    generation = package.get("expectedPublicationGeneration")
    require(type(generation) is int and generation >= 0, "Exact prior publication generation required")
    previous = package.get("previousPublication")
    require((generation == 0 and previous is None) or (generation > 0 and isinstance(previous, dict)),
            "Prior generation and publication evidence must agree")
    entries, uploads = [], []
    prefix = f"governed-sales/{package['lmPcode']}/{package['provider']}"
    for evidence in package["months"]:
        snapshot_bytes, snapshot = read_evidence(evidence["snapshot"])
        report_bytes, report = read_evidence(evidence["report"])
        snapshot_hash, report_hash = evidence["snapshot"]["sha256"], evidence["report"]["sha256"]
        entries.append((snapshot, snapshot_hash, report, report_hash))
        uploads.extend([(f"{prefix}/snapshots/{snapshot_hash}.json", snapshot_bytes),
                        (f"{prefix}/reports/{report_hash}.json", report_bytes)])
    publication = build_publication(entries, project_id="ireps2", lm_pcode="ZA5241", provider="contour",
                                    baseline_month="2026-06")
    publication_bytes, supplied_publication = read_evidence(package["publication"])
    require(publication == supplied_publication, "Publication differs from verified monthly evidence")
    if previous is not None:
        previous_bytes, previous_publication = read_evidence(previous)
        require(previous_publication.get("projectId") == "ireps2"
                and previous_publication.get("lmPcode") == "ZA5241"
                and previous_publication.get("provider") == "contour", "Prior publication scope mismatch")
        prior_months = previous_publication.get("months")
        require(isinstance(prior_months, dict) and bool(prior_months)
                and all(publication["months"].get(month) == value for month, value in prior_months.items()),
                "Published history cannot shrink or change")
        require(publication["latestMonth"] >= previous_publication.get("latestMonth", ""),
                "Latest governed month cannot move backwards")
    else:
        previous_bytes = None
    return {"uploads": uploads, "publicationPath": f"{prefix}/publication.json",
            "publicationBytes": publication_bytes, "previousBytes": previous_bytes,
            "expectedGeneration": generation, "latestMonth": publication["latestMonth"]}


def publish_prepared(prepared, bucket, *, not_found, precondition_failed, progress=None):
    """Used only by explicit remote mode; tests inject a local fake bucket."""
    pointer = bucket.blob(prepared["publicationPath"])
    try:
        pointer.reload(timeout=20, retry=None)
        generation = int(pointer.generation)
    except not_found:
        generation = 0
    require(generation == prepared["expectedGeneration"], "Publication generation changed; nothing uploaded")
    if generation:
        previous = pointer.download_as_bytes(if_generation_match=generation, timeout=20, retry=None)
        require(previous == prepared["previousBytes"], "Remote prior publication bytes differ; nothing uploaded")
    uploaded, reused = 0, 0
    def record(status):
        if progress:
            progress({"result": status, "immutableUploads": uploaded, "immutableReused": reused,
                      "publicationWrites": 0, "firestoreWrites": 0})
    record("UPLOADING_IMMUTABLE_ARTIFACTS")
    for path, raw in prepared["uploads"]:
        blob = bucket.blob(path)
        try:
            blob.upload_from_string(raw, content_type="application/json", if_generation_match=0,
                                    timeout=20, retry=None)
            uploaded += 1
        except precondition_failed:
            # Identical immutable content is safe to reuse; disagreement is an
            # explicit failure. Never overwrite a supposedly addressed object.
            blob.reload(timeout=20, retry=None)
            existing = blob.download_as_bytes(if_generation_match=int(blob.generation), timeout=20, retry=None)
            require(existing == raw, "Immutable artifact content differs; publication unchanged")
            reused += 1
        record("UPLOADING_IMMUTABLE_ARTIFACTS")
    record("PUBLICATION_COMMIT_STARTED")
    pointer.upload_from_string(prepared["publicationBytes"], content_type="application/json",
                               if_generation_match=generation, timeout=20, retry=None)
    return {"result": "PUBLISHED", "projectId": "ireps2", "bucket": BUCKET,
            "latestMonth": prepared["latestMonth"], "immutableUploads": uploaded,
            "immutableReused": reused, "publicationWrites": 1, "firestoreWrites": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--service-account", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    require(not args.report.exists(), "Execution report already exists")
    _, package = read_evidence({"path": args.package, "sha256": args.package_sha256})
    prepared = validate_package(package)
    import os
    result = {"result": "STARTED", "projectId": "ireps2", "bucket": BUCKET,
              "packageSha256": args.package_sha256, "firestoreWrites": 0, "publicationWrites": 0}
    with args.report.open("x", encoding="utf-8") as handle:
        def persist(update):
            result.update(update)
            handle.seek(0)
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        persist({})
        try:
            if not args.execute:
                persist({"result": "OFFLINE_VALIDATED_NOT_PUBLISHED", "latestMonth": prepared["latestMonth"],
                         "storageWrites": 0})
            else:
                require(args.service_account is not None and args.service_account.resolve() == SERVICE_ACCOUNT.resolve(),
                        "The inspected DEV service-account path is required")
                require(not os.environ.get("FIRESTORE_EMULATOR_HOST") and not os.environ.get("STORAGE_EMULATOR_HOST"),
                        "Unexpected emulator environment")
                from google.oauth2 import service_account
                from google.cloud import storage
                from google.api_core.exceptions import NotFound, PreconditionFailed
                credential_data = json.loads(args.service_account.read_text(encoding="utf-8"))
                require(credential_data.get("project_id") == "ireps2", "Credential project mismatch")
                credentials = service_account.Credentials.from_service_account_info(credential_data)
                client = storage.Client(project="ireps2", credentials=credentials)
                persist(publish_prepared(prepared, client.bucket(BUCKET), not_found=NotFound,
                                         precondition_failed=PreconditionFailed, progress=persist))
        except Exception as exc:
            # A lost response to the final CAS is uncertain; do not automatically
            # retry or claim that publication definitely remained unchanged.
            persist({"result": "FAILED_REQUIRES_RECONCILIATION", "errorType": type(exc).__name__,
                     "publicationOutcomeUncertain": result.get("result") == "PUBLICATION_COMMIT_STARTED"})
            raise
    print(json.dumps(result))


if __name__ == "__main__":
    main()
