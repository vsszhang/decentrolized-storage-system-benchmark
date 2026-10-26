import csv
import json
from contextlib import contextmanager

import pytest
from botocore.exceptions import ClientError

from conftest import FakeS3Client
from storage_benchmark import cli
from storage_benchmark.cog_gdal import run_cog_workloads
from storage_benchmark.config import load_config, S3Settings, effective_config
from storage_benchmark.run_state import RunJournal


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-not-real")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-not-real")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://rgw.example.test:8080")
    monkeypatch.setenv("S3_REGION", "test-region")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_USE_SSL", raising=False)
    path = tmp_path / "run.toml"
    path.write_text(f'''[s3]
bucket="zhang_rep"
[experiment]
experiment_id="method-v1"
backend="ceph"
storage_policy="rep3"
[run]
output_dir="{tmp_path / 'results'}"
[[workloads]]
name="write"
operation="seqwrite"
object_size=64
chunk_size=16
iterations=2
''')
    client = FakeS3Client()
    client.bucket = "zhang_rep"
    monkeypatch.setattr(cli, "BotoS3Client", lambda settings: client)
    return path, client, tmp_path / "results"


def read_run(root, kind="io"):
    directory = max((root / kind).iterdir(),
                    key=lambda p: json.loads((p / "run_metadata.json").read_text())["started_at"])
    return directory, json.loads((directory / "run_metadata.json").read_text())


def test_initial_identity_effective_values_and_two_run_isolation(setup_run, monkeypatch):
    path, client, root = setup_run
    client.objects["unrelated.bin"] = b"leave alone"
    client.objects["cog/existing.tif"] = b"leave alone"
    def factory(settings):
        directory, meta = read_run(root)
        assert meta["status"] == "running" and meta["ended_at"] is None
        assert (directory / "created_objects.json").read_text() == "[]"
        assert (directory / "run.log").is_file()
        return client
    monkeypatch.setattr(cli, "BotoS3Client", factory)
    assert cli.main(["run", "-c", str(path), "--pair-index", "2"]) == 0
    assert cli.run(path) == 0
    directories = list((root / "io").iterdir())
    assert len(directories) == 2
    assert len(set(client.put_calls)) == 4
    assert len(client.objects) == 6 and not client.delete_calls
    pair_indexes = set()
    for directory in directories:
        meta = json.loads((directory / "run_metadata.json").read_text())
        snapshot = load_config(directory / "run_config.toml")
        pair_indexes.add(meta["pair_index"])
        assert meta["status"] == "completed" and meta["cleanup_status"] == "not_requested"
        assert meta["bucket"] == snapshot.s3.bucket == "zhang_rep"
        assert snapshot.s3.endpoint_url == "http://rgw.example.test:8080"
        assert snapshot.s3.region == meta["environment"]["region"] == "test-region"
        assert snapshot.experiment.pair_index == meta["pair_index"]
        assert meta["environment"]["versions"]["boto3"]
        assert meta["wall_seconds"] >= 0 and meta["ended_at"]
        for entry in json.loads((directory / "created_objects.json").read_text()):
            assert entry["state"] == "uploaded" and meta["run_id"] in entry["key"]
        persisted = "".join(p.read_text() for p in directory.iterdir() if p.is_file())
        assert "test-access-not-real" not in persisted and "test-secret-not-real" not in persisted
    assert pair_indexes == {1, 2}


@pytest.mark.parametrize("interrupt", [False, True])
def test_partial_samples_and_uncertain_objects_survive_failure(setup_run, interrupt):
    path, client, root = setup_run
    original = client.put_object
    def fail_second(key, body, content_length=None):
        directory, meta = read_run(root)
        intents = json.loads((directory / "created_objects.json").read_text())
        assert intents[-1]["state"] == "planned" and intents[-1]["key"] == key
        if len(client.put_calls) == 1:
            with (directory / "samples.csv").open() as f:
                assert len(list(csv.DictReader(f))) == 1  # persisted before failure/final export
            original(key, body, content_length)
            if interrupt:
                raise KeyboardInterrupt()
            raise ClientError({"Error": {"Code": "RequestTimeout", "Message":
                "test-access-not-real test-secret-not-real https://host/?X-Amz-Signature=hidden"}}, "PutObject")
        original(key, body, content_length)
    client.put_object = fail_second
    assert cli.run(path) == 1
    directory, meta = read_run(root)
    assert meta["status"] == ("interrupted" if interrupt else "failed")
    assert meta["error"]["step"] == "seqwrite:write"
    assert len(json.loads((directory / "samples.json").read_text())) == 1
    assert json.loads((directory / "metrics.json").read_text())[0]["operations"] == 1
    entries = json.loads((directory / "created_objects.json").read_text())
    assert [e["state"] for e in entries] == ["uploaded", "upload_uncertain"]
    assert len(client.objects) == 2 and not client.delete_calls
    for file in directory.iterdir():
        assert "test-secret-not-real" not in file.read_text()
        assert "X-Amz-Signature" not in file.read_text()


