"""Tests for HTML rendering and downloadable, never-sent TranNNB drafts."""

from __future__ import annotations

from datetime import date
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest

from asset_compensation.adapters import (
    OutputExistsError,
    TranMailDraftBuilder,
    TranMailError,
    build_tran_mail_table,
)
from asset_compensation.domain import CompensationAsset, ReferenceStatus
from asset_compensation.services import CompensationService, TranAssetRequest, TranResolution


def _resolution(asset_name: str = "Synthetic laptop") -> TranResolution:
    asset = CompensationAsset(
        tag_number="LAP10001",
        asset_name=asset_name,
        domain="demo.user",
        lost_date=date(2026, 1, 1),
        cost=1_000_000,
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


def test_html_table_escapes_untrusted_values_and_includes_total() -> None:
    table = build_tran_mail_table([_resolution("<script>alert(1)</script>")])

    assert "<script>" not in table
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in table
    assert "Total:" in table
    assert 'role="table"' in table


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
    assert "\nSecond line with <review>" in parsed.get_body(
        preferencelist=("plain",)
    ).get_content()
    assert "LAP10001" in html_part.get_content()


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
