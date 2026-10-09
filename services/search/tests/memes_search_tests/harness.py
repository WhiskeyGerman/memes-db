"""Типизированное состояние изолированных сценариев."""

from dataclasses import dataclass

from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.presentation.container import Services
from memes_search_tests.fakes import (
    Admission,
    Catalog,
    Clock,
    Identity,
    Index,
    Inference,
    Requests,
    Samples,
    Sessions,
    Storage,
)


@dataclass
class SearchHarness:
    services: Services
    clock: Clock
    catalog: Catalog
    index: Index
    sessions: Sessions
    requests: Requests
    samples: Samples
    storage: Storage
    inference: Inference
    identity: Identity
    version: EmbeddingVersion
    admission: Admission
