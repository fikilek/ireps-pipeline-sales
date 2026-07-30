"""Small, dependency-free streaming reader for the XLSX sources used by M02."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterator
import xml.etree.ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class XlsxReader:
    def __init__(self, path: Path):
        self.path = path

    def iter_sheet_rows(
        self,
        sheet_name: str,
        wanted_columns: set[str],
    ) -> Iterator[tuple[int, dict[str, str]]]:
        with zipfile.ZipFile(self.path) as archive:
            sheet_path = self._resolve_sheet_path(archive, sheet_name)
            shared_strings = self._read_shared_strings(archive)

            with archive.open(sheet_path) as sheet_stream:
                for _event, element in ET.iterparse(sheet_stream, events=("end",)):
                    if element.tag != f"{{{MAIN_NS}}}row":
                        continue

                    row_number = int(element.attrib["r"])
                    values: dict[str, str] = {}

                    for cell in element.findall(f"{{{MAIN_NS}}}c"):
                        reference = cell.attrib.get("r", "")
                        column = column_letters(reference)
                        if column not in wanted_columns:
                            continue
                        values[column] = read_cell_value(cell, shared_strings)

                    yield row_number, values
                    element.clear()

    def _resolve_sheet_path(self, archive: zipfile.ZipFile, sheet_name: str) -> str:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships
        }

        sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
        if sheets is None:
            raise ValueError(f"Workbook has no sheets: {self.path}")

        for sheet in sheets:
            if sheet.attrib.get("name") != sheet_name:
                continue
            relationship_id = sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]
            target = relationship_map[relationship_id]
            if target.startswith("/"):
                return target.lstrip("/")
            return "xl/" + target.replace("..", "").lstrip("/")

        available = [sheet.attrib.get("name", "") for sheet in sheets]
        raise ValueError(
            f"Sheet {sheet_name!r} not found in {self.path}. Available: {available}"
        )

    @staticmethod
    def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return []

        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        return [
            "".join(text.text or "" for text in item.iter(f"{{{MAIN_NS}}}t"))
            for item in root.findall(f"{{{MAIN_NS}}}si")
        ]


def column_letters(cell_reference: str) -> str:
    match = re.match(r"[A-Z]+", cell_reference)
    return match.group(0) if match else ""


def read_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        if inline is None:
            return ""
        return "".join(text.text or "" for text in inline.iter(f"{{{MAIN_NS}}}t"))

    value_element = cell.find(f"{{{MAIN_NS}}}v")
    if value_element is None:
        return ""

    raw = value_element.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid shared-string reference: {raw!r}") from exc
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw
