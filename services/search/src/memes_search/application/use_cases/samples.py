"""Приём, приватное чтение и быстрая отмена временных образцов."""

import asyncio
from collections.abc import AsyncIterable
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from enum import IntEnum
from uuid import UUID, uuid4

from memes_kernel.values import Number
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import EmbeddingVersion, ProjectionState
from memes_search.application.dto.samples import (
    PrivateSample,
    SampleFeatureChange,
    SampleResultSource,
    SampleState,
)
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.catalog import ICatalogReader
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.inspection import IMediaInspector
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.search_index import ISearchIndex
from memes_search.application.ports.storage import IPrivateStorage


class UploadLimit(IntEnum):
    """Дедлайн приёма и запас для отменённого S3 I/O."""

    SECONDS = 300


class SampleService:
    """Приватный образец никогда не создаёт публикацию или автора."""

    def __init__(
        self,
        *,
        repository: ISampleRepository,
        admission: ISampleAdmission,
        storage: IPrivateStorage,
        inspector: IMediaInspector,
        catalog: ICatalogReader,
        clock: IClock,
        policy: SearchSessionPolicy,
        index: ISearchIndex,
        embedding_version: EmbeddingVersion,
        index_target: IndexTarget = IndexTarget.PRIMARY,
    ) -> None:
        """Получить порты внешнего состояния и правила срока жизни."""
        self._repository = repository
        self._admission = admission
        self._storage = storage
        self._inspector = inspector
        self._catalog = catalog
        self._clock = clock
        self._policy = policy
        self._index = index
        self._embedding_version = embedding_version
        self._index_target = index_target

    async def upload(self, owner_id: UUID, chunks: AsyncIterable[bytes]) -> PrivateSample:
        """Принять ограниченный поток и атомарно поставить REST-доставку."""
        now = self._clock.now()
        sample_id, file_id = uuid4(), uuid4()
        sample = PrivateSample(
            id=sample_id,
            embedding_version=self._embedding_version,
            callback_target=self._index_target,
            owner_id=owner_id,
            object_key=f"samples/{sample_id}/{file_id}",
            created_at=now,
            expires_at=now + self._policy.lifetime,
        )
        await self._admission.reserve(
            sample_id=sample.id,
            owner_id=sample.owner_id,
            created_at=sample.created_at,
            expires_at=sample.expires_at,
        )
        try:
            await self._repository.reserve(sample, self._policy.active_samples_per_user)
        except (SearchError, asyncio.CancelledError):
            await self._admission.release(sample.id)
            raise
        try:
            await self._repository.schedule_cleanup(
                sample.object_key, sample.expires_at + timedelta(seconds=UploadLimit.SECONDS)
            )
            async with asyncio.timeout(UploadLimit.SECONDS):
                file = await self._storage.put(
                    sample.object_key, file_id, chunks, self._policy.maximum_sample_bytes
                )
            kind = await self._inspector.inspect(sample.object_key, file)
            exact = await self._catalog.by_hash(file.sha256)
            sample = replace(
                sample,
                file=file,
                kind=kind,
                state=SampleState.QUEUED,
                exact_media_id=exact.media_id if exact is not None else None,
            )
            cached = await self._index.features_for(exact.media_id) if exact is not None else None
            if cached is not None:
                cached = replace(
                    cached,
                    vectors=tuple(
                        vector
                        for vector in cached.vectors
                        if vector.version == self._embedding_version
                    ),
                )
            await self._repository.save(sample, enqueue=cached is None or not cached.vectors)
            if cached is not None and (cached.text or cached.vectors):
                await self._repository.apply_features(
                    SampleFeatureChange(
                        event_id=uuid4(),
                        sample_id=sample.id,
                        kind=SampleResultSource.CACHED,
                        generation=Number.ONE,
                        state=ProjectionState.AVAILABLE,
                        text=cached.text,
                        vectors=cached.vectors,
                        sample_state=SampleState.READY,
                    ),
                    self._clock.now(),
                )
                sample = replace(sample, state=SampleState.READY)
            if sample.state is SampleState.READY:
                await self._admission.release(sample.id)
            return sample
        except asyncio.CancelledError:
            await self._failed_upload(sample, SearchErrorCode.UNAVAILABLE)
            raise
        except TimeoutError as error:
            await self._failed_upload(sample, SearchErrorCode.UNAVAILABLE)
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except SearchError as error:
            await self._failed_upload(sample, error.code)
            raise

    async def _failed_upload(self, sample: PrivateSample, code: SearchErrorCode) -> None:
        """Стереть поздний файл даже после стирания аккаунта."""
        now = self._clock.now()
        try:
            try:
                await self._repository.schedule_cleanup(sample.object_key, now)
            finally:
                with suppress(SearchError):
                    await self._storage.delete(sample.object_key)
                try:
                    await self._repository.save(
                        replace(sample, state=SampleState.FAILED, failure=code, expires_at=now)
                    )
                except SearchError as error:
                    if error.code not in {SearchErrorCode.FORBIDDEN, SearchErrorCode.NOT_FOUND}:
                        raise
        finally:
            await self._admission.release(sample.id)

    async def get(self, sample_id: UUID, owner_id: UUID) -> PrivateSample:
        """Запретить чтение чужого, отменённого или просроченного файла."""
        sample = await self._repository.get(sample_id)
        if sample is None or sample.expires_at <= self._clock.now():
            raise SearchError(SearchErrorCode.NOT_FOUND)
        if sample.owner_id != owner_id:
            raise SearchError(SearchErrorCode.FORBIDDEN)
        return sample

    async def cancel(self, sample_id: UUID, owner_id: UUID) -> None:
        """Сразу закрыть образец и надёжно поставить отмену координатору."""
        sample = await self._repository.get(sample_id)
        if sample is None:
            return
        if sample.owner_id != owner_id:
            raise SearchError(SearchErrorCode.FORBIDDEN)
        if sample.state is not SampleState.CANCELLED:
            await self._repository.save(
                replace(sample, state=SampleState.CANCELLED, expires_at=self._clock.now()),
                enqueue=True,
            )
        await self._storage.delete(sample.object_key)
        await self._admission.release(sample.id)

    async def download(self, sample_id: UUID, owner_id: UUID) -> str:
        """Выдать подписанную ссылку только владельцу живого образца."""
        sample = await self.get(sample_id, owner_id)
        if sample.state is SampleState.CANCELLED or sample.file is None:
            raise SearchError(SearchErrorCode.NOT_FOUND)
        seconds = min(
            self._policy.download_seconds,
            int((sample.expires_at - self._clock.now()).total_seconds()),
        )
        if seconds < Number.ONE:
            raise SearchError(SearchErrorCode.NOT_FOUND)
        return await self._storage.download_url(sample.object_key, seconds)
