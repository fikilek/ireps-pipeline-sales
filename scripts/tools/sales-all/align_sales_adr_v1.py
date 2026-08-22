"""ADR-only alignment for existing iREPS Sales All Meters documents.

This tool aligns only the canonical Firestore ``adr`` map from one frozen,
approved ZA5241 Stage 06 artifact. It never creates or deletes documents and
never writes any field other than the root ``adr`` map.

Governance:
- dry-run/preflight is the default;
- only ireps-test and ireps-5c3e9 are allowed targets;
- the project ID must be repeated and match the service account;
- execution requires an environment-specific confirmation token;
- execution is blocked unless Git is on clean ``main`` and HEAD == origin/main;
- all source/manifest/address-enrichment contracts are frozen;
- target documents are read in waves of at most 400;
- every write is an ``adr``-only batch.update with LastUpdateOption;
- existing differing/noncanonical adr is a conflict and is never overwritten;
- any non-ADR pipeline delta is a conflict;
- no per-document write fallback is permitted;
- post-write verification proves adr and all non-ADR fields were preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

COLLECTION = "sales-all-meters"
FIRESTORE_BATCH_SIZE = 400
EXPECTED_LM_PCODE = "ZA5241"
EXPECTED_ROWS = 10_216
EXPECTED_ADDRESS_ENRICHED = 10_117
EXPECTED_ADDRESS_UNRESOLVED = 99
EXPECTED_CSV_SHA256 = "1a5a7314547a239e4d7015579a5e8f3ba86d90d827e70fc7afc6c96b5a589cb2"
DEFAULT_INPUT = Path(
    "output/sales_all_meters/"
    "sales_all_meters__ZA5241__FULL__2023-12_to_2026-06.csv"
)
DEFAULT_MANIFEST = Path(
    "output/sales_all_meters/"
    "sales_all_meters__ZA5241__FULL__2023-12_to_2026-06.manifest.json"
)
DEFAULT_REPORT_DIR = Path("scripts/tools/sales-all/reports")
EXECUTE_TOKENS = {
    "ireps-test": "ALIGN_SALES_ADR_IREPS_TEST_ZA5241",
    "ireps-5c3e9": "ALIGN_SALES_ADR_IREPS_5C3E9_ZA5241",
}
ALLOWED_MASTER_VISIBILITIES = {"VISIBLE", "INVISIBLE"}
ADDRESS_KEYS = frozenset({"strNo", "strName", "strType"})
ProgressCallback = Callable[[str], None]


def _console_progress(message: str) -> None:
    """Emit operational progress immediately; long Firestore phases must never be silent."""
    print(message, flush=True)


def _emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _wave_total(item_count: int) -> int:
    return (item_count + FIRESTORE_BATCH_SIZE - 1) // FIRESTORE_BATCH_SIZE


@dataclass(frozen=True)
class SourceContract:
    csv_sha256: str
    manifest_sha256: str
    rows: int
    enriched_rows: int
    unresolved_rows: int
    lm_pcode: str


@dataclass
class AlignmentStats:
    rows: int
    inspected: int = 0
    missing_documents: int = 0
    update_missing_adr: int = 0
    matching_adr: int = 0
    adr_conflicts: int = 0
    non_adr_conflicts: int = 0
    identity_conflicts: int = 0
    read_waves: int = 0
    write_waves_attempted: int = 0
    write_waves_committed: int = 0
    writes_attempted: int = 0
    writes_succeeded: int = 0
    verification_read_waves: int = 0
    maximum_write_operations_in_any_batch: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def conflict_count(self) -> int:
        return (
            self.missing_documents
            + self.adr_conflicts
            + self.non_adr_conflicts
            + self.identity_conflicts
        )


@dataclass(frozen=True)
class PlannedUpdate:
    master_id: str
    expected_adr: dict[str, str]
    update_time: Any
    preserved_hash: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align only canonical Sales adr maps from the frozen ZA5241 Stage 06 artifact."
    )
    parser.add_argument("--project-id", required=True, choices=tuple(EXECUTE_TOKENS))
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform adr-only Firestore writes. Without this flag the tool is read-only.",
    )
    parser.add_argument(
        "--confirm-execute",
        default="",
        help="Required exact environment token when --execute is supplied.",
    )
    return parser.parse_args()


def resolve_repo_path(path: Path) -> Path:
    return (path if path.is_absolute() else PROJECT_ROOT / path).expanduser().resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _strict_equal(left: Any, right: Any) -> bool:
    if right is None:
        return left is None
    if isinstance(right, bool):
        return isinstance(left, bool) and left is right
    if isinstance(right, int) and not isinstance(right, bool):
        return isinstance(left, int) and not isinstance(left, bool) and left == right
    if isinstance(right, float):
        return isinstance(left, float) and left == right
    if isinstance(right, str):
        return isinstance(left, str) and left == right
    if isinstance(right, list):
        return (
            isinstance(left, list)
            and len(left) == len(right)
            and all(_strict_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(right, Mapping):
        return (
            isinstance(left, Mapping)
            and set(left.keys()) == set(right.keys())
            and all(_strict_equal(left[key], right[key]) for key in right)
        )
    return type(left) is type(right) and left == right


def _hashable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _hashable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_hashable(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if hasattr(value, "path"):
        return {"__reference__": str(getattr(value, "path"))}
    if hasattr(value, "latitude") and hasattr(value, "longitude"):
        return {
            "__geopoint__": [
                float(getattr(value, "latitude")),
                float(getattr(value, "longitude")),
            ]
        }
    if hasattr(value, "isoformat"):
        try:
            return {"__datetime__": value.isoformat()}
        except Exception:
            pass
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"__type__": type(value).__name__, "__str__": str(value)}


def _preserved_hash(payload: Mapping[str, Any]) -> str:
    projection = {key: value for key, value in payload.items() if key != "adr"}
    encoded = json.dumps(
        _hashable(projection),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunks(items: Sequence[Any], size: int = FIRESTORE_BATCH_SIZE):
    if size <= 0 or size > FIRESTORE_BATCH_SIZE:
        raise ValueError(f"Governed Firestore wave size must be 1..{FIRESTORE_BATCH_SIZE}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def validate_project_gate(
    project_id: str,
    confirm_project: str,
    execute: bool,
    confirm_execute: str,
) -> None:
    if project_id not in EXECUTE_TOKENS:
        raise ValueError(f"Project is not approved for ADR alignment: {project_id}")
    if project_id != confirm_project:
        raise ValueError("Project confirmation mismatch")
    if execute and confirm_execute != EXECUTE_TOKENS[project_id]:
        raise ValueError(
            "Execution confirmation token mismatch; expected the governed token for the target project"
        )
    if not execute and confirm_execute:
        raise ValueError("--confirm-execute may only be used together with --execute")


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def validate_execution_context_values(
    *,
    branch: str,
    status_porcelain: str,
    head: str,
    origin_main: str,
) -> None:
    if branch != "main":
        raise ValueError("Execution is permitted only from Git branch main")
    if status_porcelain.strip():
        raise ValueError("Execution requires a clean Git working tree")
    if not head or head != origin_main:
        raise ValueError("Execution requires local main HEAD to equal origin/main")


def enforce_git_execution_gate() -> dict[str, str]:
    branch = _git_output("branch", "--show-current")
    status = _git_output("status", "--porcelain")
    head = _git_output("rev-parse", "HEAD")
    origin_main = _git_output("rev-parse", "origin/main")
    validate_execution_context_values(
        branch=branch,
        status_porcelain=status,
        head=head,
        origin_main=origin_main,
    )
    return {"branch": branch, "head": head, "originMain": origin_main}


def validate_frozen_source(input_path: Path, manifest_path: Path) -> SourceContract:
    if not input_path.is_file():
        raise FileNotFoundError(f"Frozen Sales CSV is missing: {input_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Stage 06 manifest is missing: {manifest_path}")

    csv_sha = _sha256(input_path)
    if csv_sha != EXPECTED_CSV_SHA256:
        raise ValueError(
            f"Frozen Sales CSV SHA mismatch: expected {EXPECTED_CSV_SHA256}, got {csv_sha}"
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError("Stage 06 manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError("Stage 06 manifest must contain one JSON object")

    if manifest.get("schemaVersion") != 2:
        raise ValueError("Expected Stage 06 schemaVersion 2")
    if manifest.get("stage") != "06":
        raise ValueError("Expected Stage 06 manifest")
    if manifest.get("status") != "PASS":
        raise ValueError("Stage 06 manifest status is not PASS")

    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping):
        raise ValueError("Stage 06 source/output contract is incomplete")
    if source.get("lmPcode") != EXPECTED_LM_PCODE:
        raise ValueError(f"Expected LM {EXPECTED_LM_PCODE}")
    if output.get("rows") != EXPECTED_ROWS:
        raise ValueError(f"Expected exactly {EXPECTED_ROWS} Sales rows")
    if _safe_text(output.get("sha256")).lower() != EXPECTED_CSV_SHA256:
        raise ValueError("Manifest CSV SHA differs from frozen CSV")

    adr = output.get("addressEnrichment")
    if not isinstance(adr, Mapping) or adr.get("enabled") is not True:
        raise ValueError("Address enrichment is not enabled")
    if adr.get("enrichedRows") != EXPECTED_ADDRESS_ENRICHED:
        raise ValueError(f"Expected {EXPECTED_ADDRESS_ENRICHED} enriched address rows")
    if adr.get("unresolvedRows") != EXPECTED_ADDRESS_UNRESOLVED:
        raise ValueError(f"Expected {EXPECTED_ADDRESS_UNRESOLVED} unresolved address rows")
    if adr.get("firestoreProjection") != "adr":
        raise ValueError("Address Firestore projection must be adr")
    if adr.get("rawAddressMutationCount") != 0:
        raise ValueError("Address contract reports raw source-address mutation")
    if adr.get("fabricatedSpatialRelationshipCount") != 0:
        raise ValueError("Address contract reports fabricated spatial relationships")
    if list(adr.get("stagingColumns") or []) != ["strNo", "strName", "strType"]:
        raise ValueError("Address staging columns are not canonical")

    return SourceContract(
        csv_sha256=csv_sha,
        manifest_sha256=_sha256(manifest_path),
        rows=EXPECTED_ROWS,
        enriched_rows=EXPECTED_ADDRESS_ENRICHED,
        unresolved_rows=EXPECTED_ADDRESS_UNRESOLVED,
        lm_pcode=EXPECTED_LM_PCODE,
    )


def _load_expected_rows(input_path: Path, manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Reuse the canonical Stage 08 rich-contract loader. It validates the full
    # frozen artifact and constructs the exact expected pipeline-owned document
    # shape. This ADR tool then narrows the write scope to adr only.
    from sales_pipeline_sales_all_refresh import load_and_validate

    rows, evidence = load_and_validate(input_path, manifest_path)
    if len(rows) != EXPECTED_ROWS:
        raise ValueError(f"Canonical Stage 08 loader returned {len(rows)} rows, expected {EXPECTED_ROWS}")
    ids = [str(item.get("masterId", "")) for item in rows]
    if len(set(ids)) != EXPECTED_ROWS or any(not item for item in ids):
        raise ValueError("Canonical Stage 08 loader returned duplicate/blank document IDs")
    return rows, evidence


def _load_address_validators():
    from sales_address_enrichment import validate_address_values

    return validate_address_values


def classify_payload(
    *,
    master_id: str,
    payload: Mapping[str, Any] | None,
    expected: Mapping[str, Any],
    update_time: Any = None,
) -> dict[str, Any]:
    if payload is None:
        return {
            "masterId": master_id,
            "classification": "MISSING_DOCUMENT",
            "reason": "target document does not exist",
        }

    master = payload.get("master")
    expected_master = expected.get("master")
    if not isinstance(master, Mapping) or not isinstance(expected_master, Mapping):
        return {
            "masterId": master_id,
            "classification": "IDENTITY_CONFLICT",
            "reason": "master missing/not object",
        }
    if master.get("id") != expected_master.get("id") or master.get("id") != master_id:
        return {
            "masterId": master_id,
            "classification": "IDENTITY_CONFLICT",
            "reason": "master.id differs",
        }
    if master.get("visibility") not in ALLOWED_MASTER_VISIBILITIES:
        return {
            "masterId": master_id,
            "classification": "IDENTITY_CONFLICT",
            "reason": "master.visibility invalid",
        }
    for field in ("meterNoNormalized", "provider", "lmPcode"):
        existing_value = payload.get(field)
        expected_value = expected.get(field)
        if existing_value in (None, "") or existing_value != expected_value:
            return {
                "masterId": master_id,
                "classification": "IDENTITY_CONFLICT",
                "reason": f"{field} missing/differs",
            }
    if any(field in payload for field in ("strNo", "strName", "strType")):
        return {
            "masterId": master_id,
            "classification": "IDENTITY_CONFLICT",
            "reason": "root strNo/strName/strType is prohibited; canonical address belongs in adr",
        }

    non_adr_deltas: list[str] = []
    for field, wanted in expected.items():
        if field in {"master", "adr"}:
            continue
        if not _strict_equal(payload.get(field), wanted):
            non_adr_deltas.append(field)
    if non_adr_deltas:
        return {
            "masterId": master_id,
            "classification": "NON_ADR_CONFLICT",
            "reason": "non-ADR pipeline fields differ",
            "fields": sorted(non_adr_deltas),
        }

    wanted_adr = expected.get("adr")
    if not isinstance(wanted_adr, Mapping) or set(wanted_adr.keys()) != ADDRESS_KEYS:
        raise ValueError(f"Expected canonical adr is invalid for {master_id}")

    if "adr" not in payload:
        if update_time is None:
            raise ValueError(f"Missing update_time for existing document {master_id}")
        return {
            "masterId": master_id,
            "classification": "UPDATE_MISSING_ADR",
            "expectedAdr": dict(wanted_adr),
            "updateTime": update_time,
            "preservedHash": _preserved_hash(payload),
        }

    current_adr = payload.get("adr")
    if not isinstance(current_adr, Mapping) or set(current_adr.keys()) != ADDRESS_KEYS:
        return {
            "masterId": master_id,
            "classification": "ADR_CONFLICT",
            "reason": "existing adr is not the exact canonical 3-field map",
        }
    try:
        validate_address_values = _load_address_validators()
        validate_address_values(
            current_adr.get("strNo"), current_adr.get("strName"), current_adr.get("strType")
        )
    except ValueError as exc:
        return {
            "masterId": master_id,
            "classification": "ADR_CONFLICT",
            "reason": f"existing adr is noncanonical: {exc}",
        }

    if _strict_equal(current_adr, wanted_adr):
        return {
            "masterId": master_id,
            "classification": "MATCHING_ADR",
            "preservedHash": _preserved_hash(payload),
        }
    return {
        "masterId": master_id,
        "classification": "ADR_CONFLICT",
        "reason": "existing canonical adr differs from frozen expected adr",
    }


def _snapshot_path(snapshot: Any) -> str:
    reference = getattr(snapshot, "reference", None)
    path = getattr(reference, "path", None)
    if path:
        return str(path)
    return f"{COLLECTION}/{getattr(snapshot, 'id', '')}"


def _bulk_snapshots(db: Any, refs: Sequence[Any]) -> dict[str, Any]:
    if len(refs) > FIRESTORE_BATCH_SIZE:
        raise ValueError("Bulk read exceeds governed 400-reference limit")
    return {_snapshot_path(snapshot): snapshot for snapshot in db.get_all(list(refs))}


def _snapshot_for(by_path: Mapping[str, Any], ref: Any) -> Any | None:
    path = getattr(ref, "path", None) or f"{COLLECTION}/{getattr(ref, 'id', '')}"
    return by_path.get(str(path))


def _record_conflict(stats: AlignmentStats, decision: Mapping[str, Any]) -> None:
    classification = str(decision["classification"])
    if classification == "MISSING_DOCUMENT":
        stats.missing_documents += 1
    elif classification == "ADR_CONFLICT":
        stats.adr_conflicts += 1
    elif classification == "NON_ADR_CONFLICT":
        stats.non_adr_conflicts += 1
    elif classification == "IDENTITY_CONFLICT":
        stats.identity_conflicts += 1
    else:
        raise ValueError(f"Not a conflict classification: {classification}")
    if len(stats.conflicts) < 200:
        stats.conflicts.append(dict(decision))


def preflight_target(
    *,
    db: Any,
    expected_rows: Sequence[Mapping[str, Any]],
    progress: ProgressCallback | None = None,
) -> tuple[AlignmentStats, list[PlannedUpdate]]:
    collection = db.collection(COLLECTION)
    stats = AlignmentStats(rows=len(expected_rows))
    plans: list[PlannedUpdate] = []
    rows = list(expected_rows)
    total_waves = _wave_total(len(rows))

    for wave_index, wave in enumerate(_chunks(rows), start=1):
        start_doc = stats.inspected + 1
        end_doc = min(stats.inspected + len(wave), stats.rows)
        _emit_progress(
            progress,
            f"PREFLIGHT READ wave {wave_index}/{total_waves}: "
            f"documents {start_doc:,}-{end_doc:,} of {stats.rows:,}...",
        )
        refs = [collection.document(str(item["masterId"])) for item in wave]
        snapshots = _bulk_snapshots(db, refs)
        stats.read_waves += 1
        for item, ref in zip(wave, refs):
            master_id = str(item["masterId"])
            snapshot = _snapshot_for(snapshots, ref)
            payload = None
            update_time = None
            if snapshot is not None and getattr(snapshot, "exists", False):
                payload = snapshot.to_dict() or {}
                update_time = getattr(snapshot, "update_time", None)
            decision = classify_payload(
                master_id=master_id,
                payload=payload,
                expected=item["expected"],
                update_time=update_time,
            )
            stats.inspected += 1
            classification = decision["classification"]
            if classification == "UPDATE_MISSING_ADR":
                stats.update_missing_adr += 1
                plans.append(
                    PlannedUpdate(
                        master_id=master_id,
                        expected_adr=dict(decision["expectedAdr"]),
                        update_time=decision["updateTime"],
                        preserved_hash=str(decision["preservedHash"]),
                    )
                )
            elif classification == "MATCHING_ADR":
                stats.matching_adr += 1
            else:
                _record_conflict(stats, decision)

        _emit_progress(
            progress,
            f"PREFLIGHT PROGRESS {stats.inspected:,}/{stats.rows:,}: "
            f"updates={stats.update_missing_adr:,} matching={stats.matching_adr:,} "
            f"conflicts={stats.conflict_count:,}",
        )

    if stats.inspected != stats.rows:
        raise RuntimeError(f"Preflight accounting mismatch: inspected={stats.inspected}, rows={stats.rows}")
    if stats.update_missing_adr + stats.matching_adr + stats.conflict_count != stats.rows:
        raise RuntimeError("Preflight classification accounting mismatch")
    return stats, plans


def execute_plans(
    *,
    db: Any,
    plans: Sequence[PlannedUpdate],
    last_update_option_cls: Any,
    stats: AlignmentStats,
    progress: ProgressCallback | None = None,
) -> None:
    collection = db.collection(COLLECTION)
    plan_rows = list(plans)
    total_waves = _wave_total(len(plan_rows))
    for wave_index, wave in enumerate(_chunks(plan_rows), start=1):
        if len(wave) > FIRESTORE_BATCH_SIZE:
            raise ValueError("Write batch exceeds governed 400-operation limit")
        batch = db.batch()
        for plan in wave:
            ref = collection.document(plan.master_id)
            batch.update(
                ref,
                {"adr": dict(plan.expected_adr)},
                option=last_update_option_cls(plan.update_time),
            )
        stats.maximum_write_operations_in_any_batch = max(
            stats.maximum_write_operations_in_any_batch, len(wave)
        )
        stats.write_waves_attempted += 1
        stats.writes_attempted += len(wave)
        _emit_progress(
            progress,
            f"WRITE wave {wave_index}/{total_waves}: committing {len(wave):,} ADR updates "
            f"({stats.writes_succeeded + 1:,}-{stats.writes_succeeded + len(wave):,} "
            f"of {len(plan_rows):,})...",
        )
        # Atomic wave only. If this fails, stop. Never fall back to per-document writes.
        batch.commit()
        stats.write_waves_committed += 1
        stats.writes_succeeded += len(wave)
        _emit_progress(
            progress,
            f"WRITE PROGRESS {stats.writes_succeeded:,}/{len(plan_rows):,}: "
            f"committedWaves={stats.write_waves_committed:,}",
        )


def verify_post_write(
    *,
    db: Any,
    expected_rows: Sequence[Mapping[str, Any]],
    plan_by_id: Mapping[str, PlannedUpdate],
    stats: AlignmentStats,
    progress: ProgressCallback | None = None,
) -> None:
    collection = db.collection(COLLECTION)
    expected_by_id = {str(item["masterId"]): item["expected"] for item in expected_rows}
    checked = 0
    total_waves = _wave_total(len(expected_by_id))
    for wave_index, id_wave in enumerate(_chunks(list(expected_by_id)), start=1):
        _emit_progress(
            progress,
            f"VERIFY READ wave {wave_index}/{total_waves}: "
            f"documents {checked + 1:,}-{min(checked + len(id_wave), len(expected_by_id)):,} "
            f"of {len(expected_by_id):,}...",
        )
        refs = [collection.document(doc_id) for doc_id in id_wave]
        snapshots = _bulk_snapshots(db, refs)
        stats.verification_read_waves += 1
        for doc_id, ref in zip(id_wave, refs):
            snapshot = _snapshot_for(snapshots, ref)
            if snapshot is None or not getattr(snapshot, "exists", False):
                raise RuntimeError(f"Post-write verification missing document: {doc_id}")
            payload = snapshot.to_dict() or {}
            expected = expected_by_id[doc_id]
            if not _strict_equal(payload.get("adr"), expected.get("adr")):
                raise RuntimeError(f"Post-write adr mismatch: {doc_id}")
            plan = plan_by_id.get(doc_id)
            if plan is not None and _preserved_hash(payload) != plan.preserved_hash:
                raise RuntimeError(f"Post-write non-ADR preservation mismatch: {doc_id}")
            checked += 1
        _emit_progress(
            progress,
            f"VERIFY PROGRESS {checked:,}/{len(expected_by_id):,}",
        )
    if checked != len(expected_rows):
        raise RuntimeError("Post-write verification accounting mismatch")


def _service_account_project(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Service account not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError("Service account is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Service account must contain one JSON object")
    return _safe_text(payload.get("project_id"))


def _report_payload(
    *,
    project_id: str,
    source: SourceContract,
    source_evidence: Mapping[str, Any],
    stats: AlignmentStats,
    execute: bool,
    status: str,
    result: str,
    git_execution: Mapping[str, str] | None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation": "sales_adr_alignment_v1",
        "projectId": project_id,
        "collection": COLLECTION,
        "lmPcode": EXPECTED_LM_PCODE,
        "execute": execute,
        "status": status,
        "result": result,
        "sourceContract": {
            "csvSha256": source.csv_sha256,
            "manifestSha256": source.manifest_sha256,
            "rows": source.rows,
            "addressEnriched": source.enriched_rows,
            "addressUnresolved": source.unresolved_rows,
        },
        "sourceEvidence": dict(source_evidence),
        "stats": {
            "rows": stats.rows,
            "inspected": stats.inspected,
            "missingDocuments": stats.missing_documents,
            "updateMissingAdr": stats.update_missing_adr,
            "matchingAdr": stats.matching_adr,
            "adrConflicts": stats.adr_conflicts,
            "nonAdrConflicts": stats.non_adr_conflicts,
            "identityConflicts": stats.identity_conflicts,
            "conflicts": stats.conflict_count,
            "writesAttempted": stats.writes_attempted,
            "writesSucceeded": stats.writes_succeeded,
        },
        "batchEvidence": {
            "firestoreBatchSize": FIRESTORE_BATCH_SIZE,
            "readWaves": stats.read_waves,
            "writeWavesAttempted": stats.write_waves_attempted,
            "writeWavesCommitted": stats.write_waves_committed,
            "verificationReadWaves": stats.verification_read_waves,
            "maximumWriteOperationsInAnyBatch": stats.maximum_write_operations_in_any_batch,
            "perDocumentFallback": False,
        },
        "conflictSample": list(stats.conflicts),
        "writeScope": ["adr"],
        "creates": 0,
        "deletes": 0,
        "gitExecutionGate": dict(git_execution or {}),
        "finishedAt": datetime.now(UTC).isoformat(),
    }
    if error is not None:
        payload["errorType"] = type(error).__name__
        payload["error"] = str(error)
    return payload


def _write_report(report_dir: Path, project_id: str, payload: Mapping[str, Any]) -> Path:
    if not report_dir.is_dir():
        raise FileNotFoundError(
            f"Approved existing report directory is missing; tool will not create folders: {report_dir}"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"sales_adr_alignment__{project_id}__{run_id}.json"
    temp = report_path.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    temp.replace(report_path)
    return report_path


def run(args: argparse.Namespace) -> int:
    validate_project_gate(
        args.project_id,
        args.confirm_project,
        bool(args.execute),
        _safe_text(args.confirm_execute),
    )
    input_path = resolve_repo_path(args.input)
    manifest_path = resolve_repo_path(args.manifest)
    report_dir = resolve_repo_path(args.report_dir)
    service_account_path = resolve_repo_path(args.service_account)

    _console_progress("ADR ALIGNMENT: validating frozen Stage 06 source contract...")
    source = validate_frozen_source(input_path, manifest_path)
    _console_progress(
        f"ADR ALIGNMENT: source PASS ({source.rows:,} rows; SHA {source.csv_sha256[:12]}...)"
    )
    _console_progress("ADR ALIGNMENT: loading and validating canonical expected Sales rows...")
    expected_rows, source_evidence = _load_expected_rows(input_path, manifest_path)
    _console_progress(f"ADR ALIGNMENT: expected-row build PASS ({len(expected_rows):,} rows)")
    if _service_account_project(service_account_path) != args.project_id:
        raise ValueError("Service-account project mismatch")

    git_execution: dict[str, str] | None = None
    if args.execute:
        git_execution = enforce_git_execution_gate()

    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1 import LastUpdateOption
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore/google-auth are required for ADR alignment") from exc

    _console_progress(f"ADR ALIGNMENT: opening Firestore client for {args.project_id}...")
    credentials = service_account.Credentials.from_service_account_file(str(service_account_path))
    db = firestore.Client(project=args.project_id, credentials=credentials)
    _console_progress(
        f"ADR ALIGNMENT: starting {'EXECUTION' if args.execute else 'READ-ONLY PREFLIGHT'} "
        f"in {_wave_total(len(expected_rows))} governed read waves (max {FIRESTORE_BATCH_SIZE} documents each)"
    )
    stats = AlignmentStats(rows=len(expected_rows))
    plans: list[PlannedUpdate] = []
    try:
        stats, plans = preflight_target(db=db, expected_rows=expected_rows, progress=_console_progress)
        if stats.conflict_count:
            raise RuntimeError(
                "ADR alignment preflight blocked: "
                f"missing={stats.missing_documents}, identity={stats.identity_conflicts}, "
                f"nonAdr={stats.non_adr_conflicts}, adr={stats.adr_conflicts}"
            )

        if not args.execute:
            payload = _report_payload(
                project_id=args.project_id,
                source=source,
                source_evidence=source_evidence,
                stats=stats,
                execute=False,
                status="PASS",
                result="PREFLIGHT_PASS",
                git_execution=None,
            )
            report_path = _write_report(report_dir, args.project_id, payload)
            print("=== SALES ADR ALIGNMENT PREFLIGHT ===")
            print(f"Project: {args.project_id}")
            print(f"Rows: {stats.rows:,}")
            print(f"ADR updates required: {stats.update_missing_adr:,}")
            print(f"ADR already matching: {stats.matching_adr:,}")
            print("Conflicts: 0")
            print("Firestore writes: 0")
            print(f"Report: {report_path}")
            return 0

        plan_by_id = {plan.master_id: plan for plan in plans}
        execute_plans(
            db=db,
            plans=plans,
            last_update_option_cls=LastUpdateOption,
            stats=stats,
            progress=_console_progress,
        )
        _console_progress("ADR ALIGNMENT: write phase complete; starting full post-write verification...")
        verify_post_write(
            db=db,
            expected_rows=expected_rows,
            plan_by_id=plan_by_id,
            stats=stats,
            progress=_console_progress,
        )
        payload = _report_payload(
            project_id=args.project_id,
            source=source,
            source_evidence=source_evidence,
            stats=stats,
            execute=True,
            status="PASS",
            result="ADR_ALIGNMENT_VERIFIED",
            git_execution=git_execution,
        )
        report_path = _write_report(report_dir, args.project_id, payload)
        print("=== SALES ADR ALIGNMENT VERIFIED ===")
        print(f"Project: {args.project_id}")
        print(f"Rows verified: {stats.rows:,}")
        print(f"ADR writes: {stats.writes_succeeded:,}")
        print(f"ADR already matching: {stats.matching_adr:,}")
        print("Creates: 0")
        print("Deletes: 0")
        print(f"Report: {report_path}")
        return 0
    except Exception as exc:
        try:
            payload = _report_payload(
                project_id=args.project_id,
                source=source,
                source_evidence=source_evidence,
                stats=stats,
                execute=bool(args.execute),
                status="FAIL",
                result="FAILED",
                git_execution=git_execution,
                error=exc,
            )
            report_path = _write_report(report_dir, args.project_id, payload)
            print(f"Failure report: {report_path}", file=sys.stderr)
        except Exception as report_exc:
            print(f"Could not write failure report: {report_exc}", file=sys.stderr)
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
