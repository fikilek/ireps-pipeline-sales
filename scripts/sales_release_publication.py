"""Build a local publication proposal only from verified executed-month evidence.

No Firestore or Storage APIs are imported. Uploading the proposal is a separate
release action; snapshots/reports use content-addressed create-only objects and
publication.json must use a generation precondition against its prior version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

SHA = re.compile(r"^[a-f0-9]{64}$")
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def next_month(month: str) -> str:
    year, number = map(int, month.split("-"))
    return f"{year + (number == 12):04d}-{1 if number == 12 else number + 1:02d}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_executed_month(snapshot: Mapping[str, Any], report: Mapping[str, Any], *,
                            snapshot_sha: str, project_id: str, lm_pcode: str,
                            provider: str) -> str:
    require(isinstance(snapshot, Mapping) and isinstance(report, Mapping), "Snapshot/report must be objects")
    month = snapshot.get("month")
    require(isinstance(month, str) and MONTH.fullmatch(month) is not None, "Invalid snapshot month")
    require(SHA.fullmatch(snapshot_sha) is not None, "Invalid snapshot SHA")
    require(snapshot.get("schemaVersion") == 1 and snapshot.get("lmPcode") == lm_pcode
            and snapshot.get("provider") == provider, "Snapshot scope/schema mismatch")
    members = snapshot.get("members")
    require(isinstance(members, list) and len(members) > 0
            and all(isinstance(m, str) and re.fullmatch(r"[A-Z0-9]+", m) for m in members)
            and len(set(members)) == len(members), "Snapshot members must be exact unique canonical IDs")
    require(members == sorted(members), "Snapshot members must be sorted")
    require(SHA.fullmatch(str(snapshot.get("sourceSha256", ""))) is not None,
            "Snapshot source fingerprint required")
    require(isinstance(snapshot.get("replacements"), list) and isinstance(snapshot.get("exceptions"), list),
            "Snapshot transition/exception evidence must be explicit lists")
    seen_predecessors, seen_successors = set(), set()
    for replacement in snapshot["replacements"]:
        require(isinstance(replacement, Mapping), "Replacement must be an object")
        predecessor, successor = replacement.get("predecessor"), replacement.get("successor")
        require(all(isinstance(identity, str) and re.fullmatch(r"[A-Z0-9]+", identity)
                    for identity in (predecessor, successor)), "Replacement identities must be canonical")
        require(predecessor != successor and predecessor not in members and successor in members
                and predecessor not in seen_predecessors and successor not in seen_successors,
                "Replacement must be one-to-one and terminate in the current membership")
        seen_predecessors.add(predecessor)
        seen_successors.add(successor)
    for exception in snapshot["exceptions"]:
        require(isinstance(exception, Mapping) and isinstance(exception.get("meterId"), str)
                and re.fullmatch(r"[A-Z0-9]+", exception["meterId"])
                and isinstance(exception.get("reason"), str) and bool(exception["reason"].strip()),
                "Population exception needs a canonical identity and explicit reason")
    completeness = snapshot.get("completeness") or {}
    require(isinstance(completeness, Mapping) and completeness.get("complete") is True and SHA.fullmatch(str(completeness.get("evidenceSha256", ""))) is not None,
            "Supplier membership completeness is not attested")
    for key, value in {"projectId": project_id, "collection": "sales-all-meters", "status": "PASS", "result": "REFRESH_VERIFIED"}.items():
        require(report.get(key) == value, f"Executed report {key} mismatch")
    require(report.get("preflightOnly") is False, "Preflight is not executed-month evidence")
    gate = report.get("globalPreflight") or {}
    require(isinstance(gate, Mapping) and gate.get("complete") is True and gate.get("gatePassed") is True, "Global gate did not pass")
    source = report.get("sourceEvidence") or {}
    require(isinstance(source, Mapping) and source.get("governedMonth") == month and source.get("lmPcode") == lm_pcode
            and source.get("provider") == provider and source.get("populationSnapshotSha256") == snapshot_sha,
            "Write report is not bound to this exact monthly population snapshot")
    require(SHA.fullmatch(str(source.get("categoryPackageSha256", ""))) is not None,
            "Missing category package fingerprint")
    count_fields = ("rowsRead", "recordsInspected", "createdCount", "updatedCount", "unchangedCount",
                    "conflictCount", "failedCount", "writeAttemptCount", "writeSuccessCount", "firestoreWrites")
    require(all(type(report.get(k)) is int and report[k] >= 0 for k in count_fields), "Invalid report counters")
    require(report["rowsRead"] > 0 and report["recordsInspected"] == report["rowsRead"], "Incomplete classification")
    scope = source.get("scopeDocumentIds")
    require(isinstance(scope, list) and all(isinstance(m, str) and re.fullmatch(r"[A-Z0-9]+", m) for m in scope)
            and len(set(scope)) == len(scope) and scope == sorted(scope)
            and len(scope) == source.get("scopeRecordCount") == report["rowsRead"], "Report intended scope is incomplete")
    scope_sha = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
    require(source.get("scopeDocumentIdsSha256") == scope_sha, "Report intended-scope hash mismatch")
    exceptional = source.get("categoryExceptionDocumentIds", [])
    require(isinstance(exceptional, list) and all(isinstance(m, str) and re.fullmatch(r"[A-Z0-9]+", m) for m in exceptional)
            and len(set(exceptional)) == len(exceptional) and not (set(exceptional) & set(scope)),
            "Malformed/overlapping category exception identities")
    require(set(members) <= set(scope) | set(exceptional), "Snapshot includes unaccounted Sales identities")
    require(report["conflictCount"] == report["failedCount"] == 0, "Run has conflicts/failures")
    require(sum(report[k] for k in ("createdCount", "updatedCount", "unchangedCount")) == report["rowsRead"],
            "Classification accounting imbalance")
    writes = report["createdCount"] + report["updatedCount"]
    require(report["writeAttemptCount"] == report["writeSuccessCount"] == report["firestoreWrites"] == writes,
            "Write accounting imbalance")
    batch = report.get("batchEvidence") or {}
    require(isinstance(batch, Mapping) and batch.get("failedWriteWave") is None
            and all(type(batch.get(k)) is int and batch[k] >= 0 for k in ("writeWavesAttempted", "writeWavesCommitted"))
            and batch["writeWavesAttempted"] == batch["writeWavesCommitted"]
            and ((writes == 0 and batch["writeWavesCommitted"] == 0)
                 or (writes > 0 and 1 <= batch["writeWavesCommitted"] <= writes
                     and writes <= 400 * batch["writeWavesCommitted"])), "Partial/invalid write run cannot be published")
    verification = report.get("verification") or {}
    require(isinstance(verification, Mapping) and verification.get("status") == "PASS" and verification.get("documentsVerified") == report["rowsRead"],
            "Full post-write verification is required")
    before_hash = str(verification.get("preservedProjectionBeforeSha256", ""))
    require(SHA.fullmatch(before_hash) is not None
            and verification.get("preservedProjectionAfterSha256") == before_hash
            and verification.get("preservationVerifiedExistingDocuments") == report["rowsRead"] - report["createdCount"],
            "Existing history/operational preservation is not fully verified")
    recovery = report.get("recoveryEvidence") or {}
    require(isinstance(recovery, Mapping) and recovery.get("complete") is True
            and recovery.get("records") == report["rowsRead"]
            and SHA.fullmatch(str(recovery.get("sha256", ""))) is not None,
            "Complete fingerprinted recovery evidence is required")
    plan = report.get("planEvidence") or {}
    require(isinstance(plan, Mapping) and plan.get("complete") is True
            and plan.get("records") == report["rowsRead"]
            and SHA.fullmatch(str(plan.get("sha256", ""))) is not None
            and plan.get("scopeDocumentIdsSha256") == source["scopeDocumentIdsSha256"]
            and plan.get("categoryPackageSha256") == source["categoryPackageSha256"],
            "Complete scope-bound planned-write evidence is required")
    return month


def build_publication(entries: Sequence[tuple[Mapping[str, Any], str, Mapping[str, Any], str]], *,
                      project_id: str, lm_pcode: str, provider: str, baseline_month: str) -> dict[str, Any]:
    require(project_id == "ireps2", "This release proposal is DEV ireps2 only")
    require(MONTH.fullmatch(baseline_month) is not None and bool(entries), "Baseline and monthly evidence required")
    months: dict[str, Any] = {}
    expected_month = baseline_month
    previous_sha = None
    known_members: set[str] = set()
    prior_members: set[str] = set()
    for snapshot, snapshot_sha, report, report_sha in entries:
        month = validate_executed_month(snapshot, report, snapshot_sha=snapshot_sha,
                                        project_id=project_id, lm_pcode=lm_pcode, provider=provider)
        require(month == expected_month, "Publication must begin at baseline and progress one month at a time")
        require(snapshot.get("previousSnapshotSha256") == previous_sha, "Broken monthly snapshot chain")
        for replacement in snapshot["replacements"]:
            require(replacement["predecessor"] in prior_members and replacement["successor"] not in known_members,
                    "Replacement must remove an active predecessor and introduce a previously unseen successor")
        require(SHA.fullmatch(report_sha) is not None, "Invalid report SHA")
        months[month] = {"snapshotSha256": snapshot_sha,
                         "verification": {"status": "PASS", "complete": True, "reportSha256": report_sha}}
        previous_sha = snapshot_sha
        prior_members = set(snapshot["members"])
        known_members.update(prior_members)
        expected_month = next_month(month)
    return {"schemaVersion": 1, "projectId": project_id, "lmPcode": lm_pcode, "provider": provider,
            "latestMonth": month, "months": months}


def read_hashed(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, choices=["ireps2"])
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--baseline-month", required=True)
    parser.add_argument("--snapshot", action="append", required=True, type=Path)
    parser.add_argument("--verified-report", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require(len(args.snapshot) == len(args.verified_report), "Each snapshot requires one verified report")
    entries = [(*read_hashed(snapshot), *read_hashed(report)) for snapshot, report in zip(args.snapshot, args.verified_report)]
    publication = build_publication(entries, project_id=args.project_id, lm_pcode=args.lm_pcode,
                                    provider=args.provider, baseline_month=args.baseline_month)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(publication, handle, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps({"result": "LOCAL_PUBLICATION_PROPOSAL", "latestMonth": publication["latestMonth"],
                      "firestoreWrites": 0, "storageWrites": 0, "path": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
