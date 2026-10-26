import io
import json
import random
from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.shutil import copy as copy_raster

from conftest import FakeS3Client
from storage_benchmark import cli
from storage_benchmark import cog_gdal
from storage_benchmark.config import (
    BenchmarkConfig, CogOperation, CogWorkloadConfig, ExperimentConfig, Operation,
    RunConfig, S3Config, S3Settings, WorkloadConfig, write_config,
)
from storage_benchmark.cog_gdal import run_cog_workloads
from storage_benchmark.cog_validation import describe_cog, validate_cog
from storage_benchmark.io_basic import run_workloads, verify_io
from storage_benchmark.run_state import RunJournal, object_prefix


OPTIONS = {"connect_timeout": 60, "read_timeout": 60, "retries": {"mode": "legacy"},
           "signature_version": "s3v4", "addressing_style": "path"}


def io_run(root, side="rep", seed=42, run_id_suffix=""):
    bucket = f"zhang_{side}"
    policy = "rep3" if side == "rep" else "ec4_2"
    settings = S3Settings("http://rgw.test:8080", "test-access", "test-secret", bucket)
    config = BenchmarkConfig(
        s3=S3Config(settings.endpoint_url, bucket), run=RunConfig(seed=seed),
        experiment=ExperimentConfig("io-method-v2", "ceph", policy),
        workloads=[WorkloadConfig("write", Operation.SEQWRITE, 128, iterations=2, chunk_size=32),
                   WorkloadConfig("read", Operation.SEQREAD, 128, iterations=5, chunk_size=17, source_workload="write")],
    )
    directory = root / side / "io" / f"run-{side}{run_id_suffix}"
    prefix = object_prefix(config, directory.name)
    config = replace(config, workloads=[replace(w, key_prefix=prefix + "/io") for w in config.workloads])
    journal = RunJournal(directory, config, settings, "io")
    journal.metadata["environment"]["client_options"] = OPTIONS
    client = FakeS3Client()
    client.bucket = bucket
    samples, _ = run_workloads(config.workloads, client, random.Random(seed), journal=journal, data_seed=seed)
    before = list(samples)
    verify_io(client, journal)
    assert samples == before and journal.samples == before
    journal.finish("completed")
    return config, client, journal


@pytest.fixture
def reference_cog(tmp_path):
    source, result = tmp_path / "source.tif", tmp_path / "reference.tif"
    array = np.arange(2 * 17 * 19, dtype="uint16").reshape(2, 17, 19)
    with rasterio.open(source, "w", driver="GTiff", width=19, height=17, count=2,
                       dtype="uint16", crs="EPSG:4326", transform=from_origin(10, 20, 0.1, 0.1),
                       tiled=True, blockxsize=16, blockysize=16) as dataset:
        dataset.write(array)
    copy_raster(source, result, driver="COG", BLOCKSIZE=128, COMPRESS="DEFLATE")
    return result


def cog_run(root, reference, side="rep", *, validate=True):
    bucket = f"zhang_{side}"
    settings = S3Settings("http://rgw.test:8080", "test-access", "test-secret", bucket)
    workloads = [CogWorkloadConfig(name, op, "cog/sample.tif", bucket=bucket,
                 iterations=50 if op in {CogOperation.RANDOMWINDOW, CogOperation.TILEWINDOW} else 1,
                 window_width=512 if op in {CogOperation.RANDOMWINDOW, CogOperation.TILEWINDOW} else None,
                 window_height=512 if op in {CogOperation.RANDOMWINDOW, CogOperation.TILEWINDOW} else None)
                 for name, op in (("info", CogOperation.COGINFO), ("full", CogOperation.FULLREAD),
                                  ("random", CogOperation.RANDOMWINDOW), ("tile", CogOperation.TILEWINDOW))]
    config = BenchmarkConfig(s3=S3Config(settings.endpoint_url, bucket), run=RunConfig(repeats=3),
        experiment=ExperimentConfig("cog-method-v1", "ceph", "rep3" if side == "rep" else "ec4_2"),
        cog_workloads=workloads)
    journal = RunJournal(root / side / "cog" / f"run-{side}", config, settings, "cog")
    journal.metadata["environment"]["client_options"] = OPTIONS
    client = FakeS3Client()
    client.bucket = bucket
    client.objects["cog/sample.tif"] = reference.read_bytes()
    @contextmanager
    def opener(*args):
        with rasterio.open(reference) as dataset:
            yield dataset
    rng = random.Random(42)
    for repeat in range(1, 4):
        run_cog_workloads(workloads, settings, rng, repeat_index=repeat, opener=opener, journal=journal)
    if validate:
        validate_cog(client, settings, "cog/sample.tif", reference, describe_cog(reference), journal, opener=opener)
    journal.finish("completed")
    return config, client, journal, settings, opener


