from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from storage_benchmark.plotting import COG_OPERATIONS

REQUIRED_RESULT_FILES = ("metrics.csv", "samples.csv", "run_config.toml")
METRIC_KEYS = (
    "throughput_mb_s",
    "iops",
    "avg_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
)


@dataclass(frozen=True)
class RunResult:
    result_dir: Path
    run_id: str
    benchmark_type: str
    metrics: list[dict[str, str]]
    samples: list[dict[str, str]]


def generate_compare_report(
    result_dirs: list[Path],
    output_dir: Path | None = None,
) -> list[Path]:
    if len(result_dirs) < 2:
        raise ValueError("at least two result directories are required")

    runs = [load_run_result(path) for path in result_dirs]
    output = output_dir or create_report_output_dir(Path("reports"), benchmark_type_for_runs(runs))
    output.mkdir(parents=True, exist_ok=True)

    combined_metrics = _combine_rows(runs, "metrics")
    combined_samples = _combine_rows(runs, "samples")

    paths = [
        _write_csv(output / "combined_metrics.csv", combined_metrics),
        _write_csv(output / "combined_samples.csv", combined_samples),
    ]
    plot_paths = _write_compare_plots(output, combined_metrics, combined_samples)
    paths.extend(plot_paths)
    paths.append(_write_summary(output / "summary.md", runs, combined_metrics, plot_paths))
    return paths


def load_run_result(result_dir: Path) -> RunResult:
    missing = [filename for filename in REQUIRED_RESULT_FILES if not (result_dir / filename).exists()]
    if missing:
        raise FileNotFoundError(f"{result_dir} missing required file(s): {', '.join(missing)}")

    metrics = _read_csv(result_dir / "metrics.csv")
    samples = _read_csv(result_dir / "samples.csv")
    if not metrics:
        raise ValueError(f"no metrics rows found in {result_dir / 'metrics.csv'}")
    if not samples:
        raise ValueError(f"no sample rows found in {result_dir / 'samples.csv'}")

    return RunResult(
        result_dir=result_dir,
        run_id=result_dir.name,
        benchmark_type=benchmark_type_from_result_dir(result_dir),
        metrics=metrics,
        samples=samples,
    )


def find_latest_result_dirs(result_root: Path, latest: int) -> list[Path]:
    if latest <= 0:
        raise ValueError("latest must be positive")
    if not result_root.exists():
        raise FileNotFoundError(result_root)
    valid_dirs = [
        path
        for path in result_root.iterdir()
        if path.is_dir() and all((path / filename).exists() for filename in REQUIRED_RESULT_FILES)
    ]
    selected = sorted(valid_dirs, key=lambda path: path.name, reverse=True)[:latest]
    return sorted(selected, key=lambda path: path.name)


def benchmark_type_from_result_dir(result_dir: Path) -> str:
    parent_name = result_dir.parent.name
    if parent_name in {"io", "cog", "mixed"}:
        return parent_name
    return "unknown"


def benchmark_type_for_runs(runs: list[RunResult]) -> str:
    types = {run.benchmark_type for run in runs}
    if len(types) == 1:
        return next(iter(types))
    return "mixed"


