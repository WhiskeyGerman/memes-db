"""Версионированные эмбеддинги текущего извлеченного текста."""

from dataclasses import replace
from datetime import datetime
from math import ceil
from uuid import uuid4

from memes_processing.application.dto.coordinator import (
    CoordinatorLimit,
    CoordinatorPolicy,
    JSONValue,
    WorkItem,
)
from memes_processing.application.dto.features import mapping, sequence
from memes_processing.application.ports.repository import IProcessingRepository
from memes_processing.application.tasks import ProcessingTask, TaskInput, TaskState
from memes_processing.application.use_cases.sources import notify, source_version
from memes_processing.domain.features import CycleId, ExtractionId, FeatureKind, FeatureState


async def refresh_text_embedding(
    repository: IProcessingRepository, work: WorkItem, *, policy: CoordinatorPolicy, now: datetime
) -> None:
    """Новое распознавание отменяет устаревший цикл, сохраняя историю."""
    if work.task.input.kind not in {FeatureKind.OCR, FeatureKind.TRANSCRIPT}:
        return
    latest: dict[FeatureKind, WorkItem] = {}
    for existing in await repository.source_tasks(work.source.id):
        kind = existing.task.input.kind
        if kind not in latest or existing.generation > latest[kind].generation:
            latest[kind] = existing
    fragments: list[JSONValue] = []
    for kind in (FeatureKind.OCR, FeatureKind.TRANSCRIPT):
        item = latest.get(kind)
        if item is not None and item.task.state is TaskState.SUCCEEDED and item.result is not None:
            fragments.extend(sequence(mapping(item.result).get("fragments")))
    current = latest.get(FeatureKind.TEXT_EMBEDDING)
    if not fragments and current is None:
        return
    if len(fragments) > CoordinatorLimit.TEXT_VECTORS:
        batch = ceil(len(fragments) / CoordinatorLimit.TEXT_VECTORS)
        combined: list[JSONValue] = []
        for start in range(0, len(fragments), batch):
            selected = [mapping(item) for item in fragments[start : start + batch]]
            spans = [mapping(item["span"]) for item in selected if item.get("span") is not None]
            combined.append(
                {
                    "text": "\n".join(str(item["text"]) for item in selected),
                    "span": {
                        "start_ms": min(int(str(span["start_ms"])) for span in spans),
                        "end_ms": max(int(str(span["end_ms"])) for span in spans),
                    }
                    if spans
                    else None,
                }
            )
        fragments = combined
    if current is not None and current.text_fragments == fragments:
        return
    if current is not None:
        await repository.save_work(replace(current, task=current.task.cancel()))
    changed = WorkItem(
        task=ProcessingTask(
            id=uuid4(),
            input=TaskInput(
                extraction_id=ExtractionId(uuid4()),
                cycle_id=CycleId(uuid4()),
                source=work.source.file,
                kind=FeatureKind.TEXT_EMBEDDING,
                version=source_version(work.source, FeatureKind.TEXT_EMBEDDING, policy=policy),
            ),
            created_at=now,
            state=TaskState.WAITING if fragments else TaskState.EMPTY,
        ),
        source=work.source,
        generation=current.generation + 1 if current is not None else 1,
        text_fragments=fragments,
        result=None if fragments else {"items": []},
    )
    await repository.save_work(changed)
    await notify(repository, changed, state=FeatureState.PENDING, now=now)
    if not fragments:
        await notify(repository, changed, state=FeatureState.EMPTY, now=now)
