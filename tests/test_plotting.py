from __future__ import annotations

import csv
import json
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


def test_generate_plots_writes_cog_specific_png_files(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "metrics.csv",
        [
            {
                "workload": "cog-info",
                "operation": "coginfo",
                "operations": "1",
                "bytes_total": "0",
                "duration_seconds": "0.1",
                "throughput_mb_s": "0.0",
                "avg_latency_ms": "100.0",
                "p95_latency_ms": "100.0",
                "p99_latency_ms": "100.0",
                "iops": "10.0",
            },
            {
                "workload": "cog-random-window",
                "operation": "randomwindow",
                "operations": "2",
                "bytes_total": "524288",
                "duration_seconds": "0.2",
                "throughput_mb_s": "2.5",
                "avg_latency_ms": "100.0",
                "p95_latency_ms": "120.0",
                "p99_latency_ms": "124.0",
                "iops": "10.0",
            },
            {
                "workload": "cog-tile-window",
                "operation": "tilewindow",
                "operations": "2",
                "bytes_total": "524288",
                "duration_seconds": "0.16",
                "throughput_mb_s": "3.125",
                "avg_latency_ms": "80.0",
                "p95_latency_ms": "90.0",
                "p99_latency_ms": "92.0",
                "iops": "12.5",
            },
        ],
    )
    _write_csv(
        tmp_path / "samples.csv",
        [
            _cog_sample("cog-info", "coginfo", 0, 0.1, {}),
            _cog_sample("cog-random-window", "randomwindow", 262144, 0.08, _window(0, 0)),
            _cog_sample("cog-random-window", "randomwindow", 262144, 0.12, _window(512, 512)),
            _cog_sample("cog-tile-window", "tilewindow", 262144, 0.07, _window(0, 0)),
            _cog_sample("cog-tile-window", "tilewindow", 262144, 0.09, _window(512, 0)),
        ],
    )

    paths = generate_plots(tmp_path)

    path_names = [path.name for path in paths]
    assert "cog_latency_by_operation_ms.png" in path_names
    assert "cog_read_size_mb.png" in path_names
    assert "cog_latency_over_time_ms.png" in path_names
    assert "cog_window_latency_scatter_ms.png" in path_names
    assert len(paths) == 8
    assert all(path.exists() for path in paths)
    assert all(path.stat().st_size > 0 for path in paths)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _cog_sample(
    workload: str,
    operation: str,
    bytes_count: int,
    duration_seconds: float,
    details: dict,
) -> dict[str, str]:
    return {
        "workload": workload,
        "operation": operation,
        "object_key": "cog/sample.tif",
        "bytes_count": str(bytes_count),
        "duration_seconds": str(duration_seconds),
        "started_at": "t0",
        "repeat_index": "1",
        "details": json.dumps(details),
    }


def _window(col_off: int, row_off: int) -> dict:
    return {
        "window": {
            "col_off": col_off,
            "row_off": row_off,
            "width": 512,
            "height": 512,
        }
    }
