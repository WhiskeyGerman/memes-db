"""S3-адаптер с ограниченным чтением и проверкой неизменности байтов."""

import asyncio
from enum import IntEnum, StrEnum
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from typing import cast
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_s3 import S3Client

from memes_catalog.application.ports.resources import IObjectStorage
from memes_catalog.domain.media import MediaFormat
from memes_kernel.values import FileRef, Number, Sha256
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.infrastructure.configuration import BaseServiceSettings


class StorageLimit(IntEnum):
    """Ограничение буфера потоковой передачи."""

    CHUNK_BYTES = 1048576


class MediaContentType(StrEnum):
    """Проверенный MIME для безопасного браузерного воспроизведения."""

    JPEG = "image/jpeg"
    PNG = "image/png"
    APNG = "image/apng"
    WEBP = "image/webp"
    AVIF = "image/avif"
    GIF = "image/gif"
    MP4 = "video/mp4"
    WEBM = "video/webm"
    MP3 = "audio/mpeg"
    M4A = "audio/mp4"
    WAV = "audio/wav"
    OGG = "audio/ogg"
    FLAC = "audio/flac"


class S3ObjectStorage(IObjectStorage):
    """Ограниченный SDK-адаптер приватного bucket."""

    def __init__(
        self, settings: BaseServiceSettings, *, public_endpoint: str | None = None
    ) -> None:
        """Создать клиенты внутренних операций и публичной подписи.

        Args:
            settings: Конфигурация ресурсов.
            public_endpoint: Адрес S3, доступный браузеру.
        """
        options = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=settings.http_timeout_seconds,
            read_timeout=settings.http_timeout_seconds,
        )
        self.client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=options,
        )
        self.signer: S3Client = boto3.client(
            "s3",
            endpoint_url=public_endpoint or settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=options,
        )
        self.bucket = settings.s3_bucket

    async def upload_ticket(
        self, key: str, *, maximum_bytes: int, expires_seconds: int
    ) -> tuple[str, dict[str, str]]:
        """Выдать POST policy только для выбранного ключа и размера."""
        try:
            result = await asyncio.to_thread(
                self.signer.generate_presigned_post,
                Bucket=self.bucket,
                Key=key,
                Conditions=[["content-length-range", Number.ONE, maximum_bytes]],
                ExpiresIn=expires_seconds,
            )
        except (BotoCoreError, ClientError) as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        return cast(str, result["url"]), cast(dict[str, str], result["fields"])

    def _download(self, key: str, destination: Path, maximum_bytes: int) -> tuple[FileRef, str]:
        """Считать один snapshot GET с вычислением SHA-256."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            digest = sha256()
            size: int = Number.ZERO
            try:
                if response["ContentLength"] > maximum_bytes:
                    raise ApplicationError(
                        ErrorCode.INVALID_INPUT, "Файл превышает допустимый размер."
                    )
                with destination.open("wb") as output:
                    while chunk := body.read(StorageLimit.CHUNK_BYTES):
                        size += len(chunk)
                        if size > maximum_bytes:
                            raise ApplicationError(ErrorCode.INVALID_INPUT)
                        digest.update(chunk)
                        output.write(chunk)
            finally:
                body.close()
            return FileRef(
                id=uuid4(), sha256=Sha256(value=digest.hexdigest()), size_bytes=size
            ), response["ETag"]
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == HTTPStatus.NOT_FOUND:
                raise ApplicationError(ErrorCode.NOT_FOUND) from error
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        except BotoCoreError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def download(self, key: str, destination: Path, *, maximum_bytes: int) -> FileRef:
        """Скачать ограниченный файл."""
        file, _etag = await asyncio.to_thread(self._download, key, destination, maximum_bytes)
        return file

    async def download_verified(
        self, key: str, destination: Path, *, maximum_bytes: int
    ) -> tuple[FileRef, str]:
        """Скачать байты и вернуть ETag именно полученной версии.

        Args:
            key: Ключ объекта.
            destination: Временный файл.
            maximum_bytes: Предел чтения.
        """
        return await asyncio.to_thread(self._download, key, destination, maximum_bytes)

    async def copy(
        self, source: str, destination: str, *, expected_etag: str, media_format: MediaFormat
    ) -> None:
        """Копировать только проверенную GET-версию источника."""
        try:
            await asyncio.to_thread(
                self.client.copy_object,
                Bucket=self.bucket,
                Key=destination,
                CopySource={"Bucket": self.bucket, "Key": source},
                CopySourceIfMatch=expected_etag,
                MetadataDirective="REPLACE",
                ContentType=MediaContentType[media_format.name].value,
                ContentDisposition="inline",
            )
        except ClientError as error:
            if (
                error.response["ResponseMetadata"]["HTTPStatusCode"]
                == HTTPStatus.PRECONDITION_FAILED
            ):
                raise ApplicationError(
                    ErrorCode.CONFLICT, "Файл изменён после проверки."
                ) from error
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        except BotoCoreError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def delete(self, key: str) -> None:
        """Идемпотентно удалить строго один ключ."""
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def download_url(self, key: str, *, expires_seconds: int) -> str:
        """Подписать ограниченный GET."""
        return await asyncio.to_thread(
            self.signer.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    async def ready(self) -> bool:
        """Проверить существование bucket."""
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except (BotoCoreError, ClientError):
            return False
        return True
