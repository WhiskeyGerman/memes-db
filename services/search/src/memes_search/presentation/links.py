"""Стабильная адресация временных ресурсов конкретного deployment."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from memes_search.application.dto.indexes import IndexTarget


class SearchPath(StrEnum):
    """Публичные aliases, сохраняемые при переключении generic POST."""

    INDEXES = "/v1/search/indexes"


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResourceLinks:
    """Presentation-only ссылки на индекс, который создал ресурс."""

    prefix: str

    @classmethod
    def for_target(cls, target: IndexTarget) -> "SearchResourceLinks":
        """Выбрать стабильный prefix из доверенной конфигурации."""
        return cls(prefix=f"{SearchPath.INDEXES}/{target}")

    def request(self, request_id: UUID) -> str:
        """Адресовать продолжение ожидания inference."""
        return f"{self.prefix}/requests/{request_id}"

    def page(self, session_id: UUID, offset: int, limit: int) -> str:
        """Удержать пагинацию в создавшем снимок хранилище."""
        return f"{self.prefix}/sessions/{session_id}?offset={offset}&limit={limit}"

    def sample(self, sample_id: UUID) -> str:
        """Адресовать чтение и удаление приватного образца."""
        return f"{self.prefix}/samples/{sample_id}"

    def download(self, sample_id: UUID) -> str:
        """Адресовать авторизованное создание короткой S3-ссылки."""
        return f"{self.sample(sample_id)}/download"
