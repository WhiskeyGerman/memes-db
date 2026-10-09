"""Append-only S3-журнал удаления в отдельном ресурсе резервирования."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from enum import IntEnum, StrEnum
from http import HTTPStatus
from typing import cast
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_s3 import S3Client
from mypy_boto3_s3.type_defs import ListObjectsV2RequestTypeDef

from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.application.ports.deletion_journal import IDeletionJournal
from memes_platform.infrastructure.configuration import BaseServiceSettings


class JournalLimit(IntEnum):
    """Размер страницы и одной обезличенной записи."""

    RECORD_BYTES = 65536
    PAGE_SIZE = 1000


class JournalSetting(StrEnum):
    """Независимое пространство журнала."""

    PREFIX = "deletion-journal/"
    CONTENT_TYPE = "application/json"
    ACCOUNT = "account"
    MEDIA = "media"


class S3DeletionJournal(IDeletionJournal):
    """Неизменяемая запись, сохранённая до завершения SQL-операции."""

    def __init__(self, settings: BaseServiceSettings) -> None:
        """Сохранить независимый bucket с политикой хранения.

        Args:
            settings: Адрес и полномочия ресурса.
        """
        if not settings.journal_bucket:
            raise ValueError("MEMES_JOURNAL_BUCKET required")
        self.bucket = settings.journal_bucket
        self.client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=settings.http_timeout_seconds,
                read_timeout=settings.http_timeout_seconds,
            ),
        )

    async def record(self, event: JsonObject) -> None:
        """Записать только обезличенный авторизованный intent."""
        identifier = UUID(str(event["event_id"]))
        kind = str(event["kind"])
        if kind not in (JournalSetting.ACCOUNT, JournalSetting.MEDIA):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        if len(payload) > JournalLimit.RECORD_BYTES:
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        key = f"{JournalSetting.PREFIX}{kind}/{identifier}.json"
        try:
            await asyncio.to_thread(
                self.client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType=JournalSetting.CONTENT_TYPE,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if (
                error.response["ResponseMetadata"]["HTTPStatusCode"]
                != HTTPStatus.PRECONDITION_FAILED
            ):
                raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        except BotoCoreError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def records(self, *, since: datetime | None = None) -> AsyncIterator[JsonObject]:
        """Прочитать страницы S3 без хранения всего журнала в памяти."""
        token: str | None = None
        while True:
            try:
                parameters: ListObjectsV2RequestTypeDef = {
                    "Bucket": self.bucket,
                    "Prefix": JournalSetting.PREFIX,
                    "MaxKeys": JournalLimit.PAGE_SIZE,
                }
                if token is not None:
                    parameters["ContinuationToken"] = token
                response = await asyncio.to_thread(self.client.list_objects_v2, **parameters)
                for item in response.get("Contents", []):
                    if since is not None and item["LastModified"] < since:
                        continue
                    result = await asyncio.to_thread(
                        self.client.get_object, Bucket=self.bucket, Key=item["Key"]
                    )
                    body = result["Body"]
                    try:
                        payload = await asyncio.to_thread(body.read, JournalLimit.RECORD_BYTES + 1)
                    finally:
                        body.close()
                    if len(payload) > JournalLimit.RECORD_BYTES:
                        raise ApplicationError(ErrorCode.INVALID_INPUT)
                    yield cast(JsonObject, json.loads(payload))
                if not response["IsTruncated"]:
                    return
                token = response["NextContinuationToken"]
            except (BotoCoreError, ClientError) as error:
                raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def ready(self) -> bool:
        """Проверить доступность отдельного ресурса журнала."""
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except (BotoCoreError, ClientError):
            return False
        return True

    def close(self) -> None:
        """Закрыть принадлежащий процессу пул."""
        self.client.close()
