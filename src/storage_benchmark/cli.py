from __future__ import annotations

import argparse
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from storage_benchmark.config import BenchmarkConfig, S3Settings, WorkloadConfig, load_config, write_config
from storage_benchmark.cog_gdal import run_cog_workloads
from storage_benchmark.io_basic import cleanup_keys, run_workloads
from storage_benchmark.metrics import MetricRecord, write_metrics
from storage_benchmark.plotting import generate_plots
from storage_benchmark.reporting import find_latest_result_dirs, generate_compare_report
from storage_benchmark.s3_client import BotoS3Client


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config, args.cog_object_key)
    if args.command == "plot":
        return plot(args.result_dir, args.output_dir)
    if args.command == "compare":
        return compare(args.result_dirs, args.result_root, args.latest, args.output_dir)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="storage-benchmark")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="run benchmark workloads")
    run_parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("configs/minio-smoke.toml"),
        help="benchmark TOML configuration",
    )
    run_parser.add_argument(
        "--cog-object-key",
        type=str,
        default=None,
        help="override object_key for all configured COG/GDAL workloads",
    )

    plot_parser = subparsers.add_parser("plot", help="generate PNG plots from a result directory")
    plot_parser.add_argument(
        "--result-dir",
        "-r",
        type=Path,
        required=True,
        help="result directory containing metrics.csv and samples.csv",
    )
    plot_parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="plot output directory, defaults to <result-dir>/plots",
    )

    compare_parser = subparsers.add_parser("compare", help="compare multiple benchmark result directories")
    compare_parser.add_argument(
        "--result-dir",
        "-r",
        type=Path,
        action="append",
        dest="result_dirs",
        default=[],
        help="result directory containing metrics.csv, samples.csv, and run_config.toml; repeatable",
    )
    compare_parser.add_argument(
        "--result-root",
        type=Path,
        default=None,
        help="root directory such as results/cog; latest valid result directories will be selected",
    )
    compare_parser.add_argument(
        "--latest",
        type=int,
        default=5,
        help="number of latest valid result directories to select from --result-root",
    )
    compare_parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="report output directory, defaults to reports/<type>-compare-<timestamp>",
    )
    return parser


def run(config_path: Path, cog_object_key: str | None = None) -> int:
    try:
        benchmark_config = load_config(config_path)
        benchmark_config = _with_cog_object_key_override(benchmark_config, cog_object_key)
        settings = S3Settings.from_config_and_env(benchmark_config.s3)
    except Exception as exc:
        print(f"Configuration error: {exc}")
        return 2

    client = BotoS3Client(settings)
    rng = random.Random(benchmark_config.run.seed)
    output_dir = _create_output_dir(
        benchmark_config.run.output_dir,
        _benchmark_kind(benchmark_config),
    )

    try:
        samples = []
        keys_by_workload = {}
        for repeat_index in range(1, benchmark_config.run.repeats + 1):
            if benchmark_config.workloads:
                repeat_workloads = _workloads_for_repeat(
                    benchmark_config.workloads,
                    repeat_index,
                    benchmark_config.run.repeats,
                )
                repeat_samples, repeat_keys = run_workloads(
                    repeat_workloads,
                    client,
                    rng,
                    repeat_index=repeat_index,
                )
                samples.extend(repeat_samples)
                _merge_keys(keys_by_workload, repeat_keys)
            if benchmark_config.cog_workloads:
                samples.extend(
                    run_cog_workloads(
                        benchmark_config.cog_workloads,
                        settings,
                        rng,
                        repeat_index=repeat_index,
                    )
                )
        records = write_metrics(output_dir, samples)
        write_config(output_dir / "run_config.toml", benchmark_config)
        if benchmark_config.run.cleanup:
            cleanup_keys(client, keys_by_workload)
    except Exception as exc:
        print(f"Benchmark failed. Partial output directory: {output_dir}")
        print(f"Error: {exc}")
        return 1

    print(f"Benchmark completed: {output_dir}")
    _print_records(records)
    return 0


def plot(result_dir: Path, output_dir: Path | None = None) -> int:
    try:
        paths = generate_plots(result_dir, output_dir)
    except Exception as exc:
        print(f"Plot generation failed: {exc}")
        return 1

    print("Generated plots:")
    for path in paths:
        print(f"- {path}")
    return 0


def compare(
    result_dirs: list[Path],
    result_root: Path | None,
    latest: int,
    output_dir: Path | None = None,
) -> int:
    try:
        selected_dirs = list(result_dirs)
        if result_root is not None:
            selected_dirs.extend(find_latest_result_dirs(result_root, latest))
        if not selected_dirs:
            raise ValueError("provide at least two --result-dir values or --result-root")
        paths = generate_compare_report(selected_dirs, output_dir)
    except Exception as exc:
        print(f"Compare report generation failed: {exc}")
        return 1

    print("Generated compare report:")
    for path in paths:
        print(f"- {path}")
    return 0


def _with_cog_object_key_override(
    benchmark_config: BenchmarkConfig,
    cog_object_key: str | None,
) -> BenchmarkConfig:
    if cog_object_key is None:
        return benchmark_config

    normalized = cog_object_key.strip().strip("/")
    if not normalized:
        raise ValueError("--cog-object-key must not be empty")
    if not benchmark_config.cog_workloads:
        raise ValueError("--cog-object-key requires at least one configured COG workload")
    return replace(
        benchmark_config,
        cog_workloads=[
            replace(workload, object_key=normalized)
            for workload in benchmark_config.cog_workloads
        ],
    )


def _workloads_for_repeat(
    workloads: list[WorkloadConfig],
    repeat_index: int,
    repeats: int,
) -> list[WorkloadConfig]:
    if repeats == 1:
        return workloads

    suffix = f"repeat-{repeat_index:03d}"
    return [
        replace(workload, key_prefix=f"{workload.key_prefix}/{suffix}")
        for workload in workloads
    ]


def _merge_keys(
    keys_by_workload: dict[str, list[str]],
    repeat_keys: dict[str, list[str]],
) -> None:
    for workload_name, keys in repeat_keys.items():
        keys_by_workload.setdefault(workload_name, []).extend(keys)


def _benchmark_kind(benchmark_config) -> str:
    has_io = bool(benchmark_config.workloads)
    has_cog = bool(benchmark_config.cog_workloads)
    if has_io and has_cog:
        return "mixed"
    if has_cog:
        return "cog"
    return "io"


def _create_output_dir(root: Path, benchmark_kind: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = root / benchmark_kind
    output_dir = output_root / timestamp
    counter = 1
    while output_dir.exists():
        output_dir = output_root / f"{timestamp}-{counter}"
        counter += 1
    output_dir.mkdir(parents=True)
    return output_dir


def _print_records(records: list[MetricRecord]) -> None:
    header = (
        "workload",
        "operation",
        "ops",
        "MB/s",
        "avg ms",
        "p95 ms",
        "p99 ms",
        "IOPS",
    )
    print(" | ".join(header))
    print("-" * 96)
    for record in records:
        row = (
            record.workload,
            record.operation,
            str(record.operations),
            f"{record.throughput_mb_s:.2f}",
            f"{record.avg_latency_ms:.2f}",
            f"{record.p95_latency_ms:.2f}",
            f"{record.p99_latency_ms:.2f}",
            f"{record.iops:.2f}",
        )
        print(" | ".join(row))


if __name__ == "__main__":
    raise SystemExit(main())
