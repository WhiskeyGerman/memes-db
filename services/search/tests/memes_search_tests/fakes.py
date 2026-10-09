"""Изолированные адаптеры портов для проверки сценариев, не production-хранилища."""

import asyncio
from collections.abc import AsyncIterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memes_kernel.values import FileRef, Sha256
from memes_search.application.dto.execution import SearchRequest
from memes_search.application.dto.identity import Principal
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import (
    CatalogMedia,
    EmbeddingVector,
    EmbeddingVersion,
    FeatureProjection,
    MediaStateChange,
)
from memes_search.application.dto.queries import SearchLimit, SearchMediaKind, SearchQuery
from memes_search.application.dto.samples import (
    InferenceState,
    PrivateSample,
    QueryInference,
    SampleFeatureChange,
    SampleFeatures,
    SampleState,
)
from memes_search.application.dto.sessions import SearchSession
from memes_search.application.dto.suggestions import VariationMatch
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.catalog import ICatalogReader
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.identity import IIdentityReader
from memes_search.application.ports.inference import IInferenceGateway
from memes_search.application.ports.inspection import IMediaInspector
from memes_search.application.ports.private_data import IPrivateDataRepository
from memes_search.application.ports.requests import ISearchRequestRepository
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.search_index import ISearchIndex
from memes_search.application.ports.sessions import ISearchSessionRepository
from memes_search.application.ports.storage import IPrivateStorage
from memes_search.domain.ranking import MediaCandidate


@dataclass
class Clock(IClock):
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class Catalog(ICatalogReader, IMediaInspector):
    items: dict[UUID, CatalogMedia] = field(default_factory=dict)
    exact: CatalogMedia | None = None
    invalid_file: bool = False

    async def resolve(self, media_ids: tuple[UUID, ...]) -> tuple[CatalogMedia, ...]:
        return tuple(self.items[identifier] for identifier in media_ids if identifier in self.items)

    async def by_hash(self, digest: Sha256) -> CatalogMedia | None:
        return self.exact

    async def inspect(self, key: str, file: FileRef) -> SearchMediaKind:
        if self.invalid_file:
            raise SearchError(SearchErrorCode.INVALID_SAMPLE)
        return SearchMediaKind.IMAGE


@dataclass
class Index(ISearchIndex):
    branches: tuple[tuple[MediaCandidate, ...], ...] = ()
    vector_candidates: tuple[MediaCandidate, ...] = ()
    vector_failure: bool = False
    reusable: dict[UUID, SampleFeatures] = field(default_factory=dict)
    matches: tuple[VariationMatch, ...] = ()

    async def text(self, query: SearchQuery) -> tuple[tuple[MediaCandidate, ...], ...]:
        return self.branches

    async def vectors(
        self, query: SearchQuery, vectors: tuple[EmbeddingVector, ...]
    ) -> tuple[MediaCandidate, ...]:
        if self.vector_failure:
            raise SearchError(SearchErrorCode.UNAVAILABLE)
        return self.vector_candidates

    async def variation_matches(self, media_id: UUID) -> tuple[VariationMatch, ...]:
        return self.matches

    async def features_for(self, media_id: UUID) -> SampleFeatures:
        return self.reusable.get(media_id, SampleFeatures())

    async def apply_feature(self, projection: FeatureProjection) -> bool:
        return True

    async def apply_media_state(self, change: MediaStateChange) -> bool:
        return True


@dataclass
class Sessions(ISearchSessionRepository):
    items: dict[UUID, SearchSession] = field(default_factory=dict)

    async def get(self, session_id: UUID) -> SearchSession | None:
        return self.items.get(session_id)

    async def save(self, session: SearchSession) -> None:
        self.items[session.id] = session

    async def remove_expired(self, now: datetime) -> None:
        self.items = {key: value for key, value in self.items.items() if value.expires_at > now}

    async def delete_owner(self, owner_id: UUID) -> None:
        self.items = {
            key: value for key, value in self.items.items() if value.query.owner_id != owner_id
        }


