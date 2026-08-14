from __future__ import annotations

import io
import re
import socket
from base64 import b64decode
from email.message import EmailMessage
from pathlib import Path

import pytest

from asset_compensation.adapters import pdf
from asset_compensation.adapters.pdf import (
    PdfAdapterError,
    PdfCapabilityError,
    PdfDependencyError,
    PdfNormalizationResult,
    PdfPageOverflowError,
    PypdfMerger,
    PypdfPageNormalizer,
    WeasyPrintPdfConverter,
    WordPdfConverter,
    prepare_eml_bytes,
)
from asset_compensation.services import (
    EmailUpload,
    EmailUploadService,
    MailArtifactDisabledError,
    MailArtifactIntegrityError,
    MailArtifactNotFoundError,
    MailArtifactStore,
    MailPdfService,
    safe_eml_basename,
)


def _synthetic_eml(*, marker: str = "one", html_body: str | None = None) -> bytes:
    message = EmailMessage()
    message["Subject"] = f"Synthetic evidence <{marker}>"
    message["From"] = "Synthetic Sender <sender@example.invalid>"
    message["To"] = "Synthetic Receiver <receiver@example.invalid>"
    message["Date"] = "Thu, 13 Aug 2026 03:49:56 +0000"
    message["Message-ID"] = f"<synthetic-{marker}@example.invalid>"
    message.set_content(f"Synthetic plain evidence {marker}")
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    return message.as_bytes()


def _validated_payload(filename: str, *, marker: str = "one") -> object:
    upload = EmailUpload(
        filename=filename,
        content_type="message/rfc822",
        stream=io.BytesIO(_synthetic_eml(marker=marker)),
    )
    return EmailUploadService().validate([upload])[0]


def test_prepare_eml_sanitizes_active_and_remote_content_but_resolves_safe_cid() -> None:
    message = EmailMessage()
    message["Subject"] = "Synthetic <wide> evidence"
    message["From"] = "Synthetic Sender <sender@example.invalid>"
    message["To"] = "Synthetic Receiver <receiver@example.invalid>"
    message.set_content("Synthetic plain fallback")
    message.add_alternative(
        """
        <html><head>
          <style>.unsafe-marker{background:url(https://tracker.example.invalid/x)}</style>
          <script>active-script-marker()</script>
        </head><body onload="active-event-marker()">
          <a href="javascript:active-link-marker()">Asset Name</a>
          <table width="999"><tr><th>NOTE</th><td>safe text</td></tr></table>
          <img src="https://tracker.example.invalid/pixel" alt="remote">
          <img src="cid:safe-image" onerror="active-image-marker()" alt="inline">
          <img src="cid:active-vector" alt="vector">
          <img src="cid:missing-image" alt="missing">
        </body></html>
        """,
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        b"synthetic-png-bytes",
        maintype="image",
        subtype="png",
        cid="<safe-image>",
    )
    html_part.add_related(
        b"<svg><script>active-vector-marker()</script></svg>",
        maintype="image",
        subtype="svg+xml",
        cid="<active-vector>",
    )

    prepared = prepare_eml_bytes(message.as_bytes())

    assert prepared.landscape is True
    assert "data:image/png;base64," in prepared.html
    assert "safe text" in prepared.html
    assert "Synthetic &lt;wide&gt; evidence" in prepared.html
    assert "active-script-marker" not in prepared.html
    assert "active-event-marker" not in prepared.html
    assert "active-link-marker" not in prepared.html
    assert "active-image-marker" not in prepared.html
    assert "active-vector-marker" not in prepared.html
    assert "data:image/svg" not in prepared.html
    assert "tracker.example.invalid" not in prepared.html
    assert 'alt="remote"' not in prepared.html
    assert 'alt="missing"' not in prepared.html
    assert "cid:missing-image" not in prepared.html
    assert prepared.warnings == (
        "an inline image was omitted because its type is unsafe",
        "1 inline image reference(s) were unavailable",
    )


