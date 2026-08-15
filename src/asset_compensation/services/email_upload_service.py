"""Strict transport-neutral validation for uploaded RFC 822 email files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from asset_compensation.domain import ValidationError

from .ingestion_service import EmailPayload

MAX_EMAIL_FILES = 20
MAX_EMAIL_FILE_BYTES = 2 * 1024 * 1024
MAX_EMAIL_TOTAL_BYTES = 25 * 1024 * 1024
MAX_EMAIL_HEADER_BYTES = 64 * 1024
MAX_EMAIL_HEADERS = 100
MAX_EMAIL_MIME_DEPTH = 5
MAX_EMAIL_LEAF_PARTS = 20
MAX_EMAIL_DECODED_TEXT_BYTES = 512 * 1024
_EMAIL_MIME_TYPES = frozenset(
    {"", "application/octet-stream", "message/rfc822", "text/plain"}
)
_KNOWN_HEADERS = frozenset(
    {
        "content-type",
        "date",
        "from",
        "message-id",
        "mime-version",
        "received",
        "subject",
        "to",
    }
)


class EmailUploadError(ValidationError):
    """Raised when a multipart email upload violates the safe input contract."""


@dataclass(frozen=True, slots=True)
class EmailUpload:
    filename: str
    content_type: str | None
    stream: BinaryIO


class EmailUploadService:
    """Validate EML streams in memory without retaining raw messages on disk."""

    def validate(self, uploads: list[EmailUpload]) -> tuple[EmailPayload, ...]:
        if not uploads:
            raise EmailUploadError("At least one EML file is required")
        if len(uploads) > MAX_EMAIL_FILES:
            raise EmailUploadError(f"A request cannot contain more than {MAX_EMAIL_FILES} emails")

        payloads: list[EmailPayload] = []
        total_size = 0
        for index, upload in enumerate(uploads, start=1):
            safe_name = self._safe_name(upload.filename, index)
            content_type = (upload.content_type or "").partition(";")[0].strip().casefold()
            if content_type not in _EMAIL_MIME_TYPES:
                raise EmailUploadError(
                    f"Email {index} content type does not match the .eml format"
                )

            chunks: list[bytes] = []
            file_size = 0
            while chunk := upload.stream.read(64 * 1024):
                if not isinstance(chunk, bytes):
                    raise EmailUploadError(f"Email {index} stream must be binary")
                file_size += len(chunk)
                total_size += len(chunk)
                if file_size > MAX_EMAIL_FILE_BYTES:
                    raise EmailUploadError(
                        f"Email {index} exceeds the 2 MiB per-file limit"
                    )
                if total_size > MAX_EMAIL_TOTAL_BYTES:
                    raise EmailUploadError("Email upload exceeds the 25 MiB total limit")
                chunks.append(chunk)
            if file_size == 0:
                raise EmailUploadError(f"Email {index} is empty")
            data = b"".join(chunks)
            self._validate_content(data, index)
            payloads.append(EmailPayload(filename=safe_name, data=data))
        return tuple(payloads)

    def validate_forwarded_attachment(self, upload: EmailUpload) -> EmailPayload:
        """Validate one forwarding wrapper and return its single attached EML.

        This deliberately separate entry point is used only by the bounded
        Microsoft 365 forwarding-inbox workflow. Normal uploads keep rejecting
        every real attachment.
        """

        content_type = (upload.content_type or "").partition(";")[0].strip().casefold()
        if content_type not in _EMAIL_MIME_TYPES:
            raise EmailUploadError("Forwarded email content type is invalid")
        chunks: list[bytes] = []
        size = 0
        while chunk := upload.stream.read(64 * 1024):
            if not isinstance(chunk, bytes):
                raise EmailUploadError("Forwarded email stream must be binary")
            size += len(chunk)
            if size > MAX_EMAIL_FILE_BYTES:
                raise EmailUploadError("Forwarded email exceeds the 2 MiB limit")
            chunks.append(chunk)
        if size == 0:
            raise EmailUploadError("Forwarded email is empty")
        data = b"".join(chunks)
        inner_data, attachment_name = self._extract_forwarded_eml(data)
        validated = self.validate(
            [
                EmailUpload(
                    filename=attachment_name,
                    content_type="message/rfc822",
                    stream=BytesIO(inner_data),
                )
            ]
        )[0]
        return EmailPayload(filename=attachment_name, data=validated.data)

    @staticmethod
    def _safe_name(filename: str, index: int) -> str:
        basename = re.split(r"[/\\]", str(filename or ""))[-1].strip()
        if not basename or Path(basename).suffix.casefold() != ".eml":
            raise EmailUploadError(f"Email {index} must use the .eml extension")
        return f"upload-{index:02d}.eml"

    @staticmethod
    def _validate_content(data: bytes, index: int) -> None:
        full_message = EmailUploadService._parse_content(data, index)
        state = {"headers": 0, "leaves": 0, "text_bytes": 0}
        EmailUploadService._validate_mime_part(full_message, index, 0, state)

    @staticmethod
    def _parse_content(data: bytes, index: int) -> Message:
        if b"\x00" in data:
            raise EmailUploadError(f"Email {index} contains binary content")
        separator = b"\r\n\r\n"
        boundary = data.find(separator, 0, MAX_EMAIL_HEADER_BYTES + len(separator))
        separator_length = len(separator)
        if boundary < 0:
            separator = b"\n\n"
            boundary = data.find(separator, 0, MAX_EMAIL_HEADER_BYTES + len(separator))
            separator_length = len(separator)
        if boundary <= 0:
            raise EmailUploadError(f"Email {index} does not contain valid RFC 822 headers")
        if boundary > MAX_EMAIL_HEADER_BYTES:
            raise EmailUploadError(f"Email {index} headers exceed the 64 KiB limit")
        header_bytes = data[: boundary + separator_length]
        try:
            message = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
        except Exception as exc:  # parser exception types vary by malformed input
            raise EmailUploadError(f"Email {index} has invalid RFC 822 headers") from exc
        names = {name.casefold() for name in message}
        if len(message.keys()) > MAX_EMAIL_HEADERS:
            raise EmailUploadError(f"Email {index} contains too many header fields")
        if len(names & _KNOWN_HEADERS) < 2 or not names & {"from", "subject"}:
            raise EmailUploadError(f"Email {index} does not look like an RFC 822 message")
        try:
            full_message = BytesParser(policy=policy.default).parsebytes(data)
        except Exception as exc:  # parser exception types vary by malformed input
            raise EmailUploadError(f"Email {index} has an invalid MIME structure") from exc
        return full_message

    @staticmethod
    def _extract_forwarded_eml(data: bytes) -> tuple[bytes, str]:
        message = EmailUploadService._parse_content(data, 1)
        state = {"headers": 0, "leaves": 0, "text_bytes": 0}
        attached: list[tuple[bytes, str]] = []
        EmailUploadService._validate_forwarding_wrapper_part(
            message,
            1,
            0,
            state,
            attached,
        )
        if len(attached) != 1:
            raise EmailUploadError(
                "Forwarded email must contain exactly one attached EML message"
            )
        return attached[0]

    @staticmethod
    def _validate_forwarding_wrapper_part(
        part: Message,
        index: int,
        depth: int,
        state: dict[str, int],
        attached: list[tuple[bytes, str]],
    ) -> None:
        if depth > MAX_EMAIL_MIME_DEPTH:
            raise EmailUploadError(f"Email {index} exceeds the MIME nesting-depth limit")
        state["headers"] += len(part.keys())
        if state["headers"] > MAX_EMAIL_HEADERS:
            raise EmailUploadError(f"Email {index} contains too many MIME header fields")
        content_type = part.get_content_type().casefold()
        disposition = (part.get_content_disposition() or "").casefold()

        if content_type == "message/rfc822":
            if disposition != "attachment":
                raise EmailUploadError("Forwarded EML must be an explicit attachment")
            state["leaves"] += 1
            if state["leaves"] > MAX_EMAIL_LEAF_PARTS or attached:
                raise EmailUploadError(
                    "Forwarded email must contain exactly one attached EML message"
                )
            payload = part.get_payload()
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(
                payload[0], Message
            ):
                raise EmailUploadError("Forwarded EML attachment is malformed")
            inner_data = payload[0].as_bytes(policy=policy.SMTP)
            if not inner_data or len(inner_data) > MAX_EMAIL_FILE_BYTES:
                raise EmailUploadError("Forwarded EML attachment exceeds the safe limit")
            raw_name = str(part.get_filename() or "forwarded-message.eml")
            basename = re.split(r"[/\\]", raw_name)[-1].strip()
            if not basename or Path(basename).suffix.casefold() != ".eml":
                basename = "forwarded-message.eml"
            attached.append((inner_data, basename))
            return

        if disposition == "attachment":
            raise EmailUploadError("Forwarding wrapper contains an unsupported attachment")
        if part.is_multipart():
            children = part.get_payload()
            if not isinstance(children, list):
                raise EmailUploadError(f"Email {index} has an invalid multipart structure")
            for child in children:
                if not isinstance(child, Message):
                    raise EmailUploadError(f"Email {index} has an invalid MIME part")
                EmailUploadService._validate_forwarding_wrapper_part(
                    child,
                    index,
                    depth + 1,
                    state,
                    attached,
                )
            return

        state["leaves"] += 1
        if state["leaves"] > MAX_EMAIL_LEAF_PARTS:
            raise EmailUploadError(f"Email {index} contains too many MIME parts")
        if content_type.startswith("text/"):
            decoded = part.get_payload(decode=True)
            if decoded is None:
                decoded = str(part.get_payload() or "").encode("utf-8", errors="replace")
            state["text_bytes"] += len(decoded)
            if state["text_bytes"] > MAX_EMAIL_DECODED_TEXT_BYTES:
                raise EmailUploadError(
                    f"Email {index} exceeds the decoded-text size limit"
                )
            return
        if content_type.startswith("image/") and disposition in {"", "inline"}:
            return
        raise EmailUploadError(f"Email {index} contains an unsupported MIME part")

    @staticmethod
    def _validate_mime_part(
        part: Message,
        index: int,
        depth: int,
        state: dict[str, int],
    ) -> None:
        if depth > MAX_EMAIL_MIME_DEPTH:
            raise EmailUploadError(f"Email {index} exceeds the MIME nesting-depth limit")
        state["headers"] += len(part.keys())
        if state["headers"] > MAX_EMAIL_HEADERS:
            raise EmailUploadError(f"Email {index} contains too many MIME header fields")
        content_type = part.get_content_type().casefold()
        disposition = (part.get_content_disposition() or "").casefold()
        if disposition == "attachment" or content_type == "message/rfc822":
            raise EmailUploadError(f"Email {index} contains unsupported attachments")
        if part.is_multipart():
            children = part.get_payload()
            if not isinstance(children, list):
                raise EmailUploadError(f"Email {index} has an invalid multipart structure")
            for child in children:
                if not isinstance(child, Message):
                    raise EmailUploadError(f"Email {index} has an invalid MIME part")
                EmailUploadService._validate_mime_part(child, index, depth + 1, state)
            return

        state["leaves"] += 1
        if state["leaves"] > MAX_EMAIL_LEAF_PARTS:
            raise EmailUploadError(f"Email {index} contains too many MIME parts")
        if content_type.startswith("text/"):
            decoded = part.get_payload(decode=True)
            if decoded is None:
                raw = part.get_payload()
                decoded = str(raw or "").encode("utf-8", errors="replace")
            state["text_bytes"] += len(decoded)
            if state["text_bytes"] > MAX_EMAIL_DECODED_TEXT_BYTES:
                raise EmailUploadError(
                    f"Email {index} exceeds the decoded-text size limit"
                )
            return
        if content_type.startswith("image/") and disposition in {"", "inline"}:
            return
        raise EmailUploadError(f"Email {index} contains an unsupported MIME part")
