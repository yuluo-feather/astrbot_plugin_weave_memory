"""Domain errors. These types are platform- and persistence-independent."""


class DomainError(Exception):
    """Base class for expected domain failures."""


class ValidationError(DomainError):
    pass


class AuthorizationDenied(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidTransition(DomainError):
    pass


class ServiceClosed(DomainError):
    pass


class IndexUnavailable(DomainError):
    pass


class StorageError(DomainError):
    pass


class ConsentRevocationPending(StorageError):
    """Scope is already disabled, but its append-only revoke event needs retry."""


class GroupConsentRevocationPending(StorageError):
    """Group scope is disabled, but its group_memory REVOKED event needs retry."""
