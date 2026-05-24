from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Operation(StrEnum):
    SEQWRITE = "seqwrite"
    SEQREAD = "seqread"
    RANDOMWRITE = "randomwrite"
    RANDOMREAD = "randomread"


class CogOperation(StrEnum):
    COGINFO = "coginfo"
    FULLREAD = "fullread"
    RANDOMWINDOW = "randomwindow"
    TILEWINDOW = "tilewindow"


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str = "http://127.0.0.1:9000"
    bucket: str | None = None
    region: str = "us-east-1"
    use_ssl: bool = False


@dataclass(frozen=True)
class RunConfig:
    name: str = "benchmark"
    output_dir: Path = Path("results")
    seed: int = 42
    repeats: int = 1
    cleanup: bool = False


@dataclass(frozen=True)
class WorkloadConfig:
    name: str
    operation: Operation
    object_size: int
    iterations: int = 1
    chunk_size: int = 8 * 1024 * 1024
    key_prefix: str = "benchmark"
    source_workload: str | None = None


@dataclass(frozen=True)
class CogWorkloadConfig:
    name: str
    operation: CogOperation
    object_key: str
    bucket: str | None = None
    iterations: int = 1
    window_width: int | None = None
    window_height: int | None = None
    band_indexes: list[int] = field(default_factory=lambda: [1])
    overview_level: int | None = None


@dataclass(frozen=True)
class BenchmarkConfig:
    s3: S3Config = field(default_factory=S3Config)
    run: RunConfig = field(default_factory=RunConfig)
    workloads: list[WorkloadConfig] = field(default_factory=list)
    cog_workloads: list[CogWorkloadConfig] = field(default_factory=list)


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    region: str = "us-east-1"
    use_ssl: bool = False

    @classmethod
    def from_config_and_env(cls, config: S3Config) -> S3Settings:
        endpoint_url = os.getenv("S3_ENDPOINT_URL", config.endpoint_url)
        bucket = os.getenv("S3_BUCKET", config.bucket or "")
        region = os.getenv("S3_REGION", config.region)
        use_ssl = parse_bool(os.getenv("S3_USE_SSL", str(config.use_ssl)))
        access_key_id = os.getenv("S3_ACCESS_KEY_ID", "")
        secret_access_key = os.getenv("S3_SECRET_ACCESS_KEY", "")

        missing = []
        if not access_key_id:
            missing.append("S3_ACCESS_KEY_ID")
        if not secret_access_key:
            missing.append("S3_SECRET_ACCESS_KEY")
        if not bucket:
            missing.append("S3_BUCKET or s3.bucket")
        if missing:
            raise RuntimeError(f"Missing required S3 settings: {', '.join(missing)}")

        return cls(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
            region=region,
            use_ssl=use_ssl,
        )


def load_config(path: Path) -> BenchmarkConfig:
    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    s3 = _load_s3_config(raw.get("s3", {}))
    run = _load_run_config(raw.get("run", {}))
    workloads = [_load_workload_config(item) for item in raw.get("workloads", [])]
    cog_workloads = [_load_cog_workload_config(item) for item in raw.get("cog_workloads", [])]
    config = BenchmarkConfig(s3=s3, run=run, workloads=workloads, cog_workloads=cog_workloads)
    _validate_config(config)
    return config


