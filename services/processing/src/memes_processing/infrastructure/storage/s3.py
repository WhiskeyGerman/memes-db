"""Task-scoped S3 ссылки и ограниченная проверка результата."""

import asyncio

import boto3
from botocore.config import Config
from mypy_boto3_s3 import S3Client

from memes_processing.application.dto.coordinator import CoordinatorCode, CoordinatorError
from memes_processing.application.ports.external import IArtifactStorage
from memes_processing.infrastructure.config.settings import ProcessingSettings


class S3Artifacts(IArtifactStorage):
    """SDK используется только инфраструктурным слоем."""

    def __init__(self, settings: ProcessingSettings) -> None:
        """Подключить ресурс из конфигурации релиза."""
        options = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=settings.http_timeout_seconds,
            read_timeout=settings.http_timeout_seconds,
            retries={"max_attempts": 0, "mode": "standard"},
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
            endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=options,
        )
        self.bucket = settings.s3_bucket
        self.result_expiration = settings.task_seconds + settings.lease_seconds
        self.source_expiration = settings.source_url_seconds

    async def delete(self, key: str) -> None:
        """Удалить именованный объект без затрагивания соседних вариаций."""
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def download_url(self, key: str) -> str:
        """Предоставить временный GET одного входного объекта."""
        return await asyncio.to_thread(
            self.signer.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.source_expiration,
        )

    async def upload_url(self, key: str) -> str:
        """Предоставить временный PUT единственного результата."""
        return await asyncio.to_thread(
            self.signer.generate_presigned_url,
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.result_expiration,
        )

    async def result(self, key: str, *, maximum_bytes: int) -> bytes:
        """Проверить Content-Length и ограничить фактически читаемый поток."""
        return await asyncio.to_thread(self._read, key, maximum_bytes)

    def _read(self, key: str, maximum_bytes: int) -> bytes:
        """Не загружать неограниченный объект в память координатора."""
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            if response["ContentLength"] > maximum_bytes:
                raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
            result = body.read(maximum_bytes + 1)
            if len(result) > maximum_bytes:
                raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
            return bytes(result)
        finally:
            body.close()

    async def close(self) -> None:
        """Закрыть внутренний транспорт и отдельный внешний signer."""
        await asyncio.gather(
            asyncio.to_thread(self.client.close),
            asyncio.to_thread(self.signer.close),
        )
