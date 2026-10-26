from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from storage_benchmark.config import BenchmarkConfig, S3Settings, WorkloadConfig, effective_config, load_config
from storage_benchmark.cog_gdal import run_cog_workloads
from storage_benchmark.io_basic import run_workloads, verify_io
from storage_benchmark.cog_validation import describe_cog, validate_cog
from storage_benchmark.metrics import MetricRecord
from storage_benchmark.plotting import generate_plots
from storage_benchmark.reporting import find_latest_result_dirs, generate_compare_report
from storage_benchmark.s3_client import BotoS3Client
from storage_benchmark.run_state import RunJournal, error_record, object_prefix
from storage_benchmark.s3_check import run_check, verify_deleted


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.cog_reference is not None:
            return run(args.config, args.cog_object_key, args.pair_index, args.cog_reference)
        return run(args.config, args.cog_object_key, args.pair_index)
    if args.command == "check":
        return check(args.config, args.list_objects)
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

    run_parser.add_argument("--pair-index", type=int, default=None, help="override [experiment] pair_index")
    run_parser.add_argument("--cog-reference", type=Path, default=None,
                            help="local reference raster for separate SHA-256, metadata and pixel checks")

    check_parser = subparsers.add_parser("check", help="check S3 access with a disposable small object")
    check_parser.add_argument("--config", "-c", type=Path, required=True)
    check_parser.add_argument("--list", action="store_true", dest="list_objects",
                              help="also require prefix-listing permission")

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


def run(config_path: Path, cog_object_key: str | None = None, pair_index: int | None = None,
        cog_reference: Path | None = None) -> int:
    return _execute(config_path, cog_object_key=cog_object_key, pair_index=pair_index, cog_reference=cog_reference)


def check(config_path: Path, list_objects: bool = False) -> int:
    return _execute(config_path, checking=True, list_objects=list_objects)


def _execute(
    config_path: Path, *, cog_object_key: str | None = None, pair_index: int | None = None,
    checking: bool = False, list_objects: bool = False,
    cog_reference: Path | None = None,
) -> int:
    try:
        config = load_config(config_path, for_check=checking)
        config = _with_cog_object_key_override(config, cog_object_key)
        settings = S3Settings.from_config_and_env(config.s3)
        config = effective_config(config, settings, pair_index)
        reference_manifest = None
        if cog_reference is not None:
            if not config.cog_workloads or len({w.object_key for w in config.cog_workloads}) != 1:
                raise ValueError("--cog-reference requires COG workloads selecting one object key")
            reference_manifest = describe_cog(cog_reference)
    except Exception as exc:
        message = str(exc)
        for name in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            secret = os.getenv(name)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(f"Configuration error: {message}")
        return 2

    kind = "check" if checking else _benchmark_kind(config)
    output_dir = _create_output_dir(config.run.output_dir, kind)
    prefix = object_prefix(config, output_dir.name)
    config = replace(config, workloads=[
        replace(w, key_prefix=f"{prefix}/{w.key_prefix.strip('/')}") for w in config.workloads
    ])
    journal = RunJournal(output_dir, config, settings, kind)
    if reference_manifest:
        journal.metadata["cog_reference"] = {"path": str(cog_reference), **reference_manifest}
        journal.save()
    print(f"Target: endpoint={settings.endpoint_url} bucket={settings.bucket} "
          f"region={settings.region} use_ssl={settings.use_ssl}")
    print(f"Run output: {output_dir}")
    client = None
    status, error, failed_step = "completed", None, None
    try:
        client = BotoS3Client(settings)
        options = getattr(client, "connection_options", None)
        if options:
            journal.metadata["environment"]["client_options"] = options()
            journal.save()
        if checking:
            run_check(client, journal, list_objects=list_objects)
        else:
            rng = random.Random(config.run.seed)
            for repeat_index in range(1, config.run.repeats + 1):
                if config.workloads:
                    run_workloads(
                        _workloads_for_repeat(config.workloads, repeat_index, config.run.repeats),
                        client, rng, repeat_index=repeat_index, journal=journal, data_seed=config.run.seed,
                    )
                if config.cog_workloads:
                    run_cog_workloads(config.cog_workloads, settings, rng,
                                      repeat_index=repeat_index, journal=journal)
            verification_started = time.perf_counter()
            try:
                if config.workloads:
                    verify_io(client, journal)
                if reference_manifest:
                    targets = {(w.bucket or settings.bucket, w.object_key) for w in config.cog_workloads}
                    for bucket, key in sorted(targets):
                        target_settings = replace(settings, bucket=bucket)
                        target_client = client if bucket == settings.bucket else BotoS3Client(target_settings)
                        validate_cog(target_client, target_settings, key, cog_reference, reference_manifest, journal)
            finally:
                journal.metadata["verification_seconds"] = time.perf_counter() - verification_started
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        error, failed_step = exc, journal.step
        # Preserve the primary failure before cleanup can fail or be interrupted.
        journal.metadata.update(status=status, error=error_record(exc, failed_step))
        journal.save()
    finally:
        if config.run.cleanup or checking:
            if client is not None:
                journal.cleanup(client)
                if checking and journal.metadata["cleanup_status"] == "completed":
                    for key in journal.objects:
                        verify_deleted(client, journal, key)
            else:
                journal.metadata["cleanup_status"] = "completed"
        if failed_step is not None:
            journal.step = failed_step
        records = journal.finish(status, error)

    if status != "completed" or journal.metadata["cleanup_status"] == "failed":
        diagnostic = journal.metadata["error"] or journal.metadata["cleanup_errors"][0]
        print(f"Run {status}; see {output_dir}; step={diagnostic['step']} code={journal.redact(diagnostic['code'])}")
        return 1
    print(f"{'Check' if checking else 'Benchmark'} completed: {output_dir}")
    if not checking:
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


def _benchmark_kind(benchmark_config) -> str:
    has_io = bool(benchmark_config.workloads)
    has_cog = bool(benchmark_config.cog_workloads)
    if has_io and has_cog:
        return "mixed"
    if has_cog:
        return "cog"
    return "io"


def _create_output_dir(root: Path, benchmark_kind: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
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
        "MiB/s",
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