@dataclass
class Requests(ISearchRequestRepository):
    items: dict[UUID, SearchRequest] = field(default_factory=dict)

    async def get(self, request_id: UUID) -> SearchRequest | None:
        return self.items.get(request_id)

    async def save(self, request: SearchRequest) -> None:
        self.items[request.id] = request

    async def remove_expired(self, now: datetime) -> None:
        self.items = {key: value for key, value in self.items.items() if value.expires_at > now}

    async def delete_owner(self, owner_id: UUID) -> None:
        self.items = {
            key: value for key, value in self.items.items() if value.query.owner_id != owner_id
        }


@dataclass
class Inference(IInferenceGateway):
    version: EmbeddingVersion | None = None
    state: InferenceState = InferenceState.SUCCEEDED
    task_id: UUID = field(default_factory=uuid4)
    failure: bool = False
    queries: int = 0

    async def query(
        self, request_id: UUID, text: str, space_id: str, owner_id: UUID | None
    ) -> QueryInference:
        self.queries += 1
        return await self.query_result(self.task_id)

    async def query_result(self, task_id: UUID) -> QueryInference:
        if self.failure:
            raise SearchError(SearchErrorCode.UNAVAILABLE)
        return QueryInference(
            task_id=task_id,
            state=self.state,
            embedding=EmbeddingVector(
                values=(1.0, 0.0), space_id="fixture-space", version=self.version
            )
            if self.state is InferenceState.SUCCEEDED
            else None,
        )

    async def submit_sample(self, sample: PrivateSample) -> None:
        return None

    async def cancel_sample(self, sample_id: UUID) -> None:
        return None

    async def cancel_query(self, task_id: UUID) -> None:
        return None


@dataclass
class Samples(ISampleRepository):
    items: dict[UUID, PrivateSample] = field(default_factory=dict)
    results: dict[UUID, SampleFeatures] = field(default_factory=dict)
    queued: list[UUID] = field(default_factory=list)
    cancellations: list[UUID] = field(default_factory=list)

    async def reserve(self, sample: PrivateSample, limit: int) -> None:
        active = sum(
            item.owner_id == sample.owner_id
            and item.expires_at > sample.created_at
            and item.state in {SampleState.UPLOADING, SampleState.QUEUED}
            for item in self.items.values()
        )
        if active >= limit:
            raise SearchError(SearchErrorCode.SAMPLE_LIMIT)
        self.items[sample.id] = sample

    async def get(self, sample_id: UUID) -> PrivateSample | None:
        return self.items.get(sample_id)

    async def save(self, sample: PrivateSample, *, enqueue: bool = False) -> None:
        self.items[sample.id] = sample
        if enqueue:
            self.queued.append(sample.id)

    async def features(self, sample_id: UUID) -> SampleFeatures:
        return self.results.get(sample_id, SampleFeatures())

    async def apply_features(self, change: SampleFeatureChange, now: datetime) -> bool:
        sample = self.items.get(change.sample_id)
        if sample is None or sample.state is SampleState.CANCELLED or sample.expires_at <= now:
            return False
        self.results[sample.id] = SampleFeatures(text=change.text, vectors=change.vectors)
        self.items[sample.id] = replace(sample, state=SampleState.READY)
        return True

    async def expired(self, now: datetime, limit: int) -> tuple[PrivateSample, ...]:
        return tuple(item for item in self.items.values() if item.expires_at <= now)[:limit]

    async def remove(self, sample_id: UUID) -> None:
        self.items.pop(sample_id, None)

    async def schedule_cleanup(self, object_key: str, available_at: datetime) -> None:
        return None

    async def schedule_cancel(self, sample_id: UUID, available_at: datetime) -> None:
        self.cancellations.append(sample_id)

    async def expire_owner(self, owner_id: UUID, now: datetime) -> None:
        self.items = {
            key: replace(value, state=SampleState.CANCELLED, expires_at=now)
            if value.owner_id == owner_id
            else value
            for key, value in self.items.items()
        }


@dataclass
class Storage(IPrivateStorage):
    items: dict[str, bytes] = field(default_factory=dict)
    signed_lifetimes: list[int] = field(default_factory=list)

    async def put(
        self, key: str, file_id: UUID, chunks: AsyncIterable[bytes], maximum: int
    ) -> FileRef:
        content = b"".join([chunk async for chunk in chunks])
        if not content or len(content) > maximum:
            raise SearchError(SearchErrorCode.INVALID_SAMPLE)
        self.items[key] = content
        return FileRef(
            id=file_id, sha256=Sha256(value=sha256(content).hexdigest()), size_bytes=len(content)
        )

    async def delete(self, key: str) -> None:
        self.items.pop(key, None)

    async def download_url(self, key: str, lifetime_seconds: int) -> str:
        self.signed_lifetimes.append(lifetime_seconds)
        return "https://s3.invalid/private-signed-url"


