from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from storage_benchmark.config import S3Settings


def rasterio_session_kwargs(settings: S3Settings) -> dict[str, str]:
    return {
        "aws_access_key_id": settings.access_key_id,
        "aws_secret_access_key": settings.secret_access_key,
        "region_name": settings.region,
        "endpoint_url": _endpoint_without_scheme(settings.endpoint_url),
    }


def rasterio_env_kwargs(settings: S3Settings) -> dict[str, str]:
    return {
        "AWS_HTTPS": "YES" if settings.use_ssl else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
    }


def vsi_s3_path(bucket: str, object_key: str) -> str:
    return f"/vsis3/{bucket}/{object_key.strip('/')}"


@contextmanager
def open_raster(settings: S3Settings, bucket: str, object_key: str) -> Iterator[Any]:
    import rasterio
    from rasterio.session import AWSSession

    session = AWSSession(**rasterio_session_kwargs(settings))
    with rasterio.Env(session=session, **rasterio_env_kwargs(settings)):
        with rasterio.open(vsi_s3_path(bucket, object_key)) as dataset:
            yield dataset


def _endpoint_without_scheme(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if parsed.netloc:
        return parsed.netloc
    return endpoint_url.removeprefix("http://").removeprefix("https://").strip("/")
