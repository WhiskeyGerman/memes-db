"""Транзакционный PostgreSQL unit of work каталога."""

from dataclasses import replace
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Self, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from memes_catalog.application.dto.catalog import (
    CatalogDefault,
    FeatureUpdate,
    FeedCursor,
    GroupView,
    MediaView,
    PendingCleanup,
    PublicationOutcome,
    SearchIndexTarget,
    Upload,
    UploadState,
)
from memes_catalog.application.dto.projections import media_payload
from memes_catalog.application.ports.catalog import (
    ICatalogHealth,
    ICatalogUnitOfWork,
    ICatalogUnitOfWorkFactory,
)
from memes_catalog.domain.cover import COVER_PRIORITIES
from memes_catalog.domain.groups import GroupState
from memes_catalog.domain.media import MediaState
from memes_catalog.infrastructure.persistence import mapping
from memes_catalog.infrastructure.persistence.events import (
    DeliveryTarget,
    OutboxSetting,
    delivery_targets,
    enqueue,
)
from memes_catalog.infrastructure.persistence.models import (
    AccountTombstoneRow,
    CleanupRow,
    DecisionRow,
    DismissalRow,
    FeatureRow,
    GroupRow,
    InboxRow,
    MediaRow,
    MembershipRow,
    OutboxRow,
    UploadRow,
)
from memes_catalog.infrastructure.persistence.variations import PostgresVariations
from memes_kernel.values import Number, Sha256
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode


class LockKey(IntEnum):
    """Пространство глобального ограничения хранения."""

    STORAGE = 104932


