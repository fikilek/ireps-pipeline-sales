"""
Stage 03: build the three Conlog Monthly Sales datasets for one LM and one month.

This build is environment-neutral and never connects to Firebase.

Operating grain:
    one LM + one month per execution

Input:
    output/atomic/atomic__conlog_prepaid_sales__<lmPcode>__YYYY-MM__<rows>.csv

Outputs:
    output/monthly/monthly__FULL__YYYY-MM__from_atomic.csv
    output/monthly_lm/monthly_lm__FULL__YYYY-MM__from_atomic.csv
    output/monthly_lm_groups/monthly_lm_groups__FULL__YYYY-MM__from_atomic.csv

Safety model:
    - require explicit --lm-pcode and --month;
    - select exactly one Atomic CSV for that LM/month;
    - validate the exact Atomic schema and governed identities;
    - do not impose a fixed meter-number length;
    - reconcile Atomic, meter-month, LM-month, and LM-month-group totals;
    - preflight before writing;
    - protect different existing outputs unless --replace-existing is supplied;
    - write through temporary files and verify SHA-256;
    - emit a month-specific JSON build manifest for Stage 04;
    - never process a hidden date range in one execution.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATOMIC_DIR = PROJECT_ROOT / "output" / "atomic"
DEFAULT_MONTHLY_DIR = PROJECT_ROOT / "output" / "monthly"
DEFAULT_MONTHLY_LM_DIR = PROJECT_ROOT / "output" / "monthly_lm"
DEFAULT_MONTHLY_LM_GROUPS_DIR = PROJECT_ROOT / "output" / "monthly_lm_groups"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_build"

RUN_TAG = "FULL"
CONLOG_VENDING_PROVIDER_ID = "vpr_7f4d3c91a2b84e6f"

ATOMIC_COLUMNS = [
    "atomicId",
    "vendingProviderId",
    "lmPcode",
    "meterNo",
    "txAtISO",
    "txAtMs",
    "ym",
    "y",
    "m",
    "amountTotalC",
    "costC",
    "vatC",
    "currency",
    "sourceFileId",
    "sourceRow",
    "ingestedAtISO",
    "ingestedAtMs",
]

MONTHLY_COLUMNS = [
    "docId",
    "lmPcode",
    "meterNo",
    "ym",
    "y",
    "m",
    "purchasesCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
    "salesGroupId",
    "salesGroupLabel",
]

MONTHLY_LM_COLUMNS = [
    "docId",
    "lmPcode",
    "ym",
    "y",
    "m",
    "purchasesCount",
    "metersCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
]

MONTHLY_LM_GROUP_COLUMNS = [
    "docId",
    "lmPcode",
    "ym",
    "y",
    "m",
    "salesGroupId",
    "salesGroupLabel",
    "metersCount",
    "purchasesCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
]

ATOMIC_FILENAME_RE = re.compile(
    r"^atomic__conlog_prepaid_sales__"
    r"(?P<lm_pcode>[A-Za-z0-9_-]+)__"
    r"(?P<period>\d{4}-\d{2})__"
    r"(?P<rows>\d+)\.csv$"
)


@dataclass
class AtomicMonth:
    path: Path
    period: str
    frame: pd.DataFrame
    file_sha256: str


@dataclass
class PlannedOutput:
    dataset: str
    month: str
    path: Path
    frame: pd.DataFrame
    payload: bytes
    sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build and reconcile one Conlog Monthly Sales LM/month from one Atomic CSV."
        )
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Expected LM pCode. It must match the Atomic filename and every row.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Sales month to aggregate in YYYY-MM format.",
    )
    parser.add_argument("--atomic-dir", type=Path, default=DEFAULT_ATOMIC_DIR)
    parser.add_argument("--monthly-dir", type=Path, default=DEFAULT_MONTHLY_DIR)
    parser.add_argument("--monthly-lm-dir", type=Path, default=DEFAULT_MONTHLY_LM_DIR)
    parser.add_argument(
        "--monthly-lm-groups-dir",
        type=Path,
        default=DEFAULT_MONTHLY_LM_GROUPS_DIR,
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Replace only the three different outputs for the requested LM/month "
            "after deliberate review."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate, aggregate, reconcile, and report without writing CSV outputs.",
    )
    return parser.parse_args()


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_meter_no(value: object) -> str:
    return "".join(clean_text(value).upper().split())


def validate_month(value: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"--month must use YYYY-MM format: {value!r}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def run_id(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def read_csv_robust(path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding)
        except Exception as exc:
            last_error = exc
    if last_error is None:
        raise RuntimeError(f"Unable to read CSV: {path}")
    raise last_error


def strict_integer_series(frame: pd.DataFrame, column: str) -> pd.Series:
    raw = frame[column].map(clean_text)
    invalid = ~raw.str.fullmatch(r"-?\d+")
    if invalid.any():
        examples = [int(index) + 2 for index in invalid[invalid].index[:5]]
        raise ValueError(
            f"{column} must contain integer text in every row. "
            f"Invalid CSV line examples: {examples}"
        )
    return raw.astype("int64")


def validate_atomic_ids(frame: pd.DataFrame) -> None:
    base_key = (
        frame["vendingProviderId"].astype(str)
        + "|"
        + frame["lmPcode"].astype(str)
        + "|"
        + frame["meterNo"].astype(str)
        + "|"
        + frame["txAtISO"].astype(str)
        + "|"
        + frame["amountTotalC"].astype(str)
        + "|"
        + frame["costC"].astype(str)
        + "|"
        + frame["vatC"].astype(str)
    )
    base_hash = base_key.map(sha1_text)
    duplicate_index = base_hash.groupby(base_hash, sort=False).cumcount() + 1
    expected = base_hash.where(
        duplicate_index.eq(1),
        base_hash + "__" + duplicate_index.astype(str),
    )
    mismatch = frame["atomicId"].ne(expected)
    if mismatch.any():
        examples: list[dict[str, Any]] = []
        for index in mismatch[mismatch].index[:5]:
            examples.append(
                {
                    "csvLine": int(index) + 2,
                    "actual": frame.at[index, "atomicId"],
                    "expected": expected.at[index],
                }
            )
        raise ValueError(f"atomicId validation failed. Examples: {examples}")


def select_atomic_path(
    atomic_dir: Path,
    *,
    lm_pcode: str,
    month: str,
) -> Path:
    directory = atomic_dir.expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"Atomic directory not found: {directory}")

    validate_month(month)
    pattern = f"atomic__conlog_prepaid_sales__{lm_pcode}__{month}__*.csv"
    matches = sorted(directory.glob(pattern))
    if not matches:
        raise ValueError(
            f"Expected one Atomic CSV for {lm_pcode}/{month}; none matched {pattern}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous Atomic inputs for {lm_pcode}/{month}: "
            f"{[path.name for path in matches]}"
        )
    return matches[0]


def validate_atomic_month(
    path: Path,
    *,
    expected_lm_pcode: str,
    expected_period: str,
) -> AtomicMonth:
    match = ATOMIC_FILENAME_RE.fullmatch(path.name)
    if not match:
        raise ValueError(f"Invalid Atomic filename: {path.name}")
    declared_rows = int(match.group("rows"))
    if match.group("lm_pcode").upper() != expected_lm_pcode:
        raise ValueError(f"Atomic filename LM mismatch: {path.name}")
    if match.group("period") != expected_period:
        raise ValueError(f"Atomic filename month mismatch: {path.name}")

    frame = read_csv_robust(path)
    if list(frame.columns) != ATOMIC_COLUMNS:
        raise ValueError(
            f"{path.name} Atomic schema mismatch. "
            f"Expected {ATOMIC_COLUMNS}; found {list(frame.columns)}"
        )
    if frame.empty:
        raise ValueError(f"Atomic CSV is empty: {path}")
    if len(frame) != declared_rows:
        raise ValueError(
            f"{path.name} declares {declared_rows:,} rows but contains {len(frame):,}"
        )

    string_columns = [
        "atomicId",
        "vendingProviderId",
        "lmPcode",
        "meterNo",
        "txAtISO",
        "ym",
        "currency",
        "sourceFileId",
        "ingestedAtISO",
    ]
    for column in string_columns:
        cleaned = frame[column].map(clean_text)
        if cleaned.eq("").any():
            raise ValueError(f"{path.name}: blank values in {column}")
        if frame[column].astype(str).ne(cleaned).any():
            raise ValueError(f"{path.name}: whitespace drift in {column}")
        frame[column] = cleaned

    numeric_columns = [
        "txAtMs",
        "y",
        "m",
        "amountTotalC",
        "costC",
        "vatC",
        "sourceRow",
        "ingestedAtMs",
    ]
    for column in numeric_columns:
        frame[column] = strict_integer_series(frame, column)

    if frame["atomicId"].duplicated().any():
        raise ValueError(f"{path.name}: duplicate atomicId values")

    if not frame["vendingProviderId"].eq(CONLOG_VENDING_PROVIDER_ID).all():
        raise ValueError(f"{path.name}: unexpected vendingProviderId")
    if not frame["lmPcode"].eq(expected_lm_pcode).all():
        raise ValueError(f"{path.name}: lmPcode mismatch")
    if not frame["ym"].eq(expected_period).all():
        raise ValueError(f"{path.name}: ym mismatch")

    expected_year, expected_month = (int(part) for part in expected_period.split("-"))
    if not frame["y"].eq(expected_year).all() or not frame["m"].eq(expected_month).all():
        raise ValueError(f"{path.name}: y/m values do not match {expected_period}")

    meter_normalized = frame["meterNo"].map(normalize_meter_no)
    meter_valid = frame["meterNo"].str.fullmatch(r"[A-Z0-9]+")
    if not meter_valid.all():
        examples = frame.loc[~meter_valid, "meterNo"].head(5).tolist()
        raise ValueError(
            f"{path.name}: meterNo must be non-empty uppercase alphanumeric. "
            f"Examples: {examples}"
        )
    if not frame["meterNo"].eq(meter_normalized).all():
        raise ValueError(f"{path.name}: meterNo values are not canonically normalized")

    parsed_tx = pd.to_datetime(
        frame["txAtISO"],
        format="%Y-%m-%dT%H:%M:%S",
        errors="coerce",
    )
    if parsed_tx.isna().any():
        raise ValueError(f"{path.name}: invalid txAtISO")
    if not parsed_tx.dt.strftime("%Y-%m").eq(expected_period).all():
        raise ValueError(f"{path.name}: transaction outside requested month")
    expected_tx_ms = (parsed_tx.astype("int64") // 1_000_000).astype("int64")
    if not frame["txAtMs"].eq(expected_tx_ms).all():
        raise ValueError(f"{path.name}: txAtMs does not match txAtISO")

    if not (
        frame["amountTotalC"].ge(0)
        & frame["costC"].ge(0)
        & frame["vatC"].ge(0)
    ).all():
        raise ValueError(f"{path.name}: negative monetary value")
    if not frame["amountTotalC"].eq(frame["costC"] + frame["vatC"]).all():
        raise ValueError(f"{path.name}: amountTotalC != costC + vatC")
    if not frame["currency"].eq("ZAR").all():
        raise ValueError(f"{path.name}: currency must be ZAR")

    expected_source = (
        f"conlog_prepaid_sales__{expected_lm_pcode}__{expected_period}.csv"
    )
    if not frame["sourceFileId"].eq(expected_source).all():
        raise ValueError(f"{path.name}: sourceFileId mismatch")
    expected_rows = pd.Series(range(1, len(frame) + 1), index=frame.index)
    if not frame["sourceRow"].eq(expected_rows).all():
        raise ValueError(f"{path.name}: sourceRow must be 1..N")

    parsed_ingested = pd.to_datetime(
        frame["ingestedAtISO"],
        format="%Y-%m-%dT%H:%M:%SZ",
        errors="coerce",
        utc=True,
    )
    if parsed_ingested.isna().any():
        raise ValueError(f"{path.name}: invalid ingestedAtISO")
    if frame["ingestedAtISO"].nunique() != 1 or frame["ingestedAtMs"].nunique() != 1:
        raise ValueError(f"{path.name}: ingestion values must be constant for the file")
    second_start_ms = (parsed_ingested.astype("int64") // 1_000_000).astype("int64")
    delta_ms = frame["ingestedAtMs"] - second_start_ms
    if not delta_ms.between(0, 999, inclusive="both").all():
        raise ValueError(f"{path.name}: ingestedAtMs does not match ingestedAtISO")

    validate_atomic_ids(frame)
    return AtomicMonth(
        path=path,
        period=expected_period,
        frame=frame,
        file_sha256=sha256_file(path),
    )


def load_atomic(
    path: Path,
    *,
    month: str,
    lm_pcode: str,
) -> tuple[pd.DataFrame, AtomicMonth]:
    loaded = validate_atomic_month(
        path,
        expected_lm_pcode=lm_pcode,
        expected_period=month,
    )
    frame = loaded.frame.copy()
    if frame["atomicId"].duplicated().any():
        raise ValueError("Duplicate atomicId values exist in the selected Atomic CSV")
    return frame, loaded


def sales_group_from_amount_total_c(total_cents: int) -> str:
    if total_cents <= 9_999:
        return "GR1"
    if total_cents <= 29_999:
        return "GR2"
    if total_cents <= 49_999:
        return "GR3"
    if total_cents <= 99_999:
        return "GR4"
    return "GR5"


def sales_group_label(group_id: str) -> str:
    labels = {
        "GR1": "<=99.99",
        "GR2": "100-299.99",
        "GR3": "300-499.99",
        "GR4": "500-999.99",
        "GR5": ">=1000",
    }
    return labels[group_id]


def ms_to_iso_z(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def aggregate_monthly(atomic: pd.DataFrame) -> pd.DataFrame:
    result = (
        atomic.groupby(["lmPcode", "meterNo", "ym", "y", "m"], as_index=False)
        .agg(
            purchasesCount=("atomicId", "count"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("txAtMs", "min"),
            lastPurchaseAtMs=("txAtMs", "max"),
        )
    )
    result["firstPurchaseAtISO"] = ms_to_iso_z(result["firstPurchaseAtMs"])
    result["lastPurchaseAtISO"] = ms_to_iso_z(result["lastPurchaseAtMs"])
    result["salesGroupId"] = result["amountTotalC"].map(
        sales_group_from_amount_total_c
    )
    result["salesGroupLabel"] = result["salesGroupId"].map(sales_group_label)
    result["docId"] = (
        result["lmPcode"]
        + "__"
        + result["meterNo"]
        + "__"
        + result["ym"]
    )
    return result[MONTHLY_COLUMNS].sort_values(
        ["ym", "lmPcode", "meterNo"], kind="stable"
    ).reset_index(drop=True)


def aggregate_monthly_lm(atomic: pd.DataFrame) -> pd.DataFrame:
    result = (
        atomic.groupby(["lmPcode", "ym", "y", "m"], as_index=False)
        .agg(
            purchasesCount=("atomicId", "count"),
            metersCount=("meterNo", pd.Series.nunique),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("txAtMs", "min"),
            lastPurchaseAtMs=("txAtMs", "max"),
        )
    )
    result["firstPurchaseAtISO"] = ms_to_iso_z(result["firstPurchaseAtMs"])
    result["lastPurchaseAtISO"] = ms_to_iso_z(result["lastPurchaseAtMs"])
    result["docId"] = result["lmPcode"] + "__" + result["ym"]
    return result[MONTHLY_LM_COLUMNS].sort_values(
        ["ym", "lmPcode"], kind="stable"
    ).reset_index(drop=True)


def aggregate_monthly_lm_groups(monthly: pd.DataFrame) -> pd.DataFrame:
    result = (
        monthly.groupby(
            ["lmPcode", "ym", "y", "m", "salesGroupId", "salesGroupLabel"],
            as_index=False,
        )
        .agg(
            metersCount=("meterNo", "count"),
            purchasesCount=("purchasesCount", "sum"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("firstPurchaseAtMs", "min"),
            lastPurchaseAtMs=("lastPurchaseAtMs", "max"),
        )
    )
    result["firstPurchaseAtISO"] = ms_to_iso_z(result["firstPurchaseAtMs"])
    result["lastPurchaseAtISO"] = ms_to_iso_z(result["lastPurchaseAtMs"])
    result["docId"] = (
        result["lmPcode"]
        + "__"
        + result["ym"]
        + "__"
        + result["salesGroupId"]
    )
    return result[MONTHLY_LM_GROUP_COLUMNS].sort_values(
        ["ym", "lmPcode", "salesGroupId"], kind="stable"
    ).reset_index(drop=True)


def assert_unique(frame: pd.DataFrame, column: str, dataset: str) -> None:
    if frame[column].duplicated().any():
        examples = frame.loc[frame[column].duplicated(False), column].head(5).tolist()
        raise ValueError(f"{dataset}: duplicate {column} values: {examples}")


def reconcile(
    atomic: pd.DataFrame,
    monthly: pd.DataFrame,
    monthly_lm: pd.DataFrame,
    monthly_groups: pd.DataFrame,
) -> list[dict[str, Any]]:
    assert_unique(monthly, "docId", "monthly")
    assert_unique(monthly_lm, "docId", "monthly_lm")
    assert_unique(monthly_groups, "docId", "monthly_lm_groups")

    expected_group = monthly["amountTotalC"].map(sales_group_from_amount_total_c)
    if not monthly["salesGroupId"].eq(expected_group).all():
        raise ValueError("monthly: salesGroupId classification mismatch")
    expected_label = monthly["salesGroupId"].map(sales_group_label)
    if not monthly["salesGroupLabel"].eq(expected_label).all():
        raise ValueError("monthly: salesGroupLabel mismatch")

    keys = ["lmPcode", "ym"]
    atomic_rollup = (
        atomic.groupby(keys, as_index=False)
        .agg(
            purchasesCount=("atomicId", "count"),
            metersCount=("meterNo", pd.Series.nunique),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("txAtMs", "min"),
            lastPurchaseAtMs=("txAtMs", "max"),
        )
    )
    monthly_rollup = (
        monthly.groupby(keys, as_index=False)
        .agg(
            purchasesCount=("purchasesCount", "sum"),
            metersCount=("meterNo", "count"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("firstPurchaseAtMs", "min"),
            lastPurchaseAtMs=("lastPurchaseAtMs", "max"),
        )
    )
    group_rollup = (
        monthly_groups.groupby(keys, as_index=False)
        .agg(
            purchasesCount=("purchasesCount", "sum"),
            metersCount=("metersCount", "sum"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("firstPurchaseAtMs", "min"),
            lastPurchaseAtMs=("lastPurchaseAtMs", "max"),
        )
    )

    compare_columns = [
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]

    def compare(left: pd.DataFrame, right: pd.DataFrame, label: str) -> None:
        joined = left.merge(
            right,
            on=keys,
            how="outer",
            suffixes=("_left", "_right"),
            indicator=True,
        )
        if not joined["_merge"].eq("both").all():
            raise ValueError(f"{label}: missing LM/month rows")
        differences: list[str] = []
        for column in compare_columns:
            mismatch = joined[f"{column}_left"].ne(joined[f"{column}_right"])
            if mismatch.any():
                differences.append(column)
        if differences:
            raise ValueError(f"{label}: reconciliation differences in {differences}")

    compare(atomic_rollup, monthly_rollup, "Atomic vs monthly")
    compare(atomic_rollup, monthly_lm[keys + compare_columns], "Atomic vs monthly_lm")
    compare(monthly_lm[keys + compare_columns], group_rollup, "monthly_lm vs groups")

    if not monthly["amountTotalC"].eq(monthly["costC"] + monthly["vatC"]).all():
        raise ValueError("monthly monetary reconciliation failed")
    if not monthly_lm["amountTotalC"].eq(
        monthly_lm["costC"] + monthly_lm["vatC"]
    ).all():
        raise ValueError("monthly_lm monetary reconciliation failed")
    if not monthly_groups["amountTotalC"].eq(
        monthly_groups["costC"] + monthly_groups["vatC"]
    ).all():
        raise ValueError("monthly_lm_groups monetary reconciliation failed")

    summaries: list[dict[str, Any]] = []
    for row in atomic_rollup.sort_values(keys).itertuples(index=False):
        summaries.append(
            {
                "lmPcode": row.lmPcode,
                "month": row.ym,
                "purchasesCount": int(row.purchasesCount),
                "metersCount": int(row.metersCount),
                "amountTotalC": int(row.amountTotalC),
                "costC": int(row.costC),
                "vatC": int(row.vatC),
            }
        )
    return summaries


def dataframe_bytes(frame: pd.DataFrame, columns: list[str]) -> bytes:
    return frame[columns].to_csv(index=False, lineterminator="\n").encode("utf-8")


def plan_outputs(
    monthly: pd.DataFrame,
    monthly_lm: pd.DataFrame,
    monthly_groups: pd.DataFrame,
    *,
    month: str,
    monthly_dir: Path,
    monthly_lm_dir: Path,
    monthly_groups_dir: Path,
) -> list[PlannedOutput]:
    outputs: list[PlannedOutput] = []

    datasets = [
        (
            "monthly",
            monthly_dir / f"monthly__{RUN_TAG}__{month}__from_atomic.csv",
            monthly,
            MONTHLY_COLUMNS,
        ),
        (
            "monthly_lm",
            monthly_lm_dir / f"monthly_lm__{RUN_TAG}__{month}__from_atomic.csv",
            monthly_lm,
            MONTHLY_LM_COLUMNS,
        ),
        (
            "monthly_lm_groups",
            monthly_groups_dir
            / f"monthly_lm_groups__{RUN_TAG}__{month}__from_atomic.csv",
            monthly_groups,
            MONTHLY_LM_GROUP_COLUMNS,
        ),
    ]

    for dataset, path, frame, columns in datasets:
        if not frame["ym"].eq(month).all():
            raise ValueError(f"{dataset}: output contains a month other than {month}")
        frame = frame.reset_index(drop=True)
        payload = dataframe_bytes(frame, columns)
        outputs.append(
            PlannedOutput(
                dataset=dataset,
                month=month,
                path=path.expanduser().resolve(),
                frame=frame,
                payload=payload,
                sha256=sha256_bytes(payload),
            )
        )

    return outputs


def inspect_output_state(
    outputs: list[PlannedOutput],
    *,
    replace_existing: bool,
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for item in outputs:
        if not item.path.exists():
            state = "MISSING"
            existing_sha = ""
        else:
            existing_sha = sha256_file(item.path)
            state = "IDENTICAL" if existing_sha == item.sha256 else "DIFFERENT"
            if state == "DIFFERENT" and not replace_existing:
                raise ValueError(
                    f"Different existing output requires --replace-existing: {item.path}"
                )
        states.append(
            {
                "dataset": item.dataset,
                "month": item.month,
                "path": str(item.path),
                "filename": item.path.name,
                "rows": len(item.frame),
                "sha256": item.sha256,
                "existingState": state,
                "existingSha256": existing_sha,
            }
        )
    return states


def write_outputs(outputs: list[PlannedOutput]) -> dict[str, int]:
    written = 0
    unchanged = 0
    for item in outputs:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        if item.path.exists() and sha256_file(item.path) == item.sha256:
            unchanged += 1
            continue
        temporary = item.path.with_suffix(item.path.suffix + ".tmp")
        temporary.write_bytes(item.payload)
        if sha256_file(temporary) != item.sha256:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Temporary output SHA-256 mismatch: {item.path}")
        temporary.replace(item.path)
        if sha256_file(item.path) != item.sha256:
            raise ValueError(f"Written output SHA-256 mismatch: {item.path}")
        written += 1
    return {"written": written, "unchanged": unchanged}


def report_path(
    log_dir: Path,
    lm_pcode: str,
    month: str,
    started: dt.datetime,
) -> Path:
    return (
        log_dir.expanduser().resolve()
        / f"stage03_monthly_build__{lm_pcode}__{month}__{run_id(started)}.json"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, indent=2, sort_keys=True, ensure_ascii=False)
        target.write("\n")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    started = utc_now()
    lm_pcode = clean_text(args.lm_pcode).upper()
    month = clean_text(args.month)
    report: dict[str, Any] = {
        "stage": "03",
        "script": "03_aggregate_monthly_from_atomic_outputs.py",
        "status": "STARTED",
        "operation": "preflight-only" if args.preflight_only else "build-write",
        "lmPcode": lm_pcode,
        "month": month,
        "startedAt": utc_iso(started),
    }
    log_path = report_path(
        args.log_dir,
        lm_pcode or "unknown",
        month or "unknown",
        started,
    )

    try:
        if not lm_pcode:
            raise ValueError("--lm-pcode cannot be blank")
        validate_month(month)

        atomic_path = select_atomic_path(
            args.atomic_dir,
            lm_pcode=lm_pcode,
            month=month,
        )
        atomic, loaded = load_atomic(
            atomic_path,
            month=month,
            lm_pcode=lm_pcode,
        )

        monthly = aggregate_monthly(atomic)
        monthly_lm = aggregate_monthly_lm(atomic)
        monthly_groups = aggregate_monthly_lm_groups(monthly)
        reconciliation = reconcile(atomic, monthly, monthly_lm, monthly_groups)

        outputs = plan_outputs(
            monthly,
            monthly_lm,
            monthly_groups,
            month=month,
            monthly_dir=args.monthly_dir.expanduser().resolve(),
            monthly_lm_dir=args.monthly_lm_dir.expanduser().resolve(),
            monthly_groups_dir=args.monthly_lm_groups_dir.expanduser().resolve(),
        )
        output_states = inspect_output_state(
            outputs,
            replace_existing=args.replace_existing,
        )

        report.update(
            {
                "atomicFile": {
                    "month": loaded.period,
                    "path": str(loaded.path),
                    "filename": loaded.path.name,
                    "rows": len(loaded.frame),
                    "sha256": loaded.file_sha256,
                },
                "atomicRows": len(atomic),
                "atomicUniqueMeters": int(atomic["meterNo"].nunique()),
                "monthlyRows": len(monthly),
                "monthlyLmRows": len(monthly_lm),
                "monthlyLmGroupRows": len(monthly_groups),
                "reconciliation": reconciliation,
                "outputs": output_states,
            }
        )

        print("[STAGE 03] ONE ATOMIC MONTH -> THREE MONTHLY DATASETS")
        print(f"  LM:                    {lm_pcode}")
        print(f"  month:                 {month}")
        print(f"  Atomic file:           {loaded.path.name}")
        print(f"  Atomic rows:           {len(atomic):,}")
        print(f"  unique meters:         {atomic['meterNo'].nunique():,}")
        print(f"  monthly rows:          {len(monthly):,}")
        print(f"  monthly LM rows:       {len(monthly_lm):,}")
        print(f"  monthly group rows:    {len(monthly_groups):,}")
        print(f"  planned output files:  {len(outputs)}")
        print("  reconciliation:        PASS")

        if args.preflight_only:
            report["status"] = "PASS"
            report["result"] = "PREFLIGHT_OK"
            report["writeSummary"] = {"written": 0, "unchanged": 0}
            print("\n[PREFLIGHT OK] No monthly output files were written.")
        else:
            summary = write_outputs(outputs)
            report["writeSummary"] = summary
            report["status"] = "PASS"
            report["result"] = "BUILD_WRITTEN"
            print(
                "\n[BUILD VERIFIED] "
                f"written={summary['written']}, unchanged={summary['unchanged']}"
            )
        return 0

    except Exception as exc:
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        return 1

    finally:
        report["finishedAt"] = utc_iso(utc_now())
        try:
            write_json(log_path, report)
            print(f"\n[REPORT / MANIFEST] {log_path}")
        except Exception as report_error:
            print(
                f"[WARN] Could not write Stage 03 report: {report_error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
