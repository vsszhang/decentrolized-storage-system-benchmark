from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from storage_benchmark.config import CogOperation, CogWorkloadConfig, S3Settings
from storage_benchmark.gdal_client import open_raster
from storage_benchmark.metrics import OperationSample

DatasetOpener = Callable[[S3Settings, str, str], AbstractContextManager[Any]]


def run_cog_workloads(
    workloads: list[CogWorkloadConfig],
    settings: S3Settings,
    rng: random.Random,
    repeat_index: int = 1,
    opener: DatasetOpener = open_raster,
) -> list[OperationSample]:
    samples: list[OperationSample] = []
    for workload in workloads:
        if workload.operation == CogOperation.COGINFO:
            samples.extend(coginfo(workload, settings, repeat_index, opener))
        elif workload.operation == CogOperation.FULLREAD:
            samples.extend(fullread(workload, settings, repeat_index, opener))
        elif workload.operation == CogOperation.RANDOMWINDOW:
            samples.extend(randomwindow(workload, settings, rng, repeat_index, opener))
        elif workload.operation == CogOperation.TILEWINDOW:
            samples.extend(tilewindow(workload, settings, repeat_index, opener))
        else:
            raise ValueError(f"unsupported COG operation: {workload.operation}")
    return samples


def coginfo(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    repeat_index: int = 1,
    opener: DatasetOpener = open_raster,
) -> list[OperationSample]:
    samples = []
    for _ in range(workload.iterations):
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        with opener(settings, _bucket(workload, settings), workload.object_key) as dataset:
            details = _dataset_details(dataset)
        samples.append(
            _sample(
                workload,
                settings,
                bytes_count=0,
                duration_seconds=time.perf_counter() - started,
                started_at=started_at,
                repeat_index=repeat_index,
                details=details,
            )
        )
    return samples


def fullread(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    repeat_index: int = 1,
    opener: DatasetOpener = open_raster,
) -> list[OperationSample]:
    samples = []
    for _ in range(workload.iterations):
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        with opener(settings, _bucket(workload, settings), workload.object_key) as dataset:
            data = dataset.read(workload.band_indexes)
            details = _dataset_details(dataset)
        samples.append(
            _sample(
                workload,
                settings,
                bytes_count=_nbytes(data),
                duration_seconds=time.perf_counter() - started,
                started_at=started_at,
                repeat_index=repeat_index,
                details=details,
            )
        )
    return samples


def randomwindow(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    rng: random.Random,
    repeat_index: int = 1,
    opener: DatasetOpener = open_raster,
) -> list[OperationSample]:
    from rasterio.windows import Window

    samples = []
    with opener(settings, _bucket(workload, settings), workload.object_key) as dataset:
        for _ in range(workload.iterations):
            window = _random_window(workload, dataset, rng, Window)
            samples.append(_read_window(workload, settings, dataset, window, repeat_index))
    return samples


def tilewindow(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    repeat_index: int = 1,
    opener: DatasetOpener = open_raster,
) -> list[OperationSample]:
    from rasterio.windows import Window

    samples = []
    with opener(settings, _bucket(workload, settings), workload.object_key) as dataset:
        for index in range(workload.iterations):
            window = _tile_window(workload, dataset, index, Window)
            samples.append(_read_window(workload, settings, dataset, window, repeat_index))
    return samples


def _read_window(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    dataset: Any,
    window: Any,
    repeat_index: int,
) -> OperationSample:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    data = dataset.read(workload.band_indexes, window=window)
    details = _dataset_details(dataset)
    details["window"] = {
        "col_off": int(window.col_off),
        "row_off": int(window.row_off),
        "width": int(window.width),
        "height": int(window.height),
    }
    return _sample(
        workload,
        settings,
        bytes_count=_nbytes(data),
        duration_seconds=time.perf_counter() - started,
        started_at=started_at,
        repeat_index=repeat_index,
        details=details,
    )


def _random_window(workload: CogWorkloadConfig, dataset: Any, rng: random.Random, window_cls: Any) -> Any:
    width = min(workload.window_width or dataset.width, dataset.width)
    height = min(workload.window_height or dataset.height, dataset.height)
    max_col = max(dataset.width - width, 0)
    max_row = max(dataset.height - height, 0)
    return window_cls(rng.randint(0, max_col), rng.randint(0, max_row), width, height)


def _tile_window(workload: CogWorkloadConfig, dataset: Any, index: int, window_cls: Any) -> Any:
    width = min(workload.window_width or dataset.width, dataset.width)
    height = min(workload.window_height or dataset.height, dataset.height)
    cols = max((dataset.width + width - 1) // width, 1)
    col = index % cols
    row = (index // cols) % max((dataset.height + height - 1) // height, 1)
    col_off = col * width
    row_off = row * height
    return window_cls(
        col_off,
        row_off,
        min(width, dataset.width - col_off),
        min(height, dataset.height - row_off),
    )


def _dataset_details(dataset: Any) -> dict[str, Any]:
    return {
        "width": dataset.width,
        "height": dataset.height,
        "count": dataset.count,
        "crs": str(getattr(dataset, "crs", "")),
        "block_shapes": [list(shape) for shape in getattr(dataset, "block_shapes", [])],
    }


def _sample(
    workload: CogWorkloadConfig,
    settings: S3Settings,
    bytes_count: int,
    duration_seconds: float,
    started_at: str,
    repeat_index: int,
    details: dict[str, Any],
) -> OperationSample:
    details = {
        **details,
        "bucket": _bucket(workload, settings),
        "object_key": workload.object_key,
        "band_indexes": workload.band_indexes,
    }
    return OperationSample(
        workload=workload.name,
        operation=workload.operation.value,
        object_key=workload.object_key,
        bytes_count=bytes_count,
        duration_seconds=duration_seconds,
        started_at=started_at,
        repeat_index=repeat_index,
        details=json.dumps(details, sort_keys=True),
    )


def _bucket(workload: CogWorkloadConfig, settings: S3Settings) -> str:
    return workload.bucket or settings.bucket


def _nbytes(data: Any) -> int:
    return int(getattr(data, "nbytes", len(data) if hasattr(data, "__len__") else 0))
