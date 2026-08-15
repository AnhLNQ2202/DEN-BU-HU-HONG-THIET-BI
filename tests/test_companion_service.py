"""Unit contracts for short-lived desktop companion pairing."""

from __future__ import annotations

from pathlib import Path

import pytest

from asset_compensation.services import (
    CompanionAuthenticationError,
    CompanionAuthorizationError,
    CompanionPackageError,
    CompanionPackageNotFoundError,
    CompanionService,
)

_HANDLE = "eml-sha256-" + ("a" * 64)
_OTHER_HANDLE = "eml-sha256-" + ("b" * 64)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _paired(service: CompanionService, role: str = "tran") -> tuple[str, object]:
    pairing = service.create_pairing(role, "local_bridge")
    exchange = service.exchange(pairing.code)
    return exchange.token, exchange.principal


def test_pairing_is_one_time_role_bound_and_retains_only_digests() -> None:
    service = CompanionService()
    pairing = service.create_pairing("tran", "outlook_addin")

    assert pairing.role == "tran"
    assert pairing.client_type == "outlook_addin"
    assert pairing.expires_in_seconds == 300
    assert len(pairing.code) == 14
    assert pairing.code.encode() not in repr(service._pairings).encode()  # noqa: SLF001

    exchange = service.exchange(pairing.code.lower())
    assert exchange.principal.role == "tran"
    assert exchange.principal.client_type == "outlook_addin"
    assert exchange.expires_in_seconds == 28_800
    assert exchange.token.encode() not in repr(service._sessions).encode()  # noqa: SLF001
    assert service.authenticate(exchange.token) == exchange.principal

    with pytest.raises(CompanionAuthenticationError):
        service.exchange(pairing.code)
    with pytest.raises(CompanionAuthenticationError):
        service.authenticate("not-a-token")


