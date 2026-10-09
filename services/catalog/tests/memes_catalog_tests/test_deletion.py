"""Удаление: границы владения, выбор аккаунта и повторная загрузка хэша."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.domain.groups import GroupState
from memes_catalog.domain.media import MediaState
from memes_catalog.infrastructure.persistence.events import DeliveryTarget
from memes_catalog.infrastructure.persistence.models import (
    GroupRow,
    MediaRow,
    MembershipRow,
    OutboxRow,
)
from memes_catalog_tests.conftest import CatalogHarness
from memes_platform.application.errors import ApplicationError, ErrorCode

pytestmark = pytest.mark.integration


async def test_other_account_cannot_delete_publication(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    view = await harness.publish(actor, png(18))
    with pytest.raises(ApplicationError) as error:
        await harness.catalog.delete(
            media_id=view.media.id, actor=replace(actor, user_id=uuid4()), now=harness.now
        )
    assert error.value.code is ErrorCode.FORBIDDEN
    assert (await harness.catalog.get(view.media.id)).media.id == view.media.id


async def test_deleted_file_can_be_uploaded_again_without_cleanup_race(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    body = png(19)
    old = await harness.publish(actor, body)
    await harness.catalog.delete(media_id=old.media.id, actor=actor, now=harness.now)
    new = await harness.publish(actor, body)
    assert old.media.id != new.media.id and old.object_key != new.object_key
    await harness.catalog.maintenance(now=harness.now, limit=100)
    async with httpx.AsyncClient() as client:
        removed = await client.get(
            await harness.storage.download_url(old.object_key, expires_seconds=30)
        )
        surviving = await client.get(
            await harness.storage.download_url(new.object_key, expires_seconds=30)
        )
    assert removed.is_client_error
    assert surviving.content == body


@pytest.mark.parametrize("delete_media", [False, True])
async def test_account_choice_preserves_other_authors_variations(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes], delete_media: bool
) -> None:
    first = await harness.publish(actor, png(20))
    other = await harness.publish(replace(actor, user_id=uuid4()), png(21))
    await harness.merge(first, other, actor)
    harness.identity.deleted[actor.user_id] = delete_media
    await harness.catalog.account_deleted(
        event_id=uuid4(), user_id=actor.user_id, delete_media=delete_media, now=harness.now
    )
    assert any(entry["kind"] == "account" for entry in harness.journal.entries)
    assert (await harness.catalog.get(other.media.id)).media.id == other.media.id
    if delete_media:
        with pytest.raises(ApplicationError) as error:
            await harness.catalog.get(first.media.id)
        assert error.value.code is ErrorCode.NOT_FOUND
    else:
        assert (await harness.catalog.get(first.media.id)).media.id == first.media.id
        assert (await harness.identity.authors((actor.user_id,)))[0].login is None


async def test_parallel_detach_and_delete_leave_consistent_groups(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    first = await harness.publish(actor, png(22))
    second = await harness.publish(actor, png(23))
    await harness.merge(first, second, actor)
    current = await harness.catalog.get(first.media.id)
    await harness.variations.detach(
        actor=actor, membership_id=current.membership.id, now=harness.now
    )
    outcomes = await asyncio.gather(
        harness.variations.detach(
            actor=replace(actor, user_id=uuid4()),
            membership_id=current.membership.id,
            now=harness.now,
        ),
        harness.catalog.delete(media_id=first.media.id, actor=actor, now=harness.now),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, ApplicationError)
            assert outcome.code in (ErrorCode.STALE_STATE, ErrorCode.NOT_FOUND)
    async with harness.sessions() as session:
        active = (
            await session.scalars(select(MediaRow).where(MediaRow.state == MediaState.PUBLISHED))
        ).all()
        memberships = (
            await session.scalars(select(MembershipRow).where(MembershipRow.ended_at.is_(None)))
        ).all()
        groups = (
            await session.scalars(select(GroupRow).where(GroupRow.state == GroupState.ACTIVE))
        ).all()
        assert len({row.media_id for row in memberships}) == len(active)
        assert len(memberships) == len(active)
        for group in groups:
            assert group.member_count == sum(member.group_id == group.id for member in memberships)
        for item in active:
            matching = [member for member in memberships if member.media_id == item.id]
            assert len(matching) == 1


async def test_deletion_emits_one_tombstone_and_is_idempotent(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    view = await harness.publish(actor, png(24))
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    async with harness.sessions() as session:
        events = (
            await session.scalars(
                select(OutboxRow).where(
                    OutboxRow.target == DeliveryTarget.SEARCH_STATE,
                    OutboxRow.payload["active"].as_boolean().is_(False),
                )
            )
        ).all()
        assert len(events) == 1 and events[0].payload["revision"] == 2


async def test_delete_requires_durable_journal_before_logical_ack(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    view = await harness.publish(actor, png(31))
    harness.journal.unavailable = True
    with pytest.raises(ApplicationError) as unavailable:
        await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    assert unavailable.value.code is ErrorCode.UNAVAILABLE
    assert (await harness.catalog.get(view.media.id)).media.id == view.media.id
    assert not harness.journal.entries
    harness.journal.unavailable = False
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    assert harness.journal.entries[0]["media_id"] == str(view.media.id)
    with pytest.raises(ApplicationError) as removed:
        await harness.catalog.get(view.media.id)
    assert removed.value.code is ErrorCode.NOT_FOUND
