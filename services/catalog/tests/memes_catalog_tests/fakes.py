"""Изолированные внешние контракты; БД и S3 остаются настоящими."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from memes_catalog.application.dto.catalog import Actor, ActorRole, Author, InspectedMedia
from memes_catalog.application.ports.clock import IClock
from memes_catalog.application.ports.resources import (
    IIdentityClient,
    IMediaInspector,
    IRestDelivery,
)
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.application.ports.deletion_journal import IDeletionJournal


@dataclass
class Identity(IIdentityClient):
    deleted: dict[UUID, bool] = field(default_factory=dict)

    async def authenticate(self, token: str) -> Actor:
        try:
            identifier = UUID(token)
        except ValueError as error:
            raise ApplicationError(ErrorCode.UNAUTHORIZED) from error
        if identifier in self.deleted:
            raise ApplicationError(ErrorCode.UNAUTHORIZED)
        return Actor(user_id=identifier, roles=frozenset({ActorRole.USER}))

    async def authors(self, user_ids: tuple[UUID, ...]) -> tuple[Author, ...]:
        return tuple(
            Author(
                user_id=identifier,
                login=None if identifier in self.deleted else "fixture_user",
                delete_media=self.deleted.get(identifier, False),
            )
            for identifier in user_ids
        )


@dataclass
class Delivery(IRestDelivery):
    pending: int = 0
    unavailable_targets: set[str] = field(default_factory=set)

    async def deliver(self, target: str, payload: JsonObject) -> None:
        if target in self.unavailable_targets:
            raise ApplicationError(ErrorCode.UNAVAILABLE)

    async def pending_tasks(self) -> int:
        return self.pending

    async def suggestions(self, media_id: UUID, *, limit: int) -> JsonObject:
        return {"items": []}


@dataclass
class Clock(IClock):
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class Journal(IDeletionJournal):
    entries: list[JsonObject] = field(default_factory=list)
    unavailable: bool = False

    async def record(self, event: JsonObject) -> None:
        if self.unavailable:
            raise ApplicationError(ErrorCode.UNAVAILABLE)
        self.entries.append(event)

    async def records(self, *, since: datetime | None = None) -> AsyncIterator[JsonObject]:
        for entry in self.entries:
            yield entry


@dataclass
class PausingInspector(IMediaInspector):
    delegate: IMediaInspector
    inspected: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def inspect_object(
        self, object_key: str, *, file_id: UUID, maximum_bytes: int
    ) -> InspectedMedia:
        self.calls += 1
        first = self.calls == 1
        result = await self.delegate.inspect_object(
            object_key, file_id=file_id, maximum_bytes=maximum_bytes
        )
        if first:
            self.inspected.set()
            await self.release.wait()
        return result
