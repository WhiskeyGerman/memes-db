"""Атомарная проекция большой группы без роста ORM identity map."""

from collections.abc import Callable
from enum import IntEnum

import pytest
from sqlalchemy import func, select, text

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.domain.groups import GroupId
from memes_catalog.infrastructure.persistence.events import DeliveryTarget
from memes_catalog.infrastructure.persistence.models import MediaRow, OutboxRow
from memes_catalog.infrastructure.persistence.variations import PostgresVariations
from memes_catalog_tests.conftest import CatalogHarness


class Population(IntEnum):
    CLONES = 1000
    ORIGINAL = 1
    NEXT_REVISION = 2


@pytest.fixture
async def large_group(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> GroupId:
    view = await harness.publish(actor, png(17))
    async with harness.sessions.begin() as session:
        await session.execute(
            text(
                "WITH clones AS (INSERT INTO catalog_media "
                "(id, author_id, file_id, sha256, size_bytes, format, kind, metadata_payload, "
                "object_key, published_at, state, deleted_at, revision, projection_revision) "
                "SELECT gen_random_uuid(), author_id, gen_random_uuid(), "
                "lpad(md5(gen_random_uuid()::text),64,'0'), size_bytes, format, kind, "
                "metadata_payload, object_key, published_at, state, NULL, revision, "
                "projection_revision FROM catalog_media CROSS JOIN generate_series(1,:count) "
                "WHERE id=:media RETURNING id) "
                "INSERT INTO catalog_memberships "
                "(id, media_id, group_id, joined_at, ended_at, end_reason, revision) "
                "SELECT gen_random_uuid(), id, :group, :now, NULL, NULL, 1 FROM clones"
            ),
            {
                "count": Population.CLONES,
                "media": view.media.id,
                "group": view.group.id,
                "now": harness.now,
            },
        )
    return view.group.id


@pytest.mark.integration
async def test_large_group_emits_atomic_revision_for_every_member(
    harness: CatalogHarness, large_group: GroupId
) -> None:
    total = Population.CLONES + Population.ORIGINAL
    async with harness.sessions.begin() as session:
        before = await session.scalar(select(func.count()).select_from(OutboxRow))
        await PostgresVariations(session).changed((large_group,), now=harness.now)
        assert not session.identity_map
        revisions = set(await session.scalars(select(MediaRow.projection_revision)))
        assert revisions == {Population.NEXT_REVISION}
        after = await session.scalar(select(func.count()).select_from(OutboxRow))
        assert before is not None and after == before + total
        emitted = await session.scalars(
            select(OutboxRow).where(
                OutboxRow.target == DeliveryTarget.SEARCH_STATE,
                OutboxRow.payload["revision"].as_integer() == Population.NEXT_REVISION,
            )
        )
        events = tuple(emitted)
        assert len(events) == total
        assert all(
            event.payload["event_id"] == str(event.id)
            and event.payload["group_id"] == str(large_group)
            and event.payload["active"] is True
            for event in events
        )
