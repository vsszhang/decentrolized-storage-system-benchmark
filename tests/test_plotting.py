from __future__ import annotations

import csv
from pathlib import Path

from storage_benchmark.plotting import generate_plots


def test_generate_plots_writes_png_files(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "metrics.csv",
        [
            {
                "workload": "small-random-write",
                "operation": "randomwrite",
                "operations": "3",
                "bytes_total": "49152",
                "duration_seconds": "0.3",
                "throughput_mb_s": "0.15625",
                "avg_latency_ms": "100.0",
                "p95_latency_ms": "120.0",
                "p99_latency_ms": "125.0",
                "iops": "10.0",
            },
            {
                "workload": "small-random-read",
                "operation": "randomread",
                "operations": "3",
                "bytes_total": "49152",
                "duration_seconds": "0.15",
                "throughput_mb_s": "0.3125",
                "avg_latency_ms": "50.0",
                "p95_latency_ms": "65.0",
                "p99_latency_ms": "70.0",
                "iops": "20.0",
            },
        ],
    )
    _write_csv(
        tmp_path / "samples.csv",
        [
            {
                "workload": "small-random-write",
                "operation": "randomwrite",
                "object_key": "a",
                "bytes_count": "16384",
                "duration_seconds": "0.09",
                "started_at": "t0",
                "repeat_index": "1",
            },
            {
                "workload": "small-random-write",
                "operation": "randomwrite",
                "object_key": "b",
                "bytes_count": "16384",
                "duration_seconds": "0.11",
                "started_at": "t1",
                "repeat_index": "1",
            },
            {
                "workload": "small-random-read",
                "operation": "randomread",
                "object_key": "a",
                "bytes_count": "16384",
                "duration_seconds": "0.05",
                "started_at": "t2",
                "repeat_index": "1",
            },
        ],
    )

    paths = generate_plots(tmp_path)

    assert [path.name for path in paths] == [
        "throughput_mb_s.png",
        "iops.png",
        "latency_summary_ms.png",
        "latency_distribution_ms.png",
    ]
    assert all(path.exists() for path in paths)
    assert all(path.stat().st_size > 0 for path in paths)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
