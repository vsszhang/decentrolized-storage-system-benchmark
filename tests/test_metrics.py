from __future__ import annotations

import csv
import json

import pytest

from storage_benchmark.metrics import OperationSample, summarize_samples, write_metrics


def test_summarize_samples_calculates_core_metrics() -> None:
    samples = [
        OperationSample("w", "seqread", "a", 1024 * 1024, 1.0, "t0"),
        OperationSample("w", "seqread", "b", 1024 * 1024, 3.0, "t1"),
    ]

    [record] = summarize_samples(samples)

    assert record.operations == 2
    assert record.bytes_total == 2 * 1024 * 1024
    assert record.duration_seconds == 4.0
    assert record.throughput_mb_s == 0.5
    assert record.avg_latency_ms == 2000.0
    assert record.p95_latency_ms == pytest.approx(2900.0)
    assert record.p99_latency_ms == pytest.approx(2980.0)
    assert record.iops == 0.5


def test_write_metrics_writes_aggregates_and_raw_samples(tmp_path) -> None:
    samples = [
        OperationSample("w", "seqread", "a", 1024, 0.1, "t0"),
        OperationSample("w", "seqread", "b", 2048, 0.2, "t1", repeat_index=2),
    ]

    records = write_metrics(tmp_path, samples)

    assert len(records) == 1
    assert (tmp_path / "metrics.csv").exists()
    assert (tmp_path / "metrics.json").exists()
    assert (tmp_path / "samples.csv").exists()
    assert (tmp_path / "samples.json").exists()

    with (tmp_path / "samples.json").open(encoding="utf-8") as json_file:
        sample_rows = json.load(json_file)
    assert sample_rows == [
        {
            "workload": "w",
            "operation": "seqread",
            "object_key": "a",
            "bytes_count": 1024,
            "duration_seconds": 0.1,
            "started_at": "t0",
            "repeat_index": 1,
        },
        {
            "workload": "w",
            "operation": "seqread",
            "object_key": "b",
            "bytes_count": 2048,
            "duration_seconds": 0.2,
            "started_at": "t1",
            "repeat_index": 2,
        },
    ]

    with (tmp_path / "samples.csv").open(encoding="utf-8", newline="") as csv_file:
        csv_rows = list(csv.DictReader(csv_file))
    assert [row["object_key"] for row in csv_rows] == ["a", "b"]
    assert [row["duration_seconds"] for row in csv_rows] == ["0.1", "0.2"]
    assert [row["repeat_index"] for row in csv_rows] == ["1", "2"]