class FeatureState(StrEnum):
    """Контракт состояний извлечения признака."""

    PENDING = "pending"
    AVAILABLE = "available"
    EMPTY = "empty"
    NOT_APPLICABLE = "not_applicable"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PostgresCatalogUnitOfWork(ICatalogUnitOfWork):
    """Общая транзакция всех принадлежащих Catalog таблиц."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, mirror_search: bool = False
    ) -> None:
        """Сохранить фабрику сессий.

        Args:
            sessions: Фабрика PostgreSQL.
            mirror_search: Доставлять проекции в независимый индекс-кандидат.
        """
        self.session = sessions()
        self.session.info[OutboxSetting.MIRROR_SEARCH] = mirror_search
        self._variations = PostgresVariations(self.session)

    @property
    def variations(self) -> PostgresVariations:
        """Получить порт транзакционного хранения вариаций."""
        return self._variations

    async def __aenter__(self) -> Self:
        """Начать транзакцию без изменения схемы."""
        await self.session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Освободить незавершённую транзакцию."""
        try:
            await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        """Зафиксировать данные и outbox атомарно."""
        await self.session.commit()

    async def reserve(
        self,
        upload: Upload,
        *,
        concurrent_limit: int,
        daily_limit: int,
        storage_limit: int,
        free_percent: int,
    ) -> None:
        """Зарезервировать ёмкость и персональные квоты."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": LockKey.STORAGE}
        )
        active = (UploadState.RESERVED, UploadState.PROCESSING)
        count = await self.session.scalar(
            select(func.count())
            .select_from(UploadRow)
            .where(
                UploadRow.author_id == upload.author_id,
                UploadRow.state.in_(active),
                UploadRow.expires_at > upload.created_at,
            )
        )
        day_count = await self.session.scalar(
            select(func.count())
            .select_from(MediaRow)
            .where(
                MediaRow.author_id == upload.author_id,
                MediaRow.published_at
                > upload.created_at - timedelta(seconds=CatalogDefault.DAY_SECONDS),
            )
        )
        reserved_count = await self.session.scalar(
            select(func.count())
            .select_from(UploadRow)
            .where(
                UploadRow.author_id == upload.author_id,
                UploadRow.state.in_(active),
                UploadRow.expires_at > upload.created_at,
            )
        )
        if (count or Number.ZERO) >= concurrent_limit or (day_count or Number.ZERO) + (
            reserved_count or Number.ZERO
        ) >= daily_limit:
            raise ApplicationError(ErrorCode.RATE_LIMITED)
        used = await self.session.scalar(
            select(func.coalesce(func.sum(MediaRow.size_bytes), Number.ZERO)).where(
                MediaRow.state == MediaState.PUBLISHED
            )
        )
        reserved = await self.session.scalar(
            select(func.coalesce(func.sum(UploadRow.size_bytes), Number.ZERO)).where(
                UploadRow.state.in_(active), UploadRow.expires_at > upload.created_at
            )
        )
        ceiling = (
            storage_limit
            * (CatalogDefault.PERCENT_SCALE - free_percent)
            // CatalogDefault.PERCENT_SCALE
        )
        if (used or Number.ZERO) + (reserved or Number.ZERO) + upload.size_bytes > ceiling:
            raise ApplicationError(
                ErrorCode.UNAVAILABLE, "Недостаточно доступной ёмкости хранения."
            )
        await self.save_upload(upload)
        await self._schedule_cleanup(upload.object_key, upload.expires_at)

    async def prepare_object(self, object_key: str, *, ready_at: datetime) -> None:
        """Сохранить durable уборку объекта текущей попытки."""
        await self._schedule_cleanup(object_key, ready_at)

    async def upload(self, upload_id: UUID, *, lock: bool = False) -> Upload | None:
        """Найти сохранённую передачу."""
        query = (
            select(UploadRow)
            .where(UploadRow.id == upload_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        return None if row is None else mapping.upload(row)

    async def save_upload(self, upload: Upload) -> None:
        """Сохранить прикладное состояние передачи."""
        await self.session.merge(
            UploadRow(
                id=upload.id,
                author_id=upload.author_id,
                file_id=upload.file_id,
                media_id=upload.media_id,
                object_key=upload.object_key,
                size_bytes=upload.size_bytes,
                created_at=upload.created_at,
                expires_at=upload.expires_at,
                state=upload.state.value,
                claimed_until=upload.claimed_until,
                outcome_media_id=upload.outcome_media_id,
                claim_id=upload.claim_id,
            )
        )
        await self.session.flush()

    async def publish(
        self, upload: Upload, view: MediaView, *, now: datetime
    ) -> PublicationOutcome:
        """Сохранить уникальный хэш, членство и событие публикации."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:digest, 0))"),
            {"digest": view.media.original.file.sha256.value},
        )
        duplicate = await self.by_hash(view.media.original.file.sha256)
        if duplicate is not None:
            await self.save_upload(
                replace(
                    upload,
                    state=UploadState.DUPLICATE,
                    claimed_until=None,
                    outcome_media_id=duplicate.media.id,
                )
            )
            await self._schedule_cleanup(view.object_key, now)
            await self._schedule_cleanup(upload.object_key, now)
            return PublicationOutcome(media_id=duplicate.media.id, duplicate=True)
        deleted_account = await self.session.get(AccountTombstoneRow, upload.author_id)
        if deleted_account is not None:
            raise ApplicationError(ErrorCode.UNAUTHORIZED)
        self.session.add(
            MediaRow(
                id=view.media.id,
                author_id=view.media.author_id,
                file_id=view.media.original.file.id,
                sha256=view.media.original.file.sha256.value,
                size_bytes=view.media.original.file.size_bytes,
                format=view.media.original.format.value,
                kind=view.media.original.kind.value,
                metadata_payload=view.metadata,
                object_key=view.object_key,
                published_at=view.media.published_at,
                state=view.media.state.value,
                deleted_at=None,
                revision=view.media.revision.value,
                projection_revision=Number.ONE,
            )
        )
        await self.session.flush()
        await self.variations.save_group(view.group)
        await self.variations.save_membership(view.membership)
        await self.save_upload(
            replace(
                upload,
                state=UploadState.PUBLISHED,
                claimed_until=None,
                outcome_media_id=view.media.id,
            )
        )
        await self._schedule_cleanup(upload.object_key, now)
        await self.session.execute(
            update(CleanupRow)
            .where(CleanupRow.object_key == view.object_key)
            .values(completed_at=now)
        )
        payload: JsonObject = {
            "media_id": str(view.media.id),
            "file_id": str(view.media.original.file.id),
            "sha256": view.media.original.file.sha256.value,
            "size_bytes": view.media.original.file.size_bytes,
            "media_kind": view.media.original.kind.value,
            "metadata": view.metadata,
            "object_key": view.object_key,
            "source_revision": view.media.revision.value,
            "occurred_at": now.isoformat(),
        }
        enqueue(self.session, DeliveryTarget.PUBLISHED, payload, now)
        enqueue(self.session, DeliveryTarget.SEARCH_STATE, media_payload(view), now)
        return PublicationOutcome(media_id=view.media.id, duplicate=False)

    async def get_media(self, media_id: UUID, *, lock: bool = False) -> MediaView | None:
        """Получить действующий снимок."""
        query = self._query().where(MediaRow.id == media_id)
        if lock:
            query = query.with_for_update(of=MediaRow)
        views = await self._load(query)
        return views[0] if views else None

    async def resolve_batch(self, media_ids: tuple[UUID, ...]) -> tuple[MediaView, ...]:
        """Получить актуальные публикации одним SQL-пакетом."""
        return await self._load(self._query().where(MediaRow.id.in_(media_ids)))

    async def by_hash(self, digest: Sha256) -> MediaView | None:
        """Найти точный дубль среди живых публикаций."""
        views = await self._load(self._query().where(MediaRow.sha256 == digest.value))
        return views[0] if views else None

    @staticmethod
    def _query() -> Select[tuple[MediaRow, MembershipRow, GroupRow]]:
        """Создать запрос действующего эпизода публикации."""
        return (
            select(MediaRow, MembershipRow, GroupRow)
            .join(
                MembershipRow,
                and_(MembershipRow.media_id == MediaRow.id, MembershipRow.ended_at.is_(None)),
            )
            .join(GroupRow, GroupRow.id == MembershipRow.group_id)
            .where(MediaRow.state == MediaState.PUBLISHED)
        )

    async def _load(
        self, query: Select[tuple[MediaRow, MembershipRow, GroupRow]]
    ) -> tuple[MediaView, ...]:
        """Собрать страницу с единственным пакетным запросом признаков."""
        rows = (await self.session.execute(query.execution_options(populate_existing=True))).all()
        identifiers = [row[0].id for row in rows]
        features = await self.session.execute(
            select(
                FeatureRow.media_id,
                FeatureRow.kind,
                FeatureRow.cycle_id,
                FeatureRow.generation,
                FeatureRow.state,
                FeatureRow.feature_version,
                case((FeatureRow.kind == "preview", FeatureRow.result), else_=None),
            ).where(FeatureRow.media_id.in_(identifiers))
        )
        grouped: dict[UUID, list[JsonObject]] = {}
        for media_id, kind, cycle_id, generation, state, version, result in features:
            grouped.setdefault(media_id, []).append(
                {
                    "kind": kind,
                    "state": state,
                    "cycle_id": str(cycle_id),
                    "generation": generation,
                    "feature_version": version,
                    "result": result,
                }
            )
        return tuple(
            MediaView(
                media=mapping.media(media),
                object_key=media.object_key,
                membership=mapping.membership(member),
                group=mapping.group(group),
                metadata=media.metadata_payload,
                features=tuple(grouped.get(media.id, ())),
                projection_revision=media.projection_revision,
            )
            for media, member, group in rows
        )

    async def feed(self, *, before: FeedCursor | None, limit: int) -> tuple[MediaView, ...]:
        """Получить окно ленты."""
        query = self._query()
        if before is not None:
            condition = MediaRow.published_at < before.published_at
            if before.media_id is not None:
                condition = or_(
                    condition,
                    and_(
                        MediaRow.published_at == before.published_at, MediaRow.id > before.media_id
                    ),
                )
            query = query.where(condition)
        return await self._load(
            query.order_by(MediaRow.published_at.desc(), MediaRow.id).limit(limit)
        )

    async def group(self, group_id: UUID, *, offset: int, limit: int) -> GroupView | None:
        """Разрешить цепочку преемников и получить окно участников."""
        # Цепочка слияния разрешается одним рекурсивным запросом без глубины Python.
        result = await self.session.scalar(
            text(
                "WITH RECURSIVE chain AS (SELECT id, successor_id, ARRAY[id] AS path "
                "FROM catalog_groups WHERE id=:id "
                "UNION ALL SELECT g.id,g.successor_id,c.path||g.id FROM catalog_groups g "
                "JOIN chain c "
                "ON g.id=c.successor_id WHERE NOT g.id=ANY(c.path)) "
                "SELECT id FROM chain WHERE successor_id IS NULL LIMIT 1"
            ),
            {"id": group_id},
        )
        if result is None:
            return None
        row = await self.session.get(GroupRow, result)
        if row is None or row.state != GroupState.ACTIVE:
            return None
        items = await self._load(
            self._query()
            .where(GroupRow.id == row.id)
            .order_by(MediaRow.published_at, MediaRow.id)
            .offset(offset)
            .limit(limit)
        )
        cover = await self.session.scalar(
            select(MediaRow.id)
            .join(MembershipRow, MembershipRow.media_id == MediaRow.id)
            .where(
                MembershipRow.group_id == row.id,
                MembershipRow.ended_at.is_(None),
                MediaRow.state == MediaState.PUBLISHED,
            )
            .order_by(
                case(
                    {key.value: value.value for key, value in COVER_PRIORITIES.items()},
                    value=MediaRow.kind,
                ),
                MediaRow.published_at,
                MediaRow.id,
            )
            .limit(Number.ONE)
        )
        return GroupView(group=mapping.group(row), items=items, cover_media_id=cover)

    async def remove_media(self, view: MediaView, *, now: datetime) -> None:
        """Сохранить согласованное удаление и задания очистки."""
        await self.session.execute(
            update(MediaRow)
            .where(MediaRow.id == view.media.id)
            .values(
                state=view.media.state.value,
                deleted_at=view.media.deleted_at,
                revision=view.media.revision.value,
                projection_revision=MediaRow.projection_revision + Number.ONE,
            )
        )
        await self.variations.save_group(view.group)
        await self.variations.save_membership(view.membership)
        await self.variations.obsolete(
            group_ids=(view.group.id,), membership_ids=(view.membership.id,), now=now
        )
        await self.variations.changed((view.group.id,), now=now)
        await self._schedule_cleanup(view.object_key, now)
        previews = await self.session.scalars(
            select(FeatureRow).where(
                FeatureRow.media_id == view.media.id, FeatureRow.kind == "preview"
            )
        )
        for feature in previews:
            if feature.result is not None and isinstance(feature.result.get("object_key"), str):
                await self._schedule_cleanup(str(feature.result["object_key"]), now)
        await self.session.execute(
            update(FeatureRow).where(FeatureRow.media_id == view.media.id).values(result=None)
        )
        await self.session.execute(
            delete(OutboxRow).where(OutboxRow.payload["media_id"].astext == str(view.media.id))
        )
        enqueue(
            self.session,
            DeliveryTarget.DELETED,
            {"media_id": str(view.media.id), "occurred_at": now.isoformat()},
            now,
        )
        enqueue(
            self.session,
            DeliveryTarget.SEARCH_STATE,
            {
                "media_id": str(view.media.id),
                "active": False,
                "revision": view.projection_revision + Number.ONE,
                "source_revision": view.media.revision.value,
                "group_id": str(view.group.id),
                "group_revision": view.group.revision.value,
                "media_kind": view.media.original.kind.value,
                "published_at": view.media.published_at.isoformat(),
            },
            now,
        )

    async def account_deleted(
        self, *, event_id: UUID, user_id: UUID, delete_media: bool, now: datetime
    ) -> None:
        """Сохранить обезличенный tombstone и закрыть загрузки аккаунта."""
        received = await self.session.scalar(
            insert(InboxRow)
            .values(id=event_id, received_at=now)
            .on_conflict_do_nothing()
            .returning(InboxRow.id)
        )
        if received is None:
            return
        await self.session.execute(
            insert(AccountTombstoneRow)
            .values(user_id=user_id, delete_media=delete_media, deleted_at=now)
            .on_conflict_do_update(
                index_elements=[AccountTombstoneRow.user_id],
                set_={"delete_media": AccountTombstoneRow.delete_media | delete_media},
            )
        )
        await self.session.execute(
            update(UploadRow)
            .where(
                UploadRow.author_id == user_id,
                UploadRow.state.in_((UploadState.RESERVED, UploadState.PROCESSING)),
            )
            .values(state=UploadState.CANCELLED.value)
        )

    async def dismissals(self, user_id: UUID, media_id: UUID) -> tuple[tuple[UUID, int], ...]:
        """Получить личные отклонения актуальных составов."""
        rows = await self.session.execute(
            select(DismissalRow.group_id, DismissalRow.group_revision).where(
                DismissalRow.user_id == user_id, DismissalRow.media_id == media_id
            )
        )
        return tuple((group_id, revision) for group_id, revision in rows)

    async def deleted_accounts(self, *, limit: int) -> tuple[UUID, ...]:
        """Найти оставшиеся файлы аккаунтов, выбравших удаление."""
        rows = await self.session.scalars(
            select(MediaRow.id)
            .join(AccountTombstoneRow, AccountTombstoneRow.user_id == MediaRow.author_id)
            .where(
                AccountTombstoneRow.delete_media.is_(True), MediaRow.state == MediaState.PUBLISHED
            )
            .limit(limit)
        )
        return tuple(rows)

    async def accept_feature(self, update: FeatureUpdate, *, now: datetime) -> bool:
        """Применить результат только текущего поколения живого источника."""
        row = await self.session.scalar(
            select(MediaRow).where(MediaRow.id == update.media_id).with_for_update()
        )
        if (
            row is None
            or row.state != MediaState.PUBLISHED
            or row.revision != update.source_revision
        ):
            await self._discard_preview(update, ledger=None, now=now)
            return False
        ledger = await self.session.get(FeatureRow, (update.media_id, update.kind))
        if ledger is not None:
            if (
                update.generation == ledger.generation
                and update.feature_version != ledger.feature_version
            ):
                await self._discard_preview(update, ledger=ledger, now=now)
                return False
            if update.generation < ledger.generation or (
                update.generation == ledger.generation and update.cycle_id != ledger.cycle_id
            ):
                await self._discard_preview(update, ledger=ledger, now=now)
                return False
            if update.generation == ledger.generation and ledger.state != FeatureState.PENDING:
                if ledger.state != update.state or ledger.result != update.result:
                    await self._discard_preview(update, ledger=ledger, now=now)
                    return False
                return True
        # Первое событие нового поколения обязано обозначать начало цикла.
        if (
            ledger is None or update.generation > ledger.generation
        ) and update.state != FeatureState.PENDING:
            await self._discard_preview(update, ledger=ledger, now=now)
            return False
        if (
            ledger is not None
            and update.generation > ledger.generation
            and ledger.kind == "preview"
            and ledger.result is not None
        ):
            old_key = ledger.result.get("object_key")
            if isinstance(old_key, str):
                await self._schedule_cleanup(old_key, now)
        received = await self.session.scalar(
            insert(InboxRow)
            .values(id=update.event_id, received_at=now)
            .on_conflict_do_nothing()
            .returning(InboxRow.id)
        )
        if received is None:
            return True
        await self.session.merge(
            FeatureRow(
                media_id=update.media_id,
                kind=update.kind,
                cycle_id=update.cycle_id,
                generation=update.generation,
                state=update.state,
                feature_version=update.feature_version,
                result=update.result,
                updated_at=now,
            )
        )
        member = await self.session.scalar(
            select(MembershipRow).where(
                MembershipRow.media_id == row.id, MembershipRow.ended_at.is_(None)
            )
        )
        if member is None:
            return False
        group = await self.session.get(GroupRow, member.group_id)
        if group is None:
            return False
        enqueue(
            self.session,
            DeliveryTarget.SEARCH_FEATURE,
            {
                "media_id": str(row.id),
                "group_id": str(group.id),
                "group_revision": group.revision,
                "composition_revision": group.composition_revision,
                "revision": row.projection_revision,
                "published_at": row.published_at.isoformat(),
                "media_kind": row.kind,
                "kind": update.kind,
                "source_revision": update.source_revision,
                "cycle_id": str(update.cycle_id),
                "generation": update.generation,
                "state": update.state,
                "feature_version": update.feature_version,
                "result": update.result,
            },
            now,
        )
        return True

    async def _discard_preview(
        self, update: FeatureUpdate, *, ledger: FeatureRow | None, now: datetime
    ) -> None:
        """Убрать результат отклонённой попытки, сохранив живое превью."""
        if update.kind != "preview" or update.result is None:
            return
        key = update.result.get("object_key")
        current = (
            None if ledger is None or ledger.result is None else ledger.result.get("object_key")
        )
        if isinstance(key, str) and key != current:
            await self._schedule_cleanup(key, now)

    async def history(self, group_id: UUID, *, limit: int) -> tuple[JsonObject, ...]:
        """Получить решения, непосредственно затронувшие выбранную группу."""
        rows = await self.session.scalars(
            select(DecisionRow)
            .where(
                or_(
                    DecisionRow.payload["first_id"].astext == str(group_id),
                    DecisionRow.payload["second_id"].astext == str(group_id),
                    DecisionRow.payload["group_id"].astext == str(group_id),
                )
            )
            .order_by(DecisionRow.applied_at.desc(), DecisionRow.id)
            .limit(limit)
        )
        return tuple(row.payload for row in rows)

    async def reindex(
        self,
        *,
        after: UUID | None,
        limit: int,
        now: datetime,
        target: SearchIndexTarget = SearchIndexTarget.PRIMARY,
    ) -> UUID | None:
        """Восстановить поисковые проекции без изменения исходной публикации."""
        if target is SearchIndexTarget.SHADOW and not self.session.info.get(
            OutboxSetting.MIRROR_SEARCH
        ):
            raise ApplicationError(ErrorCode.INVALID_INPUT, "Индекс-кандидат не настроен.")
        state_target = (
            DeliveryTarget.SHADOW_STATE
            if target is SearchIndexTarget.SHADOW
            else DeliveryTarget.SEARCH_STATE
        )
        feature_target = (
            DeliveryTarget.SHADOW_FEATURE
            if target is SearchIndexTarget.SHADOW
            else DeliveryTarget.SEARCH_FEATURE
        )
        result = await self.session.scalar(
            text(
                "WITH page AS MATERIALIZED (SELECT m.* FROM catalog_media m "
                "WHERE CAST(:after AS uuid) IS NULL OR m.id>CAST(:after AS uuid) "
                "ORDER BY m.id LIMIT :limit), states AS MATERIALIZED ("
                "SELECT m.id, (m.state=:published AND NOT coalesce(a.delete_media,false)) "
                "AS active, jsonb_build_object("
                "'media_id', m.id, 'group_id', coalesce(g.id,m.id), "
                "'group_revision', coalesce(g.revision,:initial), "
                "'composition_revision', coalesce(g.composition_revision,:initial), "
                "'revision',m.projection_revision,'source_revision',m.revision, "
                "'published_at',m.published_at,'media_kind',m.kind,'kind',m.kind, "
                "'author_id',m.author_id,'sha256',m.sha256,'file_id',m.file_id, "
                "'active',m.state=:published AND NOT coalesce(a.delete_media,false)) AS payload "
                "FROM page m LEFT JOIN catalog_memberships membership "
                "ON membership.media_id=m.id AND membership.ended_at IS NULL "
                "LEFT JOIN catalog_groups g ON g.id=membership.group_id "
                "LEFT JOIN catalog_account_tombstones a ON a.user_id=m.author_id), "
                "projections AS (SELECT payload, CAST(:state_targets AS text[]) AS destinations "
                "FROM states "
                "UNION ALL SELECT s.payload || jsonb_build_object("
                "'kind',f.kind,'cycle_id',f.cycle_id,'generation',f.generation,"
                "'state',f.state,'feature_version',f.feature_version,'result',f.result), "
                "CAST(:feature_targets AS text[]) AS destinations FROM states s "
                "JOIN catalog_features f ON f.media_id=s.id WHERE s.active), "
                "events AS MATERIALIZED (SELECT gen_random_uuid() AS event_id, payload, target "
                "FROM projections CROSS JOIN unnest(CAST(destinations AS text[])) "
                "destinations(target)), created AS (INSERT INTO catalog_outbox "
                "(id,target,payload,created_at,ready_at,attempts,"
                "claim_id,claimed_until,delivered_at) "
                "SELECT event_id,target,payload||jsonb_build_object('event_id',event_id), "
                ":now,:now,:attempts,NULL,NULL,NULL FROM events RETURNING id) "
                "SELECT id FROM page ORDER BY id DESC LIMIT :initial"
            ),
            {
                "after": after,
                "limit": limit,
                "now": now,
                "published": MediaState.PUBLISHED.value,
                "initial": Number.ONE,
                "attempts": Number.ZERO,
                "state_targets": [
                    value.value for value in delivery_targets(self.session, state_target)
                ],
                "feature_targets": [
                    value.value for value in delivery_targets(self.session, feature_target)
                ],
            },
        )
        return cast(UUID | None, result)

    async def _schedule_cleanup(self, object_key: str, now: datetime) -> None:
        """Сохранить идемпотентную задачу физического удаления."""
        await self.session.execute(
            insert(CleanupRow)
            .values(id=uuid4(), object_key=object_key, created_at=now, completed_at=None)
            .on_conflict_do_update(
                index_elements=[CleanupRow.object_key],
                set_={"created_at": now, "completed_at": None},
            )
        )

    async def cleanup(self, *, now: datetime, limit: int) -> tuple[PendingCleanup, ...]:
        """Получить ограниченную страницу готовой очистки."""
        await self.session.execute(
            update(UploadRow)
            .where(
                UploadRow.expires_at <= now,
                UploadRow.state.in_((UploadState.RESERVED, UploadState.PROCESSING)),
            )
            .values(state=UploadState.CANCELLED.value, claimed_until=None)
        )
        rows = await self.session.scalars(
            select(CleanupRow)
            .where(CleanupRow.created_at <= now, CleanupRow.completed_at.is_(None))
            .order_by(CleanupRow.created_at)
            .limit(limit)
        )
        return tuple(PendingCleanup(id=row.id, object_key=row.object_key) for row in rows)

    async def finish_cleanup(self, cleanup_id: UUID) -> None:
        """Записать подтверждённое S3-удаление."""
        await self.session.execute(
            update(CleanupRow).where(CleanupRow.id == cleanup_id).values(completed_at=func.now())
        )


class PostgresCatalogFactory(ICatalogUnitOfWorkFactory, ICatalogHealth):
    """Фабрика unit of work и readiness."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        *,
        mirror_search: bool = False,
    ) -> None:
        """Сохранить инфраструктурные ресурсы.

        Args:
            engine: Пул PostgreSQL.
            sessions: Фабрика транзакций.
            mirror_search: Включена независимая доставка в индекс-кандидат.
        """
        self.engine = engine
        self.sessions = sessions
        self.mirror_search = mirror_search

    def open(self) -> PostgresCatalogUnitOfWork:
        """Создать независимую транзакцию."""
        return PostgresCatalogUnitOfWork(self.sessions, mirror_search=self.mirror_search)

    async def ready(self) -> bool:
        """Проверить доступность PostgreSQL."""
        async with self.engine.connect() as connection:
            return bool(await connection.scalar(text("SELECT 1")) == Number.ONE)
