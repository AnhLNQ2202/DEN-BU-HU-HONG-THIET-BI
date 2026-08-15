"""Microsoft 365 mail ingestion and unsent Outlook draft orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from io import BytesIO
from pathlib import Path
from typing import Any

from asset_compensation.adapters.m365_graph import (
    MAX_DIRECT_ATTACHMENT_BYTES,
    M365GraphDraftUncertain,
    M365GraphError,
    M365GraphReconnectRequired,
)
from asset_compensation.adapters.tran_mail import (
    TranMailError,
    build_tran_outlook_body,
    extract_source_message_id,
)

from .email_upload_service import EmailUpload, EmailUploadError, EmailUploadService
from .ingestion_service import EmailPayload, ingest_eml_payloads
from .m365_auth_service import (
    M365ConnectionService,
    M365ProviderError,
    M365RawSyncBatch,
    M365ReconnectRequired,
)
from .mail_artifact_service import MailArtifactError, MailArtifactStore, safe_eml_basename


@dataclass(frozen=True, slots=True)
class M365SyncResult:
    role: str
    folder: dict[str, str]
    fetched_count: int
    ingested: int
    case_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    unknown_files: tuple[str, ...]
    skipped_files: tuple[str, ...]
    has_more: bool
    cursor_ready: bool
    unavailable_count: int = 0
    oversized_count: int = 0
    invalid_mime_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "folder": self.folder,
            "fetched_count": self.fetched_count,
            "ingested": self.ingested,
            "case_ids": list(self.case_ids),
            "warnings": list(self.warnings),
            "unknown_files": list(self.unknown_files),
            "skipped_files": list(self.skipped_files),
            "has_more": self.has_more,
            "cursor_ready": self.cursor_ready,
            "unavailable_count": self.unavailable_count,
            "oversized_count": self.oversized_count,
            "invalid_mime_count": self.invalid_mime_count,
        }


@dataclass(frozen=True, slots=True)
class M365OutlookDraftResult:
    subject: str | None
    web_url: str | None
    mode: str = "reply_all"

    def to_dict(self) -> dict[str, str | None]:
        return {"subject": self.subject, "web_url": self.web_url, "mode": self.mode}


class M365MailSyncService:
    """Feed bounded folder delta MIME payloads into the existing EML pipeline."""

    def __init__(
        self,
        connection_service: M365ConnectionService,
        email_upload_service: EmailUploadService,
        case_service: Any,
        artifact_store: MailArtifactStore,
        *,
        retain_raw_eml: bool,
    ) -> None:
        self._connections = connection_service
        self._uploads = email_upload_service
        self._cases = case_service
        self._artifacts = artifact_store
        self._retain = retain_raw_eml

    def sync(
        self,
        session_id: str | None,
        role: object,
        *,
        supplier_directory: Mapping[str, Any] | None,
        ambiguous_supplier_domains: frozenset[str],
    ) -> M365SyncResult:
        batch = self._connections.collect_sync_batch(session_id, role)
        payloads, invalid_mime_count = self._validated_payloads(batch)
        retained: tuple[Any, ...] = ()
        artifact_metadata: dict[str, dict[str, str]] = {}
        if self._retain and payloads:
            try:
                retained = self._artifacts.retain_many(payloads)
            except MailArtifactError as exc:
                raise M365ProviderError(
                    "Microsoft 365 source email could not be retained safely"
                ) from exc
            artifact_metadata = {
                item.content_sha256: {
                    "mail_artifact_handle": item.handle,
                    "mail_artifact_filename": item.safe_filename,
                }
                for item in retained
            }

        previous_ids: set[str] = set()
        persisted = False
        try:
            previous_ids = {case.id for case in self._cases.list_cases()}
            report = ingest_eml_payloads(
                self._cases,
                payloads,
                supplier_directory=supplier_directory,
                ambiguous_supplier_domains=ambiguous_supplier_domains,
                artifact_metadata_by_sha256=artifact_metadata or None,
            )
            persisted = True
            self._remove_unreferenced_artifacts(retained)
            self._connections.commit_sync_cursor(session_id, batch)
        except Exception as exc:
            # Once the DB transaction commits, its cases may reference these
            # content-addressed artifacts. Never create dangling handles merely
            # because a later cleanup/cursor step failed.
            if not persisted:
                try:
                    self._rollback_new_artifacts(retained)
                except M365ProviderError as cleanup_exc:
                    raise cleanup_exc from exc
            raise

        warnings = tuple(report.warnings)
        if batch.unavailable_count:
            warnings = (
                *warnings,
                f"{batch.unavailable_count} folder message(s) became unavailable and were skipped",
            )
        if invalid_mime_count:
            warnings = (
                *warnings,
                f"{invalid_mime_count} message(s) failed safe EML validation and were skipped",
            )
        if batch.oversized_count:
            warnings = (
                *warnings,
                f"{batch.oversized_count} message(s) exceeded the safe MIME limit and were skipped",
            )
        return M365SyncResult(
            role=batch.role,
            folder=batch.folder.to_dict(),
            fetched_count=len(batch.messages),
            ingested=sum(case.id not in previous_ids for case in report.cases),
            case_ids=tuple(case.id for case in report.cases),
            warnings=warnings,
            unknown_files=tuple(report.unknown_files),
            skipped_files=tuple(report.skipped_files),
            has_more=batch.has_more,
            cursor_ready=batch.cursor_ready,
            unavailable_count=batch.unavailable_count,
            oversized_count=batch.oversized_count,
            invalid_mime_count=invalid_mime_count,
        )

    def _validated_payloads(self, batch: M365RawSyncBatch) -> tuple[tuple[EmailPayload, ...], int]:
        if not batch.messages:
            return (), 0
        payloads: list[EmailPayload] = []
        invalid_count = 0
        for message in batch.messages:
            upload = EmailUpload(
                filename=message.filename,
                content_type="message/rfc822",
                stream=BytesIO(message.mime_bytes),
            )
            try:
                validated = self._uploads.validate([upload])[0]
            except EmailUploadError:
                try:
                    validated = self._uploads.validate_forwarded_attachment(
                        EmailUpload(
                            filename=message.filename,
                            content_type="message/rfc822",
                            stream=BytesIO(message.mime_bytes),
                        )
                    )
                except EmailUploadError:
                    invalid_count += 1
                    continue
            # Preserve a data-minimized role/index provenance name after the
            # validator has enforced every existing upload boundary.
            payloads.append(
                EmailPayload(
                    filename=self._subject_filename(validated.data, validated.filename),
                    data=validated.data,
                )
            )
        return tuple(payloads), invalid_count

    @staticmethod
    def _subject_filename(data: bytes, fallback: str) -> str:
        try:
            headers = BytesHeaderParser(policy=policy.default).parsebytes(data)
            subject = str(headers.get("subject") or "").strip()
            candidate = f"{subject}.eml" if subject else fallback
            return safe_eml_basename(candidate)
        except Exception:
            try:
                return safe_eml_basename(fallback)
            except MailArtifactError:
                return "message.eml"

    def _remove_unreferenced_artifacts(self, retained: Sequence[Any]) -> None:
        if not retained:
            return
        live_handles = {
            str(case.metadata.get("mail_artifact_handle"))
            for case in self._cases.list_cases()
            if case.metadata.get("mail_artifact_handle")
        }
        for item in retained:
            if item.handle not in live_handles:
                try:
                    self._artifacts.delete(item.handle)
                except (MailArtifactError, OSError) as exc:
                    raise M365ProviderError(
                        "Microsoft 365 unclassified source cleanup did not complete"
                    ) from exc

    def _rollback_new_artifacts(self, retained: Sequence[Any]) -> None:
        failed = 0
        for item in retained:
            if item.created:
                try:
                    self._artifacts.delete(item.handle)
                except (MailArtifactError, OSError):
                    failed += 1
        if failed:
            raise M365ProviderError(
                "Microsoft 365 source cleanup did not complete"
            )


class M365OutlookDraftService:
    """Create an unsent Outlook draft while structurally exposing no send call."""

    def __init__(self, connection_service: M365ConnectionService) -> None:
        self._connections = connection_service

    def create(
        self,
        session_id: str | None,
        *,
        original_eml: bytes,
        resolutions: Sequence[Any],
        workbook_path: str | Path,
        body_intro: str,
    ) -> M365OutlookDraftResult:
        workbook = Path(workbook_path)
        try:
            size = workbook.stat().st_size
        except OSError as exc:
            raise M365ProviderError("Generated workbook is unavailable") from exc
        if size <= 0 or size >= MAX_DIRECT_ATTACHMENT_BYTES:
            raise M365ProviderError("Generated workbook exceeds the safe Outlook attachment limit")

        personal_forwarding = self._connections.personal_forwarding_inbox
        try:
            # Validate the approved intro/table prefix before Graph can create
            # any draft. Company mode later adds Outlook's provider-created
            # quote; personal forwarding mode deliberately creates a standalone
            # copy-only draft with no recipients.
            standalone_body = build_tran_outlook_body(
                resolutions,
                body_intro,
                "",
                "text",
            )
            if personal_forwarding:
                standalone_subject = self._standalone_subject(original_eml)
                message_id = None
            else:
                message_id = extract_source_message_id(original_eml)
                standalone_subject = None
        except TranMailError as exc:
            raise M365ProviderError("Retained source email headers are invalid") from exc

        graph = self._connections.graph_client(session_id, "tran")
        created_id: str | None = None
        create_attempted = False
        try:
            if personal_forwarding:
                assert standalone_subject is not None
                create_attempted = True
                created = graph.create_message_draft(
                    standalone_subject,
                    standalone_body,
                )
                created_id = created.id
                graph.attach_workbook(created.id, workbook)
                return M365OutlookDraftResult(
                    subject=created.subject,
                    web_url=created.web_link,
                    mode="standalone",
                )

            assert message_id is not None
            source_id = graph.find_message_by_internet_id(message_id)
            create_attempted = True
            created = graph.create_reply_all_draft(source_id)
            created_id = created.id
            body = build_tran_outlook_body(
                resolutions,
                body_intro,
                created.body_content,
                created.body_content_type,
            )
            updated = graph.update_draft_html(created.id, body)
            graph.attach_workbook(created.id, workbook)
            return M365OutlookDraftResult(
                subject=updated.subject or created.subject,
                web_url=updated.web_link or created.web_link,
                mode="reply_all",
            )
        except M365GraphDraftUncertain as exc:
            self._rollback_or_raise(
                graph,
                exc.draft_id,
                exc,
                reconnect=False,
                uncertain_if_missing=True,
            )
        except M365GraphReconnectRequired as exc:
            self._connections.invalidate(session_id, "tran")
            self._rollback_or_raise(
                graph,
                created_id,
                exc,
                reconnect=True,
                uncertain_if_missing=False,
            )
        except (M365GraphError, TranMailError) as exc:
            self._rollback_or_raise(
                graph,
                created_id,
                exc,
                reconnect=False,
                uncertain_if_missing=create_attempted,
            )
        raise AssertionError("Unreachable Outlook draft state")

    @staticmethod
    def _standalone_subject(original_eml: bytes) -> str:
        if not original_eml:
            raise TranMailError("Source email is required")
        try:
            headers = BytesHeaderParser(policy=policy.default).parsebytes(original_eml)
        except Exception as exc:
            raise TranMailError("Source email headers are invalid") from exc
        values = headers.get_all("Subject", [])
        raw = str(values[0]).strip() if len(values) == 1 else ""
        normalized = " ".join(raw.split())
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise TranMailError("Source email Subject is invalid")
        subject = f"DRAFT - Re: {normalized or 'Tran compensation review'}"
        return subject[:998]

    @staticmethod
    def _rollback_or_raise(
        graph: Any,
        created_id: str | None,
        cause: Exception,
        *,
        reconnect: bool,
        uncertain_if_missing: bool,
    ) -> None:
        if created_id is None:
            if reconnect:
                raise M365ReconnectRequired("Reconnect the Tran Microsoft 365 role") from cause
            if uncertain_if_missing:
                raise M365ProviderError(
                    "Outlook draft creation status is uncertain; "
                    "check Outlook Drafts before retrying"
                ) from cause
            raise M365ProviderError("Outlook draft creation did not complete") from cause
        try:
            graph.delete_draft(created_id)
        except Exception as rollback_error:
            raise M365ProviderError(
                "Outlook draft creation failed and temporary-draft cleanup is uncertain"
            ) from rollback_error
        if reconnect:
            raise M365ReconnectRequired("Reconnect the Tran Microsoft 365 role") from cause
        raise M365ProviderError(
            "Outlook draft creation failed; the temporary draft was removed"
        ) from cause


__all__ = [
    "M365MailSyncService",
    "M365OutlookDraftResult",
    "M365OutlookDraftService",
    "M365SyncResult",
]