def test_prepare_eml_escapes_plain_text() -> None:
    prepared = prepare_eml_bytes(
        _synthetic_eml(marker="plain<script>unsafe()</script>")
    )

    assert "unsafe()" in prepared.html
    assert "<script>unsafe()</script>" not in prepared.html
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in prepared.html
    assert prepared.landscape is False


def test_word_capability_error_is_clear_on_a_non_windows_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf.os, "name", "posix")

    with pytest.raises(PdfCapabilityError, match="cloud/server host"):
        pdf._start_word_application()


def test_word_converter_is_hidden_applies_landscape_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTable:
        fit_mode: int | None = None

        def AutoFitBehavior(self, mode: int) -> None:
            self.fit_mode = mode

    class FakePageSetup:
        Orientation: int | None = None
        LeftMargin: int | None = None
        RightMargin: int | None = None
        TopMargin: int | None = None
        BottomMargin: int | None = None

    class FakeDocument:
        def __init__(self) -> None:
            self.PageSetup = FakePageSetup()
            self.Tables = [FakeTable()]
            self.closed = False

        def ExportAsFixedFormat(self, *, OutputFileName: str, ExportFormat: int) -> None:
            assert ExportFormat == 17
            Path(OutputFileName).write_bytes(b"%PDF-synthetic")

        def Close(self, save: bool) -> None:
            assert save is False
            self.closed = True

    document = FakeDocument()

    class FakeDocuments:
        def Open(self, html_path: str, **options: object) -> FakeDocument:
            assert "Asset Name" in Path(html_path).read_text("utf-8")
            assert options == {
                "ConfirmConversions": False,
                "ReadOnly": True,
                "AddToRecentFiles": False,
            }
            return document

    class FakeWord:
        Visible = True
        DisplayAlerts = 1
        Documents = FakeDocuments()
        quit_called = False

        def Quit(self) -> None:
            self.quit_called = True

    word = FakeWord()
    monkeypatch.setattr(pdf, "_start_word_application", lambda: word)
    source = tmp_path / "synthetic.eml"
    source.write_bytes(
        _synthetic_eml(
            html_body="<table><tr><th>Asset Name</th><th>NOTE</th></tr></table>"
        )
    )
    destination = tmp_path / "output.pdf"

    WordPdfConverter().convert_eml(source, destination)

    assert destination.read_bytes() == b"%PDF-synthetic"
    assert word.Visible is False
    assert word.DisplayAlerts == 0
    assert word.quit_called is True
    assert document.closed is True
    assert document.PageSetup.Orientation == 1
    assert document.Tables[0].fit_mode == 2


def test_cloud_converter_bounds_source_before_loading_optional_renderer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.eml"
    source.write_bytes(b"x" * 9)
    destination = tmp_path / "must-not-exist.pdf"

    with pytest.raises(PdfAdapterError, match="8-byte cloud rendering limit"):
        WeasyPrintPdfConverter(max_source_bytes=8).convert_eml(source, destination)

    assert not destination.exists()


def test_cloud_converter_capability_probe_matches_lazy_dependency_loader() -> None:
    try:
        pdf._load_weasyprint()
        pdf._load_pypdf()
    except PdfDependencyError:
        expected = False
    else:
        expected = True

    assert WeasyPrintPdfConverter.is_available() is expected


