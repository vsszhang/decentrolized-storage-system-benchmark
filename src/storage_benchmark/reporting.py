from __future__ import annotations

import csv
import json
import tomllib
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from storage_benchmark.plotting import COG_OPERATIONS
from storage_benchmark.pairing import completion_issues, validate_pairs

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
    metadata: dict | None
    config: dict


def generate_compare_report(
    result_dirs: list[Path],
    output_dir: Path | None = None,
) -> list[Path]:
    if len(result_dirs) < 2:
        raise ValueError("at least two result directories are required")

    runs = [load_run_result(path) for path in dict.fromkeys(path.resolve() for path in result_dirs)]
    output = output_dir or create_report_output_dir(Path("reports"), benchmark_type_for_runs(runs))
    output.mkdir(parents=True, exist_ok=True)

    eligible = [run for run in runs if not completion_issues(run)]
    combined_metrics = _combine_rows(eligible, "metrics")
    combined_samples = _combine_rows(eligible, "samples")
    pair_result = validate_pairs(runs)
    valid_dirs = {directory for pair in pair_result["pairs"] if pair["status"] == "valid" for directory in pair["run_dirs"]}
    paired = [run for run in runs if str(run.result_dir) in valid_dirs]
    paired_metrics = _combine_rows(paired, "metrics")
    paired_samples = _combine_rows(paired, "samples")

    paths = [
        _write_csv(output / "combined_metrics.csv", combined_metrics),
        _write_csv(output / "combined_samples.csv", combined_samples),
    ]
    paths.extend([
        _write_csv(output / "paired_metrics.csv", paired_metrics),
        _write_csv(output / "paired_samples.csv", paired_samples),
    ])
    validation_path = output / "pair_validation.json"
    validation_path.write_text(json.dumps(pair_result, indent=2), encoding="utf-8")
    paths.append(validation_path)
    plot_paths = _write_compare_plots(output, combined_metrics, combined_samples) if combined_metrics and combined_samples else []
    if paired_metrics:
        import matplotlib.pyplot as plt
        plot_paths.append(_plot_metric_by_run(plt, paired_metrics, output / "paired_throughput.png", "throughput_mb_s", "MiB/s"))
    paths.extend(plot_paths)
    paths.append(_write_summary(output / "summary.md", runs, combined_metrics, plot_paths, pair_result, paired_metrics))
    return paths


def load_run_result(result_dir: Path) -> RunResult:
    missing = [filename for filename in REQUIRED_RESULT_FILES if not (result_dir / filename).exists()]
    if missing:
        raise FileNotFoundError(f"{result_dir} missing required file(s): {', '.join(missing)}")

    metrics = _read_csv(result_dir / "metrics.csv")
    samples = _read_csv(result_dir / "samples.csv")
    meta_path = result_dir / "run_metadata.json"
    metadata = json.loads(meta_path.read_text()) if meta_path.exists() else None
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("run_metadata.json must contain an object")
    with (result_dir / "run_config.toml").open("rb") as stream:
        config = tomllib.load(stream)
    if not metrics and metadata is None:
        raise ValueError(f"no metrics rows found in {result_dir / 'metrics.csv'}")
    if not samples and metadata is None:
        raise ValueError(f"no sample rows found in {result_dir / 'samples.csv'}")

    return RunResult(
        result_dir=result_dir,
        run_id=metadata.get("run_id", result_dir.name) if metadata else result_dir.name,
        benchmark_type=metadata.get("benchmark_type", "unknown") if metadata else benchmark_type_from_result_dir(result_dir),
        metrics=metrics,
        samples=samples, metadata=metadata, config=config,
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
                    **{key: (run.metadata or {}).get(key, "unknown") for key in
                       ("backend", "bucket", "storage_policy", "experiment_id", "pair_index", "status", "cleanup_status")},
                    "run_label": run_label(run),
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
        _plot_metric_by_run(plt, metrics, output / "throughput_by_run.png", "throughput_mb_s", "MiB/s"),
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
        axis.set_title("MiB/s" if metric == "throughput_mb_s" else metric)
        axis.grid(alpha=0.25)
        axis.tick_params(axis="x", labelrotation=25)
    axes_flat[0].legend(fontsize="small", loc="best")
    fig.suptitle("Metric Trends by Run")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_lines_by_workload(axis, rows: list[dict[str, str]], metric: str) -> None:
    run_ids = _ordered_unique(row.get("run_label", row["run_id"]) for row in rows)
    grouped = _metric_values_by_label(rows, metric)
    for label, values_by_run in grouped.items():
        values = [values_by_run.get(run_id) for run_id in run_ids]
        axis.plot(run_ids, values, marker="o", linewidth=1.2, label=label)


def _plot_latency_boxplot_by_run(plt, rows: list[dict[str, str]], path: Path) -> Path:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row.get("run_label", row["run_id"]), []).append(_float(row, "duration_seconds") * 1000)

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
    labels = [f"{row.get('run_label', row['run_id'])}\n{row['workload']}\n{row['operation']}" for row in rows]
    values = [_float(row, metric) for row in rows]

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.bar(labels, values, color="#2563eb")
    ax.set_title(f"{ylabel} by Run")
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=0 if any(row.get("backend", "unknown") != "unknown" for row in rows) else 30)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_window_latency_by_run(plt, rows: list[dict[str, str]], path: Path) -> Path:
    window_rows = [row for row in rows if row["operation"] in {"randomwindow", "tilewindow"}]
    grouped: dict[str, list[float]] = {}
    for row in window_rows:
        grouped.setdefault(row.get("run_label", row["run_id"]), []).append(_float(row, "duration_seconds") * 1000)

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
    labels = [f"{row.get('run_label', row['run_id'])}\n{row['operation']}" for row in cog_rows]
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


