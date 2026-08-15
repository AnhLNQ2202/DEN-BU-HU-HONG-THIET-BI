from __future__ import annotations

import ast
import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from asset_compensation.integrations.local_bridge import (
    DOWNLOAD_FILES,
    PACKAGE_DIRECTORY,
    PACKAGE_PLACEHOLDER,
)
from asset_compensation.integrations.local_bridge.core import (
    BridgeError,
    DraftPackage,
    HubClient,
    MailSnapshot,
    ScanCheckpoint,
    SourceReference,
    build_minimized_eml,
    decode_workbook,
    match_package_source,
    sanitize_reply_html,
    select_scan_candidates,
    subject_eml_filename,
)


def _snapshot(
    marker: str,
    received_at: datetime,
    *,
    html_body: str = "<p>Thiết bị mẫu LAP00001</p>",
    plain_body: str = "Thiết bị mẫu LAP00001",
) -> MailSnapshot:
    return MailSnapshot(
        entry_id=f"entry-{marker}",
        store_id="synthetic-store",
        received_at=received_at,
        subject=f"Synthetic lost asset {marker}",
        sender_name="Synthetic Sender",
        sender_address="sender@example.invalid",
        to="receiver@example.invalid",
        cc="copy@example.invalid",
        sent_at=received_at - timedelta(minutes=1),
        transport_headers=(
            "From: Header Sender <header@example.invalid>\r\n"
            "To: receiver@example.invalid\r\n"
            f"Message-ID: <synthetic-{marker}@example.invalid>\r\n"
        ),
        html_body=html_body,
        plain_body=plain_body,
    )


def _workbook_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
    return stream.getvalue()


def test_download_allowlist_is_complete_and_text_safe() -> None:
    assert DOWNLOAD_FILES == (
        "app.py",
        "core.py",
        "install.ps1",
        "start.cmd",
        "requirements.txt",
        "README.txt",
    )
    for filename in DOWNLOAD_FILES:
        path = PACKAGE_DIRECTORY / filename
        assert path.is_file()
        path.read_text(encoding="utf-8")
    placeholder_files = [
        filename
        for filename in DOWNLOAD_FILES
        if PACKAGE_PLACEHOLDER in (PACKAGE_DIRECTORY / filename).read_text(encoding="utf-8")
    ]
    assert placeholder_files == ["app.py"]
    assert (PACKAGE_DIRECTORY / "requirements.txt").read_text(encoding="utf-8") == (
        "requests>=2.32,<3\npywin32>=306,<400\n"
    )


def test_bridge_enables_dpi_awareness_before_creating_tk_window() -> None:
    source = (PACKAGE_DIRECTORY / "app.py").read_text(encoding="utf-8")
    assert "SetProcessDpiAwarenessContext" in source
    assert 'root.tk.call("tk", "scaling"' in source
    assert "    _enable_windows_dpi_awareness()\n    root = tk.Tk()" in source


def test_readme_explains_the_bridge_in_child_simple_steps() -> None:
    readme = (PACKAGE_DIRECTORY / "README.txt").read_text(encoding="utf-8")
    assert "chiếc cầu" in readme
    assert "Classic Outlook" in readme
    assert "Không cần Microsoft Graph" in readme
    assert "Hai ô dùng hai mã riêng" in readme
    assert "Không tự gửi mail" in readme
    assert "30 ngày" in readme and "20 mail" in readme


def test_build_minimized_eml_is_valid_bounded_and_omits_attachments() -> None:
    snapshot = _snapshot("one", datetime(2026, 8, 15, 2, 0, tzinfo=UTC))
    result = build_minimized_eml(snapshot)
    parsed = BytesParser(policy=policy.default).parsebytes(result)

    assert len(result) <= 2 * 1024 * 1024
    assert parsed["Message-ID"] == "<synthetic-one@example.invalid>"
    assert parsed["X-Asset-Hub-Attachments"] == "omitted-for-minimum-data"
    assert not any(part.get_content_disposition() == "attachment" for part in parsed.walk())
    assert "Thiết bị mẫu" in parsed.get_body(preferencelist=("html",)).get_content()


