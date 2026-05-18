from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class OperationSample:
    workload: str
    operation: str
    object_key: str
    bytes_count: int
    duration_seconds: float
    started_at: str


@dataclass(frozen=True)
class MetricRecord:
    workload: str
    operation: str
    operations: int
    bytes_total: int
    duration_seconds: float
    throughput_mb_s: float
    avg_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    iops: float


def summarize_samples(samples: list[OperationSample]) -> list[MetricRecord]:
    grouped: dict[tuple[str, str], list[OperationSample]] = {}
    for sample in samples:
        grouped.setdefault((sample.workload, sample.operation), []).append(sample)

    records: list[MetricRecord] = []
    for (workload, operation), group in grouped.items():
        latencies = [sample.duration_seconds for sample in group]
        duration = sum(latencies)
        bytes_total = sum(sample.bytes_count for sample in group)
        operations = len(group)
        records.append(
            MetricRecord(
                workload=workload,
                operation=operation,
                operations=operations,
                bytes_total=bytes_total,
                duration_seconds=duration,
                throughput_mb_s=(bytes_total / 1024 / 1024 / duration) if duration else 0.0,
                avg_latency_ms=(duration / operations * 1000) if operations else 0.0,
                p95_latency_ms=percentile(latencies, 95) * 1000,
                p99_latency_ms=percentile(latencies, 99) * 1000,
                iops=(operations / duration) if duration else 0.0,
            )
        )
    return records


def percentile(values: list[float], percentile_value: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = percentile_value / 100 * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def write_metrics(output_dir: Path, samples: list[OperationSample]) -> list[MetricRecord]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = summarize_samples(samples)

    with (output_dir / "metrics.json").open("w", encoding="utf-8") as json_file:
        json.dump([asdict(record) for record in records], json_file, indent=2)

    with (output_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(MetricRecord.__dataclass_fields__))
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

    return records
