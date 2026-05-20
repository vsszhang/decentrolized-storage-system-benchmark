from __future__ import annotations

from storage_benchmark.config import S3Settings
from storage_benchmark.gdal_client import (
    rasterio_env_kwargs,
    rasterio_session_kwargs,
    vsi_s3_path,
)


def test_rasterio_session_kwargs_maps_credentials_to_awssession() -> None:
    settings = S3Settings(
        endpoint_url="http://127.0.0.1:9000",
        access_key_id="access",
        secret_access_key="secret",
        bucket="benchmark",
        region="us-east-1",
        use_ssl=False,
    )

    session_kwargs = rasterio_session_kwargs(settings)

    assert session_kwargs["aws_access_key_id"] == "access"
    assert session_kwargs["aws_secret_access_key"] == "secret"
    assert session_kwargs["region_name"] == "us-east-1"
    assert session_kwargs["endpoint_url"] == "127.0.0.1:9000"


def test_rasterio_env_kwargs_only_contains_gdal_options() -> None:
    settings = S3Settings(
        endpoint_url="http://127.0.0.1:9000",
        access_key_id="access",
        secret_access_key="secret",
        bucket="benchmark",
        region="us-east-1",
        use_ssl=False,
    )

    env = rasterio_env_kwargs(settings)

    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_S3_ENDPOINT" not in env
    assert env["AWS_HTTPS"] == "NO"
    assert env["AWS_VIRTUAL_HOSTING"] == "FALSE"


def test_vsi_s3_path_uses_bucket_and_object_key() -> None:
    assert vsi_s3_path("benchmark", "/cog/sample.tif") == "/vsis3/benchmark/cog/sample.tif"
