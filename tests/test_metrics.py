from __future__ import annotations

import pytest

from storage_benchmark.metrics import OperationSample, summarize_samples


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
