"""Source contracts, merge model, and enrichment logic for Stage M02."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lib.xlsx_stream import XlsxReader


END_SHEET_NAME = "Sheet1"
ELM_SHEET_NAME = "ELM METERS"

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

ELM_COLUMNS = {"D": "ACCOUNT NO", "E": "ERF NUMBER"}
LOOKUP_REQUIRED_COLUMNS = [
    "erfId", "erfNumber", "erfNumberNormalized", "wardNumber",
    "wardPcode", "lmPcode", "latitude", "longitude", "geometryJson",
]


@dataclass(frozen=True)
class ElmErfLink:
    account_number_normalized: str
    erf_number: str
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
    geometry: dict[str, Any]

    def as_document(self) -> dict[str, Any]:
        return {
            "ErfNumber": self.erf_number,
            "ErfId": self.erf_id,
            "WardNumber": self.ward_number,
            "WardPcode": self.ward_pcode,
            "LmPcode": self.lm_pcode,
            "Latitude": self.latitude,
            "Longitude": self.longitude,
            "Geometry": self.geometry,
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
        return bool(self.candidates)

    def as_json_document(self) -> dict[str, Any]:
        return {
            "SourceEndRow": self.source_end_row,
            **self.identity,
            "AccountNumberNormalized": self.account_number_normalized,
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
            "Geometry": compact_json([candidate.geometry for candidate in self.candidates]),
            "ErfCandidatesJson": compact_json(candidate_documents),
            "ElmSourceRows": join_unique(str(row) for row in self.elm_source_rows),
            "trnBatchIds": "[]",
            **{f"Sales_{month}": value for month, value in self.sales.items()},
            **{f"Units_{month}": value for month, value in self.units.items()},
        }


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_account_number(value: Any) -> str:
    text = re.sub(r"\s+", "", clean_text(value))
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def normalize_erf_number(value: Any) -> str:
    text = re.sub(r"\s+", "", clean_text(value).upper())
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return str(int(text)) if text.isdigit() else text


def extract_elm_erf_number(value: Any) -> str:
    text = clean_text(value).upper()
    if not text:
        return ""
    parts = text.split("-")
    if len(parts) != 4 or not parts[1].isdigit():
        raise ValueError(
            "Invalid ELM ERF NUMBER format. Expected four hyphen-separated "
            f"segments with a numeric second segment: {text!r}"
        )
    return normalize_erf_number(parts[1])


def normalize_date(value: Any) -> str:
    text = clean_text(value)
    return "" if text.startswith("1900-01-01") else text


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def format_number(value: float) -> str:
    return format(value, ".15g")


def join_unique(values: Iterable[str]) -> str:
    return "|".join(dict.fromkeys(clean_text(value) for value in values if clean_text(value)))


def erf_sort_key(value: str) -> tuple[int, Any]:
    return (0, int(value)) if value.isdigit() else (1, value)


def read_end_workbook(path: Path, expected_record_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_headers = {
        **dict(END_IDENTITY_COLUMNS),
        **dict(SALES_MONTH_COLUMNS),
        **dict(UNITS_MONTH_COLUMNS),
    }
    wanted_columns = set(expected_headers) | {"AU"}
    rows: list[dict[str, Any]] = []
    header_seen = False

    for row_number, values in XlsxReader(path).iter_sheet_rows(END_SHEET_NAME, wanted_columns):
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
                "sales": {month: clean_text(values.get(column, "")) for column, month in SALES_MONTH_COLUMNS},
                "units": {month: clean_text(values.get(column, "")) for column, month in UNITS_MONTH_COLUMNS},
                "account_normalized": normalize_account_number(identity["AccountNumber"]),
                "previous_date_placeholder_cleared": bool(original_previous_date) and not identity["PreviousInstallationDate"],
            }
        )

    if not header_seen:
        raise ValueError("END workbook header row was not found")
    if len(rows) != expected_record_count:
        raise ValueError(
            f"END record-count mismatch. Expected {expected_record_count:,}, found {len(rows):,}."
        )

    meter_numbers = [row["identity"]["MeterNumber"] for row in rows]
    blank_meters = sum(not meter for meter in meter_numbers)
    duplicate_meter_groups = sum(count > 1 for count in Counter(meter_numbers).values() if count)
    if blank_meters:
        raise ValueError(f"END contains {blank_meters:,} blank MeterNumber row(s)")
    if duplicate_meter_groups:
        raise ValueError(f"END contains {duplicate_meter_groups:,} duplicate MeterNumber group(s)")

    accounts = [row["account_normalized"] for row in rows]
    return rows, {
        "records": len(rows),
        "uniqueMeterNumbers": len(set(meter_numbers)),
        "blankAccountNumbers": sum(not account for account in accounts),
        "uniqueNonblankAccountNumbers": len(set(account for account in accounts if account)),
        "previousInstallationDatePlaceholdersCleared": sum(row["previous_date_placeholder_cleared"] for row in rows),
    }


def read_elm_workbook(path: Path, expected_record_count: int) -> tuple[dict[str, list[ElmErfLink]], dict[str, Any]]:
    pair_rows: dict[tuple[str, str], list[int]] = defaultdict(list)
    blank_accounts = 0
    blank_erf_numbers = 0
    data_rows = 0
    header_seen = False

    for row_number, values in XlsxReader(path).iter_sheet_rows(ELM_SHEET_NAME, set(ELM_COLUMNS)):
        if row_number == 1:
            header_seen = True
            for column, expected_header in ELM_COLUMNS.items():
                actual_header = clean_text(values.get(column, ""))
                if actual_header != expected_header:
                    raise ValueError(
                        f"ELM header mismatch at {column}1. Expected {expected_header!r}, found {actual_header!r}."
                    )
            continue

        data_rows += 1
        account = normalize_account_number(values.get("D", ""))
        if not account:
            blank_accounts += 1
            continue
        erf_number = extract_elm_erf_number(values.get("E", ""))
        if not erf_number:
            blank_erf_numbers += 1
            continue
        pair_rows[(account, erf_number)].append(row_number)

    if not header_seen:
        raise ValueError("ELM workbook header row was not found")
    if data_rows != expected_record_count:
        raise ValueError(
            f"ELM record-count mismatch. Expected {expected_record_count:,}, found {data_rows:,}."
        )

    by_account: dict[str, list[ElmErfLink]] = defaultdict(list)
    for (account, erf_number), source_rows in sorted(pair_rows.items()):
        by_account[account].append(ElmErfLink(account, erf_number, tuple(source_rows)))

    account_row_counts = Counter()
    for (account, _erf), source_rows in pair_rows.items():
        account_row_counts[account] += len(source_rows)

    return dict(by_account), {
        "records": data_rows,
        "blankAccountNumbers": blank_accounts,
        "blankErfNumbers": blank_erf_numbers,
        "uniqueAccounts": len(by_account),
        "uniqueAccountErfPairs": len(pair_rows),
        "duplicateExactAccountErfPairGroups": sum(len(rows) > 1 for rows in pair_rows.values()),
        "duplicateExactAccountErfRowsSuppressed": sum(len(rows) - 1 for rows in pair_rows.values() if len(rows) > 1),
        "accountsWithMultipleRows": sum(count > 1 for count in account_row_counts.values()),
        "accountsWithMultipleDistinctErfNumbers": sum(len(links) > 1 for links in by_account.values()),
    }


def _finite_float(value: Any, field_name: str, row_number: int) -> float:
    try:
        result = float(clean_text(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name} at ERF lookup row {row_number}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {field_name} at ERF lookup row {row_number}")
    return result


def read_erf_lookup(path: Path, expected_lm_pcode: str, expected_record_count: int) -> tuple[dict[str, list[ErfCandidate]], dict[str, Any]]:
    by_erf_number: dict[str, list[ErfCandidate]] = defaultdict(list)
    seen_erf_ids: set[str] = set()
    geometry_types = Counter()
    row_count = 0

    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != LOOKUP_REQUIRED_COLUMNS:
            raise ValueError(
                "ERF lookup schema mismatch. Expected exact columns and order: "
                f"{LOOKUP_REQUIRED_COLUMNS}. Found: {reader.fieldnames}"
            )

        for row_count, row in enumerate(reader, start=1):
            csv_row = row_count + 1
            erf_id = clean_text(row["erfId"])
            if not erf_id:
                raise ValueError(f"Blank erfId at ERF lookup row {csv_row}")
            if erf_id in seen_erf_ids:
                raise ValueError(f"Duplicate erfId in ERF lookup: {erf_id}")
            seen_erf_ids.add(erf_id)

            erf_number = normalize_erf_number(row["erfNumberNormalized"])
            lm_pcode = clean_text(row["lmPcode"]).upper()
            if not erf_number:
                raise ValueError(f"Blank normalized ERF number at ERF lookup row {csv_row}")
            if lm_pcode != expected_lm_pcode:
                raise ValueError(f"Wrong LM pCode at ERF lookup row {csv_row}: {lm_pcode!r}")

            latitude = _finite_float(row["latitude"], "latitude", csv_row)
            longitude = _finite_float(row["longitude"], "longitude", csv_row)
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError(f"Coordinate out of range at ERF lookup row {csv_row}")

            try:
                geometry = json.loads(row["geometryJson"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid geometryJson at ERF lookup row {csv_row}: {exc.msg}") from exc
            geometry_type = clean_text(geometry.get("type")) if isinstance(geometry, dict) else ""
            if geometry_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError(f"Unsupported geometry type at ERF lookup row {csv_row}: {geometry_type!r}")
            geometry_types[geometry_type] += 1

            by_erf_number[erf_number].append(
                ErfCandidate(
                    erf_number=erf_number,
                    erf_id=erf_id,
                    ward_number=clean_text(row["wardNumber"]),
                    ward_pcode=clean_text(row["wardPcode"]),
                    lm_pcode=lm_pcode,
                    latitude=latitude,
                    longitude=longitude,
                    geometry=geometry,
                )
            )

    if row_count != expected_record_count:
        raise ValueError(
            f"ERF lookup record-count mismatch. Expected {expected_record_count:,}, found {row_count:,}."
        )
    for candidates in by_erf_number.values():
        candidates.sort(key=lambda candidate: candidate.erf_id)

    return dict(by_erf_number), {
        "records": row_count,
        "uniqueErfIds": len(seen_erf_ids),
        "uniqueNormalizedErfNumbers": len(by_erf_number),
        "duplicateErfNumberGroups": sum(len(candidates) > 1 for candidates in by_erf_number.values()),
        "geometryTypeDistribution": dict(sorted(geometry_types.items())),
    }


def enrich_end_rows(
    end_rows: list[dict[str, Any]],
    elm_by_account: dict[str, list[ElmErfLink]],
    lookup_by_erf_number: dict[str, list[ErfCandidate]],
    progress_every: int,
) -> tuple[list[EnrichedMeter], dict[str, Any]]:
    enriched: list[EnrichedMeter] = []
    status_counts = Counter()
    matched_end_rows = 0
    multiple_elm_erf_rows = 0
    multiple_candidate_rows = 0
    candidate_relationships = 0

    for index, source in enumerate(end_rows, start=1):
        if progress_every > 0 and index % progress_every == 0:
            print(f"[PROGRESS] Enriched {index:,} / {len(end_rows):,} END meters")

        account = source["account_normalized"]
        elm_links = elm_by_account.get(account, []) if account else []
        elm_source_rows = sorted({row for link in elm_links for row in link.source_rows})
        erf_numbers = sorted({link.erf_number for link in elm_links}, key=erf_sort_key)

        candidates_by_id: dict[str, ErfCandidate] = {}
        missing_erf_numbers: list[str] = []
        for erf_number in erf_numbers:
            lookup_candidates = lookup_by_erf_number.get(erf_number, [])
            if not lookup_candidates:
                missing_erf_numbers.append(erf_number)
            for candidate in lookup_candidates:
                candidates_by_id[candidate.erf_id] = candidate
        candidates = sorted(candidates_by_id.values(), key=lambda candidate: candidate.erf_id)

        if not account:
            status = "BLANK_ACCOUNT_NUMBER"
        elif not elm_links:
            status = "ACCOUNT_NOT_FOUND_IN_ELM"
        elif not candidates:
            status = "ERF_NUMBER_NOT_FOUND_IN_PIPELINE_LOOKUP"
        elif missing_erf_numbers:
            status = "PARTIAL_ERF_LOOKUP_MATCH"
        elif len(candidates) == 1:
            status = "MATCHED_SINGLE_GPS"
        else:
            status = "MATCHED_MULTIPLE_GPS"

        matched_end_rows += bool(elm_links)
        multiple_elm_erf_rows += len(erf_numbers) > 1
        multiple_candidate_rows += len(candidates) > 1
        candidate_relationships += len(candidates)
        status_counts[status] += 1

        enriched.append(
            EnrichedMeter(
                source_end_row=source["source_row"],
                identity=source["identity"],
                sales=source["sales"],
                units=source["units"],
                account_number_normalized=account,
                elm_source_rows=elm_source_rows,
                erf_numbers=erf_numbers,
                missing_erf_numbers=missing_erf_numbers,
                candidates=candidates,
                gps_match_status=status,
            )
        )

    return enriched, {
        "records": len(enriched),
        "matchedEndRowsByAccount": matched_end_rows,
        "unmatchedEndRowsByAccount": len(enriched) - matched_end_rows,
        "endRowsWithMultipleElmErfNumbers": multiple_elm_erf_rows,
        "endRowsWithMultipleGpsCandidates": multiple_candidate_rows,
        "metersWithUsableGps": sum(item.has_usable_gps for item in enriched),
        "metersWithoutUsableGps": sum(not item.has_usable_gps for item in enriched),
        "candidateLocationRelationships": candidate_relationships,
        "statusCounts": dict(sorted(status_counts.items())),
    }
