from __future__ import annotations

import csv
import json
from pathlib import Path

COG_OPERATIONS = {"coginfo", "fullread", "randomwindow", "tilewindow"}


def generate_plots(result_dir: Path, output_dir: Path | None = None) -> list[Path]:
    metrics = _read_csv(result_dir / "metrics.csv")
    samples = _read_csv(result_dir / "samples.csv")
    if not metrics:
        raise ValueError(f"no metrics rows found in {result_dir / 'metrics.csv'}")
    if not samples:
        raise ValueError(f"no sample rows found in {result_dir / 'samples.csv'}")

    output = output_dir or result_dir / "plots"
    output.mkdir(parents=True, exist_ok=True)

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    paths = [
        _plot_metric_bars(
            plt,
            metrics,
            output / "throughput_mb_s.png",
            "Throughput by Workload",
            "throughput_mb_s",
            "MB/s",
        ),
        _plot_metric_bars(
            plt,
            metrics,
            output / "iops.png",
            "IOPS by Workload",
            "iops",
            "IOPS",
        ),
        _plot_latency_summary(plt, metrics, output / "latency_summary_ms.png"),
        _plot_latency_distribution(plt, samples, output / "latency_distribution_ms.png"),
    ]
    if _has_cog_samples(samples):
        paths.extend(_generate_cog_plots(plt, metrics, samples, output))
    return paths


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def _label(row: dict[str, str]) -> str:
    return f"{row['workload']}\n{row['operation']}"


def _float(row: dict[str, str], key: str) -> float:
    return float(row[key])


def _plot_metric_bars(plt, rows: list[dict[str, str]], path: Path, title: str, key: str, ylabel: str) -> Path:
    labels = [_label(row) for row in rows]
    values = [_float(row, key) for row in rows]

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.bar(labels, values, color="#2563eb")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_latency_summary(plt, rows: list[dict[str, str]], path: Path) -> Path:
    labels = [_label(row) for row in rows]
    x_positions = list(range(len(rows)))
    width = 0.25

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.bar(
        [position - width for position in x_positions],
        [_float(row, "avg_latency_ms") for row in rows],
        width,
        label="avg",
        color="#0f766e",
    )
    ax.bar(
        x_positions,
        [_float(row, "p95_latency_ms") for row in rows],
        width,
        label="p95",
        color="#f59e0b",
    )
    ax.bar(
        [position + width for position in x_positions],
        [_float(row, "p99_latency_ms") for row in rows],
        width,
        label="p99",
        color="#dc2626",
    )
    ax.set_title("Latency Summary by Workload")
    ax.set_ylabel("Latency (ms)")
    ax.set_xticks(x_positions, labels)
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=30)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_latency_distribution(plt, rows: list[dict[str, str]], path: Path) -> Path:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(_label(row), []).append(_float(row, "duration_seconds") * 1000)

    labels = list(grouped)
    values = [grouped[label] for label in labels]
    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.boxplot(values, tick_labels=labels, showfliers=True)
    ax.set_title("Latency Distribution by Workload")
    ax.set_ylabel("Latency (ms)")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _generate_cog_plots(plt, metrics: list[dict[str, str]], samples: list[dict[str, str]], output: Path) -> list[Path]:
    cog_metrics = [row for row in metrics if row["operation"] in COG_OPERATIONS]
    cog_samples = [row for row in samples if row["operation"] in COG_OPERATIONS]
    paths = [
        _plot_cog_latency_by_operation(plt, cog_samples, output / "cog_latency_by_operation_ms.png"),
        _plot_cog_read_size_by_operation(plt, cog_metrics, output / "cog_read_size_mb.png"),
        _plot_cog_latency_over_time(plt, cog_samples, output / "cog_latency_over_time_ms.png"),
    ]

    window_samples = [_with_details(row) for row in cog_samples if row["operation"] in {"randomwindow", "tilewindow"}]
    window_samples = [row for row in window_samples if "window" in row["_details"]]
    if window_samples:
        paths.append(
            _plot_cog_window_latency_scatter(
                plt,
                window_samples,
                output / "cog_window_latency_scatter_ms.png",
            )
        )
    return paths


def _plot_cog_latency_by_operation(plt, rows: list[dict[str, str]], path: Path) -> Path:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["operation"], []).append(_float(row, "duration_seconds") * 1000)

    labels = list(grouped)
    values = [grouped[label] for label in labels]
    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.boxplot(values, tick_labels=labels, showfliers=True)
    ax.set_title("COG Latency by Operation")
    ax.set_ylabel("Latency (ms)")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_read_size_by_operation(plt, rows: list[dict[str, str]], path: Path) -> Path:
    labels = [row["operation"] for row in rows]
    values = [_float(row, "bytes_total") / 1024 / 1024 for row in rows]

    fig, ax = plt.subplots(figsize=_figure_size(labels))
    ax.bar(labels, values, color="#0f766e")
    ax.set_title("COG Read Volume by Operation")
    ax.set_ylabel("Read volume (MiB)")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=20)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_latency_over_time(plt, rows: list[dict[str, str]], path: Path) -> Path:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["operation"], []).append(_float(row, "duration_seconds") * 1000)

    fig, ax = plt.subplots(figsize=(max(8.0, max((len(values) for values in grouped.values()), default=1) * 0.25), 5.0))
    for label, values in grouped.items():
        ax.plot(range(1, len(values) + 1), values, marker="o", markersize=3, linewidth=1.2, label=label)
    ax.set_title("COG Latency by Sample Order")
    ax.set_xlabel("Sample order within operation")
    ax.set_ylabel("Latency (ms)")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_cog_window_latency_scatter(plt, rows: list[dict[str, str]], path: Path) -> Path:
    x_values = [row["_details"]["window"]["col_off"] for row in rows]
    y_values = [row["_details"]["window"]["row_off"] for row in rows]
    latencies = [_float(row, "duration_seconds") * 1000 for row in rows]
    labels = [row["operation"] for row in rows]
    markers = {"randomwindow": "o", "tilewindow": "s"}

    fig, ax = plt.subplots(figsize=(8.0, 6.0))
    for operation in sorted(set(labels)):
        indexes = [index for index, label in enumerate(labels) if label == operation]
        scatter = ax.scatter(
            [x_values[index] for index in indexes],
            [y_values[index] for index in indexes],
            c=[latencies[index] for index in indexes],
            cmap="viridis",
            marker=markers.get(operation, "o"),
            label=operation,
            edgecolors="black",
            linewidths=0.3,
        )
    ax.set_title("COG Window Latency by Image Position")
    ax.set_xlabel("Column offset")
    ax.set_ylabel("Row offset")
    ax.invert_yaxis()
    ax.grid(alpha=0.2)
    ax.legend()
    fig.colorbar(scatter, ax=ax, label="Latency (ms)")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _has_cog_samples(rows: list[dict[str, str]]) -> bool:
    return any(row["operation"] in COG_OPERATIONS for row in rows)


def _with_details(row: dict[str, str]) -> dict[str, object]:
    try:
        details = json.loads(row.get("details", "") or "{}")
    except json.JSONDecodeError:
        details = {}
    return {**row, "_details": details}


def _figure_size(labels: list[str]) -> tuple[float, float]:
    return (max(8.0, len(labels) * 1.6), 5.0)