@dataclass
class Identity(IIdentityReader):
    user_id: UUID = field(default_factory=uuid4)

    async def introspect(self, token: str) -> Principal | None:
        return Principal(user_id=self.user_id) if token == str(self.user_id) else None


@dataclass
class PrivateData(IPrivateDataRepository):
    sessions: Sessions
    requests: Requests
    samples: Samples

    async def erase_owner(self, owner_id: UUID, now: datetime) -> None:
        await self.sessions.delete_owner(owner_id)
        await self.requests.delete_owner(owner_id)
        await self.samples.expire_owner(owner_id, now)


@dataclass
class ReceiveSpy:
    app: ASGIApp
    calls: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counted_receive() -> Message:
            self.calls += 1
            return await receive()

        await self.app(scope, counted_receive, send)


@dataclass
class MultipartSpools:
    stack: ExitStack
    files: list[SpooledTemporaryFile[bytes]] = field(default_factory=list)

    @contextmanager
    def opened(self, max_size: int) -> Iterator[SpooledTemporaryFile[bytes]]:
        with SpooledTemporaryFile(max_size=max_size) as temporary:
            yield temporary

    def create(self, *, max_size: int) -> SpooledTemporaryFile[bytes]:
        temporary = self.stack.enter_context(self.opened(max_size))
        self.files.append(temporary)
        return temporary


@dataclass
class SwitchingSearchProxy:
    primary: ASGIApp
    shadow: ASGIApp
    current: IndexTarget = IndexTarget.PRIMARY

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        target = self.current
        for candidate in IndexTarget:
            prefix = f"/v1/search/indexes/{candidate}"
            if path == prefix or path.startswith(prefix + "/"):
                target = candidate
                scope = {**scope, "path": "/v1/search" + path[len(prefix) :]}
                scope["raw_path"] = scope["path"].encode()
                break
        application = self.primary if target is IndexTarget.PRIMARY else self.shadow
        await application(scope, receive, send)


@dataclass
class Admission(ISampleAdmission):
    clock: Clock
    limit: int = SearchLimit.ACTIVE_SAMPLES_PER_USER
    items: dict[UUID, tuple[UUID, datetime]] = field(default_factory=dict)
    erased: set[UUID] = field(default_factory=set)
    released: list[UUID] = field(default_factory=list)
    unavailable: bool = False

    async def reserve(
        self, *, sample_id: UUID, owner_id: UUID, created_at: datetime, expires_at: datetime
    ) -> None:
        if self.unavailable:
            raise SearchError(SearchErrorCode.UNAVAILABLE)
        if owner_id in self.erased:
            raise SearchError(SearchErrorCode.FORBIDDEN)
        if sample_id in self.items:
            return
        used = sum(
            owner == owner_id and expiry > self.clock.now() for owner, expiry in self.items.values()
        )
        if used >= self.limit:
            raise SearchError(SearchErrorCode.SAMPLE_LIMIT)
        self.items[sample_id] = (owner_id, expires_at)

    async def release(self, sample_id: UUID) -> None:
        self.released.append(sample_id)
        self.items.pop(sample_id, None)

    async def erase_owner(self, owner_id: UUID) -> None:
        self.erased.add(owner_id)
        for identifier, (owner, _expiry) in tuple(self.items.items()):
            if owner == owner_id:
                self.items.pop(identifier, None)

    async def purge(self, now: datetime) -> None:
        for identifier, (_owner, expiry) in tuple(self.items.items()):
            if expiry <= now:
                self.items.pop(identifier, None)


class PausingStorage(Storage):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def put(
        self, key: str, file_id: UUID, chunks: AsyncIterable[bytes], maximum: int
    ) -> FileRef:
        self.entered.set()
        await self.release.wait()
        return await super().put(key, file_id, chunks, maximum)
