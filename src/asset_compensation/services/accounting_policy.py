"""Resolve semantic parser output into deployment-owned accounting accounts.

The parser deliberately emits policy keys and source dimensions instead of
operational GL strings.  This module is the single boundary where a private
deployment maps those keys to approved static accounts or account templates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from string import Formatter
from typing import Any

from asset_compensation.domain import ValidationError

PREPAYMENT_POLICY = "ASSET_COMPENSATION_PREPAYMENT"
DAMAGED_REPAIR_POLICY = "DAMAGED_REPAIR"
DAMAGED_NO_REPAIR_POLICY = "DAMAGED_NO_REPAIR"
LOST_DEPRECIATION_ASSET_POLICY = "LOST_DEPRECIATION_ASSET"
LOST_DEPRECIATION_OTHER_POLICY = "LOST_DEPRECIATION_OTHER"
LOST_RESPONSIBILITY_POLICY = "LOST_RESPONSIBILITY"
LOST_FALLBACK_POLICY = "LOST_FALLBACK"

_ALLOWED_DIMENSIONS = frozenset({"cost_center", "product_code", "location"})
_DIMENSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_REPAIRED = frozenset({"REPAIRED", "CÓ SỬA CHỮA", "CO SUA CHUA"})
_NOT_REPAIRED = frozenset(
    {"NOT_REPAIRED", "KHÔNG SỬA CHỮA", "KHONG SUA CHUA"}
)


def _value(case: object, name: str, default: Any = None) -> Any:
    if isinstance(case, Mapping):
        return case.get(name, default)
    return getattr(case, name, default)


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _whole_vnd(value: object, field_name: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ValidationError(f"{field_name} must be an exact whole VND amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an exact whole VND amount") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValidationError(f"{field_name} must be an exact nonnegative whole VND amount")
    return int(amount)


def _mapping(case: object) -> dict[str, Any]:
    if isinstance(case, Mapping):
        return dict(case)
    to_dict = getattr(case, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    names = (
        "id",
        "source_id",
        "case_type",
        "domain",
        "asset_code",
        "amount",
        "residual_value",
        "responsibility_fee",
        "repair_status",
        "supplier_number",
        "supplier_site",
        "supplier_name",
        "metadata",
    )
    return {name: getattr(case, name) for name in names if hasattr(case, name)}


@dataclass(frozen=True, slots=True)
class AccountingPolicyResult:
    """One Case-like mapping accepted by ``AccountingTemplateAdapter``."""

    case: Mapping[str, Any]
    policy_keys: tuple[str, ...]


class AccountingPolicyResolver:
    """Map semantic keys to accounts without embedding operational GL data.

    Values may be static accounts or templates using only
    ``{cost_center}``, ``{product_code}``, and ``{location}``.
    """

    def __init__(self, account_templates: Mapping[str, str]) -> None:
        if not account_templates:
            raise ValidationError("At least one accounting policy mapping is required")
        self._templates: dict[str, str] = {}
        for raw_key, raw_template in account_templates.items():
            key = str(raw_key or "").strip().upper()
            template = str(raw_template or "").strip()
            if not key or not template:
                raise ValidationError("Accounting policy keys and account templates are required")
            fields = {
                field_name
                for _, field_name, _, _ in Formatter().parse(template)
                if field_name is not None
            }
            if not fields.issubset(_ALLOWED_DIMENSIONS):
                unsupported = ", ".join(sorted(fields - _ALLOWED_DIMENSIONS))
                raise ValidationError(
                    f"Unsupported accounting account placeholder(s): {unsupported}"
                )
            self._templates[key] = template

    def _account(self, policy_key: object, dimensions: Mapping[str, object]) -> str:
        key = str(policy_key or "").strip().upper()
        try:
            template = self._templates[key]
        except KeyError as exc:
            raise ValidationError(
                f"Accounting policy {key or '<blank>'} is not configured"
            ) from exc
        safe_dimensions: dict[str, str] = {}
        for name in _ALLOWED_DIMENSIONS:
            value = str(dimensions.get(name) or "").strip().lstrip("'")
            if "{" + name + "}" in template and not _DIMENSION_RE.fullmatch(value):
                raise ValidationError(f"Invalid {name} for accounting policy {key}")
            safe_dimensions[name] = value
        try:
            return template.format_map(safe_dimensions)
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"Accounting policy template {key} is invalid") from exc

    @staticmethod
    def _dimensions(metadata: Mapping[str, Any], component: Mapping[str, Any]) -> dict[str, object]:
        return {
            name: component.get(name, metadata.get(name))
            for name in _ALLOWED_DIMENSIONS
        }

    def resolve(self, case: object) -> AccountingPolicyResult:
        resolved = _mapping(case)
        case_type = _enum_text(_value(case, "case_type"))
        if case_type not in {"DAMAGED", "LOST"}:
            raise ValidationError(f"Unsupported accounting case type: {case_type or '<blank>'}")
        amount = _whole_vnd(_value(case, "amount"), "amount")
        if amount == 0:
            raise ValidationError("Accounting case amount must be greater than zero")
        metadata_value = _value(case, "metadata", {}) or {}
        if not isinstance(metadata_value, Mapping):
            raise ValidationError("case metadata must be a mapping")
        metadata = dict(metadata_value)
        debit_policy = metadata.get("prepayment_policy_key", PREPAYMENT_POLICY)
        debit_gl = self._account(debit_policy, metadata)
        policy_keys = [str(debit_policy).upper()]
        credit_lines: list[dict[str, object]] = []

        if case_type == "DAMAGED":
            repair_status = _enum_text(_value(case, "repair_status"))
            if repair_status in _REPAIRED:
                credit_policy = DAMAGED_REPAIR_POLICY
            elif repair_status in _NOT_REPAIRED:
                credit_policy = DAMAGED_NO_REPAIR_POLICY
            else:
                raise ValidationError(
                    "Damaged case repair status must be verified before accounting"
                )
            credit_lines.append(
                {
                    "account": self._account(credit_policy, metadata),
                    "amount": amount,
                }
            )
            policy_keys.append(credit_policy)
        else:
            raw_components = metadata.get("credit_components")
            if raw_components not in (None, ""):
                if not isinstance(raw_components, (list, tuple)) or not raw_components:
                    raise ValidationError("credit_components must be a non-empty list")
                for index, raw_component in enumerate(raw_components, start=1):
                    if not isinstance(raw_component, Mapping):
                        raise ValidationError(
                            f"credit_components[{index}] must be an object"
                        )
                    policy_key = str(raw_component.get("policy_key") or "").upper()
                    line_amount = _whole_vnd(
                        raw_component.get("amount"),
                        f"credit_components[{index}].amount",
                    )
                    dimensions = self._dimensions(metadata, raw_component)
                    credit_lines.append(
                        {
                            "account": self._account(policy_key, dimensions),
                            "amount": line_amount,
                            "highlight": (
                                "green" if raw_component.get("entity_non_vng") is True else None
                            ),
                        }
                    )
                    policy_keys.append(policy_key)
            else:
                residual = _value(case, "residual_value")
                fee = _value(case, "responsibility_fee")
                if residual is not None and fee is not None:
                    residual_amount = _whole_vnd(residual, "residual_value")
                    fee_amount = _whole_vnd(fee, "responsibility_fee")
                    for policy_key, line_amount in (
                        (LOST_DEPRECIATION_OTHER_POLICY, residual_amount),
                        (LOST_RESPONSIBILITY_POLICY, fee_amount),
                    ):
                        if line_amount:
                            credit_lines.append(
                                {
                                    "account": self._account(policy_key, metadata),
                                    "amount": line_amount,
                                }
                            )
                            policy_keys.append(policy_key)
                else:
                    credit_lines.append(
                        {
                            "account": self._account(LOST_FALLBACK_POLICY, metadata),
                            "amount": amount,
                        }
                    )
                    policy_keys.append(LOST_FALLBACK_POLICY)

        if sum(int(line["amount"]) for line in credit_lines) != amount:
            raise ValidationError("Semantic credit components do not reconcile to case amount")
        fallback_credit = str(credit_lines[0]["account"])
        metadata.update(
            {
                "debit_gl": debit_gl,
                "credit_gl": fallback_credit,
                "credit_lines": credit_lines,
                "prepayment_highlight": (
                    "green"
                    if metadata.get("entity_non_vng") is True
                    or any(line.get("highlight") == "green" for line in credit_lines)
                    else "yellow"
                    if metadata.get("employee_inactive") is True
                    else None
                ),
            }
        )
        resolved["metadata"] = metadata
        return AccountingPolicyResult(case=resolved, policy_keys=tuple(policy_keys))

    def resolve_many(self, cases: Iterable[object]) -> tuple[AccountingPolicyResult, ...]:
        """Resolve all cases with the original stable DAMAGED-then-LOST ordering."""

        prepared = list(cases)
        indexed = list(enumerate(prepared))
        indexed.sort(
            key=lambda item: (
                0 if _enum_text(_value(item[1], "case_type")) == "DAMAGED" else 1,
                item[0],
            )
        )
        return tuple(self.resolve(case) for _, case in indexed)
