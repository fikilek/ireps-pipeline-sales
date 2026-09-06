"""Fingerprint-bound, offline Stage 14 planner. No Firestore client or writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sales_pipeline_sales_all_refresh import load_and_validate
from sales_sg_erf_plan import build_plan


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--erf-jsonl", required=True, type=Path)
    parser.add_argument("--erf-sha256", required=True)
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    actual = sha(args.erf_jsonl)
    if actual != args.erf_sha256.lower():
        raise ValueError("Authoritative ERF source hash mismatch")
    rows, evidence = load_and_validate(args.input, args.manifest)
    with args.erf_jsonl.open(encoding="utf-8-sig") as handle:
        plan = build_plan(rows, (json.loads(line) for line in handle if line.strip()), lm_pcode=args.lm_pcode)
    if sha(args.erf_jsonl) != actual:
        raise ValueError("Authoritative ERF source changed while planning")
    plan["sourceEvidence"] = {"sales": evidence, "erfPath": str(args.erf_jsonl.resolve()), "erfSha256": actual}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({k: plan[k] for k in ("status", "result", "recordsInspected", "authoritativeOneToOneCount", "exceptionCount", "firestoreWrites")}))
    print(f"Evidence: {args.output.resolve()}")


if __name__ == "__main__":
    main()
