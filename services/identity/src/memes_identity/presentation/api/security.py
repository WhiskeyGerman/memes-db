"""Защита REST-входов и state-changing cookie-запросов."""

from enum import IntEnum, StrEnum
from hashlib import sha256
from hmac import compare_digest, new
from secrets import token_urlsafe
from typing import Annotated

from fastapi import Depends, Header, Request, Response

from memes_identity.application.dto.authentication import IssuedTokens
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.presentation.dependencies import IdentityContainer, container


class CookieName(StrEnum):
    """Cookie и пути аутентификационного API."""

    REFRESH = "memes_refresh"
    CSRF = "memes_csrf"
    PATH = "/v1/auth"
    CSRF_PATH = "/"
    SAME_SITE = "strict"


class SecurityHeader(StrEnum):
    """HTTP-заголовки безопасности."""

    ORIGIN = "Origin"
    CSRF = "X-CSRF-Token"
    INTERNAL_KEY = "X-Internal-Key"


class CsrfEntropy(IntEnum):
    """Размер nonce для подписанного double-submit cookie."""

    BYTES = 32


def require_origin(request: Request, dependencies: IdentityContainer) -> None:
    """Проверить точное разрешённое browser origin.

    Args:
        request: HTTP-запрос.
        dependencies: Конфигурация релиза.

    Raises:
        IdentityError: Origin отсутствует либо не разрешён.
    """
    origin = request.headers.get(SecurityHeader.ORIGIN)
    if origin not in dependencies.settings.csrf_allowed_origins:
        raise IdentityError(IdentityFailure.CSRF_FAILED)


def csrf_signature(nonce: str, refresh_token: str, key: str) -> str:
    """Связать CSRF nonce с текущим refresh token.

    Args:
        nonce: Случайный открытый nonce.
        refresh_token: Защищённый секрет браузерной сессии.
        key: Ключ подписи текущего релиза.

    Returns:
        str: HMAC SHA-256.
    """
    message = f"{nonce}:{sha256(refresh_token.encode()).hexdigest()}"
    return new(key.encode(), message.encode(), sha256).hexdigest()


def set_token_cookies(
    response: Response, tokens: IssuedTokens, dependencies: IdentityContainer
) -> None:
    """Установить HttpOnly refresh и связанный CSRF cookie.

    Args:
        response: HTTP-ответ.
        tokens: Новые секреты сессии.
        dependencies: Конфигурация релиза.
    """
    nonce = token_urlsafe(CsrfEntropy.BYTES)
    signature = csrf_signature(nonce, tokens.refresh_token, dependencies.settings.internal_api_key)
    csrf = f"{nonce}.{signature}"
    response.set_cookie(
        key=CookieName.REFRESH,
        value=tokens.refresh_token,
        httponly=True,
        secure=dependencies.settings.cookie_secure,
        samesite=CookieName.SAME_SITE.value,
        path=CookieName.PATH,
        expires=tokens.refresh_expires_at,
    )
    response.set_cookie(
        key=CookieName.CSRF,
        value=csrf,
        httponly=False,
        secure=dependencies.settings.cookie_secure,
        samesite=CookieName.SAME_SITE.value,
        path=CookieName.CSRF_PATH,
        expires=tokens.refresh_expires_at,
    )
    response.headers["Cache-Control"] = "no-store"


def clear_token_cookies(response: Response) -> None:
    """Стереть браузерные cookie при завершении сессии.

    Args:
        response: HTTP-ответ.
    """
    response.delete_cookie(CookieName.REFRESH, path=CookieName.PATH)
    response.delete_cookie(CookieName.CSRF, path=CookieName.CSRF_PATH)


def refresh_cookie(request: Request, dependencies: IdentityContainer) -> str:
    """Проверить Origin, подпись и двойную передачу CSRF.

    Args:
        request: Недоверенный HTTP-запрос.
        dependencies: Конфигурация процесса.

    Returns:
        str: Проверенный cookie-секрет.

    Raises:
        IdentityError: Нарушена CSRF-защита либо отсутствует refresh token.
    """
    require_origin(request, dependencies)
    refresh = request.cookies.get(CookieName.REFRESH)
    cookie = request.cookies.get(CookieName.CSRF)
    header = request.headers.get(SecurityHeader.CSRF)
    if refresh is None or cookie is None or header is None:
        raise IdentityError(IdentityFailure.CSRF_FAILED)
    if not compare_digest(cookie, header):
        raise IdentityError(IdentityFailure.CSRF_FAILED)
    nonce, separator, signature = cookie.partition(".")
    expected = csrf_signature(nonce, refresh, dependencies.settings.internal_api_key)
    if not separator or not compare_digest(signature, expected):
        raise IdentityError(IdentityFailure.CSRF_FAILED)
    return refresh


def internal_authorization(
    dependencies: Annotated[IdentityContainer, Depends(container)],
    key: Annotated[str | None, Header(alias=SecurityHeader.INTERNAL_KEY)] = None,
) -> None:
    """Отклонить внутренний запрос без межсервисного секрета.

    Args:
        dependencies: Composition-контейнер.
        key: Предъявленный секрет.

    Raises:
        IdentityError: Предъявленный секрет отсутствует либо неверен.
    """
    if key is None or not compare_digest(key, dependencies.settings.internal_api_key):
        raise IdentityError(IdentityFailure.INTERNAL_AUTH_FAILED)
