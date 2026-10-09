"""Одноразовая подготовка приватных S3 buckets в окружении релиза."""

from enum import IntEnum, StrEnum
from typing import Literal, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from mypy_boto3_s3.literals import BucketLocationConstraintType
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSetting(StrEnum):
    """Параметры подписанного S3-транспорта."""

    PREFIX = "MEMES_"
    REGION = "us-east-1"
    ADDRESSING_STYLE = "path"
    SIGNATURE = "s3v4"
    NO_SUCH_BUCKET = "NoSuchBucket"
    NOT_FOUND = "404"


class StorageLimit(IntEnum):
    """Ограниченные ожидания административного процесса."""

    TIMEOUT_SECONDS = 5
    RETRY_ATTEMPTS = 5


class StorageSettings(BaseSettings):
    """Подключаемый S3 ресурс без ненужных PostgreSQL credentials."""

    model_config = SettingsConfigDict(env_prefix=StorageSetting.PREFIX, extra="ignore")
    s3_endpoint_url: str | None = None
    s3_access_key: str = Field(min_length=1, repr=False)
    s3_secret_key: str = Field(min_length=1, repr=False)
    s3_bucket: str = Field(min_length=1)
    journal_bucket: str = Field(min_length=1)
    s3_region: str = StorageSetting.REGION


def main() -> None:
    """Создать приватные media и journal buckets без публичного ACL."""
    settings = StorageSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(
            signature_version=StorageSetting.SIGNATURE,
            s3={"addressing_style": cast(Literal["path"], StorageSetting.ADDRESSING_STYLE)},
            connect_timeout=StorageLimit.TIMEOUT_SECONDS,
            read_timeout=StorageLimit.TIMEOUT_SECONDS,
            retries={"max_attempts": StorageLimit.RETRY_ATTEMPTS},
        ),
    )
    try:
        for bucket in dict.fromkeys((settings.s3_bucket, settings.journal_bucket)):
            try:
                client.head_bucket(Bucket=bucket)
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code not in {StorageSetting.NO_SUCH_BUCKET, StorageSetting.NOT_FOUND}:
                    raise
                if settings.s3_region == StorageSetting.REGION:
                    client.create_bucket(Bucket=bucket)
                else:
                    client.create_bucket(
                        Bucket=bucket,
                        CreateBucketConfiguration={
                            "LocationConstraint": cast(
                                BucketLocationConstraintType, settings.s3_region
                            )
                        },
                    )
    finally:
        client.close()


if __name__ == "__main__":
    main()