def test_io_identity_and_access_sequence_are_independent_of_bucket_and_run_id(tmp_path):
    _, rep, rep_journal = io_run(tmp_path)
    _, ec, ec_journal = io_run(tmp_path, "ec")
    assert set(rep.objects).isdisjoint(ec.objects)
    assert list(rep.objects.values()) == list(ec.objects.values())
    assert [json.loads(s.details)["content_identity"] for s in rep_journal.samples] == [json.loads(s.details)["content_identity"] for s in ec_journal.samples]
    assert len(rep_journal.samples) == 7  # two writes, five sequential reads, not silently capped at two
    _, changed, _ = io_run(tmp_path / "different-seed", seed=99)
    assert list(rep.objects.values()) != list(changed.objects.values())
    assert all(entry["status"] == "passed" and entry["bytes_checked"] == 128 for entry in rep_journal.metadata["io_verification"])


@pytest.mark.parametrize("damage", ["content", "truncate"])
def test_full_stream_verification_detects_corruption_and_closes_stream(tmp_path, damage):
    _, client, journal = io_run(tmp_path)
    key = next(iter(client.objects))
    raw = client.objects[key]
    client.objects[key] = (b"!" + raw[1:]) if damage == "content" else raw[:-1]
    streams = []
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 32
            return super().read(min(size, 7))  # fragmented network chunks
    def get(key):
        stream = BoundedStream(client.objects[key])
        streams.append(stream)
        return stream
    client.get_object_stream = get
    journal.metadata["io_verification"] = []
    before = list(journal.samples)
    with pytest.raises(ValueError):
        verify_io(client, journal)
    assert journal.samples == before and streams[0].closed
    assert journal.metadata["io_verification"][0]["status"] == "failed"


def test_cli_runs_io_verification_after_all_timed_samples(tmp_path, monkeypatch):
    config, client, _ = io_run(tmp_path / "setup")
    output = tmp_path / "cli-results"
    path = tmp_path / "run.toml"
    write_config(path, replace(config, run=replace(config.run, output_dir=output)))
    for name in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_REGION", "S3_USE_SSL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(cli, "BotoS3Client", lambda settings: client)
    def verify_after_samples(client, journal):
        assert len(journal.samples) == 7
        durations = [s.duration_seconds for s in journal.samples]
        verify_io(client, journal)
        assert durations == [s.duration_seconds for s in journal.samples]
    monkeypatch.setattr(cli, "verify_io", verify_after_samples)
    assert cli.run(path) == 0
    directory, = (output / "io").iterdir()
    assert json.loads((directory / "run_metadata.json").read_text())["verification_seconds"] >= 0


def test_real_cog_smoke_has_306_samples_and_verified_manifest(tmp_path, reference_cog):
    _, _, rep, _, _ = cog_run(tmp_path, reference_cog)
    _, _, ec, _, _ = cog_run(tmp_path, reference_cog, "ec")
    assert len(rep.samples) == len(ec.samples) == 306
    windows = lambda journal: [json.loads(s.details).get("window") for s in journal.samples]
    assert windows(rep) == windows(ec)
    for journal in (rep, ec):
        entry, = journal.metadata["dataset_manifest"]
        assert entry["verification"] == "passed" and entry["pixel_checks"] > 0
        assert entry["sha256"] == entry["reference_sha256"] == describe_cog(reference_cog)["sha256"]
        assert entry["dtypes"] == ["uint16", "uint16"] and entry["block_shapes"]
        assert "deflate" in entry["compression"].lower()
        assert not journal.objects  # reference COG is never registered for cleanup
    from storage_benchmark.pairing import validate_pairs
    from storage_benchmark.reporting import load_run_result
    result = validate_pairs([load_run_result(j.output_dir) for j in (rep, ec)])
    assert result["pairs"][0]["status"] == "valid", result
    small = CogWorkloadConfig("small", CogOperation.RANDOMWINDOW, "cog/sample.tif",
                              iterations=10, window_width=5, window_height=7)
    def positions(settings, opener):
        return [json.loads(s.details)["window"] for s in run_cog_workloads([small], settings, random.Random(42), opener=opener)]
    _, _, _, rep_settings, rep_opener = cog_run(tmp_path / "windows", reference_cog, "rep")
    _, _, _, ec_settings, ec_opener = cog_run(tmp_path / "windows", reference_cog, "ec")
    first, second = positions(rep_settings, rep_opener), positions(ec_settings, ec_opener)
    assert first == second and len({(w["col_off"], w["row_off"]) for w in first}) > 1


