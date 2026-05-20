from __future__ import annotations

from storage_benchmark.config import S3Settings
from storage_benchmark.gdal_client import rasterio_env_kwargs, vsi_s3_path


def test_rasterio_env_kwargs_maps_s3_settings_to_gdal_env() -> None:
    settings = S3Settings(
        endpoint_url="http://127.0.0.1:9000",
        access_key_id="access",
        secret_access_key="secret",
        bucket="benchmark",
        region="us-east-1",
        use_ssl=False,
    )

    env = rasterio_env_kwargs(settings)

    assert env["AWS_ACCESS_KEY_ID"] == "access"
    assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_S3_ENDPOINT"] == "127.0.0.1:9000"
    assert env["AWS_HTTPS"] == "NO"
    assert env["AWS_VIRTUAL_HOSTING"] == "FALSE"


def test_vsi_s3_path_uses_bucket_and_object_key() -> None:
    assert vsi_s3_path("benchmark", "/cog/sample.tif") == "/vsis3/benchmark/cog/sample.tif"
