from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest

from asset_compensation.parsers import EmlParser, EmlSkipError, SupplierRecord
from asset_compensation.repositories import SQLiteCaseRepository
from asset_compensation.services import CaseService
from asset_compensation.services.ingestion_service import EmailPayload, ingest_eml_payloads


def _message(subject: str, body: str, *, message_id: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Synthetic Asset Team <asset@example.invalid>"
    message["To"] = "Synthetic Reviewer <reviewer@example.invalid>"
    message["Date"] = "Thu, 13 Aug 2026 03:49:56 +0000"
    message["Message-ID"] = f"<{message_id}>"
    message.set_content(body)
    return message.as_bytes()


def _lost_table_message(*, message_id: str = "multi-lost@example.invalid") -> bytes:
    message = EmailMessage()
    message["Subject"] = "IT - Thông tin tài sản thất lạc"
    message["From"] = "Synthetic Asset Team <asset@example.invalid>"
    message["To"] = "Synthetic Reviewer <reviewer@example.invalid>"
    message["Date"] = "Thu, 13 Aug 2026 03:49:56 +0000"
    message["Message-ID"] = f"<{message_id}>"
    message.set_content("Synthetic lost-asset table follows.")
    message.add_alternative(
        """
        <html><body><table>
          <tr>
            <th>Asset Name</th><th>Product Name</th><th>Domain</th>
            <th>Ngày bắt đầu sử dụng</th><th>Ngày thất lạc/mất</th>
            <th>Nguyên giá ban đầu</th><th>Mức khấu hao sử dụng còn lại</th>
            <th>Phí đền bù trách nhiệm</th><th>Tổng số tiền đền bù</th>
            <th>NOTE</th><th>Entity</th><th>Cost center</th>
            <th>Product code</th><th>Location</th>
          </tr>
          <tr>
            <td>DEMO-LAP-101</td><td>Synthetic Laptop</td><td>demo.alpha</td>
            <td>01/01/2025</td><td>01/08/2026</td><td>1,000</td>
            <td>600</td><td>100</td><td>700</td><td>Asset</td><td>VNG</td>
            <td>'0603</td><td>'000</td><td>'01</td>
          </tr>
          <tr>
            <td>DEMO-MON-102</td><td>Synthetic Monitor</td><td>demo.alpha</td>
            <td>01/01/2025</td><td>01/08/2026</td><td>500</td>
            <td>200</td><td>50</td><td>250</td><td>Expense</td><td>OTHER</td>
            <td>'0603</td><td>'000</td><td>'01</td>
          </tr>
          <tr>
            <td>DEMO-CAB-201</td><td>Synthetic Cable</td><td>demo.beta</td>
            <td>02/02/2025</td><td>02/08/2026</td><td>400</td>
            <td>300</td><td>30</td><td>330</td><td>Asset</td><td>VNG</td>
            <td>'0000</td><td>'001</td><td>'02</td>
          </tr>
          <tr>
            <td>DEMO-OLD-999</td><td>Non-compensable</td><td>demo.alpha</td>
            <td>01/01/2020</td><td>01/08/2026</td><td>100</td>
            <td>Không tính đền bù</td><td>-</td><td>-</td><td>Asset</td><td>VNG</td>
            <td>'0603</td><td>'000</td><td>'01</td>
          </tr>
        </table></body></html>
        """,
        subtype="html",
    )
    return message.as_bytes()


def test_damaged_mail_emits_every_domain_and_explicit_repair_asset() -> None:
    data = _message(
        "IT - Thông tin tài sản hư hỏng",
        """
        Người dùng: demo.alpha
        Chi phí đền bù: 100.000
        Chi phí sửa chữa: 10.000
        Thiết bị có sửa chữa: DEMO-LAP-101
        Thiết bị không sửa chữa: DEMO-CAB-102

        Người dùng: demo.beta
        Chi phí đền bù: 200000
        Chi phí sửa chữa: 0
        Mã thiết bị: DEMO-LAP-203
        """,
        message_id="multi-damaged@example.invalid",
    )

    parsed = EmlParser().parse_bytes_many(data)

    assert [(case.domain, case.asset_code, case.repair_status) for case in parsed] == [
        ("demo.alpha", "DEMO-LAP-101", "REPAIRED"),
        ("demo.alpha", "DEMO-CAB-102", "NOT_REPAIRED"),
        ("demo.beta", "DEMO-LAP-203", "NOT_REPAIRED"),
    ]
    assert [case.amount for case in parsed] == [
        Decimal("100000"),
        Decimal("100000"),
        Decimal("200000"),
    ]
    assert parsed[0].metadata["repair_cost"] == Decimal("10000")
    assert parsed[0].metadata["credit_components"][0]["policy_key"] == "DAMAGED_REPAIR"
    assert (
        parsed[1].metadata["credit_components"][0]["policy_key"]
        == "DAMAGED_NO_REPAIR"
    )
    assert EmlParser().parse_bytes(data) == parsed[0]


@pytest.mark.parametrize(
    ("subject", "expected_reason"),
    [
        ("MOU - IT - Thông tin tài sản hư hỏng", "MOU"),
        (
            "IT - Lỗi kỹ thuật - không đền bù - thiết bị hư hỏng",
            "TECHNICAL_NO_COMPENSATION",
        ),
    ],
)
def test_original_mail_exclusion_rules_are_explicit(
    subject: str, expected_reason: str
) -> None:
    data = _message(
        subject,
        "Người dùng: demo.skip\nChi phí đền bù: 100\nMã thiết bị: DEMO-LAP-001",
        message_id=f"skip-{expected_reason.casefold()}@example.invalid",
    )

    with pytest.raises(EmlSkipError) as error:
        EmlParser().parse_bytes_many(data)

    assert error.value.reason == expected_reason


def test_lost_html_table_groups_by_domain_and_preserves_credit_dimensions() -> None:
    parsed = EmlParser().parse_bytes_many(_lost_table_message())

    assert len(parsed) == 2
    alpha, beta = parsed
    assert alpha.domain == "demo.alpha"
    assert alpha.asset_code == "DEMO-LAP-101, DEMO-MON-102"
    assert alpha.asset_name == "Synthetic Laptop, Synthetic Monitor"
    assert alpha.residual_value == Decimal("800")
    assert alpha.responsibility_fee == Decimal("150")
    assert alpha.amount == Decimal("950")
    assert alpha.metadata["original_value"] == Decimal("1500")
    assert alpha.metadata["usage_start"] == "01/01/2025"
    assert alpha.metadata["loss_date"] == "01/08/2026"
    assert alpha.metadata["non_compensable_row_count"] == 1
    assert any("non-compensable" in warning for warning in alpha.warnings)

    components = alpha.metadata["credit_components"]
    assert [(item["policy_key"], item["amount"]) for item in components] == [
        ("LOST_DEPRECIATION_ASSET", Decimal("600")),
        ("LOST_DEPRECIATION_OTHER", Decimal("200")),
        ("LOST_RESPONSIBILITY", Decimal("150")),
    ]
    assert all(item["cost_center"] == "0603" for item in components)
    assert all(item["product_code"] == "000" for item in components)
    assert all(item["location"] == "01" for item in components)
    assert components[1]["entity_non_vng"] is True
    assert components[2]["entity_non_vng"] is True
    assert beta.domain == "demo.beta"
    assert beta.amount == Decimal("330")


def test_ingestion_persists_all_cases_stably_and_marks_inactive_metadata(tmp_path) -> None:
    repository = SQLiteCaseRepository(tmp_path / "cases.sqlite3")
    service = CaseService(
        repository,
        clock=lambda: datetime(2026, 8, 14, 4, 0, tzinfo=UTC),
    )
    supplier_directory = {
        "demo.alpha": SupplierRecord(
            domain="demo.alpha",
            supplier_number="SYN-001",
            supplier_site="01",
            active=False,
        )
    }
    payload = EmailPayload("synthetic-table.eml", _lost_table_message())
    try:
        first = ingest_eml_payloads(
            service,
            [payload],
            supplier_directory=supplier_directory,
        )
        second = ingest_eml_payloads(
            service,
            [payload],
            supplier_directory=supplier_directory,
        )

        assert len(first.cases) == 2
        assert [case.id for case in second.cases] == [case.id for case in first.cases]
        assert len(service.list_cases()) == 2
        alpha = next(case for case in first.cases if case.domain == "demo.alpha")
        beta = next(case for case in first.cases if case.domain == "demo.beta")
        assert alpha.metadata["supplier_lookup_status"] == "INACTIVE"
        assert alpha.metadata["supplier_active"] is False
        assert alpha.metadata["employee_inactive"] is True
        assert alpha.metadata["credit_components"][0]["amount"] == 600
        assert beta.metadata["supplier_lookup_status"] == "NOT_FOUND"
    finally:
        repository.close()


def test_ingestion_reports_skipped_mail_separately(tmp_path) -> None:
    repository = SQLiteCaseRepository(tmp_path / "cases.sqlite3")
    service = CaseService(repository)
    payload = EmailPayload(
        "synthetic-skip.eml",
        _message(
            "MOU - hư hỏng",
            "Người dùng: demo.skip\nChi phí đền bù: 100",
            message_id="skip-report@example.invalid",
        ),
    )
    try:
        report = ingest_eml_payloads(service, [payload])

        assert report.cases == ()
        assert report.unknown_files == ()
        assert report.skipped_files == ("synthetic-skip.eml",)
        assert report.warnings == ("synthetic-skip.eml: skipped (MOU)",)
    finally:
        repository.close()
