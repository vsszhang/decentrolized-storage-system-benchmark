from __future__ import annotations

from collections.abc import Callable
from typing import Any, BinaryIO

from botocore.exceptions import ClientError

from storage_benchmark.run_state import RunJournal, error_record
from storage_benchmark.s3_client import S3Client


class CheckFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_up_to(stream: BinaryIO, limit: int) -> bytes:
    chunks = []
    remaining = limit
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _step(journal: RunJournal, name: str, action: Callable[[], Any]) -> Any:
    journal.step = name
    try:
        result = action()
    except (Exception, KeyboardInterrupt) as exc:
        journal.metadata["checks"].append({"step": name, "status": "failed", "error": error_record(exc, name)})
        journal.save()
        raise
    journal.metadata["checks"].append({"step": name, "status": "passed"})
    journal.save()
    journal.log(f"Check passed: {name}")
    return result


def run_check(client: S3Client, journal: RunJournal, *, list_objects: bool = False) -> str:
    key = f"{journal.prefix}/check/object.bin"
    payload = bytes(range(256)) * 16

    def put() -> None:
        journal.record_object(key, "planned")
        try:
            client.put_object(key, payload, content_length=len(payload))
        except (Exception, KeyboardInterrupt):
            journal.record_object(key, "upload_uncertain")
            raise
        journal.record_object(key, "uploaded")

    def head() -> None:
        if client.head_object(key).get("ContentLength") != len(payload):
            raise CheckFailure("head_length_mismatch")

    def get() -> None:
        stream = client.get_object_stream(key)
        try:
            if _read_up_to(stream, len(payload) + 1) != payload:
                raise CheckFailure("get_content_mismatch")
        finally:
            stream.close()

    def ranged_get() -> None:
        start, end = 17, 126
        response = client.get_object_range(key, start, end)
        stream = response["Body"]
        try:
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 206:
                raise CheckFailure("range_status_mismatch")
            if response.get("ContentRange") != f"bytes {start}-{end}/{len(payload)}":
                raise CheckFailure("content_range_mismatch")
            if response.get("ContentLength") != end - start + 1:
                raise CheckFailure("range_length_mismatch")
            if _read_up_to(stream, end - start + 2) != payload[start:end + 1]:
                raise CheckFailure("range_content_mismatch")
        finally:
            stream.close()

    def listing() -> None:
        if key not in client.list_keys(journal.prefix + "/"):
            raise CheckFailure("list_missing_object")

    for name, action in (("put", put), ("head", head), ("get", get), ("range_get", ranged_get)):
        _step(journal, name, action)
    if list_objects:
        _step(journal, "list", listing)
    else:
        journal.metadata["checks"].append({"step": "list", "status": "skipped", "reason": "not_requested"})
        journal.save()
    return key


def verify_deleted(client: S3Client, journal: RunJournal, key: str) -> None:
    def verify() -> None:
        try:
            client.head_object(key)
        except ClientError as exc:
            if (exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
                    and exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}):
                return
            raise
        raise CheckFailure("deleted_object_still_present")

    try:
        _step(journal, "delete_verify", verify)
    except (Exception, KeyboardInterrupt) as exc:
        journal.metadata["cleanup_status"] = "failed"
        journal.metadata["cleanup_errors"].append(error_record(exc, "delete_verify"))
        journal.record_object(key, "delete_failed")
        journal.save()
