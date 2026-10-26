import io
import json
from pathlib import Path

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from botocore.exceptions import ClientError

from conftest import FakeS3Client

from storage_benchmark import cli
from storage_benchmark.config import S3Config, S3Settings
from storage_benchmark.s3_client import BotoS3Client


@pytest.mark.parametrize("scenario,expected_exit,expected_error", [
    ("success", 0, None), ("list", 0, None),
    ("put_denied", 1, "AccessDenied"), ("head_denied", 1, "AccessDenied"),
    ("head_length", 1, "head_length_mismatch"), ("get_content", 1, "get_content_mismatch"),
    ("range_status", 1, "range_status_mismatch"),
    ("range_header", 1, "content_range_mismatch"),
    ("range_length", 1, "range_length_mismatch"),
    ("range_content", 1, "range_content_mismatch"),
    ("list_denied", 1, "AccessDenied"),
    ("delete_denied", 1, None), ("verify_denied", 1, None),
    ("verify_bucket_missing", 1, None), ("still_present", 1, None),
    ("primary_and_cleanup_denied", 1, "AccessDenied"),
])
def test_check_sdk_requests_failures_cleanup_and_stream_closure(
    tmp_path, monkeypatch, capsys, scenario, expected_exit, expected_error,
):
    for name in ("S3_BUCKET", "S3_REGION", "S3_USE_SSL", "S3_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-not-real")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-not-real")
    path = tmp_path / "check.toml"
    # check must not execute, or even need to parse, performance workloads.
    path.write_text(f'''[s3]
endpoint_url="http://rgw.example.test:8080"
bucket="zhang_rep"
[run]
output_dir="{tmp_path}"
[[workloads]]
operation="invalid-performance-workload"
''')
    output = tmp_path / "check" / "fixed-run"
    def create(root, kind):
        assert kind == "check"
        output.mkdir(parents=True)
        return output
    monkeypatch.setattr(cli, "_create_output_dir", create)
    settings = S3Settings.from_config_and_env(S3Config(endpoint_url="http://rgw.example.test:8080", bucket="zhang_rep"))
    client = BotoS3Client(settings)
    assert client.connection_options()["addressing_style"] == "path"
    monkeypatch.setattr(cli, "BotoS3Client", lambda settings: client)
    stub = Stubber(client._client)
    key = "benchmark/standalone/fixed-run/check/object.bin"
    target = {"Bucket": "zhang_rep", "Key": key}
    payload = bytes(range(256)) * 16
    raw_streams = []
    def body(data):
        raw = io.BytesIO(data)
        raw_streams.append(raw)
        return StreamingBody(raw, len(data))
    def denied(operation, params):
        stub.add_client_error(operation, "AccessDenied",
            service_message="test-secret-not-real https://host/?X-Amz-Signature=hidden",
            http_status_code=403, expected_params=params)
    primary_put = scenario == "put_denied"
    primary_head = scenario in {"head_denied", "primary_and_cleanup_denied"}
    if primary_put:
        denied("put_object", {**target, "Body": payload, "ContentLength": len(payload)})
    else:
        stub.add_response("put_object", {}, {**target, "Body": payload, "ContentLength": len(payload)})
        if primary_head:
            denied("head_object", target)
        else:
            stub.add_response("head_object", {"ContentLength": 1 if scenario == "head_length" else len(payload)}, target)
            if scenario != "head_length":
                full = b"incorrect" if scenario == "get_content" else payload
                stub.add_response("get_object", {"Body": body(full)}, target)
                if scenario != "get_content":
                    ranged = b"X" * 110 if scenario == "range_content" else payload[17:127]
                    response = {"Body": body(ranged), "ContentRange": "bytes 17-126/4096",
                                "ContentLength": 110, "ResponseMetadata": {"HTTPStatusCode": 206}}
                    if scenario == "range_status": response["ResponseMetadata"]["HTTPStatusCode"] = 200
                    if scenario == "range_header": response["ContentRange"] = "bytes 0-109/4096"
                    if scenario == "range_length": response["ContentLength"] = 1
                    stub.add_response("get_object", response, {**target, "Range": "bytes=17-126"})
    listing = scenario in {"list", "list_denied"}
    if listing:
        params = {"Bucket": "zhang_rep", "Prefix": "benchmark/standalone/fixed-run/"}
        if scenario == "list_denied":
            denied("list_objects_v2", params)
        else:
            stub.add_response("list_objects_v2", {"Contents": [{"Key": key}], "IsTruncated": False}, params)
    cleanup_denied = scenario in {"delete_denied", "primary_and_cleanup_denied"}
    if cleanup_denied:
        denied("delete_object", target)
    else:
        stub.add_response("delete_object", {}, target)
        if scenario == "verify_denied":
            denied("head_object", target)
        elif scenario == "still_present":
            stub.add_response("head_object", {"ContentLength": len(payload)}, target)
        else:
            code = "NoSuchBucket" if scenario == "verify_bucket_missing" else "NoSuchKey"
            stub.add_client_error("head_object", code, http_status_code=404, expected_params=target)
    with stub:
        args = ["check", "-c", str(path)] + (["--list"] if listing else [])
        assert cli.main(args) == expected_exit
        stub.assert_no_pending_responses()
    meta = json.loads((output / "run_metadata.json").read_text())
    cleanup_failed = scenario in {"delete_denied", "verify_denied", "verify_bucket_missing", "still_present", "primary_and_cleanup_denied"}
    assert meta["cleanup_status"] == ("failed" if cleanup_failed else "completed")
    assert meta["status"] == ("failed" if expected_error else "completed")
    if expected_error:
        assert meta["error"]["code"] == expected_error
    if cleanup_failed:
        assert meta["cleanup_errors"]
    assert meta["benchmark_type"] == "check" and meta["pair_index"] is None
    assert json.loads((output / "samples.json").read_text()) == []
    assert all(stream.closed for stream in raw_streams)
    persisted = "".join(file.read_text() for file in output.iterdir() if file.is_file())
    persisted += capsys.readouterr().out
    assert "test-access-not-real" not in persisted
    assert "test-secret-not-real" not in persisted
    assert "X-Amz-Signature" not in persisted