def test_pairings_sessions_and_packages_expire_and_stores_stay_bounded(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = CompanionService(
        clock=clock,
        pairing_ttl_seconds=5,
        session_ttl_seconds=10,
        package_ttl_seconds=15,
        max_pairings=1,
        max_sessions=1,
        max_packages=1,
    )
    expired_pairing = service.create_pairing("ngan", "local_bridge")
    replacement_pairing = service.create_pairing("tran", "local_bridge")
    with pytest.raises(CompanionAuthenticationError):
        service.exchange(expired_pairing.code)
    first = service.exchange(replacement_pairing.code)

    second_code = service.create_pairing("tran", "outlook_addin")
    second = service.exchange(second_code.code)
    with pytest.raises(CompanionAuthenticationError):
        service.authenticate(first.token)
    assert service.authenticate(second.token) == second.principal

    root = tmp_path / "tran"
    root.mkdir()
    first_id = "1" * 32
    first_path = root / f"workbook-{first_id}.xlsx"
    first_path.write_bytes(b"first")
    service.record_uploaded_handle(second.principal, _HANDLE)
    service.register_draft_package(
        package_id=first_id,
        artifact_handle=_HANDLE,
        asset_count=1,
        body_html="<p>first</p>",
        workbook_path=first_path,
        output_root=root,
    )
    second_id = "2" * 32
    second_path = root / f"workbook-{second_id}.xlsx"
    second_path.write_bytes(b"second")
    service.register_draft_package(
        package_id=second_id,
        artifact_handle=_HANDLE,
        asset_count=1,
        body_html="<p>second</p>",
        workbook_path=second_path,
        output_root=root,
    )
    with pytest.raises(CompanionPackageNotFoundError):
        service.get_draft_package(second.principal, first_id, output_root=root)

    clock.advance(11)
    with pytest.raises(CompanionAuthenticationError):
        service.authenticate(second.token)


def test_draft_queue_is_source_bound_tran_only_acknowledgeable_and_private(
    tmp_path: Path,
) -> None:
    service = CompanionService()
    _, tran = _paired(service, "tran")
    _, other_tran = _paired(service, "tran")
    _, ngan = _paired(service, "ngan")
    service.record_uploaded_handle(tran, _HANDLE)
    service.record_uploaded_handle(other_tran, _OTHER_HANDLE)
    service.record_uploaded_handle(ngan, _HANDLE)

    root = tmp_path / "tran"
    root.mkdir()
    package_id = "a" * 32
    workbook = root / f"workbook-{package_id}.xlsx"
    workbook.write_bytes(b"synthetic workbook")
    created = service.register_draft_package(
        package_id=package_id,
        artifact_handle=_HANDLE,
        asset_count=2,
        body_html="<p>Approved</p>",
        workbook_path=workbook,
        output_root=root,
    )

    assert created.metadata()["sent"] is False
    assert [item.package_id for item in service.list_draft_packages(tran)] == [package_id]
    assert service.list_draft_packages(other_tran) == ()
    with pytest.raises(CompanionAuthorizationError):
        service.list_draft_packages(ngan)
    with pytest.raises(CompanionPackageNotFoundError):
        service.get_draft_package(other_tran, package_id, output_root=root)

    service.acknowledge_draft_package(tran, package_id)
    service.acknowledge_draft_package(tran, package_id)
    assert service.list_draft_packages(tran) == ()
    assert service.get_draft_package(tran, package_id, output_root=root).body_html == (
        "<p>Approved</p>"
    )

    outside = tmp_path / f"workbook-{'b' * 32}.xlsx"
    outside.write_bytes(b"outside")
    with pytest.raises(CompanionPackageError):
        service.register_draft_package(
            package_id="b" * 32,
            artifact_handle=_HANDLE,
            asset_count=1,
            body_html="<p>unsafe</p>",
            workbook_path=outside,
            output_root=root,
        )


def test_new_draft_for_same_source_supersedes_the_old_package(tmp_path: Path) -> None:
    service = CompanionService()
    _, tran = _paired(service, "tran")
    service.record_uploaded_handle(tran, _HANDLE)
    root = tmp_path / "tran"
    root.mkdir()

    first_id = "d" * 32
    first_path = root / f"workbook-{first_id}.xlsx"
    first_path.write_bytes(b"old workbook")
    service.register_draft_package(
        package_id=first_id,
        artifact_handle=_HANDLE,
        asset_count=1,
        body_html="<p>Old draft</p>",
        workbook_path=first_path,
        output_root=root,
    )

    second_id = "e" * 32
    second_path = root / f"workbook-{second_id}.xlsx"
    second_path.write_bytes(b"new workbook")
    service.register_draft_package(
        package_id=second_id,
        artifact_handle=_HANDLE,
        asset_count=1,
        body_html="<p>Newest draft</p>",
        workbook_path=second_path,
        output_root=root,
    )

    assert [item.package_id for item in service.list_draft_packages(tran)] == [second_id]
    with pytest.raises(CompanionPackageNotFoundError):
        service.get_draft_package(tran, first_id, output_root=root)
    assert service.get_draft_package(tran, second_id, output_root=root).body_html == (
        "<p>Newest draft</p>"
    )


def test_clear_all_invalidates_every_ephemeral_credential_and_package(
    tmp_path: Path,
) -> None:
    service = CompanionService()
    unused = service.create_pairing("ngan", "local_bridge")
    token, tran = _paired(service)
    service.record_uploaded_handle(tran, _HANDLE)
    root = tmp_path / "tran"
    root.mkdir()
    package_id = "c" * 32
    workbook = root / f"workbook-{package_id}.xlsx"
    workbook.write_bytes(b"workbook")
    service.register_draft_package(
        package_id=package_id,
        artifact_handle=_HANDLE,
        asset_count=1,
        body_html="<p>draft</p>",
        workbook_path=workbook,
        output_root=root,
    )

    assert service.clear_all() == {
        "companion_pairing_count": 1,
        "companion_session_count": 1,
        "companion_package_count": 1,
    }
    with pytest.raises(CompanionAuthenticationError):
        service.exchange(unused.code)
    with pytest.raises(CompanionAuthenticationError):
        service.authenticate(token)
