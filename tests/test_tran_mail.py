"""Tests for HTML rendering and downloadable, never-sent TranNNB drafts."""

from __future__ import annotations

import re
from datetime import date
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest

from asset_compensation.adapters import (
    TRAN_MAIL_HEADERS,
    OutputExistsError,
    TranMailDraftBuilder,
    TranMailError,
    build_tran_mail_table,
)
from asset_compensation.domain import CompensationAsset, ReferenceStatus
from asset_compensation.services import CompensationService, TranAssetRequest, TranResolution

EXPECTED_MAIL_HEADERS = (
    "Asset Name",
    "Product Name",
    "Domain",
    "Ngày bắt đầu sử dụng",
    "Ngày thất lạc/mất",
    "Nguyên giá ban đầu (vnd)",
    "Mức khấu hao sử dụng còn lại (vnd)",
    "Phí đền bù trách nhiệm (vnd)",
    "Tổng số tiền đền bù (vnd)",
    "Thời gian đã sử dụng (tháng)",
    "NOTE",
    "Entity",
    "Cost center",
    "Product code",
    "Location",
)


def _resolution(
    asset_name: str = "Synthetic laptop", *, cost: int = 1_000_000
) -> TranResolution:
    asset = CompensationAsset(
        tag_number="LAP10001",
        asset_name=asset_name,
        domain="demo.user",
        lost_date=date(2026, 1, 1),
        cost=cost,
        start_date=date(2025, 1, 1),
        asset_number="SYN-001",
        book="Asset",
        entity="VNG",
        cost_center="0603",
        product_code="000",
        location="01",
        physical=True,
        lookup_status=ReferenceStatus.MATCHED,
    )
    return TranResolution(
        request=TranAssetRequest(
            tag_number=asset.tag_number,
            asset_name=asset.asset_name,
            domain=asset.domain,
            lost_date=asset.lost_date,
        ),
        asset=asset,
        preview=CompensationService().preview(asset),
        notes=("Synthetic provenance.",),
        issues=(),
        fa_status=ReferenceStatus.MATCHED,
        classification_status=ReferenceStatus.MATCHED,
    )


def _original_email() -> bytes:
    message = EmailMessage()
    message["From"] = "IT Desk <it@example.invalid>"
    message["To"] = "Operator <operator@example.invalid>, Manager <manager@example.invalid>"
    message["Cc"] = "Audit <audit@example.invalid>"
    message["Subject"] = "Synthetic lost asset"
    message["Message-ID"] = "<synthetic-thread@example.invalid>"
    message.set_content("Synthetic body")
    return message.as_bytes()


def _html_only_original_email() -> bytes:
    message = EmailMessage()
    message["From"] = "IT Desk <it@example.invalid>"
    message["To"] = "Operator <operator@example.invalid>"
    message["Subject"] = "Synthetic HTML-only source"
    message["Message-ID"] = "<synthetic-html-thread@example.invalid>"
    message.set_content(
        """
        <html><body>
          <p>Visible quoted source</p>
          <script>unsafeScript()</form>unsafeAfterMismatch()</script>
          <img src="https://tracker.example/pixel" onerror="unsafeEvent()">
          <form action="https://attacker.example"><p>unsafe form content</p></form>
          <a href="https://remote.example">Safe visible link text</a>
        </body></html>
        """,
        subtype="html",
    )
    return message.as_bytes()


def test_html_table_matches_approved_template_and_escapes_values() -> None:
    table = build_tran_mail_table([_resolution("<script>alert(1)</script>")])

    assert TRAN_MAIL_HEADERS == EXPECTED_MAIL_HEADERS
    header_positions = [table.index(f">{header}</td>") for header in EXPECTED_MAIL_HEADERS]
    assert header_positions == sorted(header_positions)
    assert "<script>" not in table
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in table
    assert "Total:" in table
    assert 'role="table"' in table
    assert table.count("background:#9CC2E5;") == 15
    assert table.count("background:#FFFF00;") == 2
    assert table.count("border:1px solid #000;") == 45
    assert "#d9eaf7" not in table
    assert "#777" not in table
    assert 'text-align:left;">LAP10001</td>' in table
    assert 'text-align:center;">demo.user</td>' in table
    assert 'text-align:right;">01/01/2025</td>' in table
    assert 'text-align:right;">1,000,000</td>' in table
    assert 'text-align:center;">Asset</td>' in table


def test_html_table_matches_sent_out_blanks_for_exempt_amounts() -> None:
    table = build_tran_mail_table([_resolution(cost=499_999)])
    rows = re.findall(r"<tr>(.*?)</tr>", table)
    data_cells = re.findall(r">([^<>]*)</td>", rows[1])

    assert data_cells[6:9] == ["Không tính đền bù", "", ""]


