from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import quote

from storage_benchmark.config import BenchmarkConfig, IO_GENERATOR_VERSION, S3Settings, write_config
from storage_benchmark.metrics import MetricRecord, OperationSample, write_metrics
from storage_benchmark.s3_client import S3Client


def object_prefix(config: BenchmarkConfig, run_id: str) -> str:
    experiment = config.experiment.experiment_id if config.experiment else "standalone"
    namespace = quote(experiment, safe="").replace(".", "%2E")
    return f"benchmark/{namespace}/{run_id}"


def error_record(exc: BaseException, step: str) -> dict[str, str]:
    # SDK exception messages can contain signed URLs/headers. Keep the service code,
    # exception type and our operation name, never the raw SDK diagnostic string.
    response = getattr(exc, "response", {}) or {}
    code = response.get("Error", {}).get("Code", getattr(exc, "code", type(exc).__name__))
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", code):
        code = type(exc).__name__
    return {"step": step, "code": str(code), "message": f"{type(exc).__name__} during {step}"}


def _git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _environment(settings: S3Settings) -> dict[str, Any]:
    versions = {}
    for package in ("boto3", "botocore", "rasterio"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    try:
        import rasterio
        versions["gdal"] = rasterio.__gdal_version__
    except (ImportError, OSError):
        versions["gdal"] = None
    dirty = _git_output("status", "--porcelain")
    code_hash = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        code_hash.update(path.name.encode())
        code_hash.update(path.read_bytes())
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "hostname": platform.node(), "versions": versions,
        "git_revision": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(dirty) if dirty is not None else None,
        "code_sha256": code_hash.hexdigest(),
        "endpoint_url": settings.endpoint_url, "region": settings.region,
        "use_ssl": settings.use_ssl, "client_options": None,
        "gdal_cache_options": {name: os.getenv(name) for name in (
            "GDAL_CACHEMAX", "CPL_VSIL_CURL_CACHE_SIZE", "VSI_CACHE", "VSI_CACHE_SIZE")},
        "cluster": None, "cluster_note": "Not observed by this client run",
    }


