from __future__ import annotations

from typing import BinaryIO, Protocol

import boto3
from botocore.config import Config

from storage_benchmark.config import S3Settings


class S3Client(Protocol):
    bucket: str

    def put_object(self, key: str, body: bytes | BinaryIO, content_length: int | None = None) -> None:
        ...

    def get_object_stream(self, key: str) -> BinaryIO:
        ...

    def delete_object(self, key: str) -> None:
        ...

    def list_keys(self, prefix: str) -> list[str]:
        ...


class BotoS3Client:
    def __init__(self, settings: S3Settings) -> None:
        self.bucket = settings.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=settings.region,
            use_ssl=settings.use_ssl,
            config=Config(signature_version="s3v4"),
        )

    def put_object(self, key: str, body: bytes | BinaryIO, content_length: int | None = None) -> None:
        kwargs = {"Bucket": self.bucket, "Key": key, "Body": body}
        if content_length is not None:
            kwargs["ContentLength"] = content_length
        self._client.put_object(**kwargs)

    def get_object_stream(self, key: str) -> BinaryIO:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"]

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys
