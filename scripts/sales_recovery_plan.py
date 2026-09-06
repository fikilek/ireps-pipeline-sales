"""Conservative offline recovery proposals; this module never connects or writes remotely.

Only paths recorded in a fingerprinted execution plan may be restored. A fresh,
typed current-state export must still match each planned after-value; its update
time is an obligatory precondition for any separately authorized executor.
New documents require manual review and are never proposed for deletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def load_exact(path: Path, expected: str, *, lines: bool = False) -> Any:
    raw = path.read_bytes()
    require(re.fullmatch(r"[a-f0-9]{64}", expected) is not None
            and hashlib.sha256(raw).hexdigest() == expected, "Evidence SHA-256 mismatch: " + str(path))
    return [json.loads(line) for line in raw.splitlines() if line.strip()] if lines else json.loads(raw)


def index(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        identity = row.get("masterId")
        require(isinstance(identity, str) and re.fullmatch(r"[A-Z0-9]+", identity) is not None,
                "Invalid recovery identity")
        require(identity not in result, "Duplicate recovery identity")
        result[identity] = row
    return result


def value_at(document: dict, path: str) -> tuple[bool, Any]:
    # Only the writer's simple dotted paths are supported. Reject quoted or
    # escaped Firestore field paths rather than risk restoring a different key.
    parts = path.split(".")
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*|\d{4}-\d{2}", part) for part in parts),
            "Unsupported recovery field path")
    fields = document.get("fields", {})
    for offset, part in enumerate(parts):
        if part not in fields:
            return False, None
        value = fields[part]
        if offset == len(parts) - 1:
            return True, value
        require(isinstance(value, dict) and "mapValue" in value, "Non-map recovery path ancestor")
        fields = value["mapValue"].get("fields", {})
    raise ValueError("Empty recovery path")


def build_recovery(report: dict, before_rows: list[dict], plan_rows: list[dict],
                   current_rows: list[dict]) -> dict:
    require(report.get("projectId") == "ireps2" and report.get("collection") == "sales-all-meters",
            "Recovery is restricted to ireps2/sales-all-meters")
    require(report.get("preflightOnly") is False, "Preflight has no writes to recover")
    before, plans, current = index(before_rows), index(plan_rows), index(current_rows)
    require(set(before) == set(plans) == set(current), "Recovery scope mismatch")
    source = report.get("sourceEvidence", {})
    require(sorted(plans) == source.get("scopeDocumentIds"), "Report scope mismatch")
    require(source.get("scopeDocumentIdsSha256") == hashlib.sha256(
        json.dumps(sorted(plans), separators=(",", ":")).encode()).hexdigest(), "Scope fingerprint mismatch")
    for key in ("recoveryEvidence", "planEvidence"):
        evidence = report.get(key, {})
        require(evidence.get("complete") is True and evidence.get("records") == len(plans),
                "Incomplete recovery evidence")
    proposals, exceptions = [], []
    for identity in sorted(plans):
        plan, old, now = plans[identity], before[identity], current[identity]
        classification = plan.get("classification")
        if classification in ("UNCHANGED", "CONFLICT"):
            continue
        if classification == "CREATED":
            exceptions.append({"masterId": identity, "reason": "CREATED_DOCUMENT_REQUIRES_MANUAL_REVIEW_NO_DELETE"})
            continue
        require(classification == "UPDATED", "Unknown plan classification")
        if old.get("exists") is not True or now.get("exists") is not True:
            exceptions.append({"masterId": identity, "reason": "EXISTENCE_CHANGED"})
            continue
        old_doc, now_doc = old.get("document", {}), now.get("document", {})
        require(old_doc.get("updateTime") == plan.get("preconditionUpdateTime"), "Before-image version mismatch")
        require(isinstance(now_doc.get("updateTime"), str) and bool(now_doc["updateTime"]),
                "Current update-time precondition missing")
        patch = plan.get("afterPatch")
        require(isinstance(patch, dict) and bool(patch), "Updated plan must contain a patch")
        paths = sorted(patch)
        require(not any(right.startswith(left + ".") for left in paths for right in paths if left != right),
                "Overlapping recovery paths")
        restore, deletes, diverged = {}, [], []
        for path, expected_after in patch.items():
            exists, actual = value_at(now_doc, path)
            if not exists or actual != expected_after:
                diverged.append(path)
                continue
            was_present, old_value = value_at(old_doc, path)
            if was_present:
                restore[path] = old_value
            else:
                deletes.append(path)
        if diverged:
            exceptions.append({"masterId": identity, "reason": "CURRENT_STATE_DIFFERS_FROM_PLANNED_WRITE",
                               "paths": sorted(diverged)})
            continue
        proposals.append({"masterId": identity, "preconditionUpdateTime": now_doc["updateTime"],
                          "restoreFields": restore, "deleteFields": sorted(deletes)})
    return {"schemaVersion": 1, "projectId": "ireps2", "collection": "sales-all-meters",
            "status": "REVIEW_REQUIRED", "result": "OFFLINE_RECOVERY_PROPOSAL",
            "firestoreWrites": 0, "storageWrites": 0, "automaticExecution": False,
            "scopeRecordCount": len(plans), "proposals": proposals, "exceptions": exceptions}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report", "before", "plan", "current"):
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = load_exact(args.report, args.report_sha256)
    require(report.get("recoveryEvidence", {}).get("sha256") == args.before_sha256, "Report/before SHA mismatch")
    require(report.get("planEvidence", {}).get("sha256") == args.plan_sha256, "Report/plan SHA mismatch")
    result = build_recovery(report, load_exact(args.before, args.before_sha256, lines=True),
                            load_exact(args.plan, args.plan_sha256, lines=True),
                            load_exact(args.current, args.current_sha256, lines=True))
    result["evidence"] = {name: {"path": str(getattr(args, name).resolve()),
                                   "sha256": getattr(args, name + "_sha256")}
                          for name in ("report", "before", "plan", "current")}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"result": result["result"], "proposals": len(result["proposals"]),
                      "exceptions": len(result["exceptions"]), "firestoreWrites": 0}))


if __name__ == "__main__":
    main()