def test_draft_is_reply_all_attachment_with_unsent_marker(tmp_path: Path) -> None:
    attachment = tmp_path / "synthetic-result.xlsx"
    attachment.write_bytes(b"synthetic workbook bytes")
    output = tmp_path / "reply-draft.eml"

    result = TranMailDraftBuilder().build(
        _original_email(),
        [_resolution()],
        attachment,
        output,
        from_address="operator@example.invalid",
        body_intro="Synthetic approved mail body\nSecond line with <review>",
    )

    assert result.path == output
    parsed = BytesParser(policy=policy.default).parsebytes(output.read_bytes())
    assert parsed["Subject"] == "Re: Synthetic lost asset"
    assert parsed["X-Unsent"] == "1"
    assert parsed["In-Reply-To"] == "<synthetic-thread@example.invalid>"
    assert "it@example.invalid" in str(parsed["To"])
    assert "manager@example.invalid" in str(parsed["To"])
    assert "operator@example.invalid" not in str(parsed["To"])
    assert "audit@example.invalid" in str(parsed["Cc"])
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == attachment.name
    html_part = parsed.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "Synthetic approved mail body" in html_part.get_content()
    assert "Second line with &lt;review&gt;" in html_part.get_content()
    assert "<br>" in html_part.get_content()
    assert "Synthetic body" in html_part.get_content()
    assert "--- Original message ---" in parsed.get_body(
        preferencelist=("plain",)
    ).get_content()
    assert "Synthetic body" in parsed.get_body(preferencelist=("plain",)).get_content()
    assert "\nSecond line with <review>" in parsed.get_body(
        preferencelist=("plain",)
    ).get_content()
    assert "LAP10001" in html_part.get_content()


def test_draft_quotes_html_source_as_bounded_inert_text(tmp_path: Path) -> None:
    attachment = tmp_path / "synthetic-result.xlsx"
    attachment.write_bytes(b"synthetic workbook bytes")
    output = tmp_path / "safe-source-draft.eml"

    TranMailDraftBuilder().build(
        _html_only_original_email(),
        [_resolution()],
        attachment,
        output,
        from_address="operator@example.invalid",
        body_intro="Synthetic approved body",
    )

    parsed = BytesParser(policy=policy.default).parsebytes(output.read_bytes())
    html_content = parsed.get_body(preferencelist=("html",)).get_content()
    plain_content = parsed.get_body(preferencelist=("plain",)).get_content()
    assert "Visible quoted source" in html_content
    assert "Safe visible link text" in html_content
    assert "Visible quoted source" in plain_content
    assert "unsafeScript" not in html_content
    assert "unsafeAfterMismatch" not in html_content
    assert "unsafeEvent" not in html_content
    assert "unsafe form content" not in html_content
    assert "tracker.example" not in html_content
    assert "attacker.example" not in html_content
    assert "remote.example" not in html_content
    assert "<script" not in html_content
    assert "<img" not in html_content
    assert "<form" not in html_content
    assert "onerror" not in html_content


def test_draft_bounds_quoted_source_text(tmp_path: Path) -> None:
    source = EmailMessage()
    source["From"] = "IT Desk <it@example.invalid>"
    source["To"] = "Operator <operator@example.invalid>"
    source["Subject"] = "Synthetic oversized source"
    source.set_content("quoted-start\n" + ("x" * 110_000) + "\nquoted-tail")
    attachment = tmp_path / "synthetic-result.xlsx"
    attachment.write_bytes(b"synthetic workbook bytes")
    output = tmp_path / "bounded-source-draft.eml"

    TranMailDraftBuilder().build(
        source.as_bytes(),
        [_resolution()],
        attachment,
        output,
        from_address="operator@example.invalid",
        body_intro="Synthetic approved body",
    )

    parsed = BytesParser(policy=policy.default).parsebytes(output.read_bytes())
    plain_content = parsed.get_body(preferencelist=("plain",)).get_content()
    assert "quoted-start" in plain_content
    assert "[quoted source truncated]" in plain_content
    assert "quoted-tail" not in plain_content
    assert len(plain_content) < 105_000


def test_draft_validates_headers_and_never_overwrites(tmp_path: Path) -> None:
    attachment = tmp_path / "synthetic-result.xlsx"
    attachment.write_bytes(b"synthetic workbook bytes")
    output = tmp_path / "reply-draft.eml"
    output.write_bytes(b"existing")

    with pytest.raises(OutputExistsError):
        TranMailDraftBuilder().build(
            _original_email(),
            [_resolution()],
            attachment,
            output,
            from_address="operator@example.invalid",
            body_intro="Synthetic body",
        )
    with pytest.raises(TranMailError, match="control characters"):
        TranMailDraftBuilder().build(
            _original_email(),
            [_resolution()],
            attachment,
            tmp_path / "new.eml",
            from_address="operator@example.invalid",
            body_intro="Unsafe\x00body",
        )
    with pytest.raises(TranMailError, match="single non-empty"):
        TranMailDraftBuilder().build(
            _original_email(),
            [_resolution()],
            attachment,
            tmp_path / "header-injection.eml",
            from_address="operator@example.invalid\nBcc: attacker@example.invalid",
            body_intro="Safe body",
        )
    with pytest.raises(TranMailError, match="must be non-empty"):
        TranMailDraftBuilder().build(
            _original_email(),
            [_resolution()],
            attachment,
            tmp_path / "empty-body.eml",
            from_address="operator@example.invalid",
            body_intro="   ",
        )
