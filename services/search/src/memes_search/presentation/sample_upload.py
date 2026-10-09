"""Ленивый multipart-поток, который читается только после auth и квоты."""

from collections.abc import AsyncGenerator
from enum import StrEnum
from http import HTTPStatus

from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from memes_kernel.values import Number
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.presentation.body_limit import BodyTooLargeError


class UploadField(StrEnum):
    """Контракт формы приватного образца."""

    FILE = "file"
    CONTENT_TYPE = "multipart/form-data"


UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            UploadField.CONTENT_TYPE: {
                "schema": {
                    "type": "object",
                    "required": [UploadField.FILE],
                    "properties": {UploadField.FILE: {"type": "string", "format": "binary"}},
                    "additionalProperties": False,
                }
            }
        },
    }
}


class ClosingMultipartParser(MultiPartParser):
    """Закрыть также незавершённые файлы при обрыве парсинга."""

    def close_open_files(self) -> None:
        """Стереть все spools, включая не попавшие в FormData.

        У Starlette нет публичного close для незавершённого parser.
        Доступ к его scratch-списку изолирован этим адаптером
        и защищён регрессией обрыва multipart-потока.
        """
        for temporary in self._files_to_close_on_error:
            temporary.close()


async def sample_chunks(request: Request) -> AsyncGenerator[bytes, None]:
    """Разобрать единственный файл, когда приложение начинает читать поток.

    Args:
        request: HTTP-запрос, авторизованный до начала потребления.

    Raises:
        SearchError: Отказ parser или превышение ограничений файла.
    """
    parser = ClosingMultipartParser(
        request.headers,
        request.stream(),
        max_files=Number.ONE,
        max_fields=Number.ZERO,
        max_part_size=SearchLimit.MAXIMUM_SAMPLE_BYTES,
    )
    form: FormData | None = None
    try:
        form = await parser.parse()
        file = form.get(UploadField.FILE)
        if not isinstance(file, UploadFile) or len(form) != Number.ONE:
            raise SearchError(SearchErrorCode.INVALID_SAMPLE)
        while data := await file.read(SearchLimit.CHUNK_BYTES):
            yield data
    except BodyTooLargeError as error:
        raise SearchError(SearchErrorCode.SAMPLE_TOO_LARGE) from error
    except HTTPException as error:
        code = (
            SearchErrorCode.SAMPLE_TOO_LARGE
            if error.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            else SearchErrorCode.INVALID_SAMPLE
        )
        raise SearchError(code) from error
    except (MultiPartException, MultipartParseError, ClientDisconnect) as error:
        raise SearchError(SearchErrorCode.INVALID_SAMPLE) from error
    except OSError as error:
        raise SearchError(SearchErrorCode.UNAVAILABLE) from error
    finally:
        if form is not None:
            await form.close()
        parser.close_open_files()