def run_label(run: RunResult) -> str:
    meta = run.metadata or {}
    if not meta:
        return run.run_id
    identity = textwrap.fill(run.run_id, width=24, break_on_hyphens=False)
    return f"{meta.get('storage_policy', 'unknown')} / {meta.get('bucket', 'unknown')}\nround {meta.get('pair_index')}\ncleanup {meta.get('cleanup_status')}\n{identity}"


def _write_summary(path, runs, metrics, plot_paths, pair_result, paired_metrics) -> Path:
    lines = ["# Benchmark Compare Summary", "", "## Input Runs", "",
             "| run | backend / bucket / policy | round | status / cleanup | samples | failures / exclusions |",
             "| --- | --- | --- | --- | ---: | --- |"]
    for run in runs:
        meta = run.metadata or {}
        problems = completion_issues(run)
        diagnostic = meta.get("error")
        errors = ([str(diagnostic)] if diagnostic else []) + [str(e) for e in meta.get("cleanup_errors", [])]
        lines.append(f"| {run.run_id} | {meta.get('backend', 'unknown')} / {meta.get('bucket', 'unknown')} / {meta.get('storage_policy', 'unknown')} | "
                     f"{meta.get('pair_index', 'unknown')} | {meta.get('status', 'unknown')} / {meta.get('cleanup_status', 'unknown')} | "
                     f"{len(run.samples)} | {'; '.join(problems + errors) or 'none recorded'} |")
    lines.extend(["", "Sources:"] + [f"- `{run.result_dir}`" for run in runs])
    lines.extend(["", "## Pair Validation", "", "Only valid pairs enter paired_metrics.csv / paired_samples.csv."])
    for pair in pair_result["pairs"]:
        lines.append(f"- {pair['experiment_id']} / round {pair['pair_index']} / {pair['benchmark_type']}: **{pair['status']}**; "
                     + ("; ".join(pair["reasons"]) or "inputs and verification evidence match"))
        lines.append("  Cluster observations: `" + json.dumps(pair["cluster_observations"], ensure_ascii=False) + "`")
    for run in pair_result["unpaired_runs"]:
        lines.append(f"- {run['run_id']}: unpaired ({'; '.join(run['reasons'])})")
    lines.extend(["", "## Descriptive Workload Results", "",
                  "These individual run metrics do not establish a valid REP/EC comparison.", "",
                  "| run | workload | operation | samples | MiB/s | avg ms | P95 ms | P99 ms | ops/s |",
                  "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in metrics:
        lines.append(f"| {row['run_id']} | {row['workload']} | {row['operation']} | {row['operations']} | "
                     + " | ".join(f"{float(row[key]):.4f}" for key in ("throughput_mb_s", "avg_latency_ms", "p95_latency_ms", "p99_latency_ms", "iops")) + " |")
    lines.extend(["", "## Valid Paired Workload Summary", "",
                  "| experiment | policy | workload | operation | metric | runs | mean | min | max |",
                  "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |"])
    grouped = {}
    for row in paired_metrics:
        group = (row["experiment_id"], row["storage_policy"], row["workload"], row["operation"])
        grouped.setdefault(group, []).append(row)
    for group, rows in grouped.items():
        for metric in METRIC_KEYS:
            values = [float(row[metric]) for row in rows]
            unit = "MiB/s" if metric == "throughput_mb_s" else metric
            lines.append("| " + " | ".join(group) + f" | {unit} | {len(values)} | {mean(values):.4f} | {min(values):.4f} | {max(values):.4f} |")
    lines.extend(["", "## Interpretation Limits", "",
                  "- Throughput uses MiB/s; COG byte counts are decoded arrays, not network traffic. Metadata reads have zero counted bytes.",
                  "- Read-only window timings and open-and-read timings are separate operations. Verification runs outside performance samples.",
                  "- IOPS here is operations / summed operation time, not a cluster maximum. Wall time includes verification and bookkeeping.",
                  "- Generated IO data repeats SHA-256 digest patterns; compression/deduplication settings can affect results.",
                  "- P95/P99 summaries above average run-level percentiles; they are not pooled percentiles. Small sample counts limit tail conclusions.",
                  "- First and repeated reads may have different cache states. No cold-cache or redundancy-only causal claim is established.",
                  "- Cluster placement, media, load and recovery conditions may be unknown. Legacy results lack verified pairing metadata.",
                  "- Failed/incomplete runs are listed above and excluded from descriptive performance aggregates; cleanup failures remain visible.",
                  "", "## Generated Plots", ""])
    lines.extend(f"- `{plot.name}`" for plot in plot_paths)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metric_values_by_label(rows: list[dict[str, str]], metric: str) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        label = f"{row['workload']} / {row['operation']}"
        grouped.setdefault(label, {})[row.get("run_label", row["run_id"])] = _float(row, metric)
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
