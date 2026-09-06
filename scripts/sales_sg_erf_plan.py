"""Read-only Stage 14 evidence planning; never infer or write a Sales parcel.

The ERF pipeline parcel key is retained under its own source name. It is not
silently renamed to Sales sgCode: that destination projection needs a governed
contract. Existing candidate lists are evidence, not sufficient authority alone.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


def build_plan(rows: Iterable[Mapping[str, Any]], erfs: Iterable[Mapping[str, Any]], *, lm_pcode: str) -> dict[str, Any]:
    by_id: dict[str, dict[str, str]] = {}
    keys: Counter[str] = Counter()
    for erf in erfs:
        erf_id = str(erf.get("erfId") or "").strip()
        if not erf_id or erf_id in by_id:
            raise ValueError("Missing or duplicate authoritative erfId")
        sg = erf.get("sg") or {}
        lm = ((erf.get("admin") or {}).get("localMunicipality") or {}).get("pcode")
        if lm != lm_pcode:
            raise ValueError("Authoritative ERF source crosses the approved LM scope")
        if not isinstance(sg, Mapping):
            raise ValueError("Malformed authoritative ERF sg map")
        record = {"erfId": erf_id, "sourceParcelKey": str(sg.get("prclKey") or "").strip(),
                  "erfNo": str(sg.get("erfNo") if sg.get("erfNo") is not None else "").strip()}
        by_id[erf_id] = record
        if record["sourceParcelKey"]:
            keys[record["sourceParcelKey"]] += 1

    resolved, exceptions, seen = [], [], set()
    for row in rows:
        meter = str(row.get("masterId") or "")
        expected = row.get("expected") or {}
        if not meter or meter in seen:
            raise ValueError("Missing or duplicate canonical Sales identity")
        seen.add(meter)
        if expected.get("lmPcode") != lm_pcode:
            raise ValueError("Sales input crosses the approved LM scope")
        candidates = expected.get("erfCandidates") or []
        reason = None
        source = None
        if not isinstance(candidates, list) or len(candidates) != 1:
            reason = "NO_SINGLE_AUTHORITATIVE_CANDIDATE"
        elif not isinstance(candidates[0], Mapping):
            reason = "MALFORMED_CANDIDATE"
        elif expected.get("hasUsableGps") is not True:
            reason = "SOURCE_GPS_NOT_VALIDATED"
        else:
            candidate = candidates[0]
            source = by_id.get(str(candidate.get("ErfId") or ""))
            if source is None:
                reason = "CANDIDATE_NOT_IN_AUTHORITATIVE_ERF_SOURCE"
            elif not source["sourceParcelKey"] or not source["erfNo"]:
                reason = "AUTHORITATIVE_PARCEL_FIELDS_INCOMPLETE"
            elif keys[source["sourceParcelKey"]] != 1:
                reason = "AMBIGUOUS_AUTHORITATIVE_PARCEL_KEY"
            elif candidate.get("LmPcode") != lm_pcode:
                reason = "CANDIDATE_LM_MISMATCH"
            elif str(candidate.get("ErfNumber") or "").strip() != source["erfNo"]:
                reason = "CANDIDATE_ERF_NUMBER_MISMATCH"
        if reason:
            exceptions.append({"masterId": meter, "reason": reason, "candidateEvidence": candidates})
        else:
            resolved.append({"masterId": meter, **source})
    if len(resolved) + len(exceptions) != len(seen):
        raise ValueError("Stage 14 classification accounting imbalance")
    return {
        "stage": "14", "operation": "sales_sg_erf_evidence_plan", "lmPcode": lm_pcode,
        "status": "BLOCKED", "result": "PROJECTION_CONTRACT_REQUIRED",
        "recordsInspected": len(seen), "authoritativeOneToOneCount": len(resolved),
        "exceptionCount": len(exceptions), "reasonCounts": dict(Counter(e["reason"] for e in exceptions)),
        "authoritativeResolutions": resolved, "exceptions": exceptions,
        "proposedFirestoreWrites": [], "firestoreReads": 0, "firestoreWrites": 0,
        "blockers": ["Approve the canonical source-to-sgCode/erfNo projection before producing write operations.",
                     "Live existing-field/metadata classification and recovery evidence are required before execution."],
        "sourceParcelKeyIsNotSalesSgCode": True,
    }