def test_subject_eml_filename_is_readable_bounded_and_windows_safe() -> None:
    assert subject_eml_filename(
        " FW: Thất lạc / Thiết bị thử? ",
        "outlook-tran-fallback.eml",
    ) == "FW- Thất lạc - Thiết bị thử.eml"
    assert subject_eml_filename("", "outlook-tran-fallback.eml") == (
        "outlook-tran-fallback.eml"
    )
    assert len(subject_eml_filename("A" * 200, "fallback.eml")) == 84


def test_bridge_upload_uses_subject_filename_with_technical_fallback() -> None:
    source = (PACKAGE_DIRECTORY / "app.py").read_text(encoding="utf-8")
    assert "filename = subject_eml_filename(snapshot.subject, fallback)" in source


def test_build_minimized_eml_is_deterministic_across_reconnect_and_item_move() -> None:
    received = datetime(2026, 8, 15, 2, 0, tzinfo=UTC)
    original = _snapshot("stable", received)
    moved = MailSnapshot(
        entry_id="different-entry-after-move",
        store_id="different-store-proxy",
        received_at=original.received_at,
        subject=original.subject,
        sender_name=original.sender_name,
        sender_address=original.sender_address,
        to=original.to,
        cc=original.cc,
        sent_at=original.sent_at,
        transport_headers=original.transport_headers,
        html_body=original.html_body,
        plain_body=original.plain_body,
    )

    first = build_minimized_eml(original)
    assert first == build_minimized_eml(original)
    assert first == build_minimized_eml(moved)


def test_oversized_html_falls_back_to_valid_plain_message() -> None:
    snapshot = _snapshot(
        "large",
        datetime(2026, 8, 15, 2, 0, tzinfo=UTC),
        html_body="<p>" + ("x" * 100_000) + "</p>",
        plain_body="x" * 100_000,
    )
    result = build_minimized_eml(snapshot, max_bytes=32_000)
    parsed = BytesParser(policy=policy.default).parsebytes(result)
    assert len(result) <= 32_000
    assert parsed.get_body(preferencelist=("html",)) is None
    assert parsed.get_body(preferencelist=("plain",)) is not None


def test_scan_is_exact_session_bounded_and_checkpointed() -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    snapshots = [_snapshot(str(day), now - timedelta(days=day)) for day in range(35)]
    checkpoint = ScanCheckpoint()
    first = select_scan_candidates(snapshots, checkpoint, now=now, limit=20)

    assert len(first) == 20
    assert all(now - item.received_at <= timedelta(days=30) for item in first)
    assert [item.entry_id for item in first] == [
        f"entry-{day}" for day in range(30, 10, -1)
    ]
    for item in first:
        checkpoint.record(item)

    fresh = _snapshot("fresh", now + timedelta(minutes=1))
    second = select_scan_candidates([*snapshots, fresh], checkpoint, now=now, limit=20)
    assert [item.entry_id for item in second] == [
        *[f"entry-{day}" for day in range(10, -1, -1)],
        "entry-fresh",
    ]
    for item in second:
        checkpoint.record(item)
    assert select_scan_candidates([*snapshots, fresh], checkpoint, now=now, limit=20) == []

    burst = [
        _snapshot(f"burst-{minute}", now + timedelta(minutes=minute))
        for minute in range(2, 27)
    ]
    first_burst = select_scan_candidates(burst, checkpoint, now=now, limit=20)
    assert [item.entry_id for item in first_burst] == [
        f"entry-burst-{minute}" for minute in range(2, 22)
    ]
    for item in first_burst:
        checkpoint.record(item)
    remaining_burst = select_scan_candidates(burst, checkpoint, now=now, limit=20)
    assert [item.entry_id for item in remaining_burst] == [
        f"entry-burst-{minute}" for minute in range(22, 27)
    ]


