"""Типизированное преобразование JSON PostgreSQL в прикладные модели."""

from typing import cast

from pydantic import TypeAdapter

from memes_search.application.dto.execution import SearchRequest
from memes_search.application.dto.samples import PrivateSample, SampleFeatureChange
from memes_search.application.dto.sessions import SearchSession

SESSION = TypeAdapter(SearchSession)
REQUEST = TypeAdapter(SearchRequest)
SAMPLE = TypeAdapter(PrivateSample)
SAMPLE_FEATURE = TypeAdapter(SampleFeatureChange)


def session_body(session: SearchSession) -> dict[str, object]:
    """Преобразовать снимок в JSON-совместимые значения."""
    return cast(dict[str, object], SESSION.dump_python(session, mode="json"))


def request_body(request: SearchRequest) -> dict[str, object]:
    """Преобразовать параметры асинхронного запроса."""
    return cast(dict[str, object], REQUEST.dump_python(request, mode="json"))


def sample_body(sample: PrivateSample) -> dict[str, object]:
    """Преобразовать приватное состояние образца."""
    return cast(dict[str, object], SAMPLE.dump_python(sample, mode="json"))


def sample_feature_body(feature: SampleFeatureChange) -> dict[str, object]:
    """Преобразовать признаки образца без неограниченного :class:`Any`."""
    return cast(dict[str, object], SAMPLE_FEATURE.dump_python(feature, mode="json"))
