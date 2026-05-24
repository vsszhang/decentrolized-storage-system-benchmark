from __future__ import annotations

from pathlib import Path

import pytest

from storage_benchmark.config import (
    CogOperation,
    Operation,
    S3Config,
    S3Settings,
    load_config,
    parse_size,
    write_config,
)


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


def test_load_matrix_config_contains_full_operation_matrix() -> None:
    config = load_config(ROOT / "configs/minio-matrix.toml")
    workloads_by_name = {workload.name: workload for workload in config.workloads}

    assert config.run.name == "minio-matrix"
    assert config.run.repeats == 1
    assert len(config.workloads) == 12
    for size_name in ("small", "medium", "large"):
        assert any(name.startswith(f"{size_name}-seq-write") for name in workloads_by_name)
        assert any(name.startswith(f"{size_name}-seq-read") for name in workloads_by_name)
        assert any(name.startswith(f"{size_name}-random-write") for name in workloads_by_name)
        assert any(name.startswith(f"{size_name}-random-read") for name in workloads_by_name)

    assert workloads_by_name["large-random-read-4gb"].source_workload == "large-random-write-4gb"
    assert workloads_by_name["large-seq-write-4gb"].object_size == 4 * 1024**3


def test_load_cog_smoke_config() -> None:
    config = load_config(ROOT / "configs/minio-cog-smoke.toml")
    workloads_by_name = {workload.name: workload for workload in config.cog_workloads}

    assert config.run.name == "minio-cog-smoke"
    assert config.s3.endpoint_url == "http://127.0.0.1:9000"
    assert config.s3.bucket == "benchmark"
    assert config.run.repeats == 3
    assert config.workloads == []
    assert len(config.cog_workloads) == 4
    assert workloads_by_name["cog-info"].operation == CogOperation.COGINFO
    assert workloads_by_name["cog-full-read"].band_indexes == [1]
    assert workloads_by_name["cog-random-window"].window_width == 512
    assert workloads_by_name["cog-tile-window"].window_height == 512


def test_load_mixed_config(tmp_path: Path) -> None:
    config_path = tmp_path / "mixed.toml"
    config_path.write_text(
        """
[[workloads]]
name = "write"
operation = "seqwrite"
object_size = "1KiB"

[[cog_workloads]]
name = "cog-info"
operation = "coginfo"
object_key = "cog/sample.tif"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert len(config.workloads) == 1
    assert len(config.cog_workloads) == 1


def test_invalid_cog_window_config_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "bad-cog.toml"
    config_path.write_text(
        """
[[cog_workloads]]
name = "bad-window"
operation = "randomwindow"
object_key = "cog/sample.tif"
iterations = 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires window_width and window_height"):
        load_config(config_path)


def test_write_config_round_trips_effective_cog_object_key(tmp_path: Path) -> None:
    config = load_config(ROOT / "configs/minio-cog-smoke.toml")
    updated = config.__class__(
        s3=config.s3,
        run=config.run,
        workloads=config.workloads,
        cog_workloads=[
            workload.__class__(
                name=workload.name,
                operation=workload.operation,
                object_key="cog/large.tif",
                bucket=workload.bucket,
                iterations=workload.iterations,
                window_width=workload.window_width,
                window_height=workload.window_height,
                band_indexes=workload.band_indexes,
                overview_level=workload.overview_level,
            )
            for workload in config.cog_workloads
        ],
    )
    output_path = tmp_path / "run_config.toml"

    write_config(output_path, updated)
    reloaded = load_config(output_path)

    assert {workload.object_key for workload in reloaded.cog_workloads} == {"cog/large.tif"}


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
