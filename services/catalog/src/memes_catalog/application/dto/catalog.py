"""Команды и проекции сценариев каталога."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from memes_catalog.domain.groups import GroupMembership, VariationGroup
from memes_catalog.domain.media import Media, OriginalMedia
from memes_platform.application.dto.json import JsonObject


class ActorRole(StrEnum):
    """Внешние роли, необходимые для авторизации сценария."""

    USER = "user"
    OPERATOR = "operator"


class UploadState(StrEnum):
    """Жизненный цикл временной загрузки."""

    RESERVED = "reserved"
    PROCESSING = "processing"
    PUBLISHED = "published"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CatalogDefault(IntEnum):
    """Начальные настраиваемые лимиты прикладных процессов."""

    CONCURRENT_UPLOADS = 3
    DAILY_PUBLICATIONS = 100
    VOTES_PER_MINUTE = 60
    MAX_SUGGESTIONS = 10
    UPLOAD_SECONDS = 3600
    CLAIM_SECONDS = 180
    DOWNLOAD_SECONDS = 60
    PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100
    FEED_SCAN_LIMIT = 1000
    QUEUE_LIMIT = 100000
    BATCH_SIZE = 1000
    GROUP_CHANGE_RETRIES = 3
    STORAGE_BYTES = 1099511627776
    FREE_PERCENT = 15
    PERCENT_SCALE = 100
    OUTBOX_RETRY_SECONDS = 5
    OUTBOX_BATCH = 100
    DELIVERY_CONCURRENCY = 8
    CLEANUP_SECONDS = 60
    MILLISECONDS = 1000
    DAY_SECONDS = 86400
    MINUTE_SECONDS = 60


@dataclass(frozen=True, slots=True, kw_only=True)
class Actor:
    """Авторизованный участник сценария."""

    user_id: UUID
    roles: frozenset[ActorRole]


@dataclass(frozen=True, slots=True, kw_only=True)
class Author:
    """Публичные атрибуты автора либо обезличенный tombstone."""

    user_id: UUID
    login: str | None
    delete_media: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Upload:
    """Сохранённая временная загрузка с lease приёмки."""

    id: UUID
    author_id: UUID
    file_id: UUID
    media_id: UUID
    object_key: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime
    state: UploadState = UploadState.RESERVED
    claimed_until: datetime | None = None
    outcome_media_id: UUID | None = None
    claim_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadTicket:
    """Выданная пользователю ограниченная возможность передачи."""

    upload: Upload
    url: str
    fields: dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectedMedia:
    """Результат инфраструктурного декодирования оригинала."""

    original: OriginalMedia
    metadata: JsonObject
    etag: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaView:
    """Согласованный снимок опубликованного медиа и членства."""

    media: Media
    object_key: str
    membership: GroupMembership
    group: VariationGroup
    metadata: JsonObject
    features: tuple[JsonObject, ...] = ()
    projection_revision: int = 1
    author: Author | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupView:
    """Группа с ограниченным окном участников и обложкой."""

    group: VariationGroup
    items: tuple[MediaView, ...]
    cover_media_id: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationOutcome:
    """Идемпотентный ответ о публикации либо точном дубле."""

    media_id: UUID
    duplicate: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class VoteOutcome:
    """Состояние после атомарной подачи или отзыва голоса."""

    ballot_id: UUID
    active_votes: int
    threshold: int
    applied: bool
    group_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OutgoingEvent:
    """Сохранённый пакет доставки REST с lease диспетчера."""

    id: UUID
    target: str
    payload: JsonObject
    attempts: int
    claim_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingCleanup:
    """Задание удаления строго определённого S3-объекта."""

    id: UUID
    object_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureUpdate:
    """Проверенный вход публикации результата от координатора."""

    event_id: UUID
    media_id: UUID
    source_revision: int
    cycle_id: UUID
    generation: int
    kind: str
    state: str
    feature_version: JsonObject
    result: JsonObject | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedCursor:
    """Устойчивый курсор ленты по времени и идентичности."""

    published_at: datetime
    media_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedPage:
    """Видимое окно ленты и курсор последней проверенной записи."""

    items: tuple[MediaView, ...]
    next_cursor: FeedCursor | None


class SearchIndexTarget(StrEnum):
    """Выбранная цель административного построения поисковой проекции."""

    PRIMARY = "primary"
    SHADOW = "shadow"