def test_check_missing_credentials_returns_configuration_error(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    path = tmp_path / "check.toml"
    path.write_text('[s3]\nbucket="probe"\n')
    def forbidden(settings):
        pytest.fail("No client or network request is allowed without credentials")
    monkeypatch.setattr(cli, "BotoS3Client", forbidden)
    assert cli.check(path) == 2


@pytest.mark.parametrize("profile", ["rep-io", "ec-io", "rep-cog", "ec-cog"])
def test_all_four_real_profiles_check_their_own_bucket_without_running_workloads(tmp_path, monkeypatch, profile):
    class ProbeClient(FakeS3Client):
        def head_object(self, key):
            if key not in self.objects:
                raise ClientError({"Error": {"Code": "NoSuchKey"},
                                   "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
            return {"ContentLength": len(self.objects[key])}
        def get_object_range(self, key, start, end):
            data = self.objects[key][start:end + 1]
            return {"Body": io.BytesIO(data), "ContentLength": len(data),
                    "ContentRange": f"bytes {start}-{end}/{len(self.objects[key])}",
                    "ResponseMetadata": {"HTTPStatusCode": 206}}
    client = ProbeClient()
    source = Path(__file__).resolve().parents[1] / f"configs/ceph-{profile}-smoke.toml"
    path = tmp_path / "profile.toml"
    root = tmp_path / "output"
    path.write_text(source.read_text().replace('output_dir = "results/ceph-rep"', f'output_dir = "{root}"')
                    .replace('output_dir = "results/ceph-ec"', f'output_dir = "{root}"'))
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://rgw.example.test:8080")
    monkeypatch.setenv("S3_REGION", "test-region")
    monkeypatch.setenv("S3_USE_SSL", "false")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-not-real")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-not-real")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    expected_bucket = "zhang_rep" if profile.startswith("rep") else "zhang_ec"
    def factory(settings):
        assert settings.bucket == expected_bucket
        client.bucket = settings.bucket
        return client
    monkeypatch.setattr(cli, "BotoS3Client", factory)
    assert cli.check(path, list_objects=True) == 0
    directory, = (root / "check").iterdir()
    meta = json.loads((directory / "run_metadata.json").read_text())
    assert meta["bucket"] == expected_bucket and meta["backend"] == "ceph"
    assert meta["storage_policy"] == ("rep3" if profile.startswith("rep") else "ec4_2")
    assert meta["pair_index"] is None
    assert len(client.put_calls) == len(client.delete_calls) == 1
    assert not client.objects  # no performance objects or pre-uploaded COG needed