def create_report_output_dir(root: Path, benchmark_type: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = root / f"{benchmark_type}-compare-{timestamp}"
    counter = 1
    while output_dir.exists():
        output_dir = root / f"{benchmark_type}-compare-{timestamp}-{counter}"
        counter += 1
    return output_dir


def _combine_rows(runs: list[RunResult], field_name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for run in runs:
        for row in getattr(run, field_name):
            rows.append(
                {
                    "run_id": run.run_id,
                    "result_dir": str(run.result_dir),
                    "benchmark_type": run.benchmark_type,
                    **row,
                }
            )
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    fieldnames = _fieldnames(rows)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _fieldnames(rows: list[dict[str, str]]) -> list[str]:
    preferred = ["run_id", "result_dir", "benchmark_type"]
    seen = set(preferred)
    fieldnames = preferred.copy()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    return fieldnames


def _write_compare_plots(
    output: Path,
    metrics: list[dict[str, str]],
    samples: list[dict[str, str]],
) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    paths = [
        _plot_metric_trends(plt, metrics, output / "metric_trends_by_run.png"),
        _plot_latency_boxplot_by_run(plt, samples, output / "latency_boxplot_by_run.png"),
        _plot_metric_by_run(plt, metrics, output / "throughput_by_run.png", "throughput_mb_s", "MB/s"),
        _plot_metric_by_run(plt, metrics, output / "iops_by_run.png", "iops", "IOPS"),
    ]
    if any(row["operation"] in COG_OPERATIONS for row in samples):
        cog_paths = [
            _plot_cog_operation_latency_by_run(
                plt,
                samples,
                output / "cog_operation_latency_by_run.png",
            )
        ]
        if any(row["operation"] in {"randomwindow", "tilewindow"} for row in samples):
            cog_paths.append(
                _plot_cog_window_latency_by_run(
                    plt,
                    samples,
                    output / "cog_window_latency_by_run.png",
                )
            )
        paths.extend(
            sorted(cog_paths, key=lambda path: path.name)
        )
    return paths


def _plot_metric_trends(plt, rows: list[dict[str, str]], path: Path) -> Path:
    fig, axes = plt.subplots(2, 2, figsize=(14.0, 8.0))
    axes_flat = list(axes.flat)
    for axis, metric in zip(axes_flat, ("throughput_mb_s", "iops", "avg_latency_ms", "p95_latency_ms"), strict=True):
        _plot_lines_by_workload(axis, rows, metric)
        axis.set_title(metric)
        axis.grid(alpha=0.25)
        axis.tick_params(axis="x", labelrotation=25)
    axes_flat[0].legend(fontsize="small", loc="best")
    fig.suptitle("Metric Trends by Run")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_lines_by_workload(axis, rows: list[dict[str, str]], metric: str) -> None:
    run_ids = _ordered_unique(row["run_id"] for row in rows)
    grouped = _metric_values_by_label(rows, metric)
    for label, values_by_run in grouped.items():
        values = [values_by_run.get(run_id) for run_id in run_ids]
        axis.plot(run_ids, values, marker="o", linewidth=1.2, label=label)


def _plot_latency_boxplot_by_run(plt, rows: list[dict[str, str]], path: Path) -> Path:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(_float(row, "duration_seconds") * 1000)

    labels = list(grouped)
    values = [grouped[label] for label in labels]
    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.boxplot(values, tick_labels=labels, showfliers=True)
    ax.set_title("Latency Distribution by Run")
    ax.set_ylabel("Latency (ms)")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_metric_by_run(plt, rows: list[dict[str, str]], path: Path, metric: str, ylabel: str) -> Path:
    labels = [f"{row['run_id']}\n{row['workload']}\n{row['operation']}" for row in rows]
    values = [_float(row, metric) for row in rows]

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.bar(labels, values, color="#2563eb")
    ax.set_title(f"{ylabel} by Run")
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_window_latency_by_run(plt, rows: list[dict[str, str]], path: Path) -> Path:
    window_rows = [row for row in rows if row["operation"] in {"randomwindow", "tilewindow"}]
    grouped: dict[str, list[float]] = {}
    for row in window_rows:
        grouped.setdefault(row["run_id"], []).append(_float(row, "duration_seconds") * 1000)

    labels = list(grouped)
    values = [grouped[label] for label in labels]
    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.boxplot(values, tick_labels=labels, showfliers=True)
    ax.set_title("COG Window Latency by Run")
    ax.set_ylabel("Latency (ms)")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_operation_latency_by_run(plt, rows: list[dict[str, str]], path: Path) -> Path:
    cog_rows = [row for row in rows if row["operation"] in COG_OPERATIONS]
    labels = [f"{row['run_id']}\n{row['operation']}" for row in cog_rows]
    values = [_float(row, "duration_seconds") * 1000 for row in cog_rows]

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.scatter(range(len(labels)), values, color="#0f766e", s=18)
    ax.set_title("COG Operation Latency Samples by Run")
    ax.set_ylabel("Latency (ms)")
    ax.set_xticks(range(len(labels)), labels)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _write_summary(
    path: Path,
    runs: list[RunResult],
    metrics: list[dict[str, str]],
    plot_paths: list[Path],
) -> Path:
    lines = [
        "# Benchmark Compare Summary",
        "",
        "## Input Runs",
        "",
    ]
    for run in runs:
        lines.append(f"- `{run.run_id}`: `{run.result_dir}` ({run.benchmark_type})")

    lines.extend(["", "## Workload Summary", ""])
    lines.append("| workload | operation | metric | mean | min | max |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: |")
    for (workload, operation), group in _group_metrics(metrics).items():
        for metric in METRIC_KEYS:
            values = [_float(row, metric) for row in group]
            lines.append(
                f"| {workload} | {operation} | {metric} | "
                f"{mean(values):.4f} | {min(values):.4f} | {max(values):.4f} |"
            )

    lines.extend(["", "## Generated Plots", ""])
    for plot_path in plot_paths:
        lines.append(f"- `{plot_path.name}`")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _group_metrics(rows: list[dict[str, str]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row["workload"], row["operation"]), []).append(row)
    return grouped


def _metric_values_by_label(rows: list[dict[str, str]], metric: str) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        label = f"{row['workload']} / {row['operation']}"
        grouped.setdefault(label, {})[row["run_id"]] = _float(row, metric)
    return grouped


def _ordered_unique(values) -> list[str]:
    seen = set()
    ordered = []
    for value in values:
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def _float(row: dict[str, str], key: str) -> float:
    return float(row[key])


def _figure_size(labels: list[str]) -> tuple[float, float]:
    return (max(8.0, len(labels) * 1.2), 5.0)
