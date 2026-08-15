"""Tiny Vietnamese GUI for the downloadable Classic Outlook Local Bridge."""

from __future__ import annotations

import os
import tempfile
import threading
import tkinter as tk
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:  # Works both from the downloaded folder and as an importable package.
    from .core import (
        BridgeError,
        DraftPackage,
        HubClient,
        MailSnapshot,
        Role,
        ScanCheckpoint,
        SourceReference,
        build_minimized_eml,
        decode_workbook,
        match_package_source,
        normalize_origin,
        sanitize_reply_html,
        select_scan_candidates,
    )
except ImportError:  # pragma: no cover - exercised by the downloaded script
    from core import (  # type: ignore[no-redef]
        BridgeError,
        DraftPackage,
        HubClient,
        MailSnapshot,
        Role,
        ScanCheckpoint,
        SourceReference,
        build_minimized_eml,
        decode_workbook,
        match_package_source,
        normalize_origin,
        sanitize_reply_html,
        select_scan_candidates,
    )

DEFAULT_SERVER_URL = "__ASSET_HUB_ORIGIN__"
TIMER_MS = 5 * 60 * 1_000
MAX_BULK_DRAFTS = 20
MAX_FOLDER_ITEMS_INSPECTED = 500
OL_MAIL_ITEM = 43
PR_TRANSPORT_HEADERS_UNICODE = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_TRANSPORT_HEADERS_ANSI = "http://schemas.microsoft.com/mapi/proptag/0x007D001E"
_BASE_WINDOWS_DPI = 96.0
_POINTS_PER_INCH = 72.0


def _enable_windows_dpi_awareness() -> None:
    """Stop Windows from bitmap-scaling the Tk window on high-DPI displays."""

    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            set_context = user32.SetProcessDpiAwarenessContext
            set_context.argtypes = (ctypes.c_void_p,)
            set_context.restype = ctypes.c_bool
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is the pseudo-handle -4.
            if set_context(ctypes.c_void_p(-4)):
                return
        except (AttributeError, OSError, ValueError):
            pass

        try:
            set_awareness = ctypes.windll.shcore.SetProcessDpiAwareness
            set_awareness.argtypes = (ctypes.c_int,)
            set_awareness.restype = ctypes.c_long
            if set_awareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
                return
        except (AttributeError, OSError, ValueError):
            pass

        with suppress(AttributeError, OSError, ValueError):
            user32.SetProcessDPIAware()
    except (AttributeError, ImportError, OSError):
        # A clear fallback window is preferable, but DPI setup must never stop
        # operators from reaching Outlook on an older Windows build.
        return


def _configure_tk_dpi(root: tk.Tk) -> float:
    """Configure point rendering and return the initial monitor scale factor."""

    dpi = float(root.winfo_fpixels("1i"))
    if os.name == "nt":
        try:
            import ctypes

            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes = (ctypes.c_void_p,)
            get_dpi.restype = ctypes.c_uint
            window_dpi = int(get_dpi(ctypes.c_void_p(root.winfo_id())))
            if window_dpi > 0:
                dpi = float(window_dpi)
        except (AttributeError, ImportError, OSError, ValueError):
            pass
    dpi = min(max(dpi, _BASE_WINDOWS_DPI), _BASE_WINDOWS_DPI * 3)
    root.tk.call("tk", "scaling", dpi / _POINTS_PER_INCH)
    return dpi / _BASE_WINDOWS_DPI


@dataclass(frozen=True, slots=True)
class FolderReference:
    entry_id: str
    store_id: str
    display_name: str


@dataclass(slots=True)
class RoleState:
    role: Role
    client: HubClient | None = None
    folder: FolderReference | None = None
    checkpoint: ScanCheckpoint = field(default_factory=ScanCheckpoint)
    sources: dict[str, SourceReference] = field(default_factory=dict)
    ambiguous_handles: set[str] = field(default_factory=set)
    packages: dict[str, DraftPackage] = field(default_factory=dict)
    opened_package_ids: set[str] = field(default_factory=set)


