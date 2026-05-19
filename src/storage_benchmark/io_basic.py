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

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new_position = offset
        elif whence == io.SEEK_CUR:
            new_position = self._position + offset
        elif whence == io.SEEK_END:
            new_position = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")

        if new_position < 0:
            raise ValueError("negative seek position")
        self._position = min(new_position, self._size)
        return self._position

    def read(self, size: int = -1) -> bytes:
        if self._position >= self._size:
            return b""
        if size is None or size < 0:
            size = self._size - self._position
        size = min(size, self._size - self._position)
        data = self._bytes_at(self._position, size)
        self._position += len(data)
        return data

    def _bytes_at(self, position: int, size: int) -> bytes:
        out = bytearray()
        block_size = self._chunk_size
        block_index = position // block_size
        block_offset = position % block_size

        while len(out) < size:
            block = self._block(block_index)
            needed = size - len(out)
            out.extend(block[block_offset : block_offset + needed])
            block_index += 1
            block_offset = 0
        return bytes(out)

    def _block(self, block_index: int) -> bytes:
        digest = hashlib.sha256(self._seed + block_index.to_bytes(8, "big")).digest()
        repeats = (self._chunk_size // len(digest)) + 1
        return (digest * repeats)[: self._chunk_size]


def run_workloads(
    workloads: list[WorkloadConfig],
    client: S3Client,
    rng: random.Random,
    repeat_index: int = 1,
) -> tuple[list[OperationSample], dict[str, list[str]]]:
    samples: list[OperationSample] = []
    produced_keys: dict[str, list[str]] = {}

    for workload in workloads:
        if workload.operation == Operation.SEQWRITE:
            keys = seqwrite(workload, client, samples, repeat_index)
        elif workload.operation == Operation.SEQREAD:
            keys = seqread(workload, client, produced_keys, samples, repeat_index)
        elif workload.operation == Operation.RANDOMWRITE:
            keys = randomwrite(workload, client, samples, repeat_index)
        elif workload.operation == Operation.RANDOMREAD:
            keys = randomread(workload, client, produced_keys, samples, rng, repeat_index)
        else:
            raise ValueError(f"unsupported operation: {workload.operation}")
        produced_keys[workload.name] = keys

    return samples, produced_keys


def seqwrite(
    workload: WorkloadConfig,
    client: S3Client,
    samples: list[OperationSample],
    repeat_index: int = 1,
) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/{index:06d}.bin"
        stream = DeterministicBytesStream(workload.object_size, key, workload.chunk_size)
        samples.append(_measure_upload(workload, client, key, stream, repeat_index))
        keys.append(key)
    return keys


def seqread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
    repeat_index: int = 1,
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for key in keys[: workload.iterations]:
        samples.append(_measure_download(workload, client, key, repeat_index))
    return keys


def randomwrite(
    workload: WorkloadConfig,
    client: S3Client,
    samples: list[OperationSample],
    repeat_index: int = 1,
) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/object-{index:06d}.bin"
        stream = DeterministicBytesStream(workload.object_size, key, workload.chunk_size)
        samples.append(_measure_upload(workload, client, key, stream, repeat_index))
        keys.append(key)
    return keys


def randomread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
    rng: random.Random,
    repeat_index: int = 1,
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for _ in range(workload.iterations):
        key = rng.choice(keys)
        samples.append(_measure_download(workload, client, key, repeat_index))
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
    repeat_index: int = 1,
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
        repeat_index=repeat_index,
    )


def _measure_download(
    workload: WorkloadConfig,
    client: S3Client,
    key: str,
    repeat_index: int = 1,
) -> OperationSample:
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
        repeat_index=repeat_index,
    )
