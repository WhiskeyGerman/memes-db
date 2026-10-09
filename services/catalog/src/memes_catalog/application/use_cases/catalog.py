"""Сценарии публикации и удаления; зависимости направлены только внутрь."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from memes_catalog.application.dto.catalog import (
    Actor,
    ActorRole,
    CatalogDefault,
    FeatureUpdate,
    FeedCursor,
    FeedPage,
    GroupView,
    InspectedMedia,
    MediaView,
    PublicationOutcome,
    SearchIndexTarget,
    Upload,
    UploadState,
    UploadTicket,
)
from memes_catalog.application.ports.catalog import ICatalogUnitOfWorkFactory
from memes_catalog.application.ports.clock import IClock
from memes_catalog.application.ports.resources import (
    IIdentityClient,
    IMediaInspector,
    IObjectStorage,
    IRestDelivery,
)
from memes_catalog.application.use_cases.features import validate_feature
from memes_catalog.domain.groups import (
    GroupId,
    GroupMembership,
    MembershipEnd,
    MembershipId,
    VariationGroup,
)
from memes_catalog.domain.media import AuthorId, Media, MediaId, MediaPolicy
from memes_kernel.errors import DomainError
from memes_kernel.values import Number, Sha256
from memes_platform.application.dto.json import JsonObject, JsonValue
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.application.ports.deletion_journal import IDeletionJournal


class CatalogUseCases:
    """Оркестрация устойчивого жизненного цикла публикации."""

    def __init__(
        self,
        factory: ICatalogUnitOfWorkFactory,
        storage: IObjectStorage,
        inspector: IMediaInspector,
        identity: IIdentityClient,
        delivery: IRestDelivery,
        *,
        policy: MediaPolicy,
        clock: IClock,
        journal: IDeletionJournal,
        concurrent_limit: int,
        daily_limit: int,
        storage_limit: int,
        free_percent: int,
        queue_limit: int,
        upload_seconds: int = CatalogDefault.UPLOAD_SECONDS,
        claim_seconds: int = CatalogDefault.CLAIM_SECONDS,
    ) -> None:
        """Сохранить зависимости и продуктовые пределы.

        Args:
            factory: Транзакции.
            storage: Хранилище объектов.
            inspector: Проверка содержимого.
            identity: Авторизация и публичные сведения.
            delivery: Доступность очереди обработки.
            policy: Правила медиа.
            clock: Серверное время длительного сценария.
            journal: Внешний durable журнал удаления.
            concurrent_limit: Одновременные передачи автора.
            daily_limit: Публикации за сутки.
            storage_limit: Доступная ёмкость.
            free_percent: Резерв свободного места.
            queue_limit: Допустимый backlog.
            upload_seconds: Срок передачи.
            claim_seconds: Право владения приёмкой.
        """
        self.factory = factory
        self.storage = storage
        self.inspector = inspector
        self.identity = identity
        self.delivery = delivery
        self.policy = policy
        self.clock = clock
        self.journal = journal
        self.concurrent_limit = concurrent_limit
        self.daily_limit = daily_limit
        self.storage_limit = storage_limit
        self.free_percent = free_percent
        self.queue_limit = queue_limit
        self.upload_seconds = upload_seconds
        self.claim_seconds = claim_seconds

    async def begin(self, *, actor: Actor, size_bytes: int, now: datetime) -> UploadTicket:
        """Зарезервировать квоты и ограниченную передачу в S3.

        Args:
            actor: Авторизованный автор.
            size_bytes: Объявленный верхний предел размера.
            now: Серверное время.
        """
        if not Number.ZERO < size_bytes <= self.policy.maximum_bytes:
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        if await self.delivery.pending_tasks() >= self.queue_limit:
            raise ApplicationError(ErrorCode.UNAVAILABLE)
        upload_id, file_id, media_id = uuid4(), uuid4(), uuid4()
        upload = Upload(
            id=upload_id,
            author_id=actor.user_id,
            file_id=file_id,
            media_id=media_id,
            object_key=f"staging/{upload_id}/{file_id}",
            size_bytes=size_bytes,
            created_at=now,
            expires_at=now + timedelta(seconds=self.upload_seconds),
        )
        async with self.factory.open() as uow:
            await uow.reserve(
                upload,
                concurrent_limit=self.concurrent_limit,
                daily_limit=self.daily_limit,
                storage_limit=self.storage_limit,
                free_percent=self.free_percent,
            )
            await uow.commit()
        url, fields = await self.storage.upload_ticket(
            upload.object_key, maximum_bytes=size_bytes, expires_seconds=self.upload_seconds
        )
        return UploadTicket(upload=upload, url=url, fields=fields)

    async def complete(self, *, actor: Actor, upload_id: UUID, now: datetime) -> PublicationOutcome:
        """Принять проверенные байты и немедленно опубликовать singleton.

        Args:
            actor: Автор передачи.
            upload_id: Передача.
            now: Серверное время.
        """
        async with self.factory.open() as uow:
            upload = await uow.upload(upload_id, lock=True)
            if upload is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            if upload.author_id != actor.user_id:
                raise ApplicationError(ErrorCode.FORBIDDEN)
            if upload.outcome_media_id is not None:
                return PublicationOutcome(
                    media_id=upload.outcome_media_id,
                    duplicate=upload.state is UploadState.DUPLICATE,
                )
            if upload.expires_at <= now or upload.state in (
                UploadState.CANCELLED,
                UploadState.REJECTED,
            ):
                raise ApplicationError(ErrorCode.CONFLICT)
            if upload.claimed_until is not None and upload.claimed_until > now:
                raise ApplicationError(ErrorCode.CONFLICT, "Приёмка этой передачи уже выполняется.")
            claimed = replace(
                upload,
                state=UploadState.PROCESSING,
                claimed_until=now + timedelta(seconds=self.claim_seconds),
                claim_id=uuid4(),
            )
            await uow.save_upload(claimed)
            await uow.prepare_object(
                f"originals/{claimed.media_id}/{claimed.file_id}/{claimed.claim_id}",
                ready_at=claimed.expires_at,
            )
            await uow.commit()
        try:
            inspected = await self.inspector.inspect_object(
                claimed.object_key, file_id=claimed.file_id, maximum_bytes=claimed.size_bytes
            )
            media = Media.publish(
                media_id=MediaId(claimed.media_id),
                author_id=AuthorId(actor.user_id),
                original=inspected.original,
                policy=self.policy,
                now=now,
            )
            group = VariationGroup(id=GroupId(uuid4()), created_at=now)
            member = GroupMembership(
                id=MembershipId(uuid4()), media_id=media.id, group_id=group.id, joined_at=now
            )
            destination = f"originals/{media.id}/{media.original.file.id}/{claimed.claim_id}"
            await self.storage.copy(
                claimed.object_key,
                destination,
                expected_etag=inspected.etag,
                media_format=inspected.original.format,
            )
            view = MediaView(
                media=media,
                group=group,
                membership=member,
                object_key=destination,
                metadata=inspected.metadata,
            )
            async with self.factory.open() as uow:
                current = await uow.upload(upload_id, lock=True)
                if (
                    current is None
                    or current.claim_id != claimed.claim_id
                    or current.state is not UploadState.PROCESSING
                    or current.expires_at <= self.clock.now()
                    or current.claimed_until is None
                    or current.claimed_until <= self.clock.now()
                ):
                    raise ApplicationError(ErrorCode.STALE_STATE)
                outcome = await uow.publish(current, view, now=now)
                await uow.commit()
                return outcome
        except (ApplicationError, DomainError, OSError) as error:
            async with self.factory.open() as uow:
                current = await uow.upload(upload_id, lock=True)
                if (
                    current is not None
                    and current.claim_id == claimed.claim_id
                    and current.outcome_media_id is None
                ):
                    await uow.save_upload(
                        replace(
                            current,
                            state=UploadState.RESERVED
                            if isinstance(error, ApplicationError)
                            and error.code is ErrorCode.UNAVAILABLE
                            else UploadState.REJECTED,
                            claimed_until=None,
                        )
                    )
                    await uow.commit()
            raise

    async def cancel(self, *, actor: Actor, upload_id: UUID) -> None:
        """Отменить незавершённую передачу.

        Args:
            actor: Автор.
            upload_id: Передача.
        """
        async with self.factory.open() as uow:
            upload = await uow.upload(upload_id, lock=True)
            if upload is None:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            if upload.author_id != actor.user_id:
                raise ApplicationError(ErrorCode.FORBIDDEN)
            if upload.outcome_media_id is not None:
                raise ApplicationError(ErrorCode.CONFLICT)
            await uow.save_upload(replace(upload, state=UploadState.CANCELLED, claimed_until=None))
            await uow.commit()

    async def visible(self, views: tuple[MediaView, ...]) -> tuple[MediaView, ...]:
        """Скрыть выбранные к удалению файлы сразу после удаления аккаунта.

        Args:
            views: Снимки публикаций.
        """
        if not views:
            return ()
        authors = {
            author.user_id: author
            for author in await self.identity.authors(
                tuple(dict.fromkeys(view.media.author_id for view in views))
            )
        }
        return tuple(
            replace(view, author=authors[view.media.author_id])
            for view in views
            if view.media.author_id in authors and not authors[view.media.author_id].delete_media
        )

    async def get(self, media_id: UUID) -> MediaView:
        """Получить доступную публичную публикацию.

        Args:
            media_id: Идентификатор.
        """
        async with self.factory.open() as uow:
            view = await uow.get_media(media_id)
        found = await self.visible(() if view is None else (view,))
        if not found:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        return found[Number.ZERO]

    async def feed(self, *, before: FeedCursor | None, limit: int) -> FeedPage:
        """Получить устойчивое видимое окно без пропуска одинакового времени.

        Args:
            before: Курсор времени и идентичности.
            limit: Размер страницы.
        """
        cursor = before
        visible: list[MediaView] = []
        scanned: int = Number.ZERO
        while len(visible) < limit and scanned < CatalogDefault.FEED_SCAN_LIMIT:
            batch_size = min(limit - len(visible), CatalogDefault.FEED_SCAN_LIMIT - scanned)
            async with self.factory.open() as uow:
                views = await uow.feed(before=cursor, limit=batch_size)
            if not views:
                return FeedPage(items=tuple(visible), next_cursor=None)
            visible.extend(await self.visible(views))
            scanned += len(views)
            last = views[-Number.ONE].media
            cursor = FeedCursor(published_at=last.published_at, media_id=last.id)
            if len(views) < batch_size:
                return FeedPage(items=tuple(visible), next_cursor=None)
        return FeedPage(items=tuple(visible), next_cursor=cursor)

    async def group(self, *, group_id: UUID, offset: int, limit: int) -> GroupView:
        """Получить текущий состав выбранной группы.

        Args:
            group_id: Группа или прежний адрес.
            offset: Смещение.
            limit: Размер окна.
        """
        async with self.factory.open() as uow:
            view = await uow.group(group_id, offset=offset, limit=limit)
        if view is None:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        visible = await self.visible(view.items)
        async with self.factory.open() as uow:
            cover = (
                None if view.cover_media_id is None else await uow.get_media(view.cover_media_id)
            )
        visible_cover = await self.visible(() if cover is None else (cover,))
        return replace(
            view,
            items=visible,
            cover_media_id=visible_cover[Number.ZERO].media.id if visible_cover else None,
        )

    async def suggestions(self, *, actor: Actor, media_id: UUID, limit: int) -> JsonObject:
        """Отфильтровать собственную группу и личные отклонения.

        Args:
            actor: Участник.
            media_id: Источник предложения.
            limit: Размер окна.
        """
        view = await self.get(media_id)
        data = await self.delivery.suggestions(media_id, limit=limit)
        async with self.factory.open() as uow:
            dismissed = set(await uow.dismissals(actor.user_id, media_id))
        items = cast(list[JsonObject], data.get("items", []))
        visible: list[JsonValue] = []
        for item in items:
            group_id = UUID(str(item["group_id"]))
            revision = int(str(item["group_revision"]))
            if group_id != view.group.id and (group_id, revision) not in dismissed:
                visible.append(item)
        return {**data, "items": visible}

    async def delete(self, *, media_id: UUID, actor: Actor | None, now: datetime) -> None:
        """Удалить одну публикацию, не затрагивая другие вариации.

        Args:
            media_id: Удаляемое медиа.
            actor: Автор, оператор либо доверенная задача удаления аккаунта.
            now: Серверное время.
        """
        async with self.factory.open() as uow:
            initial = await uow.get_media(media_id)
        if initial is None:
            return
        if (
            actor is not None
            and actor.user_id != initial.media.author_id
            and ActorRole.OPERATOR not in actor.roles
        ):
            raise ApplicationError(ErrorCode.FORBIDDEN)
        await self.journal.record(
            {
                "event_id": str(uuid5(NAMESPACE_URL, f"memes-db/delete/media/{media_id}")),
                "kind": "media",
                "media_id": str(media_id),
                "file_id": str(initial.media.original.file.id),
                "object_key": initial.object_key,
                "occurred_at": now.isoformat(),
            }
        )
        for _attempt in range(CatalogDefault.GROUP_CHANGE_RETRIES):
            async with self.factory.open() as uow:
                initial = await uow.get_media(media_id)
                if initial is None:
                    return
                await uow.variations.lock_groups((initial.group.id,))
                view = await uow.get_media(media_id, lock=True)
                if view is None:
                    return
                if view.group.id != initial.group.id:
                    continue
                changed = replace(
                    view,
                    media=view.media.delete(now=now),
                    group=view.group.remove_member(),
                    membership=view.membership.close(reason=MembershipEnd.MEDIA_DELETED, now=now),
                )
                await uow.remove_media(changed, now=now)
                await uow.commit()
                return
        raise ApplicationError(ErrorCode.STALE_STATE)

    async def account_deleted(
        self, *, event_id: UUID, user_id: UUID, delete_media: bool, now: datetime
    ) -> None:
        """Применить доверенное авторизованное намерение удаления аккаунта.

        Args:
            event_id: Идентичность события.
            user_id: Автор.
            delete_media: Выбор удаления файлов.
            now: Время намерения.
        """
        await self.journal.record(
            {
                "event_id": str(event_id),
                "kind": "account",
                "user_id": str(user_id),
                "delete_media": delete_media,
                "occurred_at": now.isoformat(),
            }
        )
        async with self.factory.open() as uow:
            await uow.account_deleted(
                event_id=event_id, user_id=user_id, delete_media=delete_media, now=now
            )
            await uow.commit()
        await self.maintenance(now=self.clock.now(), limit=CatalogDefault.OUTBOX_BATCH)

    async def inspect_private(
        self, *, object_key: str, file_id: UUID, maximum_bytes: int
    ) -> InspectedMedia:
        """Проверить приватный поисковый образец.

        Args:
            object_key: Путь образца внутри ограниченного пространства.
            file_id: Идентичность временного файла.
            maximum_bytes: Предел размера.
        """
        if not object_key.startswith("samples/"):
            raise ApplicationError(ErrorCode.FORBIDDEN)
        inspected = await self.inspector.inspect_object(
            object_key, file_id=file_id, maximum_bytes=min(maximum_bytes, self.policy.maximum_bytes)
        )
        self.policy.validate(inspected.original)
        return inspected

    async def resolve(self, media_ids: tuple[UUID, ...]) -> tuple[MediaView, ...]:
        """Получить пакет доступных актуальных проекций для соседнего сервиса.

        Args:
            media_ids: Ограниченный набор идентичностей.
        """
        async with self.factory.open() as uow:
            views = await uow.resolve_batch(tuple(dict.fromkeys(media_ids)))
        return await self.visible(views)

    async def by_hash(self, digest: Sha256) -> MediaView:
        """Найти доступный точный дубль без раскрытия удалённого автора.

        Args:
            digest: Хэш переданного оригинала.
        """
        async with self.factory.open() as uow:
            view = await uow.by_hash(digest)
        found = await self.visible(() if view is None else (view,))
        if not found:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        return found[Number.ZERO]

    async def accept_feature(self, update: FeatureUpdate, *, now: datetime) -> bool:
        """Проверить и атомарно принять одно поколение результата.

        Args:
            update: Межконтекстный пакет.
            now: Серверное время.
        """
        validate_feature(update)
        async with self.factory.open() as uow:
            accepted = await uow.accept_feature(update, now=now)
            await uow.commit()
        return accepted

    async def history(self, *, group_id: UUID, limit: int) -> tuple[JsonObject, ...]:
        """Получить обезличенную историю решений группы.

        Args:
            group_id: Выбранная группа.
            limit: Размер ограниченной страницы.
        """
        async with self.factory.open() as uow:
            return await uow.history(group_id, limit=limit)

    async def replay_deletions(self, *, since: datetime | None = None) -> int:
        """Повторить внешний журнал до открытия восстановленной системы.

        Args:
            since: Необязательная граница независимо сохранённого журнала.
        """
        applied: int = Number.ZERO
        async for event in self.journal.records(since=since):
            kind = event.get("kind")
            occurred_at = datetime.fromisoformat(str(event["occurred_at"]))
            if occurred_at.tzinfo is None:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            if kind == "account":
                if not isinstance(event.get("delete_media"), bool):
                    raise ApplicationError(ErrorCode.INVALID_INPUT)
                await self.account_deleted(
                    event_id=UUID(str(event["event_id"])),
                    user_id=UUID(str(event["user_id"])),
                    delete_media=cast(bool, event["delete_media"]),
                    now=occurred_at,
                )
            elif kind == "media":
                await self.delete(
                    media_id=UUID(str(event["media_id"])), actor=None, now=occurred_at
                )
            else:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            applied += Number.ONE
        return applied

    async def reindex_search(
        self,
        *,
        batch_size: int,
        target: SearchIndexTarget = SearchIndexTarget.PRIMARY,
    ) -> int:
        """Поставить полный текущий индекс в outbox ограниченными транзакциями.

        Args:
            batch_size: Размер одной SQL-транзакции.
            target: Отдельный индекс-кандидат либо текущий индекс.
        """
        after: UUID | None = None
        batches: int = Number.ZERO
        while True:
            async with self.factory.open() as uow:
                after = await uow.reindex(
                    after=after, limit=batch_size, now=self.clock.now(), target=target
                )
                await uow.commit()
            if after is None:
                return batches
            batches += Number.ONE

    async def maintenance(self, *, now: datetime, limit: int) -> int:
        """Удалить выбранные файлы аккаунтов и просроченные объекты.

        Args:
            now: Серверное время.
            limit: Ограничение пакета.
        """
        async with self.factory.open() as uow:
            media_ids = await uow.deleted_accounts(limit=limit)
        for media_id in media_ids:
            await self.delete(media_id=media_id, actor=None, now=now)
        async with self.factory.open() as uow:
            cleanup = await uow.cleanup(now=now, limit=limit)
            await uow.commit()
        for pending in cleanup:
            await self.storage.delete(pending.object_key)
            async with self.factory.open() as uow:
                await uow.finish_cleanup(pending.id)
                await uow.commit()
        return len(cleanup) + len(media_ids)