def write_config(path: Path, config: BenchmarkConfig) -> None:
    lines = [
        "[s3]",
        f"endpoint_url = {_toml_value(config.s3.endpoint_url)}",
        f"bucket = {_toml_value(config.s3.bucket)}" if config.s3.bucket is not None else "bucket = \"\"",
        f"region = {_toml_value(config.s3.region)}",
        f"use_ssl = {_toml_value(config.s3.use_ssl)}",
        "",
        "[run]",
        f"name = {_toml_value(config.run.name)}",
        f"output_dir = {_toml_value(str(config.run.output_dir))}",
        f"seed = {config.run.seed}",
        f"repeats = {config.run.repeats}",
        f"cleanup = {_toml_value(config.run.cleanup)}",
        "",
    ]

    for workload in config.workloads:
        lines.extend(
            [
                "[[workloads]]",
                f"name = {_toml_value(workload.name)}",
                f"operation = {_toml_value(workload.operation.value)}",
                f"object_size = {workload.object_size}",
                f"iterations = {workload.iterations}",
                f"chunk_size = {workload.chunk_size}",
                f"key_prefix = {_toml_value(workload.key_prefix)}",
            ]
        )
        if workload.source_workload is not None:
            lines.append(f"source_workload = {_toml_value(workload.source_workload)}")
        lines.append("")

    for workload in config.cog_workloads:
        lines.extend(
            [
                "[[cog_workloads]]",
                f"name = {_toml_value(workload.name)}",
                f"operation = {_toml_value(workload.operation.value)}",
                f"object_key = {_toml_value(workload.object_key)}",
                f"iterations = {workload.iterations}",
                f"band_indexes = {_toml_value(workload.band_indexes)}",
            ]
        )
        if workload.bucket is not None:
            lines.append(f"bucket = {_toml_value(workload.bucket)}")
        if workload.window_width is not None:
            lines.append(f"window_width = {workload.window_width}")
        if workload.window_height is not None:
            lines.append(f"window_height = {workload.window_height}")
        if workload.overview_level is not None:
            lines.append(f"overview_level = {workload.overview_level}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _load_s3_config(raw: dict[str, Any]) -> S3Config:
    return S3Config(
        endpoint_url=str(raw.get("endpoint_url", "http://127.0.0.1:9000")),
        bucket=raw.get("bucket"),
        region=str(raw.get("region", "us-east-1")),
        use_ssl=parse_bool(raw.get("use_ssl", False)),
    )


def _load_run_config(raw: dict[str, Any]) -> RunConfig:
    return RunConfig(
        name=str(raw.get("name", "benchmark")),
        output_dir=Path(raw.get("output_dir", "results")),
        seed=int(raw.get("seed", 42)),
        repeats=int(raw.get("repeats", 1)),
        cleanup=parse_bool(raw.get("cleanup", False)),
    )


def _load_workload_config(raw: dict[str, Any]) -> WorkloadConfig:
    _require_keys(raw, "name", "operation", "object_size")
    operation = Operation(str(raw["operation"]))
    return WorkloadConfig(
        name=str(raw["name"]),
        operation=operation,
        object_size=parse_size(raw["object_size"]),
        iterations=int(raw.get("iterations", 1)),
        chunk_size=parse_size(raw.get("chunk_size", 8 * 1024 * 1024)),
        key_prefix=str(raw.get("key_prefix", "benchmark")).strip("/"),
        source_workload=raw.get("source_workload"),
    )


def _load_cog_workload_config(raw: dict[str, Any]) -> CogWorkloadConfig:
    _require_keys(raw, "name", "operation", "object_key")
    operation = CogOperation(str(raw["operation"]))
    return CogWorkloadConfig(
        name=str(raw["name"]),
        operation=operation,
        object_key=str(raw["object_key"]).strip("/"),
        bucket=str(raw["bucket"]) if raw.get("bucket") else None,
        iterations=int(raw.get("iterations", 1)),
        window_width=parse_size(raw["window_width"]) if "window_width" in raw else None,
        window_height=parse_size(raw["window_height"]) if "window_height" in raw else None,
        band_indexes=[int(index) for index in raw.get("band_indexes", [1])],
        overview_level=int(raw["overview_level"]) if "overview_level" in raw else None,
    )


def _require_keys(raw: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in raw]
    if missing:
        raise ValueError(f"missing required config field(s): {', '.join(missing)}")


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.workloads and not config.cog_workloads:
        raise ValueError("at least one workload or cog_workload is required")
    if config.run.repeats <= 0:
        raise ValueError("run repeats must be positive")

    names: set[str] = set()
    for workload in config.workloads:
        if not workload.name:
            raise ValueError("workload name is required")
        if workload.name in names:
            raise ValueError(f"duplicate workload name: {workload.name}")
        if workload.object_size <= 0:
            raise ValueError(f"workload {workload.name} object_size must be positive")
        if workload.iterations <= 0:
            raise ValueError(f"workload {workload.name} iterations must be positive")
        if workload.chunk_size <= 0:
            raise ValueError(f"workload {workload.name} chunk_size must be positive")
        names.add(workload.name)

    for workload in config.workloads:
        is_read = workload.operation in {Operation.SEQREAD, Operation.RANDOMREAD}
        if is_read and not workload.source_workload:
            raise ValueError(f"{workload.operation} workload requires source_workload")
        if workload.source_workload and workload.source_workload not in names:
            raise ValueError(
                f"workload {workload.name} references unknown source_workload "
                f"{workload.source_workload}"
            )

    cog_names: set[str] = set()
    for workload in config.cog_workloads:
        if not workload.name:
            raise ValueError("cog workload name is required")
        if workload.name in cog_names:
            raise ValueError(f"duplicate cog workload name: {workload.name}")
        if not workload.object_key:
            raise ValueError(f"cog workload {workload.name} object_key is required")
        if workload.iterations <= 0:
            raise ValueError(f"cog workload {workload.name} iterations must be positive")
        if not workload.band_indexes:
            raise ValueError(f"cog workload {workload.name} band_indexes is required")
        if any(index <= 0 for index in workload.band_indexes):
            raise ValueError(f"cog workload {workload.name} band indexes must be positive")
        if workload.overview_level is not None and workload.overview_level < 0:
            raise ValueError(f"cog workload {workload.name} overview_level must be non-negative")

        is_window_read = workload.operation in {
            CogOperation.RANDOMWINDOW,
            CogOperation.TILEWINDOW,
        }
        if is_window_read:
            if workload.window_width is None or workload.window_height is None:
                raise ValueError(
                    f"cog workload {workload.name} requires window_width and window_height"
                )
            if workload.window_width <= 0 or workload.window_height <= 0:
                raise ValueError(f"cog workload {workload.name} window size must be positive")
        cog_names.add(workload.name)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def parse_size(value: Any) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise TypeError(f"size must be int bytes or string, got {type(value).__name__}")

    normalized = value.strip().replace(" ", "").upper()
    units = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)]
            return int(float(number) * multiplier)
    return int(normalized)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
