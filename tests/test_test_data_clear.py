"""Contract and deletion-boundary tests for opt-in staging cleanup."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from threading import Event, Thread

import pytest
from flask import Flask

import asset_compensation.web.routes as routes_module
from asset_compensation.config import Settings
from asset_compensation.domain import AccountingBatch
from asset_compensation.web import create_app

_ACTION_HEADERS = {"X-Asset-Hub-Action": "clear-test-data-v1"}
_CONFIRMATION = {"confirm": "CLEAR_TEST_DATA"}


@pytest.fixture
def reset_app(tmp_path: Path) -> Flask:
    external_supplier = tmp_path / "configured-supplier.csv"
    external_template = tmp_path / "configured-template.xlsx"
    external_supplier.write_text("external supplier sentinel", "utf-8")
    external_template.write_bytes(b"external template sentinel")
    application = create_app(
        Settings(
            data_dir=tmp_path / "runtime",
            demo_mode=True,
            allow_test_reset=True,
            secret_key="synthetic-clear-test-key",
            supplier_file=external_supplier,
            accounting_template=external_template,
        )
    )
    application.config.update(TESTING=True)
    yield application
    application.extensions["asset_hub"]["repository"].close()


def _supplier_csv(domain: str, number: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["Domain", "Supplier Number", "Supplier Site", "Supplier Name"])
    writer.writerow([domain, number, "OFFICE", f"Synthetic {domain}"])
    return stream.getvalue().encode()


def _upload_supplier_pair(app: Flask) -> None:
    response = app.test_client().post(
        "/api/suppliers/upload",
        data={
            "active_file": (
                io.BytesIO(_supplier_csv("active.user", "SUP-001")),
                "active.csv",
                "text/csv",
            ),
            "inactive_file": (
                io.BytesIO(_supplier_csv("inactive.user", "SUP-002")),
                "inactive.csv",
                "text/csv",
            ),
        },
        headers={"X-Asset-Hub-Upload": "supplier-v1"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201


def test_test_data_clear_is_disabled_by_default(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / "runtime",
            demo_mode=False,
            secret_key="synthetic-disabled-clear-key",
        )
    )
    app.config.update(TESTING=True)
    try:
        dashboard = app.test_client().get("/api/dashboard").get_json()
        response = app.test_client().post(
            "/api/test-data/clear", json=_CONFIRMATION, headers=_ACTION_HEADERS
        )

        assert dashboard["capabilities"]["test_reset"] is False
        assert dashboard["capabilities"]["demo_reset"] is False
        assert dashboard["capabilities"]["raw_eml_retention"] is False
        assert response.status_code == 400
        assert "disabled" in response.get_json()["error"]
    finally:
        app.extensions["asset_hub"]["repository"].close()


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, _CONFIRMATION),
        ({"X-Asset-Hub-Action": "wrong"}, _CONFIRMATION),
        (_ACTION_HEADERS, {"confirm": "wrong"}),
        (_ACTION_HEADERS, {"confirm": "CLEAR_TEST_DATA", "extra": True}),
    ],
)
def test_test_data_clear_requires_header_and_exact_confirmation(
    reset_app: Flask, headers: dict[str, str], body: dict[str, object]
) -> None:
    response = reset_app.test_client().post(
        "/api/test-data/clear", json=body, headers=headers
    )

    assert response.status_code == 400
    assert reset_app.extensions["asset_hub"]["case_service"].summary().total == 5


def test_test_data_clear_removes_only_owned_records_and_named_outputs(
    reset_app: Flask,
) -> None:
    settings = reset_app.extensions["asset_hub"]["settings"]
    repository = reset_app.extensions["asset_hub"]["repository"]
    service = reset_app.extensions["asset_hub"]["case_service"]
    first_case = service.list_cases()[0]
    output = settings.output_dir / "generated-test.xlsx"
    orphan = settings.output_dir / "unreferenced-output.xlsx"
    inbox_file = settings.inbox_dir / "inbox-sentinel.eml"
    reference_sentinel = settings.reference_dir / "reference-sentinel.txt"
    retained_digest = "a" * 64
    retained_shard = settings.mail_artifact_dir / retained_digest[:2]
    retained_shard.mkdir(parents=True)
    retained_artifact = retained_shard / f"{retained_digest}.eml"
    retained_artifact.write_bytes(b"synthetic previously retained email")
    output.write_bytes(b"synthetic generated output")
    orphan.write_bytes(b"must remain")
    inbox_file.write_bytes(b"must remain")
    reference_sentinel.write_text("must remain", "utf-8")
    repository.create_batch(
        AccountingBatch(
            id="synthetic-clear-batch",
            name="Synthetic clear batch",
            case_ids=(first_case.id,),
            metadata={"output_name": output.name},
        )
    )
    _upload_supplier_pair(reset_app)

    response = reset_app.test_client().post(
        "/api/test-data/clear", json=_CONFIRMATION, headers=_ACTION_HEADERS
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["case_count"] == 5
    assert payload["event_count"] == 10
    assert payload["batch_count"] == 1
    assert payload["output_file_count"] == 1
    assert payload["supplier_record_count"] == 2
    assert payload["supplier_version_count"] == 1
    assert payload["mail_artifact_count"] == 1
    assert service.summary().total == 0
    assert repository.list_batches() == []
    assert not output.exists()
    assert orphan.read_bytes() == b"must remain"
    assert inbox_file.read_bytes() == b"must remain"
    assert reference_sentinel.read_text("utf-8") == "must remain"
    assert not retained_artifact.exists()
    assert settings.supplier_file.read_text("utf-8") == "external supplier sentinel"
    assert settings.accounting_template.read_bytes() == b"external template sentinel"
    assert reset_app.extensions["asset_hub"]["supplier_upload_service"].status()[
        "configured"
    ] is False


def test_test_data_clear_rejects_unsafe_batch_output_before_mutation(
    reset_app: Flask, tmp_path: Path
) -> None:
    repository = reset_app.extensions["asset_hub"]["repository"]
    service = reset_app.extensions["asset_hub"]["case_service"]
    outside = tmp_path / "outside-output.xlsx"
    outside.write_bytes(b"must never be deleted")
    first_case = service.list_cases()[0]
    repository.create_batch(
        AccountingBatch(
            id="synthetic-unsafe-output-batch",
            name="Synthetic unsafe output batch",
            case_ids=(first_case.id,),
            metadata={"output_name": f"../{outside.name}"},
        )
    )

    response = reset_app.test_client().post(
        "/api/test-data/clear", json=_CONFIRMATION, headers=_ACTION_HEADERS
    )

    assert response.status_code == 400
    assert "unsafe output" in response.get_json()["error"]
    assert outside.read_bytes() == b"must never be deleted"
    assert service.summary().total == 5
    assert len(repository.list_batches()) == 1


def test_allow_test_reset_reads_explicit_environment_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASSET_HUB_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ASSET_HUB_ALLOW_TEST_RESET", "true")

    assert Settings.from_env().allow_test_reset is True


def test_clear_waits_for_inflight_batch_and_cannot_leave_orphaned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / "concurrent-runtime",
            demo_mode=True,
            allow_test_reset=True,
            secret_key="synthetic-concurrency-test-key",
        )
    )
    app.config.update(TESTING=True)
    repository = app.extensions["asset_hub"]["repository"]
    export_started = Event()
    release_export = Event()
    clear_started = Event()
    clear_finished = Event()
    original_export = routes_module.AccountingTemplateAdapter.export
    responses: dict[str, tuple[int, dict[str, object]]] = {}
    failures: list[BaseException] = []

    def paused_export(*args: object, **kwargs: object) -> object:
        result = original_export(*args, **kwargs)
        export_started.set()
        if not release_export.wait(timeout=5):
            raise RuntimeError("Synthetic concurrent export was not released")
        return result

    def create_batch_request(case_id: str) -> None:
        try:
            with app.test_client() as client:
                response = client.post(
                    "/api/batches",
                    json={"batch_name": "GN2010126", "case_ids": [case_id]},
                )
                responses["batch"] = (response.status_code, response.get_json())
        except BaseException as exc:  # pragma: no cover - reported by the main thread
            failures.append(exc)

    def clear_request() -> None:
        clear_started.set()
        try:
            with app.test_client() as client:
                response = client.post(
                    "/api/test-data/clear",
                    json=_CONFIRMATION,
                    headers=_ACTION_HEADERS,
                )
                responses["clear"] = (response.status_code, response.get_json())
        except BaseException as exc:  # pragma: no cover - reported by the main thread
            failures.append(exc)
        finally:
            clear_finished.set()

    monkeypatch.setattr(routes_module.AccountingTemplateAdapter, "export", paused_export)
    dashboard = app.test_client().get("/api/dashboard").get_json()
    ready_id = next(
        case["id"]
        for case in dashboard["cases"]
        if case["status"] == "READY_FOR_ACCOUNTING"
    )
    batch_thread = Thread(target=create_batch_request, args=(ready_id,), daemon=True)
    clear_thread = Thread(target=clear_request, daemon=True)
    try:
        batch_thread.start()
        assert export_started.wait(timeout=5)
        clear_thread.start()
        assert clear_started.wait(timeout=2)
        assert not clear_finished.wait(timeout=0.2)
    finally:
        release_export.set()
        batch_thread.join(timeout=10)
        clear_thread.join(timeout=10)

    try:
        assert not batch_thread.is_alive()
        assert not clear_thread.is_alive()
        assert failures == []
        assert responses["batch"][0] == 201
        assert responses["clear"][0] == 200
        output_name = responses["batch"][1]["batch"]["metadata"]["output_name"]
        assert not (app.extensions["asset_hub"]["settings"].output_dir / output_name).exists()
        assert repository.list_batches() == []
        assert app.extensions["asset_hub"]["case_service"].summary().total == 0
    finally:
        repository.close()
