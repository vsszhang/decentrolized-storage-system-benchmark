from __future__ import annotations

import random
from pathlib import Path

from storage_benchmark.config import load_config
from storage_benchmark.io_basic import run_workloads

from conftest import FakeS3Client


def test_basic_io_workloads_call_s3_operations(tmp_path: Path) -> None:
    config_path = tmp_path / "benchmark.toml"
    config_path.write_text(
        """
[[workloads]]
name = "seq-write"
operation = "seqwrite"
object_size = "1KiB"
iterations = 2
chunk_size = "512B"
key_prefix = "test/seq"

[[workloads]]
name = "seq-read"
operation = "seqread"
object_size = "1KiB"
iterations = 2
chunk_size = "512B"
key_prefix = "test/seq"
source_workload = "seq-write"

[[workloads]]
name = "random-write"
operation = "randomwrite"
object_size = "512B"
iterations = 3
chunk_size = "256B"
key_prefix = "test/random"

[[workloads]]
name = "random-read"
operation = "randomread"
object_size = "512B"
iterations = 4
chunk_size = "256B"
key_prefix = "test/random"
source_workload = "random-write"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    client = FakeS3Client()

    samples, produced = run_workloads(config.workloads, client, random.Random(7))

    assert len(samples) == 11
    assert len(client.put_calls) == 5
    assert len(client.get_calls) == 6
    assert len(produced["seq-write"]) == 2
    assert len(produced["random-write"]) == 3
    assert all(sample.bytes_count > 0 for sample in samples)