def test_first_scan_backlog_over_twenty_drains_without_skipping() -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(f"backlog-{minute}", now - timedelta(minutes=minute))
        for minute in range(30, 0, -1)
    ]
    checkpoint = ScanCheckpoint()

    first = select_scan_candidates(snapshots, checkpoint, now=now, limit=20)
    assert len(first) == 20
    for item in first:
        checkpoint.record(item)

    second = select_scan_candidates(snapshots, checkpoint, now=now, limit=20)
    assert len(second) == 10
    assert not ({item.entry_id for item in first} & {item.entry_id for item in second})
    assert {item.entry_id for item in [*first, *second]} == {
        item.entry_id for item in snapshots
    }


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.is_redirect = False

    def json(self) -> dict[str, Any]:
        return self.payload


class _HttpSession:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def test_http_contract_binds_role_keeps_token_in_header_and_acks_explicitly() -> None:
    package_id = "package_12345678"
    workbook = base64.b64encode(_workbook_bytes()).decode("ascii")
    fake = _HttpSession(
        [
            _Response(
                {
                    "token": "opaque-session-token",
                    "role": "tran",
                    "client_type": "local_bridge",
                    "expires_in_seconds": 3600,
                }
            ),
            _Response({"source_eml": {"handle": "source_handle_123", "filename": "mail.eml"}}),
            _Response(
                {
                    "ok": True,
                    "packages": [
                        {
                            "id": package_id,
                            "source_eml_handle": "source_handle_123",
                            "subject": "Synthetic draft",
                        }
                    ],
                }
            ),
            _Response(
                {
                    "ok": True,
                    "package": {
                        "id": package_id,
                        "source_eml_handle": "source_handle_123",
                        "subject": "Synthetic draft",
                        "body_html": "<p>Dear synthetic user</p>",
                        "workbook": {
                            "filename": "synthetic.xlsx",
                            "content_base64": workbook,
                        },
                    },
                }
            ),
            _Response({"ok": True}),
        ]
    )
    client = HubClient("https://hub.example.invalid", "tran")
    client._session = fake  # type: ignore[assignment]

    session = client.pair("ABCD-1234")
    handle = client.upload_eml("synthetic.eml", b"From: a@example.invalid\r\n\r\nHello")
    listed = client.list_draft_packages()
    detail = client.get_draft_package(package_id)
    client.acknowledge_draft(package_id)

    assert session.role == "tran"
    assert handle == "source_handle_123"
    assert listed[0].artifact_handle == "source_handle_123"
    assert detail.workbook_content_base64 == workbook
    pair_call, upload_call, _, _, ack_call = fake.calls
    assert pair_call[2]["json"] == {"code": "ABCD-1234"}
    assert "Authorization" not in pair_call[2]["headers"]
    assert set(upload_call[2]["files"]) == {"file"}
    assert upload_call[2]["headers"]["Authorization"] == "Bearer opaque-session-token"
    assert upload_call[2]["headers"]["X-Asset-Hub-Upload"] == "companion-email-v1"
    assert "role" not in upload_call[2]
    assert ack_call[2]["json"] == {}
    assert ack_call[2]["headers"]["X-Asset-Hub-Action"] == "companion-ack-v1"
    assert "opaque-session-token" not in upload_call[1]


def test_pairing_code_for_the_wrong_role_is_rejected() -> None:
    client = HubClient("https://hub.example.invalid", "ngan")
    client._session = _HttpSession(  # type: ignore[assignment]
        [
            _Response(
                {
                    "token": "opaque",
                    "role": "tran",
                    "client_type": "local_bridge",
                    "expires_in_seconds": 300,
                }
            )
        ]
    )
    with pytest.raises(BridgeError, match="không đúng ô"):
        client.pair("ABCD1234")
    assert not client.paired


