from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from memes_search.application.dto.indexes import IndexTarget
from memes_search.presentation.index_alias import IndexAliasMiddleware
from memes_search.presentation.links import SearchResourceLinks
from memes_search.presentation.main import create_app


@dataclass(frozen=True, slots=True)
class AliasServices:
    links: SearchResourceLinks


@dataclass(frozen=True, slots=True)
class AliasRuntime:
    services: AliasServices


@pytest.fixture
def alias_probe(request: pytest.FixtureRequest) -> FastAPI:
    application = FastAPI()
    target = IndexTarget(request.param)

    async def capture(incoming: Request) -> JSONResponse:
        return JSONResponse(
            {
                "path": incoming.scope["path"],
                "raw_path": incoming.scope["raw_path"].decode(),
                "query_string": incoming.scope["query_string"].decode(),
            }
        )

    application.add_route("/{path:path}", capture, methods=["GET", "POST"])
    application.state.runtime = AliasRuntime(
        services=AliasServices(links=SearchResourceLinks.for_target(target))
    )
    application.add_middleware(IndexAliasMiddleware)
    return application


@pytest.fixture
def standalone_index_app(request: pytest.FixtureRequest) -> FastAPI:
    application = create_app()
    target = IndexTarget(request.param)
    application.state.runtime = AliasRuntime(
        services=AliasServices(links=SearchResourceLinks.for_target(target))
    )
    return application
