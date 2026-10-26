from dataclasses import replace
from pathlib import Path

import pytest

from storage_benchmark import cli
from storage_benchmark.config import S3Settings, effective_config, load_config, write_config


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def s3_env(monkeypatch):
    for key in ("S3_BUCKET", "S3_USE_SSL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://rgw.example.test:8080")
    monkeypatch.setenv("S3_REGION", "test-region")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-not-real")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-not-real")


@pytest.mark.parametrize("kind", ["io", "cog"])
def test_ceph_smoke_pairs_are_equal_except_targets_and_labels(kind):
    rep = load_config(ROOT / f"configs/ceph-rep-{kind}-smoke.toml")
    ec = load_config(ROOT / f"configs/ceph-ec-{kind}-smoke.toml")
    assert (rep.s3.bucket, ec.s3.bucket) == ("zhang-rep", "zhang-ec")
    assert rep.s3.endpoint_url == ec.s3.endpoint_url == ""
    assert rep.s3.region == ec.s3.region == ""
    assert rep.run.output_dir != ec.run.output_dir
    assert rep.workloads == ec.workloads and rep.cog_workloads == ec.cog_workloads
    assert replace(rep.run, name=ec.run.name, output_dir=ec.run.output_dir) == ec.run
    assert replace(rep.experiment, storage_policy="ec4_2") == ec.experiment
    assert rep.experiment.pair_index == 1


def test_effective_configuration_round_trip_and_pair_precedence(s3_env, monkeypatch, tmp_path):
    config = load_config(ROOT / "configs/ceph-rep-cog-smoke.toml")
    settings = S3Settings.from_config_and_env(config.s3)
    changed = cli._with_cog_object_key_override(config, "cog/actual.tif")
    actual = effective_config(changed, settings, 3)
    path = tmp_path / "effective.toml"
    write_config(path, actual)
    assert load_config(path) == actual
    assert actual.s3.endpoint_url == "http://rgw.example.test:8080"
    assert actual.s3.region == "test-region"
    assert actual.experiment.pair_index == 3
    assert {w.bucket for w in actual.cog_workloads} == {"zhang-rep"}
    assert {w.object_key for w in actual.cog_workloads} == {"cog/actual.tif"}
    assert "test-access-not-real" not in path.read_text() + repr(settings)
    assert "test-secret-not-real" not in path.read_text() + repr(settings)
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://rgw.example.test:8443")
    monkeypatch.setenv("S3_USE_SSL", "true")
    assert S3Settings.from_config_and_env(config.s3).use_ssl is True


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", '"2"'])
def test_pair_index_rejects_non_positive_or_non_integer_values(tmp_path, value):
    text = (ROOT / "configs/ceph-rep-io-smoke.toml").read_text()
    path = tmp_path / "bad.toml"
    path.write_text(text.replace("pair_index = 1", f"pair_index = {value}"))
    with pytest.raises(ValueError, match="positive integer"):
        load_config(path)


def test_default_pair_legacy_compatibility_and_workload_bucket_guard(s3_env, tmp_path):
    path = tmp_path / "default.toml"
    path.write_text((ROOT / "configs/ceph-rep-io-smoke.toml").read_text().replace("pair_index = 1", ""))
    assert load_config(path).experiment.pair_index == 1
    legacy = load_config(ROOT / "configs/minio-smoke.toml")
    assert legacy.experiment is None
    settings = S3Settings.from_config_and_env(legacy.s3)
    assert effective_config(legacy, replace(settings, bucket="legacy-env-target")).s3.bucket == "legacy-env-target"
    with pytest.raises(ValueError, match="requires \\[experiment\\]"):
        effective_config(legacy, settings, 1)
    cog = load_config(ROOT / "configs/ceph-rep-cog-smoke.toml")
    cog = replace(cog, cog_workloads=[replace(cog.cog_workloads[0], bucket="zhang-ec")])
    with pytest.raises(ValueError, match="Effective bucket"):
        effective_config(cog, replace(settings, bucket="zhang-rep"))


def test_target_conflict_stops_before_client_creation(s3_env, monkeypatch, tmp_path):
    monkeypatch.setenv("S3_BUCKET", "zhang-ec")
    def forbidden_client(settings):
        pytest.fail("Client must not be constructed for a conflicting target")
    monkeypatch.setattr(cli, "BotoS3Client", forbidden_client)
    assert cli.main(["run", "-c", str(ROOT / "configs/ceph-rep-io-smoke.toml")]) == 2


def test_check_loads_connection_only_and_requires_no_workload(tmp_path):
    path = tmp_path / "check.toml"
    path.write_text('[s3]\nbucket="probe"\n[run]\nrepeats="invalid-performance-setting"\n[[workloads]]\noperation="invalid"\n')
    assert load_config(path, for_check=True).workloads == []
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("field,value", [
    ("experiment_id", '""'), ("backend", '"unsupported"'),
    ("storage_policy", '"unknown"'),
])
def test_experiment_labels_are_validated(tmp_path, field, value):
    text = (ROOT / "configs/ceph-rep-io-smoke.toml").read_text()
    old = {"experiment_id": '"ceph-io-smoke-v1"', "backend": '"ceph"', "storage_policy": '"rep3"'}
    path = tmp_path / "invalid-label.toml"
    path.write_text(text.replace(f"{field} = {old[field]}", f"{field} = {value}"))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("endpoint,ssl", [
    ("192.168.20.59", "false"), ("https://rgw.example.test", "false"),
    ("http://user:password@rgw.example.test", "false"),
    ("http://rgw.example.test/prefix", "false"),
    ("http://rgw.example.test?X-Amz-Signature=hidden", "false"),
])
def test_ambiguous_or_credential_bearing_endpoints_are_rejected(s3_env, monkeypatch, endpoint, ssl):
    monkeypatch.setenv("S3_ENDPOINT_URL", endpoint)
    monkeypatch.setenv("S3_USE_SSL", ssl)
    config = load_config(ROOT / "configs/ceph-rep-io-smoke.toml")
    with pytest.raises(ValueError):
        S3Settings.from_config_and_env(config.s3)