def test_cleanup_of_partial_run_preserves_primary_error_and_unrelated_data(setup_run):
    path, client, root = setup_run
    path.write_text(path.read_text().replace('[run]', '[run]\ncleanup=true'))
    client.objects["cog/existing.tif"] = b"keep"
    put = client.put_object
    def fail(key, body, content_length=None):
        put(key, body, content_length)
        if len(client.put_calls) == 2:
            raise RuntimeError("response uncertain")
    client.put_object = fail
    delete = client.delete_object
    def delete_after_failure_recorded(key):
        directory, meta = read_run(root)
        assert meta["status"] == "failed"
        assert meta["error"]["step"] == "seqwrite:write"
        delete(key)
    client.delete_object = delete_after_failure_recorded
    assert cli.run(path) == 1
    directory, meta = read_run(root)
    assert meta["status"] == "failed" and meta["cleanup_status"] == "completed"
    assert client.objects == {"cog/existing.tif": b"keep"}
    assert len(client.delete_calls) == 2
    assert {e["state"] for e in json.loads((directory / "created_objects.json").read_text())} == {"deleted"}


def test_cleanup_failure_does_not_discard_successful_measurements(setup_run):
    path, client, root = setup_run
    path.write_text(path.read_text().replace('[run]', '[run]\ncleanup=true'))
    def denied(key):
        client.delete_calls.append(key)
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "test-secret-not-real"}}, "DeleteObject")
    client.delete_object = denied
    assert cli.run(path) == 1
    directory, meta = read_run(root)
    assert meta["status"] == "completed" and meta["cleanup_status"] == "failed"
    assert meta["error"] is None and len(meta["cleanup_errors"]) == 2
    assert len(json.loads((directory / "samples.json").read_text())) == 2
    assert len(client.delete_calls) == 2  # attempt all owned keys, not just the first


def test_cleanup_refuses_injected_key_bucket_and_foreign_client(setup_run, tmp_path):
    path, client, root = setup_run
    config = load_config(path)
    settings = S3Settings.from_config_and_env(config.s3)
    journal = RunJournal(tmp_path / "owned-run", effective_config(config, settings), settings, "io")
    own = f"{journal.prefix}/a.bin"
    journal.record_object(own, "planned")
    client.objects["outside.bin"] = b"keep"
    journal.objects[own]["key"] = "outside.bin"
    journal.cleanup(client)
    assert not client.delete_calls and client.objects["outside.bin"] == b"keep"
    assert journal.metadata["cleanup_status"] == "failed"
    with pytest.raises(ValueError):
        journal.record_object(f"{journal.prefix}/../escape.bin", "planned")
    journal.objects[own]["key"] = own
    journal.objects[own]["bucket"] = "foreign"
    journal.cleanup(client)
    assert not client.delete_calls
    journal.objects[own]["bucket"] = client.bucket
    client.bucket = "foreign"
    journal.cleanup(client)
    assert not client.delete_calls


def test_cog_partial_samples_are_saved_within_a_single_workload(setup_run, monkeypatch):
    path, client, root = setup_run
    text = path.read_text().split('[[workloads]]')[0]
    path.write_text(text + '''[[cog_workloads]]
name="windows"
operation="randomwindow"
object_key="cog/existing.tif"
iterations=2
window_width=2
window_height=2
''')
    class Dataset:
        width, height, count, block_shapes = 8, 8, 1, [(2, 2)]
        reads = 0
        def read(self, band_indexes, window=None):
            self.reads += 1
            if self.reads == 2:
                raise OSError("remote COG read failed")
            return b"abcd"
    @contextmanager
    def opener(*args):
        yield Dataset()
    def run_cog(workloads, settings, rng, **kwargs):
        assert settings.endpoint_url == "http://rgw.example.test:8080"
        assert settings.region == "test-region" and not settings.use_ssl
        assert {w.bucket for w in workloads} == {"zhang_rep"}
        assert {w.object_key for w in workloads} == {"cog/chosen.tif"}
        return run_cog_workloads(workloads, settings, rng, opener=opener, **kwargs)
    monkeypatch.setattr(cli, "run_cog_workloads", run_cog)
    assert cli.run(path, "cog/chosen.tif", 2) == 1
    directory, meta = read_run(root, "cog")
    assert meta["error"]["step"] == "randomwindow:windows"
    assert len(json.loads((directory / "samples.json").read_text())) == 1
    assert json.loads((directory / "created_objects.json").read_text()) == []
    saved = load_config(directory / "run_config.toml")
    assert saved.cog_workloads[0].object_key == "cog/chosen.tif" and saved.experiment.pair_index == 2


def test_client_construction_failure_is_journaled(setup_run, monkeypatch):
    path, client, root = setup_run
    def fail(settings):
        raise RuntimeError("test-secret-not-real must not appear in diagnostics")
    monkeypatch.setattr(cli, "BotoS3Client", fail)
    assert cli.run(path) == 1
    directory, meta = read_run(root)
    assert meta["status"] == "failed" and meta["error"]["step"] == "client_setup"
    assert json.loads((directory / "samples.json").read_text()) == []


def test_legacy_run_keeps_environment_bucket_override_and_unknown_labels(setup_run, monkeypatch):
    path, client, root = setup_run
    text = path.read_text()
    start, end = text.index('[experiment]'), text.index('[run]')
    path.write_text(text[:start] + text[end:])
    client.bucket = "legacy-env-bucket"
    monkeypatch.setenv("S3_BUCKET", client.bucket)
    assert cli.run(path) == 0
    directory, meta = read_run(root)
    assert meta["backend"] == meta["storage_policy"] == "unknown"
    assert meta["experiment_id"] is None and meta["pair_index"] is None
    assert load_config(directory / "run_config.toml").s3.bucket == client.bucket
