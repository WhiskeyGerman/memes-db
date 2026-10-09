"""Типизированные отказы прикладных сценариев поиска."""

from enum import StrEnum


class SearchErrorCode(StrEnum):
    """Причины отказа, стабильные для HTTP-клиентов."""

    INVALID_QUERY = "invalid_search_query"
    INVALID_SESSION = "invalid_search_session"
    SESSION_EXPIRED = "search_session_expired"
    GROUPS_CHANGED = "search_groups_changed"
    NOT_FOUND = "search_resource_not_found"
    FORBIDDEN = "search_resource_forbidden"
    UNAVAILABLE = "search_dependency_unavailable"
    INFERENCE_PENDING = "query_inference_pending"
    INFERENCE_FAILED = "query_inference_failed"
    INVALID_FEATURE = "invalid_search_feature"
    INVALID_SAMPLE = "invalid_search_sample"
    SAMPLE_TOO_LARGE = "private_sample_too_large"
    SAMPLE_LIMIT = "active_sample_limit"
    SAMPLE_PENDING = "search_sample_pending"
    SAMPLE_FAILED = "search_sample_failed"


class SearchError(ValueError):
    """Ожидаемый прикладной отказ без транспортных зависимостей."""

    def __init__(self, code: SearchErrorCode) -> None:
        """Запомнить машинную причину отказа.

        Args:
            code: Причина отказа сценария.
        """
        self.code = code
        super().__init__(code.value)
