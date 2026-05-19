from __future__ import annotations

from pathlib import Path

import pytest

from storage_benchmark.config import Operation, S3Config, S3Settings, load_config, parse_size


ROOT = Path(__file__).resolve().parents[1]


def test_parse_size_units() -> None:
    assert parse_size("16KiB") == 16 * 1024
    assert parse_size("10MB") == 10 * 1000 * 1000
    assert parse_size(123) == 123


def test_load_smoke_config() -> None:
    config = load_config(ROOT / "configs/minio-smoke.toml")

    assert config.run.name == "minio-smoke"
    assert config.s3.endpoint_url == "http://127.0.0.1:9000"
    assert config.s3.bucket == "benchmark"
    assert config.run.repeats == 3
    assert len(config.workloads) == 4
    assert config.workloads[0].operation == Operation.RANDOMWRITE
    assert config.workloads[0].object_size == 16 * 1024
    assert config.workloads[3].source_workload == "medium-seq-write"


def test_load_full_config_contains_large_report_workloads() -> None:
    config = load_config(ROOT / "configs/minio-full.toml")
    sizes = {workload.object_size for workload in config.workloads}

    assert config.run.repeats == 1
    assert 4 * 1024**3 in sizes
    assert 10 * 1024**3 in sizes
    assert 10 * 1024**2 in sizes
    assert 16 * 1024 in sizes


def test_s3_settings_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://10.0.0.10:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", "override-bucket")

    settings = S3Settings.from_config_and_env(
        S3Config(endpoint_url="http://127.0.0.1:9000", bucket="config-bucket")
    )

    assert settings.endpoint_url == "http://10.0.0.10:9000"
    assert settings.bucket == "override-bucket"


def test_s3_settings_require_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="S3_ACCESS_KEY_ID"):
        S3Settings.from_config_and_env(S3Config(bucket="benchmark"))
