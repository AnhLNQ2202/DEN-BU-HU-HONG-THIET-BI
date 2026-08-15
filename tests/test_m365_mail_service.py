"""Synthetic tests for Graph draft and manual-folder ingestion orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import asset_compensation.services.m365_mail_service as mail_module
from asset_compensation.adapters import build_tran_mail_table, build_tran_outlook_body
from asset_compensation.adapters.m365_contract import MAX_GRAPH_BODY_CHARS
from asset_compensation.adapters.m365_graph import (
    GraphDraft,
    M365GraphDraftUncertain,
    M365GraphError,
    M365GraphReconnectRequired,
)
from asset_compensation.domain import CompensationAsset, ReferenceStatus
from asset_compensation.services import (
    CompensationService,
    EmailUploadService,
    M365OutlookDraftService,
    M365ProviderError,
    M365ReconnectRequired,
    TranAssetRequest,
    TranResolution,
)
from asset_compensation.services.ingestion_service import IngestionReport
from asset_compensation.services.m365_auth_service import (
    M365RawMessage,
    M365RawSyncBatch,
    M365SelectedFolder,
)
from asset_compensation.services.m365_mail_service import M365MailSyncService


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


def _eml() -> bytes:
    message = EmailMessage()
    message["From"] = "IT <it@example.test>"
    message["To"] = "Employee <employee@example.test>"
    message["Subject"] = "Synthetic source"
    message["Message-ID"] = "<exact-source@example.test>"
    message.set_content("Synthetic source body")
    return message.as_bytes()


class _DraftGraph:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        rollback_fails: bool = False,
    ) -> None:
        self.fail_at = fail_at
        self.rollback_fails = rollback_fails
        self.message_id: str | None = None
        self.updated_html: str | None = None
        self.attachment: Path | None = None
        self.deleted: list[str] = []

    def _fail(self, operation: str) -> None:
        if self.fail_at == operation:
            if operation == "lookup":
                raise M365GraphError("provider identifier detail")
            if operation == "reconnect":
                raise M365GraphReconnectRequired("secret provider token")
            raise M365GraphError("provider internal detail")

    def find_message_by_internet_id(self, message_id: str) -> str:
        self._fail("lookup")
        self._fail("reconnect")
        self.message_id = message_id
        return "source-id"

    def create_reply_all_draft(self, message_id: str) -> GraphDraft:
        assert message_id == "source-id"
        if self.fail_at == "invalid-response":
            raise M365GraphDraftUncertain("provider invalid response", draft_id="draft-id")
        self._fail("create")
        return GraphDraft(
            id="draft-id",
            subject="Re: Synthetic source",
            body_content_type="html",
            body_content="<div>Graph-created quoted thread</div>",
            web_link="https://outlook.office.com/mail/deeplink/draft/one",
        )

    def update_draft_html(self, draft_id: str, html: str) -> GraphDraft:
        assert draft_id == "draft-id"
        self._fail("patch")
        self.updated_html = html
        return GraphDraft(
            id=draft_id,
            subject="Re: Synthetic source",
            body_content_type="html",
            body_content=html,
            web_link="https://outlook.office.com/mail/deeplink/draft/two",
        )

    def attach_workbook(self, draft_id: str, path: Path) -> None:
        assert draft_id == "draft-id"
        self._fail("attachment")
        self.attachment = Path(path)

    def delete_draft(self, draft_id: str) -> None:
        if self.rollback_fails:
            raise M365GraphError("provider rollback detail")
        self.deleted.append(draft_id)


class _DraftConnections:
    def __init__(self, graph: _DraftGraph) -> None:
        self.graph = graph
        self.invalidated: list[tuple[str | None, str]] = []

    def graph_client(self, session_id: str | None, role: object) -> _DraftGraph:
        assert session_id == "browser-session"
        assert role == "tran"
        return self.graph

    def invalidate(self, session_id: str | None, role: object) -> None:
        self.invalidated.append((session_id, str(role)))


def test_outlook_draft_prepends_escaped_intro_exact_table_and_attachment(
    tmp_path: Path,
) -> None:
    graph = _DraftGraph()
    service = M365OutlookDraftService(_DraftConnections(graph))  # type: ignore[arg-type]
    workbook = tmp_path / "result.xlsx"
    workbook.write_bytes(b"synthetic workbook")
    resolution = _resolution("Laptop <approved>")

    result = service.create(
        "browser-session",
        original_eml=_eml(),
        resolutions=[resolution],
        workbook_path=workbook,
        body_intro="Dear team, <review>\nApproved",
    )

    assert graph.message_id == "<exact-source@example.test>"
    assert graph.updated_html is not None
    assert "Dear team, &lt;review&gt;<br>Approved" in graph.updated_html
    assert build_tran_mail_table([resolution]) in graph.updated_html
    assert graph.updated_html.endswith("<div>Graph-created quoted thread</div>")
    assert graph.attachment == workbook
    assert graph.deleted == []
    assert result.web_url == "https://outlook.office.com/mail/deeplink/draft/two"


@pytest.mark.parametrize("content_type", ["html", "text"])
def test_outlook_body_safely_bounds_large_quoted_thread(content_type: str) -> None:
    quote = (
        "<div>" + ("x" * (MAX_GRAPH_BODY_CHARS - 12)) + "</div>"
        if content_type == "html"
        else "<&>" * (MAX_GRAPH_BODY_CHARS // 3)
    )
    resolution = _resolution()

    body = build_tran_outlook_body(
        [resolution],
        "Approved intro",
        quote,
        content_type,
    )

    assert len(body) <= MAX_GRAPH_BODY_CHARS
    assert build_tran_mail_table([resolution]) in body
    assert "Outlook quoted thread truncated for safe draft size" in body


@pytest.mark.parametrize("failure", ["patch", "attachment"])
def test_outlook_partial_failure_deletes_temporary_draft(
    tmp_path: Path,
    failure: str,
) -> None:
    graph = _DraftGraph(fail_at=failure)
    service = M365OutlookDraftService(_DraftConnections(graph))  # type: ignore[arg-type]
    workbook = tmp_path / "result.xlsx"
    workbook.write_bytes(b"synthetic workbook")

    with pytest.raises(M365ProviderError, match="temporary draft was removed") as error:
        service.create(
            "browser-session",
            original_eml=_eml(),
            resolutions=[_resolution()],
            workbook_path=workbook,
            body_intro="Approved intro",
        )

    assert graph.deleted == ["draft-id"]
    assert "provider" not in str(error.value).casefold()


def test_invalid_create_response_uses_returned_id_for_rollback(tmp_path: Path) -> None:
    graph = _DraftGraph(fail_at="invalid-response")
    service = M365OutlookDraftService(_DraftConnections(graph))  # type: ignore[arg-type]
    workbook = tmp_path / "result.xlsx"
    workbook.write_bytes(b"synthetic workbook")

    with pytest.raises(M365ProviderError, match="temporary draft was removed"):
        service.create(
            "browser-session",
            original_eml=_eml(),
            resolutions=[_resolution()],
            workbook_path=workbook,
            body_intro="Approved intro",
        )

    assert graph.deleted == ["draft-id"]


def test_outlook_rollback_uncertainty_and_reconnect_are_sanitized(tmp_path: Path) -> None:
    workbook = tmp_path / "result.xlsx"
    workbook.write_bytes(b"synthetic workbook")
    uncertain_graph = _DraftGraph(fail_at="patch", rollback_fails=True)
    uncertain = M365OutlookDraftService(  # type: ignore[arg-type]
        _DraftConnections(uncertain_graph)
    )
    with pytest.raises(M365ProviderError, match="cleanup is uncertain") as error:
        uncertain.create(
            "browser-session",
            original_eml=_eml(),
            resolutions=[_resolution()],
            workbook_path=workbook,
            body_intro="Approved intro",
        )
    assert "provider rollback detail" not in str(error.value)

    reconnect_graph = _DraftGraph(fail_at="reconnect")
    connections = _DraftConnections(reconnect_graph)
    reconnect = M365OutlookDraftService(connections)  # type: ignore[arg-type]
    with pytest.raises(M365ReconnectRequired) as reconnect_error:
        reconnect.create(
            "browser-session",
            original_eml=_eml(),
            resolutions=[_resolution()],
            workbook_path=workbook,
            body_intro="Approved intro",
        )
    assert connections.invalidated == [("browser-session", "tran")]
    assert "token" not in str(reconnect_error.value).casefold()

    create_graph = _DraftGraph(fail_at="create")
    create_uncertain = M365OutlookDraftService(  # type: ignore[arg-type]
        _DraftConnections(create_graph)
    )
    with pytest.raises(M365ProviderError, match="status is uncertain"):
        create_uncertain.create(
            "browser-session",
            original_eml=_eml(),
            resolutions=[_resolution()],
            workbook_path=workbook,
            body_intro="Approved intro",
        )


@dataclass(frozen=True)
class _Artifact:
    handle: str
    safe_filename: str
    content_sha256: str
    created: bool = True


class _ArtifactStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def retain_many(self, payloads: tuple[Any, ...]) -> tuple[_Artifact, ...]:
        return tuple(
            _Artifact(
                handle=f"eml-sha256-{hashlib.sha256(item.data).hexdigest()}",
                safe_filename=item.filename,
                content_sha256=hashlib.sha256(item.data).hexdigest(),
            )
            for item in payloads
        )

    def delete(self, handle: str) -> bool:
        self.deleted.append(handle)
        return True


class _Cases:
    def __init__(self) -> None:
        self.live: list[Any] = []

    def list_cases(self) -> list[Any]:
        return self.live


class _SyncConnections:
    def __init__(self, *, commit_fails: bool = False) -> None:
        self.committed = False
        self.commit_fails = commit_fails
        self.batch = M365RawSyncBatch(
            role="ngan",
            folder=M365SelectedFolder("folder", "Compensation", "Inbox / Compensation"),
            messages=(M365RawMessage("m365-ngan-01.eml", _eml()),),
            base_cursor=None,
            next_cursor="https://graph.microsoft.com/v1.0/cursor?$deltatoken=x",
            has_more=False,
            cursor_ready=True,
        )

    def collect_sync_batch(self, session_id: str | None, role: object) -> M365RawSyncBatch:
        assert session_id == "browser-session"
        assert role == "ngan"
        return self.batch

    def commit_sync_cursor(self, session_id: str | None, batch: M365RawSyncBatch) -> None:
        assert session_id == "browser-session"
        assert batch is self.batch
        if self.commit_fails:
            raise M365ProviderError("synthetic cursor failure")
        self.committed = True


def test_manual_sync_uses_existing_validator_ingestion_and_commits_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = _SyncConnections()
    cases = _Cases()
    artifacts = _ArtifactStore()

    def ingest(case_service: Any, payloads: tuple[Any, ...], **kwargs: Any) -> Any:
        assert case_service is cases
        assert payloads[0].filename == "m365-ngan-01.eml"
        assert kwargs["supplier_directory"] == {"demo": "supplier"}
        digest = hashlib.sha256(payloads[0].data).hexdigest()
        case = SimpleNamespace(
            id="LOST-202601-SYNTHETIC",
            metadata={"mail_artifact_handle": f"eml-sha256-{digest}"},
        )
        cases.live = [case]
        return IngestionReport((case,), ("synthetic warning",), (), ())

    monkeypatch.setattr(mail_module, "ingest_eml_payloads", ingest)
    service = M365MailSyncService(
        connections,  # type: ignore[arg-type]
        EmailUploadService(),
        cases,
        artifacts,  # type: ignore[arg-type]
        retain_raw_eml=True,
    )

    result = service.sync(
        "browser-session",
        "ngan",
        supplier_directory={"demo": "supplier"},
        ambiguous_supplier_domains=frozenset(),
    )

    assert result.folder["path"] == "Inbox / Compensation"
    assert result.fetched_count == 1
    assert result.ingested == 1
    assert result.case_ids == ("LOST-202601-SYNTHETIC",)
    assert result.warnings == ("synthetic warning",)
    assert connections.committed is True
    assert artifacts.deleted == []


def test_sync_rolls_back_only_before_persistence_not_after_cursor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _Cases()
    before_artifacts = _ArtifactStore()
    before_connections = _SyncConnections()

    def fail_ingest(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("synthetic ingestion failure")

    monkeypatch.setattr(mail_module, "ingest_eml_payloads", fail_ingest)
    before = M365MailSyncService(
        before_connections,  # type: ignore[arg-type]
        EmailUploadService(),
        cases,
        before_artifacts,  # type: ignore[arg-type]
        retain_raw_eml=True,
    )
    with pytest.raises(RuntimeError, match="ingestion"):
        before.sync(
            "browser-session",
            "ngan",
            supplier_directory=None,
            ambiguous_supplier_domains=frozenset(),
        )
    assert len(before_artifacts.deleted) == 1

    after_artifacts = _ArtifactStore()
    after_connections = _SyncConnections(commit_fails=True)

    def persist(case_service: Any, payloads: tuple[Any, ...], **kwargs: Any) -> Any:
        del kwargs
        digest = hashlib.sha256(payloads[0].data).hexdigest()
        case = SimpleNamespace(
            id="LOST-202601-PERSISTED",
            metadata={"mail_artifact_handle": f"eml-sha256-{digest}"},
        )
        case_service.live = [case]
        return IngestionReport((case,), (), (), ())

    monkeypatch.setattr(mail_module, "ingest_eml_payloads", persist)
    after = M365MailSyncService(
        after_connections,  # type: ignore[arg-type]
        EmailUploadService(),
        cases,
        after_artifacts,  # type: ignore[arg-type]
        retain_raw_eml=True,
    )
    with pytest.raises(M365ProviderError, match="cursor"):
        after.sync(
            "browser-session",
            "ngan",
            supplier_directory=None,
            ambiguous_supplier_domains=frozenset(),
        )
    assert after_artifacts.deleted == []


def test_sync_rolls_back_new_retention_when_initial_db_read_fails() -> None:
    class FailingCases:
        def list_cases(self) -> list[Any]:
            raise RuntimeError("synthetic DB read failure")

    artifacts = _ArtifactStore()
    service = M365MailSyncService(
        _SyncConnections(),  # type: ignore[arg-type]
        EmailUploadService(),
        FailingCases(),
        artifacts,  # type: ignore[arg-type]
        retain_raw_eml=True,
    )

    with pytest.raises(RuntimeError, match="DB read"):
        service.sync(
            "browser-session",
            "ngan",
            supplier_directory=None,
            ambiguous_supplier_domains=frozenset(),
        )

    assert len(artifacts.deleted) == 1


def test_sync_reports_incomplete_private_artifact_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingDeleteArtifacts(_ArtifactStore):
        def delete(self, handle: str) -> bool:
            del handle
            raise OSError("synthetic private cleanup failure")

    def fail_ingest(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("synthetic ingestion failure")

    monkeypatch.setattr(mail_module, "ingest_eml_payloads", fail_ingest)
    service = M365MailSyncService(
        _SyncConnections(),  # type: ignore[arg-type]
        EmailUploadService(),
        _Cases(),
        FailingDeleteArtifacts(),  # type: ignore[arg-type]
        retain_raw_eml=True,
    )

    with pytest.raises(M365ProviderError, match="cleanup did not complete"):
        service.sync(
            "browser-session",
            "ngan",
            supplier_directory=None,
            ambiguous_supplier_domains=frozenset(),
        )


def test_unavailable_delta_item_is_data_minimized_and_cursor_still_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = _SyncConnections()
    connections.batch = M365RawSyncBatch(
        role="ngan",
        folder=connections.batch.folder,
        messages=(),
        base_cursor=None,
        next_cursor=connections.batch.next_cursor,
        has_more=False,
        cursor_ready=True,
        unavailable_count=1,
    )
    cases = _Cases()
    monkeypatch.setattr(
        mail_module,
        "ingest_eml_payloads",
        lambda *args, **kwargs: IngestionReport((), (), (), ()),
    )
    service = M365MailSyncService(
        connections,  # type: ignore[arg-type]
        EmailUploadService(),
        cases,
        _ArtifactStore(),  # type: ignore[arg-type]
        retain_raw_eml=False,
    )

    result = service.sync(
        "browser-session",
        "ngan",
        supplier_directory=None,
        ambiguous_supplier_domains=frozenset(),
    )

    assert result.unavailable_count == 1
    assert result.fetched_count == 0
    assert result.warnings == ("1 folder message(s) became unavailable and were skipped",)
    assert "compensation" not in result.warnings[0].casefold()
    assert "moved-message" not in result.warnings[0]
    assert connections.committed is True


def test_invalid_mime_is_skipped_individually_without_poisoning_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = _SyncConnections()
    connections.batch = M365RawSyncBatch(
        role="ngan",
        folder=connections.batch.folder,
        messages=(M365RawMessage("m365-ngan-01.eml", b"not an RFC822 email"),),
        base_cursor=None,
        next_cursor=connections.batch.next_cursor,
        has_more=False,
        cursor_ready=True,
    )
    monkeypatch.setattr(
        mail_module,
        "ingest_eml_payloads",
        lambda *args, **kwargs: IngestionReport((), (), (), ()),
    )
    service = M365MailSyncService(
        connections,  # type: ignore[arg-type]
        EmailUploadService(),
        _Cases(),
        _ArtifactStore(),  # type: ignore[arg-type]
        retain_raw_eml=False,
    )

    result = service.sync(
        "browser-session",
        "ngan",
        supplier_directory=None,
        ambiguous_supplier_domains=frozenset(),
    )

    assert result.invalid_mime_count == 1
    assert result.ingested == 0
    assert result.warnings == ("1 message(s) failed safe EML validation and were skipped",)
    assert connections.committed is True


def test_oversized_mail_count_is_minimized_while_valid_neighbors_ingest_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = _SyncConnections()
    connections.batch = M365RawSyncBatch(
        role="ngan",
        folder=connections.batch.folder,
        messages=(
            M365RawMessage("m365-ngan-01.eml", _eml()),
            M365RawMessage("m365-ngan-03.eml", _eml()),
        ),
        base_cursor=None,
        next_cursor=connections.batch.next_cursor,
        has_more=False,
        cursor_ready=True,
        oversized_count=1,
    )
    cases = _Cases()

    def ingest(case_service: Any, payloads: tuple[Any, ...], **kwargs: Any) -> Any:
        del kwargs
        created = tuple(
            SimpleNamespace(id=f"LOST-202601-{index}", metadata={})
            for index, _ in enumerate(payloads, start=1)
        )
        case_service.live = list(created)
        return IngestionReport(created, (), (), ())

    monkeypatch.setattr(mail_module, "ingest_eml_payloads", ingest)
    service = M365MailSyncService(
        connections,  # type: ignore[arg-type]
        EmailUploadService(),
        cases,
        _ArtifactStore(),  # type: ignore[arg-type]
        retain_raw_eml=False,
    )

    result = service.sync(
        "browser-session",
        "ngan",
        supplier_directory=None,
        ambiguous_supplier_domains=frozenset(),
    )

    assert result.ingested == 2
    assert result.oversized_count == 1
    assert result.warnings == ("1 message(s) exceeded the safe MIME limit and were skipped",)
    assert "folder" not in result.warnings[0].casefold()
    assert connections.committed is True
