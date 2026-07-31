"""Source contracts, one-to-one SG linkage, and enrichment logic for Stage M02."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lib.xlsx_stream import XlsxReader


END_SHEET_NAME = "Sheet1"
BRIDGE_SHEET_NAME = "CsmValRollB_2026_MPA_260731_113"

END_IDENTITY_COLUMNS = [
    ("A", "Customer"), ("B", "TariffInstance"), ("C", "MeterNumber"),
    ("D", "InstallationDate"), ("E", "PreviousMeterNumber"),
    ("F", "PreviousInstallationDate"), ("G", "StandNumber"),
    ("H", "Surname"), ("I", "AddressLine1"), ("J", "AddressLine2"),
    ("K", "Town"), ("L", "PostalAddress1"), ("M", "PostalAddress2"),
    ("N", "PostalAddressTown"), ("O", "AccountNumber"),
]

SALES_MONTH_COLUMNS = [
    ("P", "2023-12"), ("Q", "2024-01"), ("R", "2024-02"),
    ("S", "2024-03"), ("T", "2024-04"), ("U", "2024-05"),
    ("V", "2024-06"), ("W", "2024-07"), ("X", "2024-08"),
    ("Y", "2024-09"), ("Z", "2024-10"), ("AA", "2024-11"),
    ("AB", "2024-12"), ("AC", "2025-01"), ("AD", "2025-02"),
    ("AE", "2025-03"), ("AF", "2025-04"), ("AG", "2025-05"),
    ("AH", "2025-06"), ("AI", "2025-07"), ("AJ", "2025-08"),
    ("AK", "2025-09"), ("AL", "2025-10"), ("AM", "2025-11"),
    ("AN", "2025-12"), ("AO", "2026-01"), ("AP", "2026-02"),
    ("AQ", "2026-03"), ("AR", "2026-04"), ("AS", "2026-05"),
    ("AT", "2026-06"),
]

UNITS_MONTH_COLUMNS = [
    ("AV", "2023-12"), ("AW", "2024-01"), ("AX", "2024-02"),
    ("AY", "2024-03"), ("AZ", "2024-04"), ("BA", "2024-05"),
    ("BB", "2024-06"), ("BC", "2024-07"), ("BD", "2024-08"),
    ("BE", "2024-09"), ("BF", "2024-10"), ("BG", "2024-11"),
    ("BH", "2024-12"), ("BI", "2025-01"), ("BJ", "2025-02"),
    ("BK", "2025-03"), ("BL", "2025-04"), ("BM", "2025-05"),
    ("BN", "2025-06"), ("BO", "2025-07"), ("BP", "2025-08"),
    ("BQ", "2025-09"), ("BR", "2025-10"), ("BS", "2025-11"),
    ("BT", "2025-12"), ("BU", "2026-01"), ("BV", "2026-02"),
    ("BW", "2026-03"), ("BX", "2026-04"), ("BY", "2026-05"),
    ("BZ", "2026-06"),
]

BRIDGE_COLUMNS = {
    "D": "OWNER_ACCOUNT_NO",
    "W": "GIS_KEY",
}

ERF_PIPELINE_PREFIX = "K241"


@dataclass(frozen=True)
class BridgeAccountLink:
    account_number_normalized: str
    gis_keys: tuple[str, ...]
    source_rows: tuple[int, ...]


@dataclass(frozen=True)
class ErfCandidate:
    erf_number: str
    erf_id: str
    ward_number: str
    ward_pcode: str
    lm_pcode: str
    latitude: float
    longitude: float

    def as_document(self) -> dict[str, Any]:
        # Geometry is deliberately excluded from the PSD.
        return {
            "ErfNumber": self.erf_number,
            "ErfId": self.erf_id,
            "WardNumber": self.ward_number,
            "WardPcode": self.ward_pcode,
            "LmPcode": self.lm_pcode,
            "Latitude": self.latitude,
            "Longitude": self.longitude,
        }


@dataclass
class EnrichedMeter:
    source_end_row: int
    identity: dict[str, str]
    sales: dict[str, str]
    units: dict[str, str]
    account_number_normalized: str
    elm_source_rows: list[int]
    erf_numbers: list[str]
    missing_erf_numbers: list[str]
    candidates: list[ErfCandidate]
    gps_match_status: str

    @property
    def has_usable_gps(self) -> bool:
        return len(self.candidates) == 1

    def as_json_document(self) -> dict[str, Any]:
        if len(self.candidates) > 1:
            raise ValueError(
                f"Meter {self.identity['MeterNumber']} has more than one ERF candidate"
            )

        return {
            "SourceEndRow": self.source_end_row,
            **self.identity,
            "AccountNumberNormalized": self.account_number_normalized,
            # Legacy field names are intentionally preserved for frontend compatibility.
            "ElmAccountMatched": bool(self.elm_source_rows),
            "ErfNumbers": self.erf_numbers,
            "MissingErfNumbers": self.missing_erf_numbers,
            "ErfCandidateCount": len(self.candidates),
            "HasUsableGps": self.has_usable_gps,
            "GpsMatchStatus": self.gps_match_status,
            "ErfCandidates": [candidate.as_document() for candidate in self.candidates],
            "ElmSourceRows": self.elm_source_rows,
            "trnBatchIds": [],
            "Sales": self.sales,
            "Units": self.units,
        }

    def as_csv_row(self) -> dict[str, Any]:
        if len(self.candidates) > 1:
            raise ValueError(
                f"Meter {self.identity['MeterNumber']} has more than one ERF candidate"
            )

        candidate_documents = [candidate.as_document() for candidate in self.candidates]
        return {
            "SourceEndRow": self.source_end_row,
            **self.identity,
            "AccountNumberNormalized": self.account_number_normalized,
            "ElmAccountMatched": "TRUE" if self.elm_source_rows else "FALSE",
            "ErfNumber": join_unique(self.erf_numbers),
            "ErfNumberCount": len(self.erf_numbers),
            "ErfCandidateCount": len(self.candidates),
            "HasUsableGps": "TRUE" if self.has_usable_gps else "FALSE",
            "GpsMatchStatus": self.gps_match_status,
            "ErfId": join_unique(candidate.erf_id for candidate in self.candidates),
            "WardNumber": join_unique(candidate.ward_number for candidate in self.candidates),
            "WardPcode": join_unique(candidate.ward_pcode for candidate in self.candidates),
            "LmPcode": join_unique(candidate.lm_pcode for candidate in self.candidates),
            "Latitude": join_unique(format_number(candidate.latitude) for candidate in self.candidates),
            "Longitude": join_unique(format_number(candidate.longitude) for candidate in self.candidates),
            # Keep the historic CSV column but never place geometry in it.
            "Geometry": "",
            "ErfCandidatesJson": compact_json(candidate_documents),
            "ElmSourceRows": join_unique(str(row) for row in self.elm_source_rows),
            "trnBatchIds": "[]",
            **{f"Sales_{month}": value for month, value in self.sales.items()},
            **{f"Units_{month}": value for month, value in self.units.items()},
        }


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_account_number(value: Any) -> str:
    text = clean_text(value)
    while text.startswith("'"):
        text = text[1:].strip()
    text = re.sub(r"\s+", "", text)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def normalize_erf_number(value: Any) -> str:
    text = re.sub(r"\s+", "", clean_text(value).upper())
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return str(int(text)) if text.isdigit() else text


def normalize_gis_key(value: Any) -> str:
    text = clean_text(value)
    while text.startswith("'"):
        text = text[1:].strip()
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def normalize_pipeline_prcl_key(value: Any) -> str:
    text = normalize_gis_key(value)
    if text.startswith(ERF_PIPELINE_PREFIX):
        text = text[len(ERF_PIPELINE_PREFIX):]
    return text


def normalize_date(value: Any) -> str:
    text = clean_text(value)
    return "" if text.startswith("1900-01-01") else text


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def format_number(value: float) -> str:
    return format(value, ".15g")


def join_unique(values: Iterable[str]) -> str:
    return "|".join(dict.fromkeys(clean_text(value) for value in values if clean_text(value)))


def read_end_workbook(
    path: Path,
    expected_record_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_headers = {
        **dict(END_IDENTITY_COLUMNS),
        **dict(SALES_MONTH_COLUMNS),
        **dict(UNITS_MONTH_COLUMNS),
    }
    wanted_columns = set(expected_headers) | {"AU"}
    rows: list[dict[str, Any]] = []
    header_seen = False

    for row_number, values in XlsxReader(path).iter_sheet_rows(
        END_SHEET_NAME,
        wanted_columns,
    ):
        if row_number == 1:
            header_seen = True
            for column, expected_header in expected_headers.items():
                actual_header = clean_text(values.get(column, ""))
                if actual_header != expected_header:
                    raise ValueError(
                        f"END header mismatch at {column}1. Expected "
                        f"{expected_header!r}, found {actual_header!r}."
                    )
            if clean_text(values.get("AU", "")):
                raise ValueError("END column AU must remain the blank sales/units separator")
            continue

        identity = {
            header: clean_text(values.get(column, ""))
            for column, header in END_IDENTITY_COLUMNS
        }
        identity["InstallationDate"] = normalize_date(identity["InstallationDate"])
        original_previous_date = identity["PreviousInstallationDate"]
        identity["PreviousInstallationDate"] = normalize_date(original_previous_date)
        rows.append(
            {
                "source_row": row_number,
                "identity": identity,
                "sales": {
                    month: clean_text(values.get(column, ""))
                    for column, month in SALES_MONTH_COLUMNS
                },
                "units": {
                    month: clean_text(values.get(column, ""))
                    for column, month in UNITS_MONTH_COLUMNS
                },
                "account_normalized": normalize_account_number(identity["AccountNumber"]),
                "previous_date_placeholder_cleared": (
                    bool(original_previous_date)
                    and not identity["PreviousInstallationDate"]
                ),
            }
        )

    if not header_seen:
        raise ValueError("END workbook header row was not found")
    if len(rows) != expected_record_count:
        raise ValueError(
            f"END record-count mismatch. Expected {expected_record_count:,}, "
            f"found {len(rows):,}."
        )

    meter_numbers = [row["identity"]["MeterNumber"] for row in rows]
    blank_meters = sum(not meter for meter in meter_numbers)
    duplicate_meter_groups = sum(
        count > 1
        for meter, count in Counter(meter_numbers).items()
        if meter
    )
    if blank_meters:
        raise ValueError(f"END contains {blank_meters:,} blank MeterNumber row(s)")
    if duplicate_meter_groups:
        raise ValueError(
            f"END contains {duplicate_meter_groups:,} duplicate MeterNumber group(s)"
        )

    accounts = [row["account_normalized"] for row in rows]
    return rows, {
        "records": len(rows),
        "uniqueMeterNumbers": len(set(meter_numbers)),
        "blankAccountNumbers": sum(not account for account in accounts),
        "uniqueNonblankAccountNumbers": len(
            set(account for account in accounts if account)
        ),
        "previousInstallationDatePlaceholdersCleared": sum(
            row["previous_date_placeholder_cleared"]
            for row in rows
        ),
    }


def read_bridge_workbook(
    path: Path,
    expected_record_count: int,
) -> tuple[dict[str, BridgeAccountLink], dict[str, Any]]:
    account_rows: dict[str, list[int]] = defaultdict(list)
    account_gis_keys: dict[str, set[str]] = defaultdict(set)
    pair_rows: dict[tuple[str, str], list[int]] = defaultdict(list)

    blank_accounts = 0
    blank_gis_key_rows = 0
    data_rows = 0
    header_seen = False

    for row_number, values in XlsxReader(path).iter_sheet_rows(
        BRIDGE_SHEET_NAME,
        set(BRIDGE_COLUMNS),
    ):
        if row_number == 1:
            header_seen = True
            for column, expected_header in BRIDGE_COLUMNS.items():
                actual_header = clean_text(values.get(column, ""))
                if actual_header != expected_header:
                    raise ValueError(
                        f"Valuation bridge header mismatch at {column}1. Expected "
                        f"{expected_header!r}, found {actual_header!r}."
                    )
            continue

        data_rows += 1
        account = normalize_account_number(values.get("D", ""))
        gis_key = normalize_gis_key(values.get("W", ""))

        if not account:
            blank_accounts += 1
            continue

        account_rows[account].append(row_number)

        if not gis_key:
            blank_gis_key_rows += 1
            continue

        account_gis_keys[account].add(gis_key)
        pair_rows[(account, gis_key)].append(row_number)

    if not header_seen:
        raise ValueError("Valuation bridge workbook header row was not found")
    if data_rows != expected_record_count:
        raise ValueError(
            f"Valuation bridge record-count mismatch. "
            f"Expected {expected_record_count:,}, found {data_rows:,}."
        )

    by_account = {
        account: BridgeAccountLink(
            account_number_normalized=account,
            gis_keys=tuple(sorted(account_gis_keys.get(account, set()))),
            source_rows=tuple(sorted(source_rows)),
        )
        for account, source_rows in sorted(account_rows.items())
    }

    all_gis_keys = {
        gis_key
        for link in by_account.values()
        for gis_key in link.gis_keys
    }

    return by_account, {
        "records": data_rows,
        "blankAccountNumbers": blank_accounts,
        "blankGisKeyRowsForPopulatedAccounts": blank_gis_key_rows,
        "uniqueAccounts": len(by_account),
        "accountsWithGisKey": sum(bool(link.gis_keys) for link in by_account.values()),
        "accountsWithoutGisKey": sum(not link.gis_keys for link in by_account.values()),
        "accountsWithMultipleDistinctGisKeys": sum(
            len(link.gis_keys) > 1 for link in by_account.values()
        ),
        "uniqueGisKeys": len(all_gis_keys),
        "uniqueAccountGisPairs": len(pair_rows),
        "duplicateExactAccountGisPairGroups": sum(
            len(rows) > 1 for rows in pair_rows.values()
        ),
        "duplicateExactAccountGisRowsSuppressed": sum(
            len(rows) - 1 for rows in pair_rows.values() if len(rows) > 1
        ),
    }


def _finite_float(value: Any, field_name: str, line_number: int) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid {field_name} at ERF JSONL line {line_number}"
        ) from exc
    if not math.isfinite(result):
        raise ValueError(
            f"Non-finite {field_name} at ERF JSONL line {line_number}"
        )
    return result


def _require_object(
    value: Any,
    field_name: str,
    line_number: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"Expected object {field_name} at ERF JSONL line {line_number}"
        )
    return value


def read_erf_documents(
    path: Path,
    expected_lm_pcode: str,
    expected_record_count: int,
    progress_every: int,
) -> tuple[dict[str, list[ErfCandidate]], dict[str, Any]]:
    by_comparison_key: dict[str, list[ErfCandidate]] = defaultdict(list)
    seen_erf_ids: set[str] = set()
    row_count = 0

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON at ERF JSONL line {line_number}: {exc.msg}"
                ) from exc

            row_count += 1
            if progress_every > 0 and row_count % progress_every == 0:
                print(
                    f"[PROGRESS] Read {row_count:,} / "
                    f"{expected_record_count:,} ERF pipeline records"
                )

            erf_id = clean_text(record.get("erfId"))
            if not erf_id:
                raise ValueError(f"Blank erfId at ERF JSONL line {line_number}")
            if erf_id in seen_erf_ids:
                raise ValueError(f"Duplicate erfId in ERF JSONL: {erf_id}")
            seen_erf_ids.add(erf_id)

            sg = _require_object(record.get("sg"), "sg", line_number)
            prcl_key = clean_text(sg.get("prclKey"))
            if not prcl_key:
                raise ValueError(
                    f"Blank sg.prclKey at ERF JSONL line {line_number}"
                )
            if erf_id != prcl_key:
                raise ValueError(
                    f"erfId/sg.prclKey mismatch at ERF JSONL line {line_number}"
                )

            comparison_key = normalize_pipeline_prcl_key(prcl_key)
            if not comparison_key:
                raise ValueError(
                    f"Invalid sg.prclKey at ERF JSONL line {line_number}"
                )

            admin = _require_object(record.get("admin"), "admin", line_number)
            lm = _require_object(
                admin.get("localMunicipality"),
                "admin.localMunicipality",
                line_number,
            )
            ward = _require_object(
                admin.get("ward"),
                "admin.ward",
                line_number,
            )
            centroid = _require_object(
                record.get("centroid"),
                "centroid",
                line_number,
            )

            lm_pcode = clean_text(lm.get("pcode")).upper()
            if lm_pcode != expected_lm_pcode:
                raise ValueError(
                    f"Wrong LM pCode at ERF JSONL line {line_number}: "
                    f"{lm_pcode!r}"
                )

            latitude = _finite_float(
                centroid.get("lat"),
                "centroid.lat",
                line_number,
            )
            longitude = _finite_float(
                centroid.get("lng"),
                "centroid.lng",
                line_number,
            )
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError(
                    f"Coordinate out of range at ERF JSONL line {line_number}"
                )

            erf_number = normalize_erf_number(
                sg.get("erfNo")
                if sg.get("erfNo") is not None
                else sg.get("parcelNo")
            )
            if not erf_number:
                raise ValueError(
                    f"Blank sg.erfNo/sg.parcelNo at ERF JSONL line {line_number}"
                )

            candidate = ErfCandidate(
                erf_number=erf_number,
                erf_id=erf_id,
                ward_number=clean_text(ward.get("name")),
                ward_pcode=clean_text(ward.get("pcode")),
                lm_pcode=lm_pcode,
                latitude=latitude,
                longitude=longitude,
            )
            by_comparison_key[comparison_key].append(candidate)

    if row_count != expected_record_count:
        raise ValueError(
            f"ERF JSONL record-count mismatch. Expected "
            f"{expected_record_count:,}, found {row_count:,}."
        )

    for candidates in by_comparison_key.values():
        candidates.sort(key=lambda candidate: candidate.erf_id)

    return dict(by_comparison_key), {
        "records": row_count,
        "uniqueErfIds": len(seen_erf_ids),
        "uniqueNormalizedParcelKeys": len(by_comparison_key),
        "duplicateNormalizedParcelKeyGroups": sum(
            len(candidates) > 1
            for candidates in by_comparison_key.values()
        ),
        "erfPipelinePrefixRemovedForComparison": ERF_PIPELINE_PREFIX,
        "geometryReadForPsd": False,
    }


def resolve_gis_key(
    gis_key: str,
    lookup_by_comparison_key: dict[str, list[ErfCandidate]],
) -> tuple[list[ErfCandidate], str]:
    matching: dict[str, ErfCandidate] = {}
    matched_rules: list[str] = []

    exact = lookup_by_comparison_key.get(gis_key, [])
    if exact:
        matched_rules.append("EXACT_FORMAT")
        for candidate in exact:
            matching[candidate.erf_id] = candidate

    with_trailing_zero = lookup_by_comparison_key.get(f"{gis_key}0", [])
    if with_trailing_zero:
        matched_rules.append("APPEND_ONE_TRAILING_ZERO")
        for candidate in with_trailing_zero:
            matching[candidate.erf_id] = candidate

    if not matching:
        return [], "NO_MATCH"
    if len(matched_rules) > 1:
        return sorted(matching.values(), key=lambda item: item.erf_id), (
            "MULTIPLE_COMPARISON_FORMATS"
        )
    return sorted(matching.values(), key=lambda item: item.erf_id), matched_rules[0]


def enrich_end_rows(
    end_rows: list[dict[str, Any]],
    bridge_by_account: dict[str, BridgeAccountLink],
    lookup_by_comparison_key: dict[str, list[ErfCandidate]],
    progress_every: int,
) -> tuple[list[EnrichedMeter], dict[str, Any]]:
    enriched: list[EnrichedMeter] = []
    status_counts = Counter()
    detail_counts = Counter()
    normalization_rule_counts = Counter()

    matched_end_rows = 0
    multiple_bridge_gis_rows = 0
    candidate_relationships = 0

    for index, source in enumerate(end_rows, start=1):
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"[PROGRESS] Enriched {index:,} / "
                f"{len(end_rows):,} END meters"
            )

        account = source["account_normalized"]
        bridge_link = bridge_by_account.get(account) if account else None
        bridge_source_rows = (
            list(bridge_link.source_rows)
            if bridge_link is not None
            else []
        )
        gis_keys = (
            list(bridge_link.gis_keys)
            if bridge_link is not None
            else []
        )

        candidates: list[ErfCandidate] = []
        normalization_rule = "NONE"

        if not account:
            status = "BLANK_ACCOUNT_NUMBER"
            detail_status = "BLANK_ACCOUNT"
        elif bridge_link is None:
            # Legacy PSD status value retained.
            status = "ACCOUNT_NOT_FOUND_IN_ELM"
            detail_status = "ACCOUNT_NOT_IN_BRIDGE"
        elif not gis_keys:
            # Legacy PSD status value retained.
            status = "ERF_NUMBER_NOT_FOUND_IN_PIPELINE_LOOKUP"
            detail_status = "ACCOUNT_HAS_NO_GIS_KEY"
        elif len(gis_keys) > 1:
            status = "PARTIAL_ERF_LOOKUP_MATCH"
            detail_status = "ACCOUNT_MULTIPLE_GIS_KEYS"
            multiple_bridge_gis_rows += 1
        else:
            candidates_found, normalization_rule = resolve_gis_key(
                gis_keys[0],
                lookup_by_comparison_key,
            )
            if len(candidates_found) == 0:
                status = "ERF_NUMBER_NOT_FOUND_IN_PIPELINE_LOOKUP"
                detail_status = "GIS_KEY_NOT_FOUND_IN_PIPELINE"
            elif len(candidates_found) > 1:
                # Never place multiple locations in the PSD.
                status = "PARTIAL_ERF_LOOKUP_MATCH"
                detail_status = "GIS_KEY_MULTIPLE_ERFS"
            else:
                candidates = candidates_found
                status = "MATCHED_SINGLE_GPS"
                detail_status = "EXACT_ONE_TO_ONE"

        if len(candidates) > 1:
            raise ValueError(
                f"One-to-one gate failed for meter "
                f"{source['identity']['MeterNumber']}"
            )

        matched_end_rows += bridge_link is not None
        candidate_relationships += len(candidates)
        status_counts[status] += 1
        detail_counts[detail_status] += 1
        normalization_rule_counts[normalization_rule] += 1

        erf_numbers = [candidate.erf_number for candidate in candidates]

        enriched.append(
            EnrichedMeter(
                source_end_row=source["source_row"],
                identity=source["identity"],
                sales=source["sales"],
                units=source["units"],
                account_number_normalized=account,
                elm_source_rows=bridge_source_rows,
                erf_numbers=erf_numbers,
                missing_erf_numbers=[],
                candidates=candidates,
                gps_match_status=status,
            )
        )

    meters_with_gps = sum(item.has_usable_gps for item in enriched)
    meters_without_gps = len(enriched) - meters_with_gps

    return enriched, {
        "records": len(enriched),
        "matchedEndRowsByAccount": matched_end_rows,
        "unmatchedEndRowsByAccount": len(enriched) - matched_end_rows,
        # Legacy summary key retained, now measuring multiple bridge GIS keys.
        "endRowsWithMultipleElmErfNumbers": multiple_bridge_gis_rows,
        "endRowsWithMultipleGpsCandidates": 0,
        "metersWithUsableGps": meters_with_gps,
        "metersWithoutUsableGps": meters_without_gps,
        "candidateLocationRelationships": candidate_relationships,
        "statusCounts": dict(sorted(status_counts.items())),
        "linkageDetailCounts": dict(sorted(detail_counts.items())),
        "comparisonNormalizationCounts": dict(
            sorted(normalization_rule_counts.items())
        ),
    }
