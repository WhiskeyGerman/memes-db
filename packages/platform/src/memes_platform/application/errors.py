"""Прикладные причины отказа без зависимости от HTTP."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Коды отказов публичных сценариев."""

    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    STALE_STATE = "stale_state"


class ApplicationError(ValueError):
    """Прикладной отказ с безопасным для клиента сообщением."""

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        """Сохранить причину отказа.

        Args:
            code: Код причины.
            message: Безопасное публичное пояснение.
        """
        self.code = code
        self.message = message or code.value
        super().__init__(self.message)
