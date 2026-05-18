from __future__ import annotations

import hashlib
import io
import random
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime

from storage_benchmark.config import Operation, WorkloadConfig
from storage_benchmark.metrics import OperationSample
from storage_benchmark.s3_client import S3Client


class DeterministicBytesStream(io.RawIOBase):
    def __init__(self, size: int, seed: str, chunk_size: int) -> None:
        self._size = size
        self._seed = seed.encode("utf-8")
        self._chunk_size = chunk_size
        self._position = 0
        self._block_index = 0
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._position >= self._size:
            return b""
        if size is None or size < 0:
            size = self._size - self._position
        size = min(size, self._size - self._position)

        out = bytearray()
        while len(out) < size:
            if not self._buffer:
                self._buffer = self._next_block()
            needed = size - len(out)
            out.extend(self._buffer[:needed])
            self._buffer = self._buffer[needed:]
        self._position += len(out)
        return bytes(out)

    def _next_block(self) -> bytes:
        digest = hashlib.sha256(self._seed + self._block_index.to_bytes(8, "big")).digest()
        self._block_index += 1
        repeats = (self._chunk_size // len(digest)) + 1
        return (digest * repeats)[: self._chunk_size]


def run_workloads(
    workloads: list[WorkloadConfig],
    client: S3Client,
    rng: random.Random,
) -> tuple[list[OperationSample], dict[str, list[str]]]:
    samples: list[OperationSample] = []
    produced_keys: dict[str, list[str]] = {}

    for workload in workloads:
        if workload.operation == Operation.SEQWRITE:
            keys = seqwrite(workload, client, samples)
        elif workload.operation == Operation.SEQREAD:
            keys = seqread(workload, client, produced_keys, samples)
        elif workload.operation == Operation.RANDOMWRITE:
            keys = randomwrite(workload, client, samples)
        elif workload.operation == Operation.RANDOMREAD:
            keys = randomread(workload, client, produced_keys, samples, rng)
        else:
            raise ValueError(f"unsupported operation: {workload.operation}")
        produced_keys[workload.name] = keys

    return samples, produced_keys


def seqwrite(workload: WorkloadConfig, client: S3Client, samples: list[OperationSample]) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/{index:06d}.bin"
        stream = DeterministicBytesStream(workload.object_size, key, workload.chunk_size)
        samples.append(_measure_upload(workload, client, key, stream))
        keys.append(key)
    return keys


def seqread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for key in keys[: workload.iterations]:
        samples.append(_measure_download(workload, client, key))
    return keys


def randomwrite(workload: WorkloadConfig, client: S3Client, samples: list[OperationSample]) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/object-{index:06d}.bin"
        stream = DeterministicBytesStream(workload.object_size, key, workload.chunk_size)
        samples.append(_measure_upload(workload, client, key, stream))
        keys.append(key)
    return keys


def randomread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
    rng: random.Random,
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for _ in range(workload.iterations):
        key = rng.choice(keys)
        samples.append(_measure_download(workload, client, key))
    return keys


def cleanup_keys(client: S3Client, keys_by_workload: dict[str, list[str]]) -> None:
    seen: set[str] = set()
    for keys in keys_by_workload.values():
        for key in keys:
            if key not in seen:
                client.delete_object(key)
                seen.add(key)


def _source_keys(
    workload: WorkloadConfig,
    produced_keys: MutableMapping[str, list[str]],
) -> list[str]:
    assert workload.source_workload is not None
    keys = produced_keys.get(workload.source_workload, [])
    if not keys:
        raise RuntimeError(f"source workload has no keys: {workload.source_workload}")
    return keys


def _measure_upload(
    workload: WorkloadConfig,
    client: S3Client,
    key: str,
    body: DeterministicBytesStream,
) -> OperationSample:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    client.put_object(key, body, content_length=workload.object_size)
    return OperationSample(
        workload=workload.name,
        operation=workload.operation.value,
        object_key=key,
        bytes_count=workload.object_size,
        duration_seconds=time.perf_counter() - started,
        started_at=started_at,
    )


def _measure_download(workload: WorkloadConfig, client: S3Client, key: str) -> OperationSample:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    bytes_count = 0
    stream = client.get_object_stream(key)
    try:
        while True:
            chunk = stream.read(workload.chunk_size)
            if not chunk:
                break
            bytes_count += len(chunk)
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return OperationSample(
        workload=workload.name,
        operation=workload.operation.value,
        object_key=key,
        bytes_count=bytes_count,
        duration_seconds=time.perf_counter() - started,
        started_at=started_at,
    )
