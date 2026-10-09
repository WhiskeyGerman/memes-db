"""Ошибки прикладных сценариев без привязки к HTTP."""

from enum import StrEnum


class IdentityFailure(StrEnum):
    """Причины отказа прикладной операции."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_PASSWORD_LENGTH = "invalid_password_length"  # noqa: S105
    LOGIN_OCCUPIED = "login_occupied"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "concurrent_change"
    RATE_LIMITED = "rate_limited"
    REFRESH_REPLAY = "refresh_replay"
    INTERNAL_AUTH_FAILED = "internal_auth_failed"
    CSRF_FAILED = "csrf_failed"
    BACKING_SERVICE_UNAVAILABLE = "backing_service_unavailable"


class IdentityError(ValueError):
    """Типизированный отказ прикладного сценария."""

    def __init__(self, code: IdentityFailure) -> None:
        """Запомнить машинный код отказа.

        Args:
            code: Причина отказа.
        """
        self.code = code
        super().__init__(code.value)