class OutlookAdapter:
    """Narrow COM adapter.  Every method is interactive and session-local."""

    def __init__(self) -> None:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:  # pragma: no cover - Windows install concern
            raise BridgeError("Thiếu pywin32. Hãy chạy lại install.ps1.") from exc
        self._pythoncom = pythoncom
        self._pythoncom.CoInitialize()
        try:
            self._application = win32com.client.Dispatch("Outlook.Application")
            self._namespace = self._application.GetNamespace("MAPI")
        except Exception as exc:
            self._pythoncom.CoUninitialize()
            raise BridgeError(
                "Không mở được Classic Outlook. Hãy mở Outlook bản classic rồi thử lại."
            ) from exc

    def close(self) -> None:
        self._namespace = None
        self._application = None
        self._pythoncom.CoUninitialize()

    def pick_folder(self) -> FolderReference | None:
        folder = self._namespace.PickFolder()
        if folder is None:
            return None
        try:
            store_id = str(folder.StoreID)
        except Exception:
            store_id = str(folder.Store.StoreID)
        display = str(getattr(folder, "FolderPath", "") or getattr(folder, "Name", "Thư mục"))
        return FolderReference(str(folder.EntryID), store_id, display)

    def read_candidates(
        self, folder_ref: FolderReference, checkpoint: ScanCheckpoint
    ) -> list[MailSnapshot]:
        try:
            folder = self._namespace.GetFolderFromID(folder_ref.entry_id, folder_ref.store_id)
            items = folder.Items
            items.Sort("[ReceivedTime]", True)
        except Exception as exc:
            raise BridgeError("Không đọc được thư mục đã chọn. Hãy chọn lại thư mục.") from exc

        metadata: list[MailSnapshot] = []
        item_by_id: dict[str, object] = {}
        try:
            count = min(int(items.Count), MAX_FOLDER_ITEMS_INSPECTED)
        except Exception:
            count = 0
        for index in range(1, count + 1):
            try:
                item = items.Item(index)
                if int(getattr(item, "Class", 0)) != OL_MAIL_ITEM:
                    continue
                snapshot = self._snapshot(item, include_body=False)
            except Exception:
                continue
            metadata.append(snapshot)
            item_by_id[snapshot.entry_id] = item

        selected = select_scan_candidates(metadata, checkpoint)
        result: list[MailSnapshot] = []
        for metadata_item in selected:
            item = item_by_id.get(metadata_item.entry_id)
            if item is None:
                continue
            try:
                result.append(self._snapshot(item, include_body=True))
            except Exception:
                continue
        return result

    def _snapshot(self, item: object, *, include_body: bool) -> MailSnapshot:
        received_at = _outlook_datetime(item.ReceivedTime)
        sent_raw = getattr(item, "SentOn", None)
        sent_at = _outlook_datetime(sent_raw) if sent_raw else None
        entry_id = str(item.EntryID)
        store_id = str(item.Parent.StoreID)
        headers = ""
        if include_body:
            accessor = item.PropertyAccessor
            for property_name in (PR_TRANSPORT_HEADERS_UNICODE, PR_TRANSPORT_HEADERS_ANSI):
                try:
                    headers = str(accessor.GetProperty(property_name) or "")
                    if headers:
                        break
                except Exception:
                    continue
        return MailSnapshot(
            entry_id=entry_id,
            store_id=store_id,
            received_at=received_at,
            subject=str(getattr(item, "Subject", "") or ""),
            sender_name=str(getattr(item, "SenderName", "") or "") if include_body else "",
            sender_address=(
                str(getattr(item, "SenderEmailAddress", "") or "") if include_body else ""
            ),
            to=str(getattr(item, "To", "") or "") if include_body else "",
            cc=str(getattr(item, "CC", "") or "") if include_body else "",
            sent_at=sent_at,
            transport_headers=headers,
            html_body=str(getattr(item, "HTMLBody", "") or "") if include_body else "",
            plain_body=str(getattr(item, "Body", "") or "") if include_body else "",
        )

    def open_reply(
        self,
        source: SourceReference,
        *,
        body_html: str,
        workbook_filename: str,
        workbook_bytes: bytes,
    ) -> None:
        try:
            original = self._namespace.GetItemFromID(source.entry_id, source.store_id)
            reply = original.ReplyAll()
            previous = str(getattr(reply, "HTMLBody", "") or "")
            reply.HTMLBody = (
                f'<div data-asset-hub="approved-draft">{body_html}</div>'
                '<div style="margin:18px 0;border-top:1px solid #ccc"></div>'
                f"{previous}"
            )
            with tempfile.TemporaryDirectory(prefix="asset-hub-draft-") as temp_dir:
                if workbook_bytes:
                    workbook_path = Path(temp_dir, workbook_filename)
                    workbook_path.write_bytes(workbook_bytes)
                    reply.Attachments.Add(str(workbook_path))
                reply.Save()
                reply.Display()
        except Exception as exc:
            raise BridgeError(
                "Không mở được cửa sổ trả lời. Mail gốc có thể đã bị chuyển/xóa khỏi Outlook."
            ) from exc


