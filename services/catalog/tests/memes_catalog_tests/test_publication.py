"""Публикации: атомарная дедупликация, квоты и настоящая проверка файлов."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_catalog.application.dto.catalog import Actor, UploadState
from memes_catalog.infrastructure.persistence.models import (
    MediaRow,
    MembershipRow,
    OutboxRow,
    UploadRow,
)
from memes_catalog_tests.conftest import CatalogHarness
from memes_catalog_tests.fakes import Clock, PausingInspector
from memes_platform.application.errors import ApplicationError, ErrorCode

pytestmark = pytest.mark.integration


async def test_publication_is_immediate_and_completion_is_idempotent(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    body = png(1)
    ticket = await harness.catalog.begin(actor=actor, size_bytes=len(body), now=harness.now)
    await asyncio.to_thread(
        harness.storage.client.put_object,
        Bucket=harness.storage.bucket,
        Key=ticket.upload.object_key,
        Body=body,
    )
    first = await harness.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=harness.now)
    repeated = await harness.catalog.complete(
        actor=actor, upload_id=ticket.upload.id, now=harness.now
    )
    assert first == repeated
    assert not first.duplicate
    view = await harness.catalog.get(first.media_id)
    assert not view.features
    assert view.group.member_count == 1
    async with harness.sessions() as session:
        assert len((await session.scalars(select(OutboxRow))).all()) == 2


async def test_parallel_identical_files_create_single_publication(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    body = png(2)
    tickets = await asyncio.gather(
        *(
            harness.catalog.begin(actor=actor, size_bytes=len(body), now=harness.now)
            for _ in range(2)
        )
    )
    for ticket in tickets:
        await asyncio.to_thread(
            harness.storage.client.put_object,
            Bucket=harness.storage.bucket,
            Key=ticket.upload.object_key,
            Body=body,
        )
    outcomes = await asyncio.gather(
        *(
            harness.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=harness.now)
            for ticket in tickets
        )
    )
    assert outcomes[0].media_id == outcomes[1].media_id
    assert sum(outcome.duplicate for outcome in outcomes) == 1
    async with harness.sessions() as session:
        assert len((await session.scalars(select(MediaRow))).all()) == 1
        assert (
            len(
                (
                    await session.scalars(
                        select(MembershipRow).where(MembershipRow.ended_at.is_(None))
                    )
                ).all()
            )
            == 1
        )


async def test_upload_quota_is_checked_atomically_for_parallel_reservations(
    harness: CatalogHarness,
    actor: Actor,
) -> None:
    harness.catalog.concurrent_limit = 1
    results = await asyncio.gather(
        *(harness.catalog.begin(actor=actor, size_bytes=100, now=harness.now) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ApplicationError) for item in results) == 1
    async with harness.sessions() as session:
        assert len((await session.scalars(select(UploadRow))).all()) == 1


async def test_queue_backlog_rejects_new_upload_before_reservation(
    harness: CatalogHarness,
    actor: Actor,
) -> None:
    harness.delivery.pending = harness.catalog.queue_limit
    with pytest.raises(ApplicationError) as error:
        await harness.catalog.begin(actor=actor, size_bytes=100, now=harness.now)
    assert error.value.code is ErrorCode.UNAVAILABLE
    async with harness.sessions() as session:
        assert not (await session.scalars(select(UploadRow))).all()


async def test_other_account_cannot_complete_upload(
    harness: CatalogHarness,
    actor: Actor,
) -> None:
    ticket = await harness.catalog.begin(actor=actor, size_bytes=100, now=harness.now)
    with pytest.raises(ApplicationError) as error:
        await harness.catalog.complete(
            actor=replace(actor, user_id=uuid4()), upload_id=ticket.upload.id, now=harness.now
        )
    assert error.value.code is ErrorCode.FORBIDDEN


async def test_invalid_file_is_rejected_by_actual_decoder(
    harness: CatalogHarness,
    actor: Actor,
) -> None:
    invalid = b"not a png"
    ticket = await harness.catalog.begin(actor=actor, size_bytes=len(invalid), now=harness.now)
    await asyncio.to_thread(
        harness.storage.client.put_object,
        Bucket=harness.storage.bucket,
        Key=ticket.upload.object_key,
        Body=invalid,
    )
    with pytest.raises(ApplicationError) as error:
        await harness.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=harness.now)
    assert error.value.code is ErrorCode.INVALID_INPUT
    async with harness.sessions() as session:
        row = await session.get(UploadRow, ticket.upload.id)
        assert row is not None and row.state == UploadState.REJECTED
        assert not (await session.scalars(select(MediaRow))).all()


async def test_expired_claim_cannot_overwrite_winning_publication(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    body = png(28)
    ticket = await harness.catalog.begin(actor=actor, size_bytes=len(body), now=harness.now)
    await asyncio.to_thread(
        harness.storage.client.put_object,
        Bucket=harness.storage.bucket,
        Key=ticket.upload.object_key,
        Body=body,
    )
    inspector = PausingInspector(delegate=harness.catalog.inspector)
    harness.catalog.inspector = inspector
    old = asyncio.create_task(
        harness.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=harness.now)
    )
    await asyncio.wait_for(inspector.inspected.wait(), timeout=10)
    clock = cast(Clock, harness.catalog.clock)
    takeover_at = harness.now + timedelta(seconds=harness.catalog.claim_seconds + 1)
    clock.value = takeover_at
    try:
        outcome = await harness.catalog.complete(
            actor=actor, upload_id=ticket.upload.id, now=takeover_at
        )
    finally:
        inspector.release.set()
    with pytest.raises(ApplicationError) as stale:
        await old
    assert stale.value.code is ErrorCode.STALE_STATE
    view = await harness.catalog.get(outcome.media_id)
    await harness.catalog.maintenance(
        now=harness.now + timedelta(seconds=harness.catalog.upload_seconds + 1), limit=100
    )
    async with httpx.AsyncClient() as transfer:
        surviving = await transfer.get(
            await harness.storage.download_url(view.object_key, expires_seconds=30)
        )
    assert surviving.content == body
    async with harness.sessions() as session:
        assert len((await session.scalars(select(MediaRow))).all()) == 1


async def test_changed_staging_version_after_inspection_is_rejected(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    body = png(29)
    ticket = await harness.catalog.begin(actor=actor, size_bytes=len(body), now=harness.now)
    await asyncio.to_thread(
        harness.storage.client.put_object,
        Bucket=harness.storage.bucket,
        Key=ticket.upload.object_key,
        Body=body,
    )
    inspector = PausingInspector(delegate=harness.catalog.inspector)
    harness.catalog.inspector = inspector
    task = asyncio.create_task(
        harness.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=harness.now)
    )
    await asyncio.wait_for(inspector.inspected.wait(), timeout=10)
    try:
        await asyncio.to_thread(
            harness.storage.client.put_object,
            Bucket=harness.storage.bucket,
            Key=ticket.upload.object_key,
            Body=png(30),
        )
    finally:
        inspector.release.set()
    with pytest.raises(ApplicationError) as conflict:
        await task
    assert conflict.value.code is ErrorCode.CONFLICT
    async with harness.sessions() as session:
        assert not (await session.scalars(select(MediaRow))).all()
