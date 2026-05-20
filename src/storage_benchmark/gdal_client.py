from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from storage_benchmark.config import S3Settings


def rasterio_env_kwargs(settings: S3Settings) -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": settings.access_key_id,
        "AWS_SECRET_ACCESS_KEY": settings.secret_access_key,
        "AWS_REGION": settings.region,
        "AWS_S3_ENDPOINT": _endpoint_without_scheme(settings.endpoint_url),
        "AWS_HTTPS": "YES" if settings.use_ssl else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
    }


def vsi_s3_path(bucket: str, object_key: str) -> str:
    return f"/vsis3/{bucket}/{object_key.strip('/')}"


@contextmanager
def open_raster(settings: S3Settings, bucket: str, object_key: str) -> Iterator[Any]:
    import rasterio

    with rasterio.Env(**rasterio_env_kwargs(settings)):
        with rasterio.open(vsi_s3_path(bucket, object_key)) as dataset:
            yield dataset


def _endpoint_without_scheme(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if parsed.netloc:
        return parsed.netloc
    return endpoint_url.removeprefix("http://").removeprefix("https://").strip("/")
