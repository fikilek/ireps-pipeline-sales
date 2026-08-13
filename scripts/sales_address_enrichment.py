"""Deterministic, record-local Sales physical-address enrichment.

Sales Enrich v1 derives the canonical iREPS staging fields ``strNo``,
``strName`` and ``strType`` from the raw commercial address on the same Sales
record.  It does not geocode, infer cadastral relationships, use neighbouring
Sales records, or mutate the raw source address.

Stage 06 keeps these three values as flat CSV columns.  Stage 08 is the sole
projection boundary into the Firestore root map ``adr``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ADDRESS_STAGING_COLUMNS = ["strNo", "strName", "strType"]
ADDRESS_MAP_FIELDS = frozenset(ADDRESS_STAGING_COLUMNS)
RAW_ADDRESS_FIELDS = (
    "addressLine1",
    "addressLine2",
    "town",
    "postalAddress1",
    "postalAddress2",
    "postalAddressTown",
    "standNumber",
)

SUPPORTED_STREET_TYPES = frozenset(
    {"Street", "Road", "Drive", "Place", "Lane", "Avenue", "Crescent", "-"}
)

_TYPE_ALIASES = {
    "ST": "Street",
    "STREET": "Street",
    "STR": "Street",
    "ROAD": "Road",
    "RD": "Road",
    "DRIVE": "Drive",
    "DR": "Drive",
    "PLACE": "Place",
    "LANE": "Lane",
    "AVENUE": "Avenue",
    "AVE": "Avenue",
    "CRESCENT": "Crescent",
    "CRES": "Crescent",
    "CR": "Crescent",
}

_REASON_NO_NUMBER = "NO_USABLE_STRNO_IN_SOURCE_ADDRESS"
_REASON_UNIT_ONLY = "UNIT_BLOCK_DWELLING_NUMBER_NOT_PROVEN_STRNO"
_REASON_CONFLICT = "MULTIPLE_RANGE_OR_CONFLICTING_ADDRESS_CANDIDATES"
_REASON_ZERO = "ZERO_OR_INVALID_STREET_NUMBER"
_REASON_NUMERIC_ROLE = "NUMERIC_ROLE_NOT_PROVEN_STRNO"


@dataclass(frozen=True)
class AddressEnrichmentResult:
    strNo: str
    strName: str
    strType: str
    status: str
    reason: str
    method: str
    sourceLineUsed: str
    sourceEvidence: str

    @property
    def enrichable(self) -> bool:
        return self.status == "ENRICHABLE"


@dataclass(frozen=True)
class _Candidate:
    str_no: str
    str_name: str
    str_type: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _space(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def _display_name(value: str) -> str:
    """Apply the iREPS display rule without correcting source spelling.

    Every whitespace-delimited source token receives an uppercase first
    character and lowercase remaining characters.  Punctuation and digits are
    retained exactly.  This intentionally turns e.g. ``PH0LI`` into ``Ph0li``
    while preserving the source's zero rather than correcting it to a letter.
    """

    tokens: list[str] = []
    for token in _space(value).split(" "):
        if not token:
            continue
        tokens.append(token[:1].upper() + token[1:].lower())
    return " ".join(tokens)


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _split_name_type_and_qualifier(rest: str) -> tuple[str, str]:
    rest = _space(rest).strip(" ,")
    # In the approved ZA5241 source this one locality suffix is auxiliary to a
    # number-first street address rather than part of the street name.
    rest = re.sub(r"\s+\(GLENCOE\)\s*$", "", rest, flags=re.IGNORECASE)
    tokens = rest.split()

    type_index: int | None = None
    canonical_type = "-"
    for index, token in enumerate(tokens):
        clean = token.strip(",.").upper()
        if clean not in _TYPE_ALIASES or index == 0:
            continue

        after = tokens[index + 1 :]
        if not after:
            type_index = index
            canonical_type = _TYPE_ALIASES[clean]
            break

        first_after = after[0].strip(",.").upper()
        normalized_after = first_after.lstrip("-")
        if first_after == "-" and len(after) > 1:
            normalized_after = after[1].strip(",.").upper()
        if (
            normalized_after
            in {"FLAT", "SHOP", "HOUSE", "UNIT", "OUTBUILDING", "OUT", "SHED", "SHAD"}
            or re.fullmatch(r"FLAT?\d*[A-Z]?", normalized_after)
        ):
            type_index = index
            canonical_type = _TYPE_ALIASES[clean]
            break

    name_tokens: list[str] = []
    limit = type_index if type_index is not None else len(tokens)
    for index, token in enumerate(tokens[:limit]):
        clean = token.strip("(),.").upper()
        if clean in {"FLAT", "SHOP", "HOUSE", "UNIT"}:
            # Existing approved assessment convention deliberately retains the
            # opening bracket when the raw value is e.g. ``(FLAT)``.
            if token.startswith("("):
                name_tokens.append("(")
            break
        if (
            clean == "FL"
            and index + 1 < limit
            and re.fullmatch(r"\d+[A-Z]?", tokens[index + 1].strip("(),.").upper())
        ):
            break
        # Single digit compact flat suffixes were consistently used as unit
        # qualifiers in the approved source; multi-digit forms remain source
        # street-name evidence and are not silently reinterpreted.
        if re.fullmatch(r"FL[1-9][A-Z]?", clean):
            break
        name_tokens.append(token)

    return " ".join(name_tokens).strip(" ,"), canonical_type


def _parse_start(line: str) -> _Candidate | str | None:
    source = _space(line)
    if not source:
        return None
    # ``2ND METER`` and similar service descriptors are not street numbers.
    if re.match(r"^\d+(?:ST|ND|RD|TH)\b", source, flags=re.IGNORECASE):
        return None
    if re.match(r"^0(?:\D|$)", source):
        return _REASON_ZERO

    match = re.match(r"^(\d+)([A-Za-z]?)(.*)$", source)
    if match is None:
        return None
    digits, suffix, remainder = match.groups()
    if int(digits) == 0:
        return _REASON_ZERO
    if re.match(r"^\s*(?:&|/|-)\s*\d", remainder):
        return _REASON_CONFLICT

    str_no = digits + suffix.upper()
    remainder = remainder.strip()

    separated_suffix = re.match(r"^([A-Za-z])\s+(.+)$", remainder)
    if separated_suffix is not None and not suffix:
        str_no += separated_suffix.group(1).upper()
        remainder = separated_suffix.group(2)

    if (
        re.search(r"/\s*\d", remainder)
        or re.search(r"\bAND\s+\d", remainder, flags=re.IGNORECASE)
        or re.search(r"&\s*\d", remainder)
    ):
        return _REASON_CONFLICT

    name, str_type = _split_name_type_and_qualifier(remainder)
    if not name:
        return None
    return _Candidate(str_no=str_no, str_name=_display_name(name), str_type=str_type)


def _reverse_name_is_unit_descriptor(name: str) -> bool:
    upper = name.upper()
    if re.search(
        r"\b(?:FAMILY\s+UNIT|DWELLING|ROOM|HOUSE|BLOCK|FLAT|UNIT|HOSTEL|SECTION)\b",
        upper,
    ):
        return True
    if re.search(r"\b(?:F/U|S/Q)\b", upper):
        return True
    if upper.startswith("CRAIG H") or upper.startswith("ROADS IN EXT"):
        return True
    if re.search(r"\bAND\s*$", upper):
        return True
    return False


def _parse_reverse(line: str) -> _Candidate | str | None:
    source = _space(line)
    if not source:
        return None
    if re.search(r"\bAND\s+\d+[A-Z]?\s*$", source, flags=re.IGNORECASE):
        return _REASON_CONFLICT

    match = re.match(r"^(.+?)\s+(\d+[A-Za-z]?)$", source)
    if match is None:
        # ZA5241 includes one record-local form such as KEMP(GLENCOE)34.
        match = re.match(r"^([A-Za-z][A-Za-z ()]+?\))(\d+[A-Za-z]?)$", source)
        if match is None:
            return None

    name_part, str_no = match.groups()
    if _reverse_name_is_unit_descriptor(name_part):
        return None
    digits = re.match(r"\d+", str_no)
    if digits is None or int(digits.group(0)) == 0:
        return _REASON_ZERO

    tokens = name_part.split()
    str_type = "-"
    if tokens and tokens[-1].strip(",.").upper() in _TYPE_ALIASES:
        str_type = _TYPE_ALIASES[tokens[-1].strip(",.").upper()]
        tokens = tokens[:-1]
    name = " ".join(tokens).strip(" ,)")
    if not name:
        return None
    return _Candidate(str_no=str_no.upper(), str_name=_display_name(name), str_type=str_type)


def _special_candidate(line1: str, line2: str) -> tuple[_Candidate, str] | None:
    source1 = _space(line1)
    upper1 = source1.upper()
    upper2 = _space(line2).upper()

    match = re.match(
        r"^ZIGO\s+SITHOLE\s+(\d+[A-Z]?)(?:\s*,\s*(?:FAM(?:ILY)?\s+UNIT|BLOCK)\b.*)?$",
        upper1,
    )
    if match is not None and (
        "," in upper1 or re.match(r"^(?:FAM(?:ILY)?\s+UNIT|BLOCK)\b", upper2)
    ):
        return _Candidate(match.group(1), "Zigo Sithole", "-"), "compound_zigo"

    match = re.match(
        r"^NDUMENI\s+(\d+[A-Z]?)\s*,\s*FAM(?:ILY)?\s+UNIT\b",
        upper1,
    )
    if match is not None:
        return _Candidate(match.group(1), "Ndumeni", "-"), "compound_ndumeni"

    # MBELE and BENVILLE are explicit record-local source patterns. Compound
    # MGADI/MNGADI values such as ``577-1`` and ``562/1`` are deliberately NOT
    # special-cased: without same-record evidence they remain range/conflict
    # candidates and must fail closed.
    match = re.match(r"^MBELE\s+\(GLENCOE\)\s+(0\d+)$", upper1)
    if match is not None:
        return _Candidate(match.group(1), "Mbele", "-"), "mbele_leading_zero"
    match = re.match(r"^BENVILLE\s+(\d+[A-Z]?)\s+NO\s+\d+[A-Z]?$", upper1)
    if match is not None:
        return _Candidate(match.group(1), "Benville", "-"), "benville_unit_suffix"
    return None


def _line2_reverse_candidate(line2: str) -> _Candidate | str | None:
    source = _space(line2)
    # These source-local forms were approved as line-2 physical address
    # evidence.  A generic ``SHOP NO 2`` remains a unit/service descriptor and
    # is deliberately not accepted.
    if re.match(r"^(?:SMITH|THE\s+MEWS\s+SHOP|SHOP)\s+\d+[A-Z]?$", source, re.IGNORECASE):
        return _parse_reverse(source)

    candidate = _parse_reverse(source)
    if isinstance(candidate, _Candidate):
        if _name_key(candidate.str_name) == "phase":
            return None
        if not re.search(
            r"\b(?:SHOP|FLAT|UNIT|HOUSE|BLOCK|HOSTEL|NO)\b",
            candidate.str_name,
            flags=re.IGNORECASE,
        ):
            return candidate
    return candidate if isinstance(candidate, str) else None


def _classify_unresolved(line1: str, line2: str) -> str:
    source1 = _space(line1)
    source2 = _space(line2)
    upper1 = source1.upper()

    if re.match(r"^0(?:\s|$)", source1) or re.match(r"^0(?:\s|$)", source2):
        return _REASON_ZERO

    unit_patterns = (
        r"^FAMILY\s+UNIT\s+NO\s+\d",
        r"^STRATHMORE\s+GARDENS$",
        r"^KZN\s+DWELLING\s+NO\s+\d",
        r"^SINGLE\s+QUART\.\s+ROOM\s+\d",
        r"^BL\s+\d+\s+SITHE\s+HOSTEL$",
    )
    if any(re.search(pattern, upper1) for pattern in unit_patterns):
        return _REASON_UNIT_ONLY

    if (
        re.match(r"^CRAIG\s+H\s+\d+", upper1)
        or re.search(r"\bPLOT\s+\d+", upper1)
        or re.match(r"^EXT\s+\d+\s+", upper1)
    ):
        return _REASON_NUMERIC_ROLE

    if (
        re.search(r"\d+\s*&\s*\d+", upper1)
        or re.search(r"\d+\s*/\s*\d+", upper1)
        or re.search(r"/\s*\d+", upper1)
        or re.search(r"\d+\s*-\s*\d+", upper1)
        or re.search(r"\bAND\s+\d+", upper1)
    ):
        return _REASON_CONFLICT

    first_candidates = [_parse_start(source1), _parse_reverse(source1)]
    second_candidates = [_parse_start(source2), _parse_reverse(source2)]
    first = next((item for item in first_candidates if isinstance(item, _Candidate)), None)
    second = next((item for item in second_candidates if isinstance(item, _Candidate)), None)
    if first is not None and second is not None:
        if (first.str_no, _name_key(first.str_name)) != (
            second.str_no,
            _name_key(second.str_name),
        ):
            return _REASON_CONFLICT

    return _REASON_NO_NUMBER


def parse_physical_address(address_line1: Any, address_line2: Any) -> AddressEnrichmentResult:
    line1 = _text(address_line1)
    line2 = _text(address_line2)

    special = _special_candidate(line1, line2)
    if special is not None:
        candidate, method = special
        return _resolved(candidate, method, "addressLine1", line1)

    start1 = _parse_start(line1)
    start2 = _parse_start(line2)
    reverse1 = _parse_reverse(line1)
    reverse2 = _line2_reverse_candidate(line2)

    if isinstance(start1, str):
        return _unresolved(line1, line2)

    if isinstance(start1, _Candidate):
        # The source occasionally places only the explicit street-type token on
        # addressLine2 (for example ``42 MCKENZIE`` / ``STREET``).  This is
        # evidence from the same record, not cross-record inference.
        line2_type_token = _space(line2).strip(",.").upper()
        if start1.str_type == "-" and line2_type_token in _TYPE_ALIASES:
            start1 = _Candidate(
                str_no=start1.str_no,
                str_name=start1.str_name,
                str_type=_TYPE_ALIASES[line2_type_token],
            )
        other = start2 if isinstance(start2, _Candidate) else reverse2 if isinstance(reverse2, _Candidate) else None
        if other is not None and (
            re.match(r"^Phase\b", start1.str_name, flags=re.IGNORECASE)
            or re.match(r"^Phase\b", other.str_name, flags=re.IGNORECASE)
        ):
            if (start1.str_no, _name_key(start1.str_name)) != (
                other.str_no,
                _name_key(other.str_name),
            ):
                return _unresolved(line1, line2)
        method = (
            "line1_start_sep_letter"
            if re.match(r"^\d+\s+[A-Za-z]\s+", _space(line1))
            else "line1_start"
        )
        return _resolved(start1, method, "addressLine1", line1)

    if isinstance(start2, _Candidate):
        if isinstance(reverse1, _Candidate) and _name_key(reverse1.str_name) != _name_key(start2.str_name):
            upper1 = _space(line1).upper()
            trusted_line2_context = bool(
                re.search(r"\b(?:UNIT|HOSTEL|SECTION|F/U|S/Q|MEWS|VILLAS)\b", upper1)
                or upper1.startswith("WHITE ")
                or (start2.str_no.upper() == "16A" and _name_key(start2.str_name) == "ndumeni")
            )
            if not trusted_line2_context:
                return _unresolved(line1, line2)
        return _resolved(start2, "line2_start", "addressLine2", line2)

    if isinstance(reverse1, str):
        return _unresolved(line1, line2)
    if isinstance(reverse1, _Candidate):
        if isinstance(reverse2, _Candidate):
            return _resolved(reverse2, "line2_reverse", "addressLine2", line2)
        return _resolved(reverse1, "line1_reverse", "addressLine1", line1)

    if isinstance(reverse2, _Candidate):
        return _resolved(reverse2, "line2_reverse", "addressLine2", line2)

    return _unresolved(line1, line2)


def _resolved(
    candidate: _Candidate,
    method: str,
    source_line: str,
    evidence: str,
) -> AddressEnrichmentResult:
    validate_address_values(candidate.str_no, candidate.str_name, candidate.str_type)
    return AddressEnrichmentResult(
        strNo=candidate.str_no,
        strName=candidate.str_name,
        strType=candidate.str_type,
        status="ENRICHABLE",
        reason="",
        method=method,
        sourceLineUsed=source_line,
        sourceEvidence=_text(evidence),
    )


def _unresolved(line1: str, line2: str) -> AddressEnrichmentResult:
    return AddressEnrichmentResult(
        strNo="",
        strName="",
        strType="-",
        status="UNRESOLVED",
        reason=_classify_unresolved(line1, line2),
        method="",
        sourceLineUsed="",
        sourceEvidence="",
    )


def validate_address_values(str_no: Any, str_name: Any, str_type: Any) -> None:
    no = _text(str_no)
    name = _text(str_name)
    typ = _text(str_type)
    if bool(no) != bool(name):
        raise ValueError("strNo and strName must both be populated or both be blank")
    if typ not in SUPPORTED_STREET_TYPES:
        raise ValueError(f"Unsupported strType: {typ!r}")
    if not no and typ != "-":
        raise ValueError("Unresolved address must use strType='-'")
    if no:
        digits = re.match(r"^\d+", no)
        if digits is None or int(digits.group(0)) == 0:
            raise ValueError(f"strNo must begin with a non-zero number: {no!r}")


def address_map_from_row(row: Mapping[str, Any]) -> dict[str, str]:
    values = {
        "strNo": _text(row.get("strNo")),
        "strName": _text(row.get("strName")),
        "strType": _text(row.get("strType")),
    }
    validate_address_values(values["strNo"], values["strName"], values["strType"])
    return values



def raw_address_projection(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the canonical comparison tuple for raw address source fields."""

    return tuple(_text(record.get(field)) for field in RAW_ADDRESS_FIELDS)


