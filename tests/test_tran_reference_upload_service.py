"""Safe storage tests for uploaded TranNNB reference workbooks."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from asset_compensation.services.tran_reference_upload_service import (
    TranReferenceUpload,
    TranReferenceUploadError,
    TranReferenceUploadService,
)


def fa_gl_bytes(*, tag_number: str = "MOU10001") -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheets = {
        "VNG-Asset": (3, 4),
        "VNG-Tool": (3, 4),
        "VNGS-Asset": (2, 3),
        "VNGS-Tool": (2, 3),
    }
    headers = {
        2: "Company Name",
        7: "Cost Center",
        8: "Product Code",
        10: "Location",
        12: "Asset",
        16: "Tag Number",
        22: "Date of Depreciation",
        25: "Life(month)",
        27: "Cost",
    }
    for sheet_name, (header_row, data_row) in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for column, header in headers.items():
            sheet.cell(header_row, column, header)
        if sheet_name == "VNG-Tool":
            sheet.cell(data_row, 2, "Synthetic Company")
            sheet.cell(data_row, 7, "0603")
            sheet.cell(data_row, 8, "000")
            sheet.cell(data_row, 10, "01")
            sheet.cell(data_row, 12, "SYN-001")
            sheet.cell(data_row, 16, tag_number)
            sheet.cell(data_row, 22, date(2025, 1, 1))
            sheet.cell(data_row, 25, 48)
            sheet.cell(data_row, 27, 1_000_000)
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def ccdc_bytes() -> bytes:
    workbook = Workbook()
    define = workbook.active
    define.title = "Define"
    define.append(["Product Type", "Barcode", "Group Type"])
    define.append(["Other/Spe.part", "MOU", "Hardware"])
    warehouse = workbook.create_sheet("BC Xuatkho")
    warehouse.append(["Unused", "Asset Name", "Unused", "Unused", "Start time"])
    warehouse.append([None, "MOU10001", None, None, date(2025, 1, 1)])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def upload(data: bytes, filename: str) -> TranReferenceUpload:
    return TranReferenceUpload(
        filename=filename,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        stream=io.BytesIO(data),
    )


def test_upload_activates_atomic_pair_preserves_optional_ccdc_and_clears_explicitly(
    tmp_path: Path,
) -> None:
    service = TranReferenceUploadService(tmp_path / "references")

    first = service.upload(
        upload(fa_gl_bytes(), "client-fa.xlsx"),
        upload(ccdc_bytes(), "client-ccdc.xlsx"),
    )
    first_snapshot = service.snapshot()
    second = service.upload(upload(fa_gl_bytes(tag_number="MOU10002"), "new-fa.xlsx"))
    second_snapshot = service.snapshot()

    assert first == {
        "configured": True,
        "updated_at": first["updated_at"],
        "fa_gl_configured": True,
        "ccdc_configured": True,
    }
    assert first_snapshot.fa_gl_path is not None
    assert first_snapshot.ccdc_path is not None
    assert second["ccdc_configured"] is True
    assert second_snapshot.ccdc_path is not None
    assert "client-fa" not in str(service.status())
    assert str(tmp_path) not in str(service.status())

    cleared_ccdc = service.upload(
        upload(fa_gl_bytes(), "fa.xlsx"),
        clear_ccdc=True,
    )
    assert cleared_ccdc["ccdc_configured"] is False
    assert service.snapshot().ccdc_path is None


def test_invalid_replacement_does_not_change_active_version(tmp_path: Path) -> None:
    service = TranReferenceUploadService(tmp_path / "references")
    service.upload(upload(fa_gl_bytes(), "fa.xlsx"))
    before = service.snapshot().fa_gl_path

    with pytest.raises(TranReferenceUploadError):
        service.upload(upload(b"not an xlsx", "replacement.xlsx"))

    assert service.snapshot().fa_gl_path == before


def test_xlsx_preflight_rejects_embedded_content_before_activation(tmp_path: Path) -> None:
    malicious = io.BytesIO(fa_gl_bytes())
    with zipfile.ZipFile(malicious, mode="a") as archive:
        archive.writestr("xl/embeddings/payload.bin", b"synthetic payload")
    service = TranReferenceUploadService(tmp_path / "references")

    with pytest.raises(TranReferenceUploadError, match="embedded content"):
        service.upload(upload(malicious.getvalue(), "fa.xlsx"))

    assert service.status()["configured"] is False


def test_xlsx_preflight_rejects_external_relationship_before_activation(
    tmp_path: Path,
) -> None:
    malicious = io.BytesIO(fa_gl_bytes())
    with zipfile.ZipFile(malicious, mode="a") as archive:
        archive.writestr(
            "xl/worksheets/_rels/sheet1.xml.rels",
            b"""<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId99" Type="synthetic" Target="https://example.invalid/"
                            TargetMode="External" />
            </Relationships>""",
        )
    service = TranReferenceUploadService(tmp_path / "references")

    with pytest.raises(TranReferenceUploadError, match="external relationship"):
        service.upload(upload(malicious.getvalue(), "fa.xlsx"))

    assert service.status()["configured"] is False


def test_clear_removes_only_managed_versions(tmp_path: Path) -> None:
    reference_root = tmp_path / "references"
    service = TranReferenceUploadService(reference_root)
    service.upload(upload(fa_gl_bytes(), "fa.xlsx"))
    sentinel = reference_root / "keep-me.txt"
    sentinel.write_text("unknown file remains", "utf-8")

    counts = service.clear()

    assert counts["tran_reference_version_count"] == 1
    assert counts["tran_fa_gl_reference_count"] == 1
    assert service.status()["configured"] is False
    assert sentinel.read_text("utf-8") == "unknown file remains"
