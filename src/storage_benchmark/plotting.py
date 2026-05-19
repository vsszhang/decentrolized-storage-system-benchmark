from __future__ import annotations

import csv
from pathlib import Path


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


def _figure_size(labels: list[str]) -> tuple[float, float]:
    return (max(8.0, len(labels) * 1.6), 5.0)
