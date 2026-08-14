from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from email.message import EmailMessage

import pytest

from asset_compensation.parsers import (
    DuplicateSupplierDomainError,
    EmlParseError,
    EmlParser,
    load_supplier_directory,
)


def _eml(subject: str, body: str, *, to: str, cc: str = "") -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Demo Asset Team <asset.team@example.com>"
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Date"] = "Thu, 13 Aug 2026 03:49:56 +0000"
    message["Message-ID"] = "<synthetic-case@example.invalid>"
    message.set_content(body)
    return message.as_bytes()


def test_parse_damaged_mail_decodes_subject_and_financial_fields() -> None:
    data = _eml(
        "[Đền bù] : IT - Thông tin tài sản hư hỏng - "
        "DEMO-LAP-901 - demo.user7 - Đang làm việc",
        """
        Chị thông báo chi phí đền bù của thiết bị hư hỏng:
        Người dùng : demo.user7 - đồng ý đền bù
        Chi phí đền bù : 123.456 VNĐ
        Chi phí sửa chữa : 123.456 VNĐ
        Thiết bị có sửa chữa : DEMO-LAP-901

        Request ID : DEMO-REQ-9001
        Model : Demo Business Laptop
        Mã thiết bị : DEMO-LAP-901
        Hư hỏng: máy vào nước
        Đơn hàng DEMO-ORDER-9001
        """,
        to="Demo Accountant <accounting@example.com>",
        cc="Demo User (7) <demo.user7@example.com>",
    )

    parsed = EmlParser().parse_bytes(data, source_file="synthetic-damaged.eml")

    assert parsed.case_type == "DAMAGED"
    assert parsed.domain == "demo.user7"
    assert parsed.employee_name == "Demo User (7)"
    assert parsed.asset_code == "DEMO-LAP-901"
    assert parsed.asset_name == "Demo Business Laptop"
    assert parsed.amount == Decimal("123456")
    assert parsed.repair_status == "REPAIRED"
    assert parsed.received_at is not None
    assert parsed.received_at.tzinfo == UTC
    assert parsed.metadata["confirmed"] is True
    assert parsed.metadata["request_id"] == "DEMO-REQ-9001"
    assert parsed.metadata["order_id"] == "9001"
    assert parsed.source_id == "synthetic-case@example.invalid"
    assert parsed.warnings == ()


def test_parse_lost_mail_keeps_generic_mou_asset_and_breakdown() -> None:
    data = _eml(
        "Trả lời: IT - Thông tin tài sản thất lạc - "
        "DEMO-MOU-902 - demo.mouse - Đang làm việc",
        """
        Dear Demo Reviewer,
        Please confirm this synthetic record.

        Asset Name Product Name Domain Ngày bắt đầu sử dụng Ngày thất lạc/mất
        Nguyên giá ban đầu Mức khấu hao sử dụng còn lại Phí đền bù trách nhiệm
        Tổng số tiền đền bù
        DEMO-MOU-902 Demo Wireless Pointer demo.mouse 06/04/2026 04/08/2026
        345,678 234,567 12,345 246,912

        Total:
        234,567
        12,345
        246,912
        """,
        to="Demo Mouse User <demo.mouse@example.com>",
    )

    parsed = EmlParser().parse_bytes(data)

    assert parsed.case_type == "LOST"
    assert parsed.asset_code == "DEMO-MOU-902"
    assert parsed.domain == "demo.mouse"
    assert parsed.employee_name == "Demo Mouse User"
    assert parsed.asset_name == "Demo Wireless Pointer"
    assert parsed.residual_value == Decimal("234567")
    assert parsed.responsibility_fee == Decimal("12345")
    assert parsed.amount == Decimal("246912")
    assert parsed.metadata["original_value"] == Decimal("345678")
    assert parsed.metadata["confirmed"] is True
    assert "missing_asset_code" not in parsed.warnings


def test_parse_html_only_lost_mail() -> None:
    message = EmailMessage()
    message["Subject"] = "IT - Thông báo mất thiết bị - DEMO-CAB-903 - demo.cable"
    message["From"] = "Demo FA <fa@example.com>"
    message["To"] = "Demo Cable User <demo.cable@example.com>"
    message.set_content(
        "<html><body><p>Người quản lý thiết bị: demo.cable</p>"
        "<p>Mã thiết bị: DEMO-CAB-903</p>"
        "<p>Phí đền bù trách nhiệm khi mất thiết bị.</p>"
        "<p>Total:<br>111,111<br>22,222<br>133,333</p></body></html>",
        subtype="html",
    )

    parsed = EmlParser().parse_bytes(message.as_bytes())

    assert parsed.case_type == "LOST"
    assert parsed.asset_code == "DEMO-CAB-903"
    assert parsed.amount == Decimal("133333")


def test_unclassifiable_mail_is_not_silently_skipped() -> None:
    with pytest.raises(EmlParseError, match="distinguish"):
        EmlParser().parse_bytes(
            _eml("Weekly status", "No asset compensation data", to="user@example.invalid")
        )


def test_supplier_loader_rejects_duplicate_normalized_domain() -> None:
    rows = [
        {
            "Domain": "Demo.User@Example.com",
            "Supplier Number": "000101",
            "Supplier Site": "01",
        },
        {
            "Domain": " demo.user ",
            "Supplier Number": "000202",
            "Supplier Site": "02",
        },
    ]

    with pytest.raises(DuplicateSupplierDomainError) as error:
        load_supplier_directory(rows)

    assert error.value.domain == "demo.user"
    assert error.value.first_row == 2
    assert error.value.duplicate_row == 3


def test_supplier_loader_preserves_identifier_zeroes() -> None:
    directory = load_supplier_directory(
        [
            {
                "Domain": "Demo.Supplier",
                "Supplier Number": "'000101",
                "Supplier Site": "'01",
                "Supplier Name": "Synthetic Supplier",
                "Active": "yes",
            }
        ]
    )

    record = directory["demo.supplier"]
    assert record.supplier_number == "000101"
    assert record.supplier_site == "01"
    assert record.active is True
