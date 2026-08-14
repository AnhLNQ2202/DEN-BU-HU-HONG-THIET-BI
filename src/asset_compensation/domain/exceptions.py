"""Domain-specific exceptions for asset compensation cases."""

from __future__ import annotations


class AssetCompensationError(Exception):
    """Base exception for the core domain."""


class ValidationError(AssetCompensationError, ValueError):
    """Raised when a domain object contains invalid data."""


class CaseNotFoundError(AssetCompensationError, LookupError):
    """Raised when a requested case does not exist."""


class InvalidStatusTransition(AssetCompensationError, ValueError):
    """Raised when a case status transition is not allowed."""


class ConcurrencyError(AssetCompensationError):
    """Raised when optimistic state validation detects a concurrent update."""


class RepositoryError(AssetCompensationError):
    """Raised when persistence cannot complete an atomic operation."""
