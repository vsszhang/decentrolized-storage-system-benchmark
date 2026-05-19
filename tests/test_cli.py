from __future__ import annotations

from storage_benchmark.cli import _workloads_for_repeat
from storage_benchmark.config import Operation, WorkloadConfig


def test_workloads_for_single_repeat_preserves_key_prefix() -> None:
    workload = WorkloadConfig(
        name="write",
        operation=Operation.SEQWRITE,
        object_size=1024,
        key_prefix="benchmark/full",
    )

    [repeat_workload] = _workloads_for_repeat([workload], repeat_index=1, repeats=1)

    assert repeat_workload.key_prefix == "benchmark/full"


def test_workloads_for_multiple_repeats_namespaces_key_prefix() -> None:
    workload = WorkloadConfig(
        name="write",
        operation=Operation.SEQWRITE,
        object_size=1024,
        key_prefix="benchmark/smoke",
    )

    [repeat_workload] = _workloads_for_repeat([workload], repeat_index=2, repeats=3)

    assert repeat_workload.key_prefix == "benchmark/smoke/repeat-002"