def test_cloud_converter_creates_real_landscape_pdf_without_network_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        _, fetcher_type = pdf._load_weasyprint()
    except PdfCapabilityError as exc:
        pytest.skip(str(exc))

    def forbid_network(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("the PDF renderer attempted a network lookup")

    fetcher = fetcher_type(
        allowed_protocols={"data"},
        allow_redirects=False,
        fail_on_errors=True,
    )
    monkeypatch.setattr(socket, "getaddrinfo", forbid_network)
    with pytest.raises(ValueError, match="disallowed protocol"):
        fetcher.fetch("https://tracker.example.invalid/pixel")  # type: ignore[attr-defined]

    message = EmailMessage()
    message["Subject"] = "Chứng từ đền bù synthetic"
    message["From"] = "Synthetic Sender <sender@example.invalid>"
    message["To"] = "Synthetic Receiver <receiver@example.invalid>"
    message.set_content("Synthetic plain fallback")
    message.add_alternative(
        """
        <html><body>
          <table><tr><th>Asset Name</th><th>NOTE</th></tr>
          <tr><td>Laptop synthetic</td><td>Không có dữ liệu thật</td></tr></table>
          <img src="cid:inline-pixel" alt="inline pixel">
          <img src="https://tracker.example.invalid/remote" alt="remote pixel">
        </body></html>
        """,
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ),
        maintype="image",
        subtype="png",
        cid="<inline-pixel>",
    )
    source = tmp_path / "synthetic-cloud.eml"
    source.write_bytes(message.as_bytes())
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    converter = WeasyPrintPdfConverter(max_pages=10)

    converter.convert_eml(source, first)
    converter.convert_eml(source, second)

    from pypdf import PdfReader

    reader = PdfReader(first)
    assert len(reader.pages) >= 1
    assert float(reader.pages[0].mediabox.width) > float(
        reader.pages[0].mediabox.height
    )
    assert "Chứng từ đền bù synthetic" in "".join(
        page.extract_text() or "" for page in reader.pages
    )
    assert first.read_bytes() == second.read_bytes()

    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        converter.convert_eml(source, first)
    assert first.read_bytes() == original


class _FakePage:
    class _MediaBox:
        width = 600
        height = 800

    mediabox = _MediaBox()


class _FakeReader:
    def __init__(self, stream: object) -> None:
        count = int(stream.read().decode("ascii"))  # type: ignore[attr-defined]
        self.pages = [_FakePage() for _ in range(count)]


class _FakeWriter:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    def add_page(self, page: _FakePage) -> None:
        self.pages.append(page)

    def add_blank_page(self, *, width: float, height: float) -> None:
        assert width > 0
        assert height > 0
        self.pages.append(_FakePage())

    def write(self, stream: object) -> None:
        stream.write(str(len(self.pages)).encode("ascii"))  # type: ignore[attr-defined]


def test_fixed_page_normalization_never_truncates_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "_load_pypdf", lambda: (_FakeReader, _FakeWriter))
    source = tmp_path / "three-pages.pdf"
    source.write_bytes(b"3")
    destination = tmp_path / "normalized.pdf"
    normalizer = PypdfPageNormalizer()

    with pytest.raises(PdfPageOverflowError, match="no pages were discarded"):
        normalizer.normalize(source, destination, target_pages=2)
    assert not destination.exists()

    result = normalizer.normalize(
        source,
        destination,
        target_pages=2,
        overflow_policy="warn",
    )
    assert destination.read_bytes() == b"2"
    assert result.source_pages == 3
    assert result.output_pages == 2
    assert result.truncated_pages == 1
    assert result.padded_pages == 0
    assert result.warnings and "explicitly omitted" in result.warnings[0]


def test_fixed_page_normalization_pads_and_never_clobbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "_load_pypdf", lambda: (_FakeReader, _FakeWriter))
    source = tmp_path / "one-page.pdf"
    source.write_bytes(b"1")
    destination = tmp_path / "normalized.pdf"
    result = PypdfPageNormalizer().normalize(source, destination, target_pages=3)

    assert destination.read_bytes() == b"3"
    assert result.padded_pages == 2
    with pytest.raises(FileExistsError):
        PypdfPageNormalizer().normalize(source, destination, target_pages=3)
    assert destination.read_bytes() == b"3"


def test_pdf_merger_keeps_every_page_and_never_clobbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "_load_pypdf", lambda: (_FakeReader, _FakeWriter))
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    destination = tmp_path / "merged.pdf"
    first.write_bytes(b"2")
    second.write_bytes(b"3")

    PypdfMerger().merge([first, second], destination)

    assert destination.read_bytes() == b"5"
    with pytest.raises(FileExistsError):
        PypdfMerger().merge([first], destination)
    assert destination.read_bytes() == b"5"


