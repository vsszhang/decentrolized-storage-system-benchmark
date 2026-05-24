from __future__ import annotations

from pathlib import Path

from storage_benchmark import cli
from storage_benchmark.cli import (
    _benchmark_kind,
    _create_output_dir,
    _with_cog_object_key_override,
    _workloads_for_repeat,
)
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


def test_cog_object_key_override_updates_all_cog_workloads() -> None:
    config = BenchmarkConfig(
        cog_workloads=[
            CogWorkloadConfig(
                name="info",
                operation=CogOperation.COGINFO,
                object_key="cog/sample.tif",
            ),
            CogWorkloadConfig(
                name="read",
                operation=CogOperation.FULLREAD,
                object_key="cog/sample.tif",
            ),
        ]
    )

    updated = _with_cog_object_key_override(config, "/cog/large.tif")

    assert [workload.object_key for workload in updated.cog_workloads] == [
        "cog/large.tif",
        "cog/large.tif",
    ]
    assert [workload.object_key for workload in config.cog_workloads] == [
        "cog/sample.tif",
        "cog/sample.tif",
    ]


def test_cog_object_key_override_requires_cog_workloads() -> None:
    config = BenchmarkConfig(
        workloads=[
            WorkloadConfig(
                name="write",
                operation=Operation.SEQWRITE,
                object_size=1024,
            )
        ]
    )

    try:
        _with_cog_object_key_override(config, "cog/large.tif")
    except ValueError as exc:
        assert "requires at least one configured COG workload" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_run_cli_accepts_cog_object_key(monkeypatch) -> None:
    captured = {}

    def fake_run(config_path, cog_object_key=None):
        captured["config_path"] = config_path
        captured["cog_object_key"] = cog_object_key
        return 0

    monkeypatch.setattr(cli, "run", fake_run)

    exit_code = cli.main(
        [
            "run",
            "--config",
            "configs/minio-cog-smoke.toml",
            "--cog-object-key",
            "cog/medium.tif",
        ]
    )

    assert exit_code == 0
    assert captured["config_path"] == Path("configs/minio-cog-smoke.toml")
    assert captured["cog_object_key"] == "cog/medium.tif"


def test_compare_cli_accepts_explicit_result_dirs(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_generate_compare_report(result_dirs, output_dir=None):
        captured["result_dirs"] = result_dirs
        captured["output_dir"] = output_dir
        return [tmp_path / "summary.md"]

    monkeypatch.setattr(cli, "generate_compare_report", fake_generate_compare_report)

    exit_code = cli.main(
        [
            "compare",
            "--result-dir",
            "results/cog/a",
            "--result-dir",
            "results/cog/b",
            "--output-dir",
            str(tmp_path / "report"),
        ]
    )

    assert exit_code == 0
    assert captured["result_dirs"] == [Path("results/cog/a"), Path("results/cog/b")]
    assert captured["output_dir"] == tmp_path / "report"


def test_compare_cli_accepts_result_root_and_latest(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_find_latest_result_dirs(result_root, latest):
        captured["result_root"] = result_root
        captured["latest"] = latest
        return [Path("results/cog/a"), Path("results/cog/b"), Path("results/cog/c")]

    def fake_generate_compare_report(result_dirs, output_dir=None):
        captured["result_dirs"] = result_dirs
        captured["output_dir"] = output_dir
        return [tmp_path / "summary.md"]

    monkeypatch.setattr(cli, "find_latest_result_dirs", fake_find_latest_result_dirs)
    monkeypatch.setattr(cli, "generate_compare_report", fake_generate_compare_report)

    exit_code = cli.main(["compare", "--result-root", "results/cog", "--latest", "3"])

    assert exit_code == 0
    assert captured["result_root"] == Path("results/cog")
    assert captured["latest"] == 3
    assert captured["result_dirs"] == [
        Path("results/cog/a"),
        Path("results/cog/b"),
        Path("results/cog/c"),
    ]
