from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "03a_refresh_monthly_source_commercial.py"

COMMERCIAL_HEADERS = [
    "Customer", "TariffInstance", "MeterNumber", "InstallationDate",
    "PreviousMeterNumber", "PreviousInstallationDate", "StandNumber", "Surname",
    "AddressLine1", "AddressLine2", "Town", "PostalAddress1", "PostalAddress2",
    "PostalAddressTown", "AccountNumber",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module():
    spec = importlib.util.spec_from_file_location("stage03a", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def excel_serial(y: int, m: int, d: int = 1) -> int:
    return (datetime(y, m, d) - datetime(1899, 12, 30)).days


def col_letter(number: int) -> str:
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def inline_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def numeric_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}"><v>{value}</v></c>'


def wb_row(
    meter: str,
    *,
    previous: str = "",
    sales: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    customer: str = "CUST1",
    surname: str = "TEST CUSTOMER",
    account: str = "0000000001",
) -> dict:
    return {
        "Customer": customer,
        "TariffInstance": "00101",
        "MeterNumber": meter,
        "InstallationDate": "2026-01-01 00:00:00.000",
        "PreviousMeterNumber": previous,
        "PreviousInstallationDate": "",
        "StandNumber": "1",
        "Surname": surname,
        "AddressLine1": "1 TEST ROAD",
        "AddressLine2": "",
        "Town": "DUNDEE",
        "PostalAddress1": "",
        "PostalAddress2": "",
        "PostalAddressTown": "",
        "AccountNumber": account,
        "sales": sales or {},
        "units": units or {},
    }


def make_xlsx(path: Path, rows_data: list[dict], months: list[str]) -> None:
    # A:O commercial fields, then one contiguous Sales block, one spacer column,
    # then one identical contiguous Units block.
    header_cells: list[str] = []
    for col, name in enumerate(COMMERCIAL_HEADERS, start=1):
        header_cells.append(inline_cell(f"{col_letter(col)}1", name))

    sales_start = 16
    units_start = sales_start + len(months) + 1
    for offset, month in enumerate(months):
        y, m = map(int, month.split("-"))
        serial = str(excel_serial(y, m, 1))
        header_cells.append(numeric_cell(f"{col_letter(sales_start + offset)}1", serial))
        header_cells.append(numeric_cell(f"{col_letter(units_start + offset)}1", serial))

    xml_rows = [f'<row r="1">{"".join(header_cells)}</row>']
    for excel_row, item in enumerate(rows_data, start=2):
        cells: list[str] = []
        for col, name in enumerate(COMMERCIAL_HEADERS, start=1):
            value = str(item.get(name, "") or "")
            if value:
                cells.append(inline_cell(f"{col_letter(col)}{excel_row}", value))
        for offset, month in enumerate(months):
            sale = str(item.get("sales", {}).get(month, "") or "")
            unit = str(item.get("units", {}).get(month, "") or "")
            if sale:
                cells.append(numeric_cell(f"{col_letter(sales_start + offset)}{excel_row}", sale))
            if unit:
                cells.append(numeric_cell(f"{col_letter(units_start + offset)}{excel_row}", unit))
        xml_rows.append(f'<row r="{excel_row}">{"".join(cells)}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Purchases" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def baseline_record(meter: str) -> dict:
    return {
        "sourceDocumentId": meter,
        "sourceDocumentPath": f"demo_sales_meters/{meter}",
        "sourceEndRow": 2,
        "meterNo": meter,
        "meterNoNormalized": meter,
        "provider": "contour",
        "lmPcode": "ZA5241",
        "accountNo": "A1",
        "accountNumber": "A1",
        "accountNumberNormalized": "A1",
        "customerNo": "C1",
        "customerName": "TEST",
        "customerSurname": "TEST",
        "sourceFileName": meter,
        "sourceRow": 2,
        "addressLine1": "1 TEST ROAD",
        "addressLine2": "",
        "town": "DUNDEE",
        "postalAddress1": "",
        "postalAddress2": "",
        "postalAddressTown": "",
        "standNumber": "1",
        "tariffInstance": "12345",
        "installationDate": "",
        "previousMeterNumber": "",
        "previousInstallationDate": "",
        "leakageCategory": "Normal - No Leakage Flag",
        "riskTier": "Normal",
        "riskScore": 0,
        "salesPeriodFrom": "2026-06",
        "salesPeriodTo": "2026-06",
        "monthlySalesC": {"2026-06": 10000},
        "monthlyUnits": {"2026-06": 20.0},
        "totalSalesC": 10000,
        "totalUnits": 20.0,
        "elmAccountMatched": True,
        "elmSourceRows": [],
        "erfCandidateCount": 0,
        "erfCandidates": [],
        "erfNumbers": [],
        "missingErfNumbers": [],
        "gpsMatchStatus": "UNRESOLVED",
        "hasUsableGps": False,
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )


def write_previous_snapshot(path: Path, meters: list[str], month: str = "2026-06") -> None:
    payload = {
        "schemaVersion": 1,
        "stage": "03A",
        "snapshotType": "monthly_supplier_snapshot",
        "lmPcode": "ZA5241",
        "provider": "contour",
        "currentMonth": month,
        "sourceRunId": "TEST",
        "workbookSha256": "",
        "cumulativeSourceSha256": "",
        "meters": sorted(meters),
        "population": {},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_adapter(
    tmp_path: Path,
    baseline: Path,
    workbook: Path,
    *,
    write: bool = False,
    from_month: str = "2026-07",
    to_month: str = "2026-08",
    previous_snapshot: Path | None = None,
    tag: str = "run",
):
    report = tmp_path / f"report_{tag}.json"
    output = tmp_path / f"refreshed_{tag}.jsonl"
    snapshot_output = tmp_path / f"snapshot_{tag}.json"
    cmd = [
        sys.executable, str(SCRIPT),
        "--baseline", str(baseline),
        "--expected-baseline-sha256", sha(baseline),
        "--workbook", str(workbook),
        "--expected-workbook-sha256", sha(workbook),
        "--sheet", "Purchases",
        "--lm-pcode", "ZA5241",
        "--provider", "contour",
        "--from-month", from_month,
        "--to-month", to_month,
        "--source-run-id", "20260903T040000Z",
        "--report", str(report),
        "--progress-every", "0",
    ]
    if previous_snapshot is None:
        cmd.append("--bootstrap-previous-from-baseline")
    else:
        cmd += [
            "--previous-snapshot", str(previous_snapshot),
            "--expected-previous-snapshot-sha256", sha(previous_snapshot),
        ]
    if write:
        cmd += [
            "--write", "--output", str(output),
            "--snapshot-output", str(snapshot_output),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    payload = json.loads(report.read_text())
    return result, payload, output, snapshot_output


def test_existing_population_rounding_and_preservation(tmp_path: Path):
    baseline = tmp_path / "baseline.jsonl"
    workbook = tmp_path / "input.xlsx"
    write_jsonl(baseline, [baseline_record("00123"), baseline_record("00999")])
    months = ["2026-06", "2026-07", "2026-08"]
    make_xlsx(
        workbook,
        [
            wb_row("00123", sales={"2026-07": "1264.3800000000001"}, units={"2026-07": "73.599999999999994"}),
            wb_row("00999", sales={"2026-08": "200.00"}, units={"2026-08": "80.0"}),
        ],
        months,
    )
    result, report, output, snapshot = run_adapter(tmp_path, baseline, workbook, write=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert report["status"] == "PASS"
    assert report["result"] == "CUMULATIVE_SOURCE_AND_SNAPSHOT_WRITTEN"
    r = report["reconciliation"]
    assert r["cumulativeBeforeMeters"] == 2
    assert r["cumulativeCreatesCount"] == 0
    assert r["cumulativeAfterMeters"] == 2
    assert r["cumulativeDeletesCount"] == 0
    assert r["existingBaselineNonPurchaseProjectionPreserved"] is True
    assert report["workbook"]["targetMonthTotals"]["2026-07"]["salesTotalC"] == 126438
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    by_meter = {row["meterNoNormalized"]: row for row in rows}
    assert by_meter["00123"]["monthlySalesC"]["2026-07"] == 126438
    assert by_meter["00123"]["monthlyUnits"]["2026-07"] == 73.6
    assert by_meter["00123"]["leakageCategory"] == "Normal - No Leakage Flag"
    assert by_meter["00123"]["salesPeriodTo"] == "2026-08"
    assert by_meter["00999"]["monthlySalesC"]["2026-08"] == 20000
    snap = json.loads(snapshot.read_text())
    assert snap["currentMonth"] == "2026-08"
    assert snap["meters"] == ["00123", "00999"]


def test_append_only_population_migration(tmp_path: Path):
    baseline = tmp_path / "baseline.jsonl"
    workbook = tmp_path / "input.xlsx"
    write_jsonl(
        baseline,
        [baseline_record("00123"), baseline_record("00999"), baseline_record("00777")],
    )
    months = ["2026-06", "2026-07", "2026-08"]
    make_xlsx(
        workbook,
        [
            wb_row("00123", sales={"2026-07": "100", "2026-08": "200"}, units={"2026-07": "10", "2026-08": "20"}),
            wb_row(
                "00888",
                previous="00999",
                sales={"2026-06": "50", "2026-07": "60", "2026-08": "70"},
                units={"2026-06": "5", "2026-07": "6", "2026-08": "7"},
                customer="NEWCUST1",
                surname="REPLACEMENT CUSTOMER",
                account="0000008888",
            ),
            wb_row(
                "00666",
                previous="12345",  # not in cumulative baseline -> NEW to iREPS
                sales={"2026-06": "30", "2026-07": "40", "2026-08": "50"},
                units={"2026-06": "3", "2026-07": "4", "2026-08": "5"},
                customer="NEWCUST2",
                surname="NEW CUSTOMER",
                account="0000006666",
            ),
        ],
        months,
    )
    result, report, output, snapshot = run_adapter(
        tmp_path, baseline, workbook, write=True, tag="migration"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    r = report["reconciliation"]
    assert r["previousSnapshotMeters"] == 3
    assert r["currentSnapshotMeters"] == 3
    assert r["unchangedFromPreviousCount"] == 1
    assert r["replacementPairsCount"] == 1
    assert r["replacementPairs"] == [
        {"previousMeterNumber": "00999", "replacementMeterNumber": "00888"}
    ]
    assert r["removedNotCarriedForwardMeters"] == ["00777"]
    assert r["cumulativeCreatesCount"] == 2
    assert r["replacementNewCreatedMeters"] == ["00888"]
    assert r["newCreatedMeters"] == ["00666"]
    assert r["incomingCreatedWithUnresolvedPreviousMeterNumberCount"] == 1
    assert r["cumulativeDeletesCount"] == 0
    assert r["cumulativeAfterMeters"] == 5

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    by_meter = {row["meterNoNormalized"]: row for row in rows}
    assert set(by_meter) == {"00123", "00999", "00777", "00888", "00666"}
    # Old/replaced and removed identities remain untouched in the cumulative source.
    assert by_meter["00999"]["salesPeriodTo"] == "2026-06"
    assert by_meter["00777"]["salesPeriodTo"] == "2026-06"
    # New identities carry the complete source history through the target month.
    assert by_meter["00888"]["monthlySalesC"] == {
        "2026-06": 5000, "2026-07": 6000, "2026-08": 7000
    }
    assert by_meter["00888"]["previousMeterNumber"] == "00999"
    assert by_meter["00888"]["customerNo"] == "NEWCUST1"
    assert by_meter["00888"]["customerName"] == "REPLACEMENT CUSTOMER"
    assert by_meter["00888"]["leakageCategory"] == ""
    assert json.loads(snapshot.read_text())["population"]["cumulativeMeters"] == 5


def test_previous_snapshot_prevents_recounting_old_removed_meters(tmp_path: Path):
    baseline1 = tmp_path / "baseline1.jsonl"
    workbook1 = tmp_path / "input1.xlsx"
    write_jsonl(
        baseline1,
        [baseline_record("00123"), baseline_record("00999"), baseline_record("00777")],
    )
    make_xlsx(
        workbook1,
        [
            wb_row("00123", sales={"2026-07": "10", "2026-08": "20"}, units={"2026-07": "1", "2026-08": "2"}),
            wb_row("00888", previous="00999", sales={"2026-07": "30", "2026-08": "40"}, units={"2026-07": "3", "2026-08": "4"}),
            wb_row("00666", sales={"2026-07": "50", "2026-08": "60"}, units={"2026-07": "5", "2026-08": "6"}),
        ],
        ["2026-06", "2026-07", "2026-08"],
    )
    result1, _, cumulative1, snapshot1 = run_adapter(
        tmp_path, baseline1, workbook1, write=True, tag="first"
    )
    assert result1.returncode == 0, result1.stderr + result1.stdout

    workbook2 = tmp_path / "input2.xlsx"
    make_xlsx(
        workbook2,
        [
            wb_row("00123", sales={"2026-09": "25"}, units={"2026-09": "2.5"}),
            wb_row("00888", previous="00999", sales={"2026-09": "45"}, units={"2026-09": "4.5"}),
            wb_row("00666", sales={"2026-09": "65"}, units={"2026-09": "6.5"}),
            wb_row("00555", sales={"2026-09": "75"}, units={"2026-09": "7.5"}),
        ],
        ["2026-06", "2026-07", "2026-08", "2026-09"],
    )
    result2, report2, cumulative2, snapshot2 = run_adapter(
        tmp_path,
        cumulative1,
        workbook2,
        write=True,
        from_month="2026-09",
        to_month="2026-09",
        previous_snapshot=snapshot1,
        tag="second",
    )
    assert result2.returncode == 0, result2.stderr + result2.stdout
    r = report2["reconciliation"]
    assert r["previousSnapshotMeters"] == 3
    assert r["currentSnapshotMeters"] == 4
    assert r["previousMissingCount"] == 0
    assert r["removedNotCarriedForwardCount"] == 0
    assert r["cumulativeBeforeMeters"] == 5
    assert r["cumulativeCreatesMeters"] == ["00555"]
    assert r["cumulativeAfterMeters"] == 6
    assert r["cumulativeDeletesCount"] == 0
    by_meter = {
        row["meterNoNormalized"]: row
        for row in (json.loads(line) for line in cumulative2.read_text().splitlines())
    }
    assert "00777" in by_meter and "00999" in by_meter
    assert json.loads(snapshot2.read_text())["meters"] == ["00123", "00555", "00666", "00888"]


def test_conflicting_existing_target_month_fails(tmp_path: Path):
    record = baseline_record("00123")
    record["monthlySalesC"]["2026-07"] = 999
    record["monthlyUnits"]["2026-07"] = 1.0
    record["totalSalesC"] += 999
    record["totalUnits"] += 1.0
    record["salesPeriodTo"] = "2026-07"
    baseline = tmp_path / "baseline.jsonl"
    workbook = tmp_path / "input.xlsx"
    previous = tmp_path / "previous.json"
    write_jsonl(baseline, [record])
    write_previous_snapshot(previous, ["00123"], month="2026-06")
    make_xlsx(
        workbook,
        [wb_row("00123", sales={"2026-07": "100", "2026-08": "200"}, units={"2026-07": "10", "2026-08": "20"})],
        ["2026-06", "2026-07", "2026-08"],
    )
    result, report, output, snapshot = run_adapter(
        tmp_path, baseline, workbook, write=True, previous_snapshot=previous, tag="conflict"
    )
    assert result.returncode == 2
    assert report["status"] == "FAIL"
    assert "conflicts with workbook" in report["error"]["message"]
    assert not output.exists()
    assert not snapshot.exists()


def test_sparse_history_preserves_governed_sales_period(tmp_path: Path):
    record = baseline_record("00123")
    record["salesPeriodFrom"] = "2023-12"
    record["salesPeriodTo"] = "2026-06"
    record["monthlySalesC"] = {"2025-01": 50000, "2026-06": 50000}
    record["monthlyUnits"] = {"2025-01": 114.2, "2026-06": 109.5}
    record["totalSalesC"] = 100000
    record["totalUnits"] = 223.7

    baseline = tmp_path / "baseline.jsonl"
    workbook = tmp_path / "input.xlsx"
    write_jsonl(baseline, [record])
    make_xlsx(
        workbook,
        [wb_row("00123", sales={"2026-07": "100.00", "2026-08": "200.00"}, units={"2026-07": "20.0", "2026-08": "40.0"})],
        ["2026-06", "2026-07", "2026-08"],
    )

    result, report, output, _ = run_adapter(tmp_path, baseline, workbook, write=True, tag="sparse")
    assert result.returncode == 0, result.stderr + result.stdout
    assert report["status"] == "PASS"
    row = json.loads(output.read_text().strip())
    assert row["salesPeriodFrom"] == "2023-12"
    assert row["salesPeriodTo"] == "2026-08"
    assert sorted(row["monthlySalesC"]) == ["2025-01", "2026-06", "2026-07", "2026-08"]
    assert row["totalSalesC"] == 130000


def test_bootstrap_rejects_non_previous_month_baseline(tmp_path: Path):
    record = baseline_record("00123")
    record["salesPeriodTo"] = "2026-05"
    record["salesPeriodFrom"] = "2026-05"
    record["monthlySalesC"] = {"2026-05": 10000}
    record["monthlyUnits"] = {"2026-05": 20.0}
    baseline = tmp_path / "baseline.jsonl"
    workbook = tmp_path / "input.xlsx"
    write_jsonl(baseline, [record])
    make_xlsx(workbook, [wb_row("00123")], ["2026-06", "2026-07", "2026-08"])
    result, report, _, _ = run_adapter(tmp_path, baseline, workbook, write=False, tag="badbootstrap")
    assert result.returncode == 2
    assert "bootstrap-previous-from-baseline" in report["error"]["message"]


def test_real_workbook_header_blocks_and_commercial_if_available():
    real = Path("/mnt/data/END20260902.xlsx")
    if not real.is_file():
        return
    module = load_module()
    snapshot = module.read_workbook(
        real,
        expected_sha256=sha(real),
        sheet_name="Purchases",
        target_months=["2026-07", "2026-08"],
        progress_every=0,
    )
    assert snapshot.rows == 10201
    assert snapshot.totals["2026-07"]["salesTotalC"] == 727111461
    assert snapshot.totals["2026-08"]["salesTotalC"] == 719526263
    assert snapshot.totals["2026-07"]["purchasingMeters"] == 6684
    assert snapshot.totals["2026-08"]["purchasingMeters"] == 6810
    assert snapshot.totals["2026-07"]["unitsTotal"] == "1868999.3"
    assert snapshot.totals["2026-08"]["unitsTotal"] == "1823793.9"
    assert snapshot.months[0] == "2023-12"
    assert snapshot.months[-1] == "2026-08"
    assert snapshot.commercial["04302064763"]["PreviousMeterNumber"] == "04297772107"
    assert snapshot.commercial["04302064763"]["AccountNumber"] == "0000003916"
