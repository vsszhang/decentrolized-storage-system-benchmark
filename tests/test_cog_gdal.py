from __future__ import annotations

import json
import random
from contextlib import contextmanager
from typing import Any

from storage_benchmark.cog_gdal import run_cog_workloads
from storage_benchmark.config import CogOperation, CogWorkloadConfig, S3Settings


class FakeArray:
    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes


class FakeDataset:
    def __init__(self) -> None:
        self.width = 1024
        self.height = 768
        self.count = 3
        self.crs = "EPSG:4326"
        self.block_shapes = [(512, 512), (512, 512), (512, 512)]
        self.read_calls: list[dict[str, Any]] = []

    def read(self, band_indexes, window=None):
        self.read_calls.append({"band_indexes": band_indexes, "window": window})
        if window is None:
            pixels = self.width * self.height
        else:
            pixels = int(window.width * window.height)
        return FakeArray(pixels * len(band_indexes))


class FakeOpener:
    def __init__(self) -> None:
        self.datasets: list[FakeDataset] = []
        self.open_calls: list[tuple[str, str]] = []

    @contextmanager
    def __call__(self, settings: S3Settings, bucket: str, object_key: str):
        dataset = FakeDataset()
        self.datasets.append(dataset)
        self.open_calls.append((bucket, object_key))
        yield dataset


def test_cog_workloads_generate_samples_with_details() -> None:
    settings = _settings()
    opener = FakeOpener()
    workloads = [
        CogWorkloadConfig(
            name="info",
            operation=CogOperation.COGINFO,
            object_key="cog/sample.tif",
        ),
        CogWorkloadConfig(
            name="full",
            operation=CogOperation.FULLREAD,
            object_key="cog/sample.tif",
            band_indexes=[1, 2],
        ),
        CogWorkloadConfig(
            name="random-window",
            operation=CogOperation.RANDOMWINDOW,
            object_key="cog/sample.tif",
            iterations=2,
            window_width=128,
            window_height=64,
            band_indexes=[1],
        ),
        CogWorkloadConfig(
            name="tile-window",
            operation=CogOperation.TILEWINDOW,
            object_key="cog/sample.tif",
            iterations=2,
            window_width=256,
            window_height=256,
            band_indexes=[1],
        ),
    ]

    samples = run_cog_workloads(workloads, settings, random.Random(7), repeat_index=2, opener=opener)

    assert len(samples) == 6
    assert {sample.repeat_index for sample in samples} == {2}
    assert [sample.operation for sample in samples] == [
        "coginfo",
        "fullread",
        "randomwindow",
        "randomwindow",
        "tilewindow",
        "tilewindow",
    ]
    assert samples[0].bytes_count == 0
    assert samples[1].bytes_count == 1024 * 768 * 2
    assert samples[2].bytes_count == 128 * 64
    assert samples[4].bytes_count == 256 * 256

    details = json.loads(samples[2].details)
    assert details["bucket"] == "benchmark"
    assert details["object_key"] == "cog/sample.tif"
    assert details["band_indexes"] == [1]
    assert details["window"]["width"] == 128
    assert details["window"]["height"] == 64

    assert opener.open_calls == [
        ("benchmark", "cog/sample.tif"),
        ("benchmark", "cog/sample.tif"),
        ("benchmark", "cog/sample.tif"),
        ("benchmark", "cog/sample.tif"),
    ]


def _settings() -> S3Settings:
    return S3Settings(
        endpoint_url="http://127.0.0.1:9000",
        access_key_id="access",
        secret_access_key="secret",
        bucket="benchmark",
    )
