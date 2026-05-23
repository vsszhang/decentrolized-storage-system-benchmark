from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from storage_benchmark.reporting import (
    find_latest_result_dirs,
    generate_compare_report,
    load_run_result,
)


def test_generate_compare_report_writes_combined_outputs_and_io_plots(tmp_path: Path) -> None:
    run_a = _write_result_dir(tmp_path / "results" / "io" / "20260520T100000Z")
    run_b = _write_result_dir(tmp_path / "results" / "io" / "20260520T110000Z", scale=2.0)
    output_dir = tmp_path / "report"

    paths = generate_compare_report([run_a, run_b], output_dir)

    path_names = [path.name for path in paths]
    assert path_names == [
        "combined_metrics.csv",
        "combined_samples.csv",
        "metric_trends_by_run.png",
        "latency_boxplot_by_run.png",
        "throughput_by_run.png",
        "iops_by_run.png",
        "summary.md",
    ]
    assert all(path.exists() for path in paths)
    assert all(path.stat().st_size > 0 for path in paths)

    with (output_dir / "combined_metrics.csv").open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert {row["run_id"] for row in rows} == {"20260520T100000Z", "20260520T110000Z"}
    assert {row["benchmark_type"] for row in rows} == {"io"}
    assert "small-random-read" in (output_dir / "summary.md").read_text(encoding="utf-8")


def test_generate_compare_report_writes_cog_specific_plots(tmp_path: Path) -> None:
    run_a = _write_result_dir(tmp_path / "results" / "cog" / "20260520T100000Z", cog=True)
    run_b = _write_result_dir(tmp_path / "results" / "cog" / "20260520T110000Z", cog=True, scale=1.5)
    output_dir = tmp_path / "cog-report"

    paths = generate_compare_report([run_a, run_b], output_dir)
    path_names = {path.name for path in paths}

    assert "cog_operation_latency_by_run.png" in path_names
    assert "cog_window_latency_by_run.png" in path_names
    assert (output_dir / "combined_samples.csv").exists()


def test_find_latest_result_dirs_selects_latest_valid_results(tmp_path: Path) -> None:
    root = tmp_path / "results" / "cog"
    _write_result_dir(root / "20260520T100000Z")
    selected_a = _write_result_dir(root / "20260520T110000Z")
    selected_b = _write_result_dir(root / "20260520T120000Z")
    (root / "20260520T130000Z").mkdir(parents=True)

    assert find_latest_result_dirs(root, 2) == [selected_a, selected_b]


def test_load_run_result_reports_missing_files(tmp_path: Path) -> None:
    result_dir = tmp_path / "results" / "io" / "20260520T100000Z"
    result_dir.mkdir(parents=True)
    (result_dir / "metrics.csv").write_text("workload,operation\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="samples.csv"):
        load_run_result(result_dir)


def _write_result_dir(path: Path, scale: float = 1.0, cog: bool = False) -> Path:
    path.mkdir(parents=True)
    (path / "run_config.toml").write_text("[run]\nname = \"test\"\n", encoding="utf-8")
    if cog:
        metrics = [
            _metric("cog-info", "coginfo", 0, 0.1 * scale, 0.0, 10.0 / scale),
            _metric("cog-random-window", "randomwindow", 524288, 0.2 * scale, 2.5 / scale, 10.0 / scale),
        ]
        samples = [
            _sample("cog-info", "coginfo", 0, 0.1 * scale, {}),
            _sample("cog-random-window", "randomwindow", 262144, 0.08 * scale, _window(0, 0)),
            _sample("cog-random-window", "randomwindow", 262144, 0.12 * scale, _window(512, 0)),
        ]
    else:
        metrics = [
            _metric("small-random-write", "randomwrite", 49152, 0.3 * scale, 0.15625 / scale, 10.0 / scale),
            _metric("small-random-read", "randomread", 49152, 0.15 * scale, 0.3125 / scale, 20.0 / scale),
        ]
        samples = [
            _sample("small-random-write", "randomwrite", 16384, 0.09 * scale, {}),
            _sample("small-random-write", "randomwrite", 16384, 0.11 * scale, {}),
            _sample("small-random-read", "randomread", 16384, 0.05 * scale, {}),
        ]
    _write_csv(path / "metrics.csv", metrics)
    _write_csv(path / "samples.csv", samples)
    return path


def _metric(
    workload: str,
    operation: str,
    bytes_total: int,
    duration_seconds: float,
    throughput_mb_s: float,
    iops: float,
) -> dict[str, str]:
    return {
        "workload": workload,
        "operation": operation,
        "operations": "3",
        "bytes_total": str(bytes_total),
        "duration_seconds": str(duration_seconds),
        "throughput_mb_s": str(throughput_mb_s),
        "avg_latency_ms": str(duration_seconds / 3 * 1000),
        "p95_latency_ms": str(duration_seconds / 3 * 1200),
        "p99_latency_ms": str(duration_seconds / 3 * 1250),
        "iops": str(iops),
    }


def _sample(
    workload: str,
    operation: str,
    bytes_count: int,
    duration_seconds: float,
    details: dict,
) -> dict[str, str]:
    return {
        "workload": workload,
        "operation": operation,
        "object_key": "object",
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


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
