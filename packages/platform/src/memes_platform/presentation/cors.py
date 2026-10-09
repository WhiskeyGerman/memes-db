"""CORS для явно разрешённых frontend origins на HTTP-границе."""

from enum import StrEnum
from http import HTTPMethod

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from memes_platform.infrastructure.cors import CorsSettings


class BrowserHeader(StrEnum):
    """Разрешённые браузерные заголовки без internal REST credentials."""

    AUTHORIZATION = "Authorization"
    CONTENT_TYPE = "Content-Type"
    CSRF = "X-CSRF-Token"
    REQUEST_ID = "X-Request-ID"


def install_cors(application: FastAPI) -> None:
    """Разрешить точные origins из окружения, сохранив cookie/CSRF-защиту.

    Args:
        application: HTTP composition root.
    """
    settings = CorsSettings()
    if "*" in settings.cors_allowed_origins:
        raise ValueError("Explicit CORS origins required for credentialed API")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=[
            HTTPMethod.GET,
            HTTPMethod.POST,
            HTTPMethod.PUT,
            HTTPMethod.PATCH,
            HTTPMethod.DELETE,
            HTTPMethod.OPTIONS,
        ],
        allow_headers=[header.value for header in BrowserHeader],
        expose_headers=[BrowserHeader.REQUEST_ID.value],
    )
