"""Immutable local population artifacts and publication gates; never uploads.

Deployment accesses these exact blobs through the existing Firebase Storage
bucket. A local artifact is not evidence of remote publication.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def build_snapshot(*, lm_pcode, provider, month, source_sha256, members,
                   evidence_sha256, previous_sha256=None, replacements=(), exceptions=()):
    hashes = [source_sha256, evidence_sha256] + ([previous_sha256] if previous_sha256 else [])
    if any(not re.fullmatch(r"[a-f0-9]{64}", value or "") for value in hashes):
        raise ValueError("Snapshot requires verified source/completeness hashes")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("Invalid snapshot month")
    if not members or len(members) != len(set(members)) or any(not re.fullmatch(r"[A-Z0-9]+", m) for m in members):
        raise ValueError("Snapshot members must be distinct canonical identities")
    predecessors, successors = set(), set()
    for replacement in replacements:
        if set(replacement) != {"predecessor", "successor"}:
            raise ValueError("Replacement has unexpected fields")
        old, new = replacement["predecessor"], replacement["successor"]
        if old in members or new not in members or old == new or old in predecessors or new in successors:
            raise ValueError("Replacement must be authoritative one-to-one membership movement")
        predecessors.add(old)
        successors.add(new)
    return {"schemaVersion": 1, "lmPcode": lm_pcode, "provider": provider,
        "month": month, "sourceSha256": source_sha256, "members": sorted(members),
        "previousSnapshotSha256": previous_sha256, "replacements": list(replacements),
        "exceptions": list(exceptions), "completeness": {"complete": True,
        "evidenceSha256": evidence_sha256}}


def write_snapshot(directory, snapshot):
    payload = encode(snapshot)
    digest = hashlib.sha256(payload).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("Immutable snapshot already exists with different bytes")
    return path, digest