def test_draft_package_matches_only_the_exact_role_bound_source() -> None:
    package = DraftPackage("package_12345678", "source-handle", "Synthetic draft")
    tran_source = SourceReference("tran", "entry", "store")
    assert match_package_source(package, {"source-handle": tran_source}, "tran") is tran_source
    with pytest.raises(BridgeError, match="Không tìm thấy mail gốc"):
        match_package_source(package, {"source-handle": tran_source}, "ngan")
    with pytest.raises(BridgeError, match="Không tìm thấy mail gốc"):
        match_package_source(package, {}, "tran")
    with pytest.raises(BridgeError, match="Không tìm thấy mail gốc"):
        match_package_source(
            package, {"source-handle": tran_source}, "tran", {"source-handle"}
        )


def test_workbook_decode_and_reply_html_are_bounded_and_sanitized() -> None:
    payload = _workbook_bytes()
    filename, decoded = decode_workbook(
        "../Synthetic Compensation.xlsx", base64.b64encode(payload).decode("ascii")
    )
    assert filename == "Synthetic Compensation.xlsx"
    assert decoded == payload

    safe = sanitize_reply_html(
        '<script>bad()</script><p onclick="bad()">Hello</p>'
        '<img src="https://tracker.invalid/pixel"><a href="javascript:bad()">click</a>'
    )
    assert "script" not in safe
    assert "onclick" not in safe
    assert "tracker" not in safe
    assert "javascript" not in safe
    assert "<p>Hello</p>" in safe


def test_outlook_adapter_opens_exact_reply_then_saves_and_displays() -> None:
    # Importing here keeps pure core tests independent from Tk availability.
    from asset_compensation.integrations.local_bridge.app import OutlookAdapter

    calls: list[str] = []

    class _Attachments:
        def Add(self, path: str) -> None:  # noqa: N802 - Outlook API spelling
            assert Path(path).is_file()
            calls.append("attachment")

    class _Reply:
        HTMLBody = "<p>Original conversation</p>"
        Attachments = _Attachments()

        def Save(self) -> None:  # noqa: N802 - Outlook API spelling
            calls.append("save")

        def Display(self) -> None:  # noqa: N802 - Outlook API spelling
            calls.append("display")

    class _Original:
        def ReplyAll(self) -> _Reply:  # noqa: N802 - Outlook API spelling
            calls.append("reply_all")
            return _Reply()

    class _Namespace:
        def GetItemFromID(self, entry_id: str, store_id: str) -> _Original:  # noqa: N802
            assert (entry_id, store_id) == ("exact-entry", "exact-store")
            return _Original()

    adapter = object.__new__(OutlookAdapter)
    adapter._namespace = _Namespace()
    adapter.open_reply(
        SourceReference("tran", "exact-entry", "exact-store"),
        body_html="<p>Approved body</p>",
        workbook_filename="synthetic.xlsx",
        workbook_bytes=_workbook_bytes(),
    )
    assert calls == ["reply_all", "attachment", "save", "display"]


def test_draft_refresh_skips_ingest_only_ngan_session() -> None:
    from asset_compensation.integrations.local_bridge.app import BridgeGui

    calls: list[str] = []

    class _Client:
        paired = True

        def __init__(self, role: str) -> None:
            self.role = role

        def list_draft_packages(self) -> list[object]:
            calls.append(self.role)
            return []

    class _Tree:
        def get_children(self) -> tuple[()]:
            return ()

        def delete(self, item: object) -> None:
            raise AssertionError(item)

    gui = object.__new__(BridgeGui)
    gui.states = {
        role: SimpleNamespace(
            client=_Client(role),
            packages={},
            sources={},
            ambiguous_handles=set(),
            opened_package_ids=set(),
        )
        for role in ("ngan", "tran")
    }
    gui.package_tree = _Tree()
    gui._write_log = lambda message: None
    gui._run_async = lambda label, work, done: done(work())

    gui._refresh_packages()

    assert calls == ["tran"]