def _outlook_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Outlook datetime is missing")
    if value.tzinfo is None:
        return value.astimezone().astimezone(UTC)
    return value.astimezone(UTC)


class BridgeGui:
    def __init__(self, root: tk.Tk, *, ui_scale: float = 1.0) -> None:
        self.root = root
        self.ui_scale = min(max(float(ui_scale), 1.0), 3.0)
        self.root.title("Cầu nối Outlook - Asset Compensation Hub")
        self.root.geometry(f"{self._px(940)}x{self._px(760)}")
        self.root.minsize(self._px(820), self._px(650))
        self.states = {role: RoleState(role) for role in ("ngan", "tran")}
        self._busy = False
        self._active_origin = ""
        self.server_var = tk.StringVar(value=DEFAULT_SERVER_URL)
        self.auto_var = tk.BooleanVar(value=False)
        self.code_vars = {role: tk.StringVar() for role in self.states}
        self.pair_vars = {role: tk.StringVar(value="Chưa kết nối") for role in self.states}
        self.folder_vars = {role: tk.StringVar(value="Chưa chọn thư mục") for role in self.states}
        self._buttons: list[ttk.Button] = []
        self._build()
        self.root.after(TIMER_MS, self._timer_tick)

    def _px(self, value: int) -> int:
        return max(1, round(value * self.ui_scale))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=self._px(16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Cầu nối Outlook", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Hãy tưởng tượng đây là một chiếc cầu: Outlook đưa mail qua cầu vào Product. "
                "Product làm draft rồi chiếc cầu mở đúng cửa sổ Trả lời tất cả."
            ),
            wraplength=self._px(880),
        ).pack(anchor="w", pady=(self._px(4), self._px(12)))

        server_row = ttk.Frame(outer)
        server_row.pack(fill="x", pady=(0, self._px(10)))
        ttk.Label(server_row, text="1. Địa chỉ Product:").pack(side="left")
        ttk.Entry(server_row, textvariable=self.server_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(self._px(8), 0)
        )

        roles = ttk.Frame(outer)
        roles.pack(fill="x")
        roles.columnconfigure(0, weight=1)
        roles.columnconfigure(1, weight=1)
        self._build_role(roles, "ngan", "NganTLT", 0)
        self._build_role(roles, "tran", "TranNNB", 1)

        timer_row = ttk.Frame(outer)
        timer_row.pack(fill="x", pady=(self._px(12), self._px(8)))
        ttk.Checkbutton(
            timer_row,
            text="Tự kiểm tra mail mới mỗi 5 phút (mặc định TẮT)",
            variable=self.auto_var,
            command=self._auto_changed,
        ).pack(side="left")
        ttk.Label(timer_row, text="Không đọc, không chuyển, không xóa mail.").pack(side="right")

        draft = ttk.LabelFrame(outer, text="Draft đang chờ", padding=self._px(10))
        draft.pack(fill="both", expand=True, pady=(self._px(4), self._px(8)))
        self.package_tree = ttk.Treeview(
            draft,
            columns=("role", "subject", "match"),
            show="headings",
            height=7,
            selectmode="extended",
        )
        self.package_tree.heading("role", text="Phần")
        self.package_tree.heading("subject", text="Draft")
        self.package_tree.heading("match", text="Mail gốc")
        self.package_tree.column("role", width=self._px(90), stretch=False)
        self.package_tree.column("subject", width=self._px(560))
        self.package_tree.column("match", width=self._px(120), stretch=False)
        self.package_tree.pack(fill="both", expand=True)
        action_row = ttk.Frame(draft)
        action_row.pack(fill="x", pady=(self._px(8), 0))
        check_button = ttk.Button(
            action_row, text="Kiểm tra draft mới", command=self._refresh_packages
        )
        check_button.pack(side="left")
        select_button = ttk.Button(
            action_row, text="Chọn tất cả", command=self._select_all_packages
        )
        select_button.pack(side="left", padx=(self._px(8), 0))
        open_button = ttk.Button(
            action_row,
            text="Mở các draft đã chọn",
            command=self._open_selected_package,
        )
        open_button.pack(side="left", padx=(self._px(8), 0))
        ttk.Label(
            action_row,
            text="Ctrl/Shift để chọn nhiều · tối đa 20/lần",
        ).pack(side="left", padx=(self._px(10), 0))
        self._buttons.extend((check_button, select_button, open_button))

        ttk.Label(outer, text="Nhật ký dễ đọc:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.log = ScrolledText(outer, height=7, state="disabled", wrap="word")
        self.log.pack(fill="x")
        self._write_log("Sẵn sàng. Bắt đầu ở ô mã Ngan hoặc Tran.")

    def _build_role(self, parent: ttk.Frame, role: Role, title: str, column: int) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=self._px(12))
        frame.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0, self._px(6)) if column == 0 else (self._px(6), 0),
        )
        ttk.Label(
            frame,
            text="2. Lấy mã ở Product, rồi dán vào đây:",
            wraplength=self._px(390),
        ).pack(anchor="w")
        pair_row = ttk.Frame(frame)
        pair_row.pack(fill="x", pady=(self._px(5), self._px(3)))
        ttk.Entry(pair_row, textvariable=self.code_vars[role], show="•").pack(
            side="left", fill="x", expand=True
        )
        pair_button = ttk.Button(pair_row, text="Kết nối", command=lambda: self._pair(role))
        pair_button.pack(side="left", padx=(self._px(7), 0))
        self._buttons.append(pair_button)
        ttk.Label(frame, textvariable=self.pair_vars[role]).pack(anchor="w")
        ttk.Separator(frame).pack(fill="x", pady=self._px(9))
        ttk.Label(frame, text="3. Chọn đúng một thư mục Outlook:").pack(anchor="w")
        ttk.Label(
            frame,
            textvariable=self.folder_vars[role],
            wraplength=self._px(390),
        ).pack(
            anchor="w", pady=self._px(3)
        )
        folder_button = ttk.Button(
            frame, text="Chọn thư mục", command=lambda: self._choose_folder(role)
        )
        folder_button.pack(anchor="w")
        scan_button = ttk.Button(
            frame, text="4. Nạp mail mới", command=lambda: self._scan_one(role)
        )
        scan_button.pack(anchor="w", pady=(self._px(8), 0))
        self._buttons.extend((folder_button, scan_button))

    def _pair(self, role: Role) -> None:
        # Read Tk variables on the UI thread.  The immutable strings can then
        # safely be used by the network worker.
        raw_origin = self.server_var.get()
        code = self.code_vars[role].get()

        def work() -> tuple[HubClient, int]:
            origin = normalize_origin(raw_origin)
            client = HubClient(origin, role)
            session = client.pair(code)
            return client, session.expires_in_seconds

        def done(result: object) -> None:
            client, expires = result  # type: ignore[misc]
            origin = client.origin
            if self._active_origin and self._active_origin != origin:
                self._clear_all_connections()
            self._active_origin = origin
            state = self.states[role]
            if state.client is not None:
                state.client.clear()
            # A bearer session owns its uploaded handles.  A freshly paired
            # session must re-upload instead of reusing an old in-memory cursor.
            state.client = client
            state.sources.clear()
            state.ambiguous_handles.clear()
            state.packages.clear()
            state.opened_package_ids.clear()
            state.checkpoint = ScanCheckpoint()
            self.code_vars[role].set("")
            minutes = expires // 60
            suffix = f" (còn khoảng {minutes} phút)" if minutes else ""
            self.pair_vars[role].set(f"Đã kết nối{suffix}")
            self._write_log(f"{_role_label(role)}: đã nối với Product.")

        self._run_async(f"Kết nối {_role_label(role)}", work, done)

    def _choose_folder(self, role: Role) -> None:
        def work() -> FolderReference | None:
            adapter = OutlookAdapter()
            try:
                return adapter.pick_folder()
            finally:
                adapter.close()

        def done(result: object) -> None:
            folder = result if isinstance(result, FolderReference) else None
            if folder is None:
                self._write_log(f"{_role_label(role)}: bạn chưa chọn thư mục.")
                return
            state = self.states[role]
            state.folder = folder
            state.checkpoint = ScanCheckpoint()
            state.sources.clear()
            state.ambiguous_handles.clear()
            self.folder_vars[role].set(folder.display_name)
            self._write_log(
                f"{_role_label(role)}: đã chọn đúng thư mục “{folder.display_name}”."
            )

        self._run_async(f"Chọn thư mục {_role_label(role)}", work, done)

    def _scan_one(self, role: Role) -> None:
        self._run_async(
            f"Nạp mail {_role_label(role)}",
            lambda: self._scan_role(role),
            lambda result: self._scan_done(role, result),
        )

    def _scan_role(self, role: Role) -> dict[str, int]:
        state = self.states[role]
        if state.client is None or not state.client.paired:
            raise BridgeError(f"{_role_label(role)} chưa kết nối Product.")
        if state.folder is None:
            raise BridgeError(f"{_role_label(role)} chưa chọn thư mục Outlook.")
        adapter = OutlookAdapter()
        try:
            candidates = adapter.read_candidates(state.folder, state.checkpoint)
        finally:
            adapter.close()
        uploaded = 0
        for index, snapshot in enumerate(candidates, start=1):
            eml = build_minimized_eml(snapshot)
            stamp = snapshot.received_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
            filename = f"outlook-{role}-{stamp}-{index}.eml"
            handle = state.client.upload_eml(filename, eml)
            source = SourceReference(
                role=role,
                entry_id=snapshot.entry_id,
                store_id=snapshot.store_id,
                subject=snapshot.subject,
            )
            previous = state.sources.get(handle)
            if previous is not None and (
                previous.entry_id != source.entry_id or previous.store_id != source.store_id
            ):
                # The server handle is content-addressed.  Two Outlook items can
                # therefore share it; never guess which duplicate to reply to.
                state.ambiguous_handles.add(handle)
            else:
                state.sources[handle] = source
            state.checkpoint.record(snapshot)
            uploaded += 1
        return {"found": len(candidates), "uploaded": uploaded}

    def _scan_done(self, role: Role, result: object) -> None:
        counts = result if isinstance(result, dict) else {}
        uploaded = int(counts.get("uploaded", 0))
        if uploaded:
            self._write_log(
                f"{_role_label(role)}: đã đưa {uploaded} mail mới qua cầu. "
                "Mail Outlook vẫn nguyên vẹn."
            )
        else:
            self._write_log(f"{_role_label(role)}: chưa thấy mail mới trong đúng thư mục đã chọn.")

    def _refresh_packages(self) -> None:
        def work() -> list[tuple[Role, DraftPackage, bool]]:
            rows: list[tuple[Role, DraftPackage, bool]] = []
            for state in self.states.values():
                state.packages.clear()
            # Draft packages exist only in the Tran workflow.  Ngan is an
            # ingest-only mailbox and must never make this request.
            for role in ("tran",):
                state = self.states[role]
                if state.client is None or not state.client.paired:
                    continue
                for package in state.client.list_draft_packages():
                    if package.package_id in state.opened_package_ids:
                        continue
                    state.packages[package.package_id] = package
                    rows.append(
                        (
                            role,
                            package,
                            package.artifact_handle in state.sources
                            and package.artifact_handle not in state.ambiguous_handles,
                        )
                    )
            return rows

        def done(result: object) -> None:
            for item in self.package_tree.get_children():
                self.package_tree.delete(item)
            rows = result if isinstance(result, list) else []
            for role, package, matched in rows:
                iid = f"{role}:{package.package_id}"
                self.package_tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        _role_label(role),
                        package.subject,
                        "Đúng mail" if matched else "Không ở phiên này",
                    ),
                )
            self._write_log(f"Đã kiểm tra: có {len(rows)} draft đang chờ.")

        self._run_async("Kiểm tra draft", work, done)

    def _select_all_packages(self) -> None:
        items = tuple(self.package_tree.get_children())[:MAX_BULK_DRAFTS]
        if not items:
            messagebox.showinfo("Chưa có draft", "Hãy bấm Kiểm tra draft mới trước.")
            return
        self.package_tree.selection_set(items)

    def _open_selected_package(self) -> None:
        selected = tuple(self.package_tree.selection())
        if not selected:
            messagebox.showinfo("Chọn draft", "Hãy chọn ít nhất một dòng draft trước.")
            return
        if len(selected) > MAX_BULK_DRAFTS:
            messagebox.showwarning(
                "Chọn quá nhiều",
                f"Mỗi lần chỉ mở tối đa {MAX_BULK_DRAFTS} draft. Hãy chia thành nhiều lượt.",
            )
            return

        requests: list[tuple[str, Role, str]] = []
        for item_id in selected:
            if ":" not in item_id:
                continue
            role_raw, package_id = item_id.split(":", 1)
            if role_raw in self.states:
                requests.append((item_id, role_raw, package_id))  # type: ignore[arg-type]
        if not requests:
            messagebox.showwarning("Draft không hợp lệ", "Không có dòng draft hợp lệ để mở.")
            return

        def work() -> list[tuple[str, Role, str, bool, str]]:
            outcomes: list[tuple[str, Role, str, bool, str]] = []
            for item_id, role, package_id in requests:
                state = self.states[role]
                subject = package_id
                try:
                    if state.client is None:
                        raise BridgeError("Phiên kết nối đã mất. Hãy lấy mã mới.")
                    package = state.client.get_draft_package(package_id)
                    subject = package.subject
                    source = match_package_source(
                        package, state.sources, role, state.ambiguous_handles
                    )
                    body_html = sanitize_reply_html(package.body_html)
                    workbook_name = ""
                    workbook_bytes = b""
                    if package.workbook_content_base64:
                        workbook_name, workbook_bytes = decode_workbook(
                            package.workbook_filename, package.workbook_content_base64
                        )
                    adapter = OutlookAdapter()
                    try:
                        adapter.open_reply(
                            source,
                            body_html=body_html,
                            workbook_filename=workbook_name,
                            workbook_bytes=workbook_bytes,
                        )
                    finally:
                        adapter.close()
                    # Opening the native Reply-All window is irreversible.
                    # Suppress it immediately even if acknowledgement fails.
                    state.opened_package_ids.add(package_id)
                    acknowledged = True
                    try:
                        state.client.acknowledge_draft(package_id)
                    except BridgeError:
                        acknowledged = False
                    outcomes.append((item_id, role, subject, acknowledged, ""))
                except BridgeError as exc:
                    outcomes.append((item_id, role, subject, False, str(exc)))
                except Exception:
                    outcomes.append(
                        (
                            item_id,
                            role,
                            subject,
                            False,
                            "Classic Outlook không mở được draft này.",
                        )
                    )
            return outcomes

        def done(result: object) -> None:
            outcomes = result if isinstance(result, list) else []
            opened = 0
            failed: list[str] = []
            for item_id, role, subject, acknowledged, error in outcomes:
                if error:
                    failed.append(f"{subject}: {error}")
                    self._write_log(f"Không mở được draft “{subject}”: {error}")
                    continue
                opened += 1
                _, package_id = item_id.split(":", 1)
                self.states[role].packages.pop(package_id, None)
                if self.package_tree.exists(item_id):
                    self.package_tree.delete(item_id)
                if acknowledged:
                    self._write_log(
                        f"Đã mở draft “{subject}” trên đúng mail gốc. "
                        "Hãy đọc kỹ rồi tự bấm Gửi."
                    )
                else:
                    self._write_log(
                        f"Draft “{subject}” đã mở an toàn, nhưng Product chưa ghi nhận. "
                        "Không mở lại; hãy dùng cửa sổ Reply-All đang có."
                    )
            if opened:
                self._write_log(f"Đã mở {opened} draft đã chọn; không có thao tác tự gửi.")
            if failed:
                preview = "\n".join(failed[:5])
                suffix = f"\n… và {len(failed) - 5} draft khác." if len(failed) > 5 else ""
                messagebox.showwarning(
                    "Có draft chưa mở được",
                    f"Đã mở {opened}; chưa mở được {len(failed)}.\n\n{preview}{suffix}",
                )

        self._run_async(f"Mở {len(requests)} draft", work, done)

    def _auto_changed(self) -> None:
        if self.auto_var.get():
            self._write_log(
                "Đã BẬT kiểm tra 5 phút. Chương trình chỉ kiểm tra khi cửa sổ này đang mở."
            )
        else:
            self._write_log("Đã TẮT kiểm tra 5 phút.")

    def _timer_tick(self) -> None:
        if self.auto_var.get() and not self._busy:
            roles = [role for role, state in self.states.items() if state.client and state.folder]
            if roles:
                def work() -> dict[Role, dict[str, int]]:
                    return {role: self._scan_role(role) for role in roles}

                def done(result: object) -> None:
                    if isinstance(result, dict):
                        for role, counts in result.items():
                            self._scan_done(role, counts)

                self._run_async("Kiểm tra tự động", work, done)
        self.root.after(TIMER_MS, self._timer_tick)

    def _run_async(
        self,
        label: str,
        work: Callable[[], object],
        done: Callable[[object], None],
    ) -> None:
        if self._busy:
            messagebox.showinfo("Đang làm việc", "Cầu đang chở một việc khác. Chờ một chút nhé.")
            return
        self._busy = True
        self._set_buttons(False)
        self._write_log(f"{label}: đang làm…")

        def target() -> None:
            try:
                result = work()
            except BridgeError as exc:
                message = str(exc)
                self.root.after(0, lambda message=message: self._finish_error(label, message))
            except Exception:
                self.root.after(
                    0,
                    lambda: self._finish_error(
                        label, "Có lỗi bất ngờ. Đóng/mở lại Classic Outlook rồi thử lại."
                    ),
                )
            else:
                self.root.after(0, lambda result=result: self._finish_success(done, result))

        threading.Thread(target=target, name="asset-hub-bridge-job", daemon=True).start()

    def _finish_success(self, done: Callable[[object], None], result: object) -> None:
        self._busy = False
        self._set_buttons(True)
        done(result)

    def _finish_error(self, label: str, message: str) -> None:
        self._busy = False
        self._set_buttons(True)
        self._write_log(f"{label}: {message}")
        messagebox.showerror("Chưa làm được", message)

    def _set_buttons(self, enabled: bool) -> None:
        for button in self._buttons:
            button.configure(state="normal" if enabled else "disabled")

    def _clear_all_connections(self) -> None:
        for role, state in self.states.items():
            if state.client:
                state.client.clear()
            state.client = None
            state.sources.clear()
            state.ambiguous_handles.clear()
            state.packages.clear()
            state.opened_package_ids.clear()
            state.checkpoint = ScanCheckpoint()
            self.pair_vars[role].set("Chưa kết nối")
        self._active_origin = ""

    def _write_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def _role_label(role: Role) -> str:
    return "NganTLT" if role == "ngan" else "TranNNB"


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Local Bridge chỉ chạy trên Windows với Classic Outlook.")
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    ui_scale = _configure_tk_dpi(root)
    with suppress(tk.TclError):
        ttk.Style().theme_use("vista")
    BridgeGui(root, ui_scale=ui_scale)
    root.mainloop()


if __name__ == "__main__":
    main()
