"""Потоковые приватные объекты S3 с временными производными файлами."""

import asyncio
from collections.abc import AsyncIterable, Callable
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from typing import cast
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_s3.client import S3Client

from memes_kernel.values import FileRef, Number, Sha256
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.storage import IPrivateStorage
from memes_search.infrastructure.configuration import Settings


class S3PrivateStorage(IPrivateStorage):
    """S3 - единственное постоянное хранилище файлов образца."""

    def __init__(self, settings: Settings) -> None:
        """Создать подключаемый SDK-клиент с IAM или явными credentials."""
        self._bucket = settings.s3_bucket
        options = Config(
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            retries={"max_attempts": settings.s3_retry_attempts, "mode": "standard"},
        )
        factory = cast(Callable[..., S3Client], boto3.client)
        self._client = factory(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            config=options,
        )
        self._signer = factory(
            "s3",
            endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            config=options,
        )

    async def put(
        self,
        key: str,
        file_id: UUID,
        chunks: AsyncIterable[bytes],
        maximum: int,
    ) -> FileRef:
        """Принять ограниченный поток без сохранения важного локального состояния."""
        digest = sha256()
        size: int = Number.ZERO
        with SpooledTemporaryFile(max_size=SearchLimit.CHUNK_BYTES) as temporary:
            async for chunk in chunks:
                size += len(chunk)
                if size > maximum:
                    raise SearchError(SearchErrorCode.SAMPLE_TOO_LARGE)
                digest.update(chunk)
                temporary.write(chunk)
            if size == Number.ZERO:
                raise SearchError(SearchErrorCode.INVALID_SAMPLE)
            temporary.seek(Number.ZERO)
            try:
                await asyncio.to_thread(self._client.upload_fileobj, temporary, self._bucket, key)
            except (BotoCoreError, ClientError, OSError) as error:
                raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        return FileRef(id=file_id, sha256=Sha256(value=digest.hexdigest()), size_bytes=size)

    async def delete(self, key: str) -> None:
        """Удалить конкретный объект идемпотентно, не затронув новые загрузки."""
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def download_url(self, key: str, lifetime_seconds: int) -> str:
        """Выдать короткую подписанную ссылку без публичного ACL."""
        if lifetime_seconds < Number.ONE:
            raise SearchError(SearchErrorCode.NOT_FOUND)
        try:
            return await asyncio.to_thread(
                self._signer.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=lifetime_seconds,
            )
        except (BotoCoreError, ClientError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    def close(self) -> None:
        """Закрыть подключения SDK при остановке web-процесса."""
        self._client.close()
        self._signer.close()