def test_opened_draft_is_suppressed_even_when_acknowledgement_fails(monkeypatch) -> None:
    from asset_compensation.integrations.local_bridge import app as bridge_app

    package = DraftPackage(
        "package_12345678",
        "source-handle",
        "Synthetic draft",
        "<p>Approved</p>",
    )

    class _Client:
        def get_draft_package(self, package_id: str) -> DraftPackage:
            assert package_id == package.package_id
            return package

        def acknowledge_draft(self, package_id: str) -> None:
            assert package_id == package.package_id
            raise BridgeError("Synthetic acknowledgement failure")

    class _Adapter:
        def open_reply(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            return None

    class _Tree:
        deleted: list[str] = []

        def selection(self) -> tuple[str]:
            return (f"tran:{package.package_id}",)

        def exists(self, item: str) -> bool:
            return item not in self.deleted

        def delete(self, item: str) -> None:
            self.deleted.append(item)

    state = bridge_app.RoleState("tran")
    state.client = _Client()  # type: ignore[assignment]
    state.sources[package.artifact_handle] = SourceReference(
        "tran", "exact-entry", "exact-store"
    )
    state.packages[package.package_id] = package
    logs: list[str] = []
    gui = object.__new__(bridge_app.BridgeGui)
    gui.states = {"tran": state}
    gui.package_tree = _Tree()
    gui._write_log = logs.append
    gui._run_async = lambda label, work, done: done(work())
    monkeypatch.setattr(bridge_app, "OutlookAdapter", _Adapter)

    gui._open_selected_package()

    assert package.package_id in state.opened_package_ids
    assert package.package_id not in state.packages
    assert gui.package_tree.deleted == [f"tran:{package.package_id}"]
    assert any("Không mở lại" in line for line in logs)
    assert logs[-1] == "Đã mở 1 draft đã chọn; không có thao tác tự gửi."


def test_multiple_selected_drafts_open_independently(monkeypatch) -> None:
    from asset_compensation.integrations.local_bridge import app as bridge_app

    packages = [
        DraftPackage(f"package_{index:08d}", f"handle-{index}", f"Draft {index}", "<p>OK</p>")
        for index in (1, 2)
    ]

    class _Client:
        def get_draft_package(self, package_id: str) -> DraftPackage:
            return next(package for package in packages if package.package_id == package_id)

        def acknowledge_draft(self, package_id: str) -> None:
            assert package_id in {package.package_id for package in packages}

    opened: list[str] = []

    class _Adapter:
        def open_reply(self, source: SourceReference, **kwargs: object) -> None:
            del kwargs
            opened.append(source.entry_id)

        def close(self) -> None:
            return None

    class _Tree:
        deleted: list[str] = []

        def selection(self) -> tuple[str, ...]:
            return tuple(f"tran:{package.package_id}" for package in packages)

        def exists(self, item: str) -> bool:
            return item not in self.deleted

        def delete(self, item: str) -> None:
            self.deleted.append(item)

    state = bridge_app.RoleState("tran")
    state.client = _Client()  # type: ignore[assignment]
    for index, package in enumerate(packages, start=1):
        state.sources[package.artifact_handle] = SourceReference(
            "tran", f"entry-{index}", f"store-{index}"
        )
        state.packages[package.package_id] = package
    logs: list[str] = []
    gui = object.__new__(bridge_app.BridgeGui)
    gui.states = {"tran": state}
    gui.package_tree = _Tree()
    gui._write_log = logs.append
    gui._run_async = lambda label, work, done: done(work())
    monkeypatch.setattr(bridge_app, "OutlookAdapter", _Adapter)

    gui._open_selected_package()

    assert opened == ["entry-1", "entry-2"]
    assert state.packages == {}
    assert set(state.opened_package_ids) == {package.package_id for package in packages}
    assert logs[-1] == "Đã mở 2 draft đã chọn; không có thao tác tự gửi."


def test_outlook_source_contains_no_automatic_mail_transmission_member() -> None:
    source = (PACKAGE_DIRECTORY / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    member_names = {node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "send" not in member_names
    assert {"replyall", "save", "display"}.issubset(member_names)
