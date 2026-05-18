from __future__ import annotations

import io


class FakeS3Client:
    def __init__(self) -> None:
        self.bucket = "test-bucket"
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def put_object(self, key: str, body: bytes | io.RawIOBase, content_length: int | None = None) -> None:
        self.put_calls.append(key)
        if not isinstance(body, bytes):
            start = body.tell()
            body.seek(0, io.SEEK_END)
            body.seek(start, io.SEEK_SET)
        data = body if isinstance(body, bytes) else body.read()
        if content_length is not None:
            assert len(data) == content_length
        self.objects[key] = data

    def get_object_stream(self, key: str) -> io.BytesIO:
        self.get_calls.append(key)
        return io.BytesIO(self.objects[key])

    def delete_object(self, key: str) -> None:
        self.delete_calls.append(key)
        self.objects.pop(key, None)

    def list_keys(self, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]
