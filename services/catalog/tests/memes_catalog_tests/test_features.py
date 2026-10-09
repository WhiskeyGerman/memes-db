"""Приёмка признаков: поколение, цикл, удаление и durable REST-проекция."""

from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_catalog.application.dto.catalog import Actor, FeatureUpdate
from memes_catalog.application.use_cases.features import FeatureKind, FeatureState, validate_feature
from memes_catalog.infrastructure.persistence.events import DeliveryTarget
from memes_catalog.infrastructure.persistence.models import FeatureRow, OutboxRow
from memes_catalog_tests.conftest import CatalogHarness
from memes_platform.application.dto.json import JsonObject

pytestmark = pytest.mark.integration


@pytest.fixture
def feature() -> Callable[[UUID], FeatureUpdate]:
    def create(media_id: UUID) -> FeatureUpdate:
        return FeatureUpdate(
            event_id=uuid4(),
            media_id=media_id,
            source_revision=1,
            cycle_id=uuid4(),
            generation=1,
            kind=FeatureKind.OCR,
            state=FeatureState.PENDING,
            feature_version={
                "model": "fixture-ocr",
                "weights": "fixture-revision",
                "task_adapter": "ocr",
                "preprocessing": "fixture-v1",
                "space": None,
                "dimensions": None,
            },
            result=None,
        )

    return create


async def accept(harness: CatalogHarness, update: FeatureUpdate) -> bool:
    validate_feature(update)
    async with harness.factory.open() as uow:
        accepted = await uow.accept_feature(update, now=harness.now)
        await uow.commit()
        return accepted


async def test_feature_requires_pending_and_rejects_stale_generation_and_cycle(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
    feature: Callable[[UUID], FeatureUpdate],
) -> None:
    view = await harness.publish(actor, png(14))
    pending = feature(view.media.id)
    result: JsonObject = {"fragments": [{"text": "hello", "language": "en", "span": None}]}
    available = replace(pending, event_id=uuid4(), state=FeatureState.AVAILABLE, result=result)
    assert not await accept(harness, available)
    assert await accept(harness, pending)
    assert not await accept(harness, replace(available, cycle_id=uuid4()))
    assert await accept(harness, available)
    assert await accept(harness, available)
    newer = replace(pending, event_id=uuid4(), cycle_id=uuid4(), generation=2)
    assert await accept(harness, newer)
    assert not await accept(harness, available)
    async with harness.sessions() as session:
        current = await session.get(FeatureRow, (view.media.id, FeatureKind.OCR))
        assert current is not None and current.generation == 2
        deliveries = (
            await session.scalars(
                select(OutboxRow).where(OutboxRow.target == DeliveryTarget.SEARCH_FEATURE)
            )
        ).all()
        assert len(deliveries) == 3


async def test_feature_delivery_uses_current_membership_and_projection_revision(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
    feature: Callable[[UUID], FeatureUpdate],
) -> None:
    first = await harness.publish(actor, png(15))
    second = await harness.publish(actor, png(16))
    pending = feature(first.media.id)
    assert await accept(harness, pending)
    group_id = await harness.merge(first, second, actor)
    available = replace(
        pending,
        event_id=uuid4(),
        state=FeatureState.AVAILABLE,
        result={"fragments": [{"text": "current", "language": "en", "span": None}]},
    )
    assert await accept(harness, available)
    current = await harness.catalog.get(first.media.id)
    async with harness.sessions() as session:
        rows = (
            await session.scalars(
                select(OutboxRow)
                .where(OutboxRow.target == DeliveryTarget.SEARCH_FEATURE)
                .order_by(OutboxRow.sequence)
            )
        ).all()
        payload = rows[-1].payload
        assert payload["group_id"] == str(group_id)
        assert payload["revision"] == current.projection_revision
        assert payload["source_revision"] == 1


async def test_deleted_media_rejects_late_result(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
    feature: Callable[[UUID], FeatureUpdate],
) -> None:
    view = await harness.publish(actor, png(17))
    pending = feature(view.media.id)
    assert await accept(harness, pending)
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    available = replace(
        pending,
        event_id=uuid4(),
        state=FeatureState.AVAILABLE,
        result={"fragments": [{"text": "late", "language": "en", "span": None}]},
    )
    assert not await accept(harness, available)
    async with harness.sessions() as session:
        row = await session.get(FeatureRow, (view.media.id, FeatureKind.OCR))
        assert row is not None and row.result is None


async def test_feature_callback_http_validates_canonical_body_and_advances_pending(
    harness: CatalogHarness,
    client: httpx.AsyncClient,
    actor: Actor,
    png: Callable[[int], bytes],
    feature: Callable[[UUID], FeatureUpdate],
) -> None:
    view = await harness.publish(actor, png(44))
    pending = feature(view.media.id)
    body = {
        "event_id": str(pending.event_id),
        "media_id": str(pending.media_id),
        "source_revision": pending.source_revision,
        "cycle_id": str(pending.cycle_id),
        "generation": pending.generation,
        "kind": pending.kind,
        "state": pending.state,
        "feature_version": pending.feature_version,
        "result": pending.result,
    }
    path = f"/internal/media/{view.media.id}/features"
    headers = {"X-Internal-Key": harness.settings.internal_api_key}
    started = await client.post(path, json=body, headers=headers)
    assert started.status_code == HTTPStatus.OK and started.json()["accepted"]
    available = await client.post(
        path,
        json={
            **body,
            "event_id": str(uuid4()),
            "state": FeatureState.AVAILABLE,
            "result": {"fragments": [{"text": "hello", "language": "en", "span": None}]},
        },
        headers=headers,
    )
    assert available.status_code == HTTPStatus.OK and available.json()["accepted"]