class RunJournal:
    """Local evidence and ownership for one synchronous run; no storage operations on init."""

    def __init__(self, output_dir: Path, config: BenchmarkConfig, settings: S3Settings, kind: str) -> None:
        self.output_dir = output_dir
        self.bucket = settings.bucket
        self.prefix = object_prefix(config, output_dir.name)
        self.samples: list[OperationSample] = []
        self.objects: dict[str, dict[str, Any]] = {}
        self._owned_keys: set[str] = set()
        self._secrets = (settings.access_key_id, settings.secret_access_key)
        self._started = time.perf_counter()
        self.step = "client_setup"
        experiment = config.experiment
        datasets = [
            {"kind": "generated_io", "workload": w.name, "object_size": w.object_size,
             "chunk_size": w.chunk_size, "generator": IO_GENERATOR_VERSION,
             "content_seed_source": "seed/workload/index/size/chunk_size", "seed": config.run.seed}
            for w in config.workloads
        ]
        cog_datasets = {}
        for workload in config.cog_workloads:
            bucket = workload.bucket or settings.bucket
            cog_datasets[(bucket, workload.object_key)] = {
                "kind": "cog", "bucket": bucket, "object_key": workload.object_key,
                "sha256": None, "size_bytes": None, "verification": "pending",
            }
        datasets.extend(cog_datasets.values())
        self.metadata: dict[str, Any] = {
            "schema_version": 1, "run_id": output_dir.name,
            "experiment_id": experiment.experiment_id if experiment else None,
            "pair_index": experiment.pair_index if experiment and kind != "check" else None,
            "backend": experiment.backend if experiment else "unknown",
            "storage_policy": experiment.storage_policy if experiment else "unknown",
            "storage_policy_source": "mentor_provided" if experiment and experiment.backend == "ceph" else "unknown",
            "benchmark_type": kind, "bucket": settings.bucket,
            "status": "running", "cleanup_status": "not_requested",
            "started_at": datetime.now(UTC).isoformat(), "ended_at": None,
            "wall_seconds": None, "error": None, "cleanup_errors": [], "checks": [],
            "environment": _environment(settings), "dataset_manifest": datasets,
            "io_verification": [], "verification_seconds": None,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        write_config(output_dir / "run_config.toml", config)
        write_metrics(output_dir, [])
        self._save_objects()
        self.save()
        self.log("Run started; configuration and ownership initialized")

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def _write_json(self, filename: str, value: Any) -> None:
        path = self.output_dir / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.redact(value), indent=2), encoding="utf-8")
        temporary.replace(path)

    def save(self) -> None:
        self._write_json("run_metadata.json", self.metadata)

    def log(self, message: str) -> None:
        with (self.output_dir / "run.log").open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now(UTC).isoformat()} {self.redact(message)}\n")

    def record_sample(self, sample: OperationSample) -> None:
        self.samples.append(sample)
        # CSV is the incremental journal. JSON/aggregates are exported on ordinary
        # completion, failure or interruption, avoiding quadratic sample rewrites.
        with (self.output_dir / "samples.csv").open("a", encoding="utf-8", newline="") as output:
            csv.DictWriter(output, fieldnames=list(OperationSample.__dataclass_fields__)).writerow(asdict(sample))
        if sample.operation in {"coginfo", "fullread", "randomwindow", "tilewindow"}:
            details = json.loads(sample.details)
            changed = False
            for entry in self.metadata["dataset_manifest"]:
                if (entry["kind"] == "cog" and entry["object_key"] == sample.object_key
                        and entry["bucket"] == details["bucket"] and "width" not in entry):
                    for name in ("width", "height", "count", "crs", "block_shapes", "dtypes", "compression", "overviews"):
                        entry[name] = details[name]
                    changed = True
            if changed:
                self.save()

    def _assert_owned(self, key: str) -> None:
        if not key.startswith(self.prefix + "/") or any(part in {".", ".."} for part in key.split("/")):
            raise ValueError("Object is outside this run's ownership prefix")

    def record_object(self, key: str, state: str, content_identity: dict[str, Any] | None = None) -> None:
        self._assert_owned(key)
        if state == "planned":
            if key in self._owned_keys:
                raise ValueError("An object key may only be planned once per run")
            self._owned_keys.add(key)
        elif key not in self._owned_keys:
            raise ValueError("Object has no write intent in this run")
        previous = self.objects.get(key, {})
        self.objects[key] = {**previous, "bucket": self.bucket, "key": key, "state": state, "upload_id": None}
        if content_identity is not None:
            self.objects[key]["content_identity"] = content_identity
        self._save_objects()

    def _save_objects(self) -> None:
        # ponytail: whole ownership list per update is adequate for smoke runs;
        # use an append journal if experiments grow to millions of objects.
        self._write_json("created_objects.json", list(self.objects.values()))

    def cleanup(self, client: S3Client) -> None:
        self.metadata["cleanup_status"] = "pending"
        self.save()
        for item in self.objects.values():
            if item["state"] == "deleted":
                continue
            key = item["key"]
            try:
                self._assert_owned(key)
                if client.bucket != self.bucket or item["bucket"] != self.bucket or key not in self._owned_keys:
                    raise ValueError("Cleanup target does not belong to this run")
                if item["upload_id"] is not None:
                    raise ValueError("Multipart cleanup is not implemented in this stage")
                client.delete_object(key)
                item["state"] = "deleted"
                if self.metadata["benchmark_type"] == "check":
                    self.metadata["checks"].append({"step": "delete", "status": "passed"})
                self.log("Owned object deleted")
            except (Exception, KeyboardInterrupt) as exc:
                item["state"] = "delete_failed"
                self.metadata["cleanup_errors"].append(error_record(exc, "cleanup"))
                if self.metadata["benchmark_type"] == "check":
                    self.metadata["checks"].append({"step": "delete", "status": "failed",
                                                     "error": error_record(exc, "cleanup")})
                self.log(f"Cleanup failed: {type(exc).__name__}")
            self._save_objects()
        self.metadata["cleanup_status"] = "failed" if self.metadata["cleanup_errors"] else "completed"
        self.save()

    def finish(self, status: str, error: BaseException | None = None) -> list[MetricRecord]:
        if error is not None:
            self.metadata["error"] = error_record(error, self.step)
            self.log(f"Run failed at {self.step}: {self.metadata['error']['code']}")
        records = write_metrics(self.output_dir, self.samples)
        self.metadata.update(status=status, ended_at=datetime.now(UTC).isoformat(),
                             wall_seconds=time.perf_counter() - self._started)
        self.save()
        self.log(f"Run {status}; cleanup {self.metadata['cleanup_status']}")
        return records


def record_sample(samples: list[OperationSample], sample: OperationSample, journal: RunJournal | None) -> None:
    samples.append(sample)
    if journal is not None:
        journal.record_sample(sample)