def raw_address_snapshot(
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """Snapshot raw address evidence by canonical record identity."""

    return {str(key): raw_address_projection(value) for key, value in records.items()}


def raw_address_mutation_count(
    before: Mapping[str, tuple[str, ...]],
    after: Mapping[str, tuple[str, ...]],
) -> int:
    """Count records whose governed raw address projection changed.

    Population drift is a separate contract failure and is rejected rather than
    folded into the mutation count.
    """

    if set(before) != set(after):
        missing = sorted(set(before) - set(after))
        extra = sorted(set(after) - set(before))
        raise ValueError(
            "Raw address preservation population mismatch: "
            f"missing={missing[:10]}; extra={extra[:10]}"
        )
    return sum(before[key] != after[key] for key in before)

def build_enrichment_report(
    records: Iterable[tuple[str, str, str, AddressEnrichmentResult]],
    *,
    source_label: str,
    source_sha256: str,
    raw_address_mutation_count: int,
) -> dict[str, Any]:
    serialized: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for master_id, line1, line2, result in records:
        status_counts[result.status] += 1
        if result.reason:
            reason_counts[result.reason] += 1
        serialized.append(
            {
                "masterId": master_id,
                "addressLine1": _text(line1),
                "addressLine2": _text(line2),
                **asdict(result),
            }
        )
    return {
        "schemaVersion": 1,
        "operation": "sales_address_enrichment",
        "source": {"label": source_label, "sha256": source_sha256},
        "rows": len(serialized),
        "enrichedRows": status_counts.get("ENRICHABLE", 0),
        "unresolvedRows": status_counts.get("UNRESOLVED", 0),
        "reasonCounts": dict(sorted(reason_counts.items())),
        "rawAddressMutationCount": int(raw_address_mutation_count),
        "fabricatedSpatialRelationshipCount": 0,
        "records": serialized,
    }


def write_json_atomic(payload: Mapping[str, Any], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        temporary.write_bytes(encoded)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(encoded).hexdigest()


def enrichment_contract(report: Mapping[str, Any], *, report_path: Path, report_sha256: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "stagingColumns": list(ADDRESS_STAGING_COLUMNS),
        "firestoreProjection": "adr",
        "enrichedRows": int(report.get("enrichedRows", 0)),
        "unresolvedRows": int(report.get("unresolvedRows", 0)),
        "reasonCounts": dict(report.get("reasonCounts") or {}),
        "rawAddressMutationCount": int(report.get("rawAddressMutationCount", 0)),
        "fabricatedSpatialRelationshipCount": int(report.get("fabricatedSpatialRelationshipCount", 0)),
        "reportFilename": report_path.name,
        "reportSha256": report_sha256,
    }