def test_cli_reference_validation_is_separate_and_persists_effective_manifest(tmp_path, reference_cog, monkeypatch):
    config, client, _, settings, opener = cog_run(tmp_path / "setup", reference_cog, validate=False)
    output = tmp_path / "cli-cog"
    path = tmp_path / "run-cog.toml"
    write_config(path, replace(config, run=replace(config.run, output_dir=output)))
    for name in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_REGION", "S3_USE_SSL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(cli, "BotoS3Client", lambda settings: client)
    def timed_reads(workloads, settings, rng, **kwargs):
        return run_cog_workloads(workloads, settings, rng, opener=opener, **kwargs)
    def reference_check(client, settings, key, reference, expected, journal):
        assert len(journal.samples) == 306
        validate_cog(client, settings, key, reference, expected, journal, opener=opener)
        assert len(journal.samples) == 306
    monkeypatch.setattr(cli, "run_cog_workloads", timed_reads)
    monkeypatch.setattr(cli, "validate_cog", reference_check)
    assert cli.main(["run", "-c", str(path), "--cog-reference", str(reference_cog)]) == 0
    directory, = (output / "cog").iterdir()
    meta = json.loads((directory / "run_metadata.json").read_text())
    assert meta["verification_seconds"] >= 0
    assert meta["cog_reference"]["sha256"] == meta["dataset_manifest"][0]["sha256"]


def test_window_metadata_recording_is_outside_read_timer(monkeypatch):
    events = []
    ticks = iter([10.0, 12.0])
    def clock():
        events.append("timer")
        return next(ticks)
    def metadata(dataset):
        events.append("metadata")
        return {}
    class Dataset:
        def read(self, *args, **kwargs):
            events.append("read")
            return np.ones((1, 2, 2), dtype="uint16")
    from types import SimpleNamespace
    monkeypatch.setattr(cog_gdal, "time", SimpleNamespace(perf_counter=clock))
    monkeypatch.setattr(cog_gdal, "_dataset_details", metadata)
    from rasterio.windows import Window
    workload = CogWorkloadConfig("window", CogOperation.RANDOMWINDOW, "sample.tif")
    settings = S3Settings("http://rgw.test", "test-access", "test-secret", "zhang_rep")
    sample = cog_gdal._read_window(workload, settings, Dataset(), Window(0, 0, 2, 2), 1)
    assert sample.duration_seconds == 2 and events == ["timer", "read", "timer", "metadata"]


@pytest.mark.parametrize("damage", ["bytes", "pixels", "metadata"])
def test_cog_validation_checks_file_identity_metadata_and_reference_pixels(tmp_path, reference_cog, damage):
    _, client, journal, settings, opener = cog_run(tmp_path, reference_cog, validate=False)
    if damage == "bytes":
        client.objects["cog/sample.tif"] += b"changed"
    @contextmanager
    def broken_opener(*args):
        with opener(*args) as dataset:
            class Proxy:
                def __getattr__(self, name):
                    if name == "width" and damage == "metadata": return dataset.width + 1
                    return getattr(dataset, name)
                def read(self, *args, **kwargs):
                    data = dataset.read(*args, **kwargs)
                    return data + 1 if damage == "pixels" else data
            yield Proxy()
    with pytest.raises(ValueError):
        validate_cog(client, settings, "cog/sample.tif", reference_cog, describe_cog(reference_cog), journal, opener=broken_opener)
    entry, = journal.metadata["dataset_manifest"]
    assert entry["verification"] == "failed"
    assert len(journal.samples) == 306
