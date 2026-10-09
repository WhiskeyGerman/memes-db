"""Явное преобразование известных ошибок на границе HTTP."""

from enum import StrEnum
from http import HTTPStatus
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from memes_kernel.errors import DomainError
from memes_platform.application.errors import ApplicationError, ErrorCode


class ErrorField(StrEnum):
    """Поля безопасного ответа об ошибке."""

    ERROR = "error"
    CODE = "code"
    MESSAGE = "message"


ERROR_STATUS = {
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    ErrorCode.INVALID_INPUT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.STALE_STATE: HTTPStatus.CONFLICT,
}


async def application_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Преобразовать прикладной отказ.

    Args:
        request: Внешний запрос.
        error: Известная причина отказа.
    """
    application_error = cast(ApplicationError, error)
    return JSONResponse(
        status_code=ERROR_STATUS[application_error.code],
        content={
            ErrorField.ERROR: {
                ErrorField.CODE: application_error.code,
                ErrorField.MESSAGE: application_error.message,
            }
        },
    )


async def domain_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Вернуть нарушение доменного правила без внутреннего стека.

    Args:
        request: Внешний запрос.
        error: Проверенное нарушение.
    """
    domain_error = cast(DomainError, error)
    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content={
            ErrorField.ERROR: {
                ErrorField.CODE: domain_error.code,
                ErrorField.MESSAGE: domain_error.code,
            },
        },
    )


async def validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Скрыть содержимое невалидного входа, включая пароли и токены.

    Args:
        request: Внешний запрос.
        error: Ошибка схемы.
    """
    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content={
            ErrorField.ERROR: {
                ErrorField.CODE: ErrorCode.INVALID_INPUT,
                ErrorField.MESSAGE: ErrorCode.INVALID_INPUT,
            },
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Установить обработчики только известных классов ошибок.

    Args:
        app: Собранное HTTP-приложение.
    """
    app.add_exception_handler(ApplicationError, application_error_handler)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
