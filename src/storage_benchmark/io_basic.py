from __future__ import annotations

import hashlib
import io
import json
import random
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime

from storage_benchmark.config import IO_GENERATOR_VERSION, Operation, WorkloadConfig
from storage_benchmark.metrics import OperationSample
from storage_benchmark.s3_client import S3Client
from storage_benchmark.run_state import RunJournal, record_sample


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
    journal: RunJournal | None = None,
    data_seed: int = 42,
) -> tuple[list[OperationSample], dict[str, list[str]]]:
    samples: list[OperationSample] = []
    produced_keys: dict[str, list[str]] = {}

    for workload in workloads:
        if journal is not None:
            journal.step = f"{workload.operation.value}:{workload.name}"
        if workload.operation == Operation.SEQWRITE:
            keys = seqwrite(workload, client, samples, repeat_index, journal, data_seed)
        elif workload.operation == Operation.SEQREAD:
            keys = seqread(workload, client, produced_keys, samples, repeat_index, journal)
        elif workload.operation == Operation.RANDOMWRITE:
            keys = randomwrite(workload, client, samples, repeat_index, journal, data_seed)
        elif workload.operation == Operation.RANDOMREAD:
            keys = randomread(workload, client, produced_keys, samples, rng, repeat_index, journal)
        else:
            raise ValueError(f"unsupported operation: {workload.operation}")
        produced_keys[workload.name] = keys

    return samples, produced_keys


def seqwrite(
    workload: WorkloadConfig,
    client: S3Client,
    samples: list[OperationSample],
    repeat_index: int = 1,
    journal: RunJournal | None = None,
    data_seed: int = 42,
) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/{index:06d}.bin"
        identity = content_identity(workload, index, data_seed)
        stream = content_stream(identity)
        record_sample(samples, _measure_upload(workload, client, key, stream, repeat_index, journal, identity), journal)
        keys.append(key)
    return keys


def seqread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
    repeat_index: int = 1,
    journal: RunJournal | None = None,
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for index in range(workload.iterations):
        key = keys[index % len(keys)]
        record_sample(samples, _measure_download(workload, client, key, repeat_index, journal), journal)
    return keys


def randomwrite(
    workload: WorkloadConfig,
    client: S3Client,
    samples: list[OperationSample],
    repeat_index: int = 1,
    journal: RunJournal | None = None,
    data_seed: int = 42,
) -> list[str]:
    keys: list[str] = []
    for index in range(workload.iterations):
        key = f"{workload.key_prefix}/{workload.name}/object-{index:06d}.bin"
        identity = content_identity(workload, index, data_seed)
        stream = content_stream(identity)
        record_sample(samples, _measure_upload(workload, client, key, stream, repeat_index, journal, identity), journal)
        keys.append(key)
    return keys


def randomread(
    workload: WorkloadConfig,
    client: S3Client,
    produced_keys: MutableMapping[str, list[str]],
    samples: list[OperationSample],
    rng: random.Random,
    repeat_index: int = 1,
    journal: RunJournal | None = None,
) -> list[str]:
    keys = _source_keys(workload, produced_keys)
    for _ in range(workload.iterations):
        key = rng.choice(keys)
        record_sample(samples, _measure_download(workload, client, key, repeat_index, journal), journal)
    return keys


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
    journal: RunJournal | None = None,
    identity: dict | None = None,
) -> OperationSample:
    if journal is not None:
        journal.record_object(key, "planned", identity)
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    try:
        client.put_object(key, body, content_length=workload.object_size)
    except (Exception, KeyboardInterrupt):
        if journal is not None:
            journal.record_object(key, "upload_uncertain")
        raise
    duration = time.perf_counter() - started
    if journal is not None:
        journal.record_object(key, "uploaded")
    return OperationSample(
        workload=workload.name,
        operation=workload.operation.value,
        object_key=key,
        bytes_count=workload.object_size,
        duration_seconds=duration,
        started_at=started_at,
        repeat_index=repeat_index,
        details=json.dumps({"content_identity": identity}, sort_keys=True),
    )


def _measure_download(
    workload: WorkloadConfig,
    client: S3Client,
    key: str,
    repeat_index: int = 1,
    journal: RunJournal | None = None,
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
    duration = time.perf_counter() - started
    if bytes_count != workload.object_size:
        raise ValueError("Downloaded length differs from workload object_size")
    return OperationSample(
        workload=workload.name,
        operation=workload.operation.value,
        object_key=key,
        bytes_count=bytes_count,
        duration_seconds=duration,
        started_at=started_at,
        repeat_index=repeat_index,
        details=json.dumps({"content_identity": journal.objects[key].get("content_identity")}, sort_keys=True) if journal else "",
    )


def content_identity(workload: WorkloadConfig, index: int, seed: int) -> dict:
    return {"generator": IO_GENERATOR_VERSION, "seed": seed, "workload": workload.name,
            "index": index, "size": workload.object_size, "chunk_size": workload.chunk_size}


def content_stream(identity: dict) -> DeterministicBytesStream:
    seed = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return DeterministicBytesStream(identity["size"], seed, identity["chunk_size"])


def verify_io(client: S3Client, journal: RunJournal) -> None:
    """Separate full-stream verification; never emits performance samples."""
    journal.step = "io_verification"
    for item in journal.objects.values():
        identity = item.get("content_identity")
        if identity is None:
            continue
        expected = content_stream(identity)
        entry = {"bucket": item["bucket"], "object_key": item["key"], "content_identity": identity,
                 "coverage": "full_object", "bytes_checked": 0, "sha256": None, "status": "running"}
        journal.metadata["io_verification"].append(entry)
        journal.save()
        stream = None
        try:
            stream = client.get_object_stream(item["key"])
            digest = hashlib.sha256()
            while chunk := stream.read(identity["chunk_size"]):
                if chunk != expected.read(len(chunk)):
                    entry["error"] = "content_mismatch"
                    raise ValueError("Object content differs from its logical data identity")
                digest.update(chunk)
                entry["bytes_checked"] += len(chunk)
            if entry["bytes_checked"] != identity["size"]:
                entry["error"] = "length_mismatch"
                raise ValueError("Object length differs from its logical data identity")
            entry.update(status="passed", sha256=digest.hexdigest())
        except (Exception, KeyboardInterrupt):
            entry["status"] = "failed"
            raise
        finally:
            if stream is not None:
                stream.close()
            journal.save()
