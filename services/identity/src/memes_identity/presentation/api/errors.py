"""Преобразование типизированных отказов в HTTP без утечки деталей."""

from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.presentation.api.security import clear_token_cookies
from memes_kernel.errors import DomainError

FAILURE_STATUS = {
    IdentityFailure.INVALID_REQUEST: HTTPStatus.UNPROCESSABLE_ENTITY,
    IdentityFailure.INVALID_CREDENTIALS: HTTPStatus.UNAUTHORIZED,
    IdentityFailure.INVALID_PASSWORD_LENGTH: HTTPStatus.BAD_REQUEST,
    IdentityFailure.LOGIN_OCCUPIED: HTTPStatus.CONFLICT,
    IdentityFailure.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    IdentityFailure.FORBIDDEN: HTTPStatus.FORBIDDEN,
    IdentityFailure.NOT_FOUND: HTTPStatus.NOT_FOUND,
    IdentityFailure.CONFLICT: HTTPStatus.CONFLICT,
    IdentityFailure.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    IdentityFailure.REFRESH_REPLAY: HTTPStatus.UNAUTHORIZED,
    IdentityFailure.INTERNAL_AUTH_FAILED: HTTPStatus.UNAUTHORIZED,
    IdentityFailure.CSRF_FAILED: HTTPStatus.FORBIDDEN,
    IdentityFailure.BACKING_SERVICE_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
}


async def identity_error(request: Request, error: Exception) -> JSONResponse:
    """Выдать машинный код без содержимого секретов.

    Args:
        request: Исходный HTTP-запрос.
        error: Типизированный прикладной отказ.
    """
    if not isinstance(error, IdentityError):
        raise error
    response = JSONResponse(
        status_code=FAILURE_STATUS[error.code],
        content={"code": error.code},
        headers={"Cache-Control": "no-store"},
    )
    if error.code in {IdentityFailure.UNAUTHORIZED, IdentityFailure.REFRESH_REPLAY}:
        clear_token_cookies(response)
    return response


async def domain_error(request: Request, error: Exception) -> JSONResponse:
    """Отобразить нарушение бизнес-инварианта как ошибку ввода.

    Args:
        request: Исходный запрос.
        error: Нарушенный инвариант.
    """
    if not isinstance(error, DomainError):
        raise error
    return JSONResponse(status_code=HTTPStatus.BAD_REQUEST, content={"code": error.code})


async def database_error(request: Request, error: Exception) -> JSONResponse:
    """Закрыть отказ подключаемой БД без вывода DSN и параметров запроса.

    Args:
        request: Исходный запрос.
        error: Известный отказ PostgreSQL либо пула.
    """
    return JSONResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        content={"code": IdentityFailure.BACKING_SERVICE_UNAVAILABLE},
    )


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    """Отклонить неверный JSON без повторения входных секретов.

    Args:
        request: Исходный HTTP-запрос.
        error: Ошибка валидации входного контракта.
    """
    if not isinstance(error, RequestValidationError):
        raise error
    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content={"code": IdentityFailure.INVALID_REQUEST},
    )
