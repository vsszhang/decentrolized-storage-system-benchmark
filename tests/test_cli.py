from __future__ import annotations

from storage_benchmark.cli import _benchmark_kind, _create_output_dir, _workloads_for_repeat
from storage_benchmark.config import (
    BenchmarkConfig,
    CogOperation,
    CogWorkloadConfig,
    Operation,
    WorkloadConfig,
)


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


def test_benchmark_kind_distinguishes_io_cog_and_mixed() -> None:
    io_workload = WorkloadConfig(
        name="write",
        operation=Operation.SEQWRITE,
        object_size=1024,
    )
    cog_workload = CogWorkloadConfig(
        name="info",
        operation=CogOperation.COGINFO,
        object_key="cog/sample.tif",
    )

    assert _benchmark_kind(BenchmarkConfig(workloads=[io_workload])) == "io"
    assert _benchmark_kind(BenchmarkConfig(cog_workloads=[cog_workload])) == "cog"
    assert (
        _benchmark_kind(BenchmarkConfig(workloads=[io_workload], cog_workloads=[cog_workload]))
        == "mixed"
    )


def test_create_output_dir_uses_benchmark_kind_subdirectory(tmp_path) -> None:
    output_dir = _create_output_dir(tmp_path, "cog")

    assert output_dir.parent == tmp_path / "cog"
    assert output_dir.exists()