def test_normalizer_and_merger_with_real_pypdf(tmp_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with source.open("wb") as stream:
        writer.write(stream)

    normalized = tmp_path / "normalized.pdf"
    result = PypdfPageNormalizer().normalize(source, normalized, target_pages=2)
    merged = tmp_path / "merged.pdf"
    PypdfMerger().merge([normalized, source], merged)

    assert result.padded_pages == 1
    assert len(PdfReader(normalized).pages) == 2
    assert len(PdfReader(merged).pages) == 3


def test_mail_artifact_retention_is_opt_in_content_addressed_and_private(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-mail"
    payload = _validated_payload(r"..\client path\unsafe:name.eml")
    disabled = MailArtifactStore(root, enabled=False)

    with pytest.raises(MailArtifactDisabledError, match="disabled"):
        disabled.retain_validated(payload)  # type: ignore[arg-type]
    assert not root.exists()

    store = MailArtifactStore(root, enabled=True)
    first = store.retain_validated(payload)  # type: ignore[arg-type]
    second = store.retain_validated(payload)  # type: ignore[arg-type]
    stored_path = store.resolve(first.handle)

    assert re.fullmatch(r"eml-sha256-[0-9a-f]{64}", first.handle)
    assert first.safe_filename == "upload-01.eml"
    assert first.created is True
    assert second.created is False
    assert stored_path.name == f"{first.content_sha256}.eml"
    assert stored_path.read_bytes() == payload.data  # type: ignore[attr-defined]
    assert stored_path.is_relative_to(root)
    assert not list(root.rglob("*unsafe*"))


def test_mail_artifact_retain_many_rolls_back_only_new_artifacts_on_second_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private-mail"
    store = MailArtifactStore(root, enabled=True)
    preexisting_payload = _validated_payload(
        "preexisting.eml", marker="preexisting"
    )
    preexisting = store.retain_validated(  # type: ignore[arg-type]
        preexisting_payload
    )
    first_new = _validated_payload("first-new.eml", marker="first-new")
    second_new = _validated_payload("second-new.eml", marker="second-new")
    original_commit = store._commit_content_addressed
    commit_calls = 0

    def fail_on_second_commit(
        temp_path: Path, destination: Path, digest: str
    ) -> bool:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise OSError("synthetic second-retain I/O failure")
        return original_commit(temp_path, destination, digest)

    monkeypatch.setattr(store, "_commit_content_addressed", fail_on_second_commit)

    with pytest.raises(OSError, match="second-retain"):
        store.retain_many([first_new, second_new])  # type: ignore[list-item]

    preexisting_path = store.resolve(preexisting.handle)
    assert preexisting_path.read_bytes() == preexisting_payload.data  # type: ignore[attr-defined]
    assert list(root.rglob("*.eml")) == [preexisting_path]
    assert not list(root.rglob("*.tmp"))
    assert commit_calls == 2


def test_mail_artifact_handle_rejects_traversal_and_detects_tampering(
    tmp_path: Path,
) -> None:
    store = MailArtifactStore(tmp_path / "private-mail", enabled=True)
    retained = store.retain_validated(  # type: ignore[arg-type]
        _validated_payload("synthetic.eml")
    )

    with pytest.raises(MailArtifactNotFoundError, match="Invalid"):
        store.resolve("../../synthetic.eml")
    stored_path = store.resolve(retained.handle)
    stored_path.write_bytes(b"x" * retained.size_bytes)
    with pytest.raises(MailArtifactIntegrityError, match="integrity"):
        store.resolve(retained.handle)


def test_mail_artifact_clear_removes_only_managed_hash_files(tmp_path: Path) -> None:
    root = tmp_path / "private-mail"
    store = MailArtifactStore(root, enabled=True)
    first = store.retain_validated(  # type: ignore[arg-type]
        _validated_payload("first.eml", marker="first")
    )
    store.retain_validated(  # type: ignore[arg-type]
        _validated_payload("second.eml", marker="second")
    )
    unknown_root_file = root / "keep.txt"
    unknown_root_file.write_text("not app-managed", "utf-8")
    first_shard = root / first.content_sha256[:2]
    unknown_shard_file = first_shard / "keep.eml"
    unknown_shard_file.write_text("not hash-named", "utf-8")

    assert store.clear_managed() == 2
    assert unknown_root_file.read_text("utf-8") == "not app-managed"
    assert unknown_shard_file.read_text("utf-8") == "not hash-named"
    assert not list(root.glob("[0-9a-f][0-9a-f]/" + "[0-9a-f]" * 64 + ".eml"))


def test_mail_artifact_clear_never_follows_a_shard_symlink(tmp_path: Path) -> None:
    root = tmp_path / "private-mail"
    store = MailArtifactStore(root, enabled=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_artifact = outside / ("f" * 64 + ".eml")
    outside_artifact.write_bytes(b"outside-marker")
    try:
        (root / "ff").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this Windows host")

    assert store.clear_managed() == 0
    assert outside_artifact.read_bytes() == b"outside-marker"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\private\normal.eml", "normal.eml"),
        ("CON.eml", "message-CON.eml"),
        ("spaces and : marks.eml", "spaces_and_marks.eml"),
    ],
)
def test_safe_eml_basename(raw: str, expected: str) -> None:
    assert safe_eml_basename(raw) == expected


class _FakeConverter:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path:
        del source
        assert overwrite is False
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("synthetic converter failure")
        path = Path(destination)
        path.write_bytes(b"1")
        return path


class _FakeNormalizer:
    def normalize(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        target_pages: int,
        overflow_policy: object = "fail",
        overwrite: bool = False,
    ) -> PdfNormalizationResult:
        del source, overflow_policy
        assert overwrite is False
        path = Path(destination)
        path.write_bytes(str(target_pages).encode("ascii"))
        return PdfNormalizationResult(
            path=path,
            source_pages=1,
            output_pages=target_pages,
            padded_pages=target_pages - 1,
            truncated_pages=0,
            warnings=(),
        )


class _FakeMerger:
    def merge(
        self,
        sources: list[str | Path],
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        assert overwrite is False
        path = Path(destination)
        path.write_bytes(b"|".join(Path(source).read_bytes() for source in sources))
        return path


def test_mail_pdf_service_publishes_individual_and_merged_batch_without_raw_eml(
    tmp_path: Path,
) -> None:
    store = MailArtifactStore(tmp_path / "private-mail", enabled=True)
    artifacts = store.retain_many(  # type: ignore[arg-type]
        [
            _validated_payload("first evidence.eml", marker="first"),
            _validated_payload("second evidence.eml", marker="second"),
        ]
    )
    service = MailPdfService(
        store,
        _FakeConverter(),
        normalizer=_FakeNormalizer(),
        merger=_FakeMerger(),
    )

    result = service.create_batch(
        list(artifacts),
        tmp_path / "outputs",
        batch_name="Synthetic Batch",
        pages_per_mail=2,
    )

    assert result.directory.name == "Synthetic_Batch"
    assert result.merged_path.read_bytes() == b"2|2"
    assert [item.path.name for item in result.items] == [
        "01_upload-01.pdf",
        "02_upload-01.pdf",
    ]
    assert all(item.output_pages == 2 for item in result.items)
    assert not list(result.directory.rglob("*.eml"))
    assert not (result.directory / ".raw").exists()

    with pytest.raises(FileExistsError, match="already exists"):
        service.create_batch(
            list(artifacts),
            tmp_path / "outputs",
            batch_name="Synthetic Batch",
        )
    assert result.merged_path.read_bytes() == b"2|2"


def test_mail_pdf_service_does_not_publish_partial_batch_on_failure(
    tmp_path: Path,
) -> None:
    store = MailArtifactStore(tmp_path / "private-mail", enabled=True)
    artifacts = store.retain_many(  # type: ignore[arg-type]
        [
            _validated_payload("first.eml", marker="first"),
            _validated_payload("second.eml", marker="second"),
        ]
    )
    service = MailPdfService(
        store,
        _FakeConverter(fail_on_call=2),
        normalizer=_FakeNormalizer(),
        merger=_FakeMerger(),
    )
    output_root = tmp_path / "outputs"

    with pytest.raises(RuntimeError, match="synthetic converter failure"):
        service.create_batch(artifacts, output_root, batch_name="failed-batch")

    assert not (output_root / "failed-batch").exists()
    assert not list(output_root.glob(".failed-batch.*"))
