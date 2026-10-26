"""Evidence checks for reports; historical runs remain descriptive, not inferred pairs."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict

from storage_benchmark.config import IO_GENERATOR_VERSION, parse_size
from storage_benchmark.metrics import OperationSample, summarize_samples


def completion_issues(run) -> list[str]:
    try:
        return _completion_issues(run)
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["invalid_completion_evidence"]


def _completion_issues(run) -> list[str]:
    if run.metadata is None:
        return [] if run.metrics and run.samples else ["empty_legacy_run"]
    issues = []
    meta, config = run.metadata, run.config
    if meta.get("status") != "completed" or not meta.get("ended_at"):
        issues.append("run_not_completed")
    if meta.get("benchmark_type") not in {"io", "cog", "mixed"}:
        issues.append("not_performance_run")
    workloads = config.get("workloads", []) + config.get("cog_workloads", [])
    kind = "mixed" if config.get("workloads") and config.get("cog_workloads") else "cog" if config.get("cog_workloads") else "io"
    if kind != meta.get("benchmark_type"):
        issues.append("benchmark_type_mismatch")
    repeats = config.get("run", {}).get("repeats", 1)
    expected = {(w["name"], w["operation"]): w.get("iterations", 1) * repeats for w in workloads}
    observed = Counter((s["workload"], s["operation"]) for s in run.samples)
    by_repeat = Counter((s["workload"], s["operation"], int(s["repeat_index"])) for s in run.samples)
    expected_repeats = {(w["name"], w["operation"], r): w.get("iterations", 1)
                        for w in workloads for r in range(1, repeats + 1)}
    if not expected or observed != expected or by_repeat != expected_repeats:
        issues.append("sample_count_mismatch")
    metric_counts = {(m["workload"], m["operation"]): int(m["operations"]) for m in run.metrics}
    if len(metric_counts) != len(run.metrics) or metric_counts != expected:
        issues.append("metric_count_mismatch")
    samples = [OperationSample(s["workload"], s["operation"], s["object_key"], int(s["bytes_count"]),
               float(s["duration_seconds"]), s["started_at"], int(s["repeat_index"]), s.get("details", "")) for s in run.samples]
    sizes = {(w["name"], w["operation"]): parse_size(w["object_size"]) for w in config.get("workloads", [])}
    if any(not math.isfinite(s.duration_seconds) or s.duration_seconds <= 0 or s.bytes_count < 0
           or ((s.workload, s.operation) in sizes and s.bytes_count != sizes[(s.workload, s.operation)]) for s in samples):
        issues.append("invalid_sample_measurement")
    recalculated = {(record.workload, record.operation): asdict(record) for record in summarize_samples(samples)}
    for row in run.metrics:
        expected_metric = recalculated.get((row["workload"], row["operation"]), {})
        if any(not math.isclose(float(row[name]), value, rel_tol=1e-9, abs_tol=1e-9)
               for name, value in expected_metric.items() if isinstance(value, (float, int))):
            issues.append("metric_sample_mismatch")
    return issues


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _data_signature(run) -> tuple[dict, list[str]]:
    meta, config = run.metadata, run.config
    issues, signature = [], {}
    if config.get("workloads"):
        writes = {w["name"]: w for w in config["workloads"] if w["operation"] in {"seqwrite", "randomwrite"}}
        repeats = config["run"]["repeats"]
        entries = meta.get("io_verification", [])
        expected = Counter({(name, i): repeats for name, w in writes.items() for i in range(w["iterations"])})
        observed = Counter()
        fingerprints = []
        for entry in entries:
            identity = entry.get("content_identity", {})
            source = writes.get(identity.get("workload"), {})
            observed[(identity.get("workload"), identity.get("index"))] += 1
            if (entry.get("status") != "passed" or not _sha(entry.get("sha256"))
                    or entry.get("bytes_checked") != source.get("object_size")
                    or entry.get("bucket") != meta.get("bucket")
                    or identity.get("size") != source.get("object_size")
                    or identity.get("chunk_size") != source.get("chunk_size")
                    or identity.get("generator") != IO_GENERATOR_VERSION
                    or identity.get("seed") != config["run"]["seed"]):
                issues.append("io_content_not_verified")
            fingerprints.append({"identity": identity, "sha256": entry.get("sha256"), "bytes": entry.get("bytes_checked")})
        if observed != expected or not entries:
            issues.append("io_verification_count_mismatch")
        signature["io_content"] = sorted(fingerprints, key=lambda value: json.dumps(value, sort_keys=True))
    if config.get("cog_workloads"):
        entries = [e for e in meta.get("dataset_manifest", []) if e.get("kind") == "cog"]
        expected_keys = {w["object_key"] for w in config["cog_workloads"]}
        if len(entries) != len(expected_keys) or {e.get("object_key") for e in entries} != expected_keys:
            issues.append("cog_manifest_missing")
        fields = ("object_key", "sha256", "size_bytes", "width", "height", "count", "crs", "block_shapes", "dtypes", "compression", "overviews")
        for entry in entries:
            if (entry.get("verification") != "passed" or not _sha(entry.get("sha256"))
                    or entry.get("reference_sha256") != entry.get("sha256")
                    or entry.get("bucket") != meta.get("bucket") or entry.get("pixel_checks", 0) < 1
                    or any(entry.get(field) is None for field in fields)):
                issues.append("cog_reference_not_verified")
        signature["cog_content"] = sorted([{field: e.get(field) for field in fields} for e in entries], key=lambda e: e["object_key"])
    sequence = []
    for sample in run.samples:
        details = json.loads(sample.get("details") or "{}")
        if sample["operation"] in {"seqwrite", "seqread", "randomwrite", "randomread"} and not details.get("content_identity"):
            issues.append("sample_content_identity_missing")
        if sample["operation"] in {"randomwindow", "tilewindow"} and not details.get("window"):
            issues.append("sample_window_missing")
        sequence.append([sample["workload"], sample["operation"], sample["repeat_index"], sample["bytes_count"],
                         details.get("content_identity"), details.get("band_indexes"), details.get("window"),
                         details.get("timing_scope"), details.get("bytes_semantics")])
    signature["access_sequence"] = sequence
    return signature, issues


def _signature(run) -> tuple[dict, list[str]]:
    meta, config = run.metadata, run.config
    issues = completion_issues(run)
    if meta.get("cleanup_status") not in {"completed", "not_requested"}:
        issues.append("cleanup_not_successful")
    experiment = config.get("experiment", {})
    for field in ("experiment_id", "pair_index", "backend", "storage_policy"):
        if experiment.get(field) != meta.get(field):
            issues.append(f"metadata_config_mismatch:{field}")
    if config.get("s3", {}).get("bucket") != meta.get("bucket"):
        issues.append("metadata_config_mismatch:bucket")
    for workload in config.get("cog_workloads", []):
        if workload.get("bucket", meta.get("bucket")) != meta.get("bucket"):
            issues.append("cog_bucket_mismatch")
    method = {
        "seed": config.get("run", {}).get("seed"), "repeats": config.get("run", {}).get("repeats"),
        "workloads": [{k: v for k, v in w.items() if k != "key_prefix"} for w in config.get("workloads", [])],
        "cog_workloads": [{k: v for k, v in w.items() if k != "bucket"} for w in config.get("cog_workloads", [])],
    }
    environment = meta.get("environment", {})
    fields = ("python", "platform", "hostname", "versions", "code_sha256", "endpoint_url", "region", "use_ssl", "client_options", "gdal_cache_options")
    for field in fields:
        if environment.get(field) is None:
            issues.append(f"environment_unknown:{field}")
    if not _sha(environment.get("code_sha256")):
        issues.append("code_fingerprint_missing")
    for name in ("boto3", "botocore", "rasterio", "gdal"):
        if not (environment.get("versions") or {}).get(name):
            issues.append(f"environment_unknown:version:{name}")
    for name in ("connect_timeout", "read_timeout", "retries", "signature_version", "addressing_style"):
        if (environment.get("client_options") or {}).get(name) is None:
            issues.append(f"environment_unknown:client:{name}")
    method["environment"] = {field: environment.get(field) for field in fields}
    data, data_issues = _data_signature(run)
    method.update(data)
    issues.extend(data_issues)
    return method, sorted(set(issues))


def _difference_paths(left, right, path: str) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        return [difference for key in sorted(left.keys() | right.keys())
                for difference in _difference_paths(left.get(key), right.get(key), f"{path}.{key}")]
    if isinstance(left, list) and isinstance(right, list):
        differences = [f"{path}.length"] if len(left) != len(right) else []
        return differences + [difference for i, (a, b) in enumerate(zip(left, right))
                              for difference in _difference_paths(a, b, f"{path}[{i}]")]
    return [path] if left != right else []


def validate_pairs(runs) -> dict:
    groups, unpaired, pairs = defaultdict(list), [], []
    identities = Counter(r.run_id for r in runs if r.metadata is not None)
    for run in runs:
        meta = run.metadata or {}
        if (meta.get("backend") != "ceph" or not meta.get("experiment_id")
                or type(meta.get("pair_index")) is not int or meta["pair_index"] < 1):
            unpaired.append({"result_dir": str(run.result_dir), "run_id": run.run_id,
                             "reasons": ["pair_identity_missing_or_not_ceph"]})
            continue
        groups[(meta["experiment_id"], meta["pair_index"], run.benchmark_type)].append(run)
    signatures = {}
    for (experiment, index, kind), group in groups.items():
        reasons, methods = [], []
        for run in group:
            try:
                method, issues = _signature(run)
            except (KeyError, TypeError, ValueError, AttributeError):
                method, issues = {}, ["invalid_pair_evidence"]
            methods.append(method)
            reasons.extend(f"{run.run_id}:{issue}" for issue in issues)
            if identities[run.run_id] > 1:
                reasons.append("duplicate_run_identity")
        if len(group) != 2 or {r.metadata.get("storage_policy") for r in group} != {"rep3", "ec4_2"}:
            reasons.append("expected_one_rep3_and_one_ec4_2")
        if len(group) == 2 and group[0].metadata.get("bucket") == group[1].metadata.get("bucket"):
            reasons.append("same_bucket_on_both_sides")
        if len(methods) == 2:
            for field in sorted(methods[0].keys() | methods[1].keys()):
                if methods[0].get(field) != methods[1].get(field):
                    paths = _difference_paths(methods[0].get(field), methods[1].get(field), field)
                    reasons.extend(f"different:{path}" for path in paths[:20])
                    if len(paths) > 20:
                        reasons.append(f"additional_differences:{field}")
        pair = {"experiment_id": experiment, "pair_index": index, "benchmark_type": kind,
                "run_dirs": [str(r.result_dir) for r in group], "reasons": reasons,
                "status": "invalid" if reasons else "valid",
                "cluster_observations": [{"run_id": r.run_id, "bucket": r.metadata.get("bucket"),
                                          "cluster": (r.metadata.get("environment") or {}).get("cluster")} for r in group],
                "notes": ["Cluster placement/media/load are not controlled by this client; no cold-cache claim."]}
        pairs.append(pair)
        if not reasons:
            signatures[id(pair)] = json.dumps(methods[0], sort_keys=True)
    # An experiment ID denotes one method across all independent rounds too.
    experiments = defaultdict(list)
    for pair in pairs:
        if pair["status"] == "valid":
            experiments[(pair["experiment_id"], pair["benchmark_type"])].append(pair)
    for group in experiments.values():
        if len({signatures[id(pair)] for pair in group}) > 1:
            for pair in group:
                pair["status"] = "invalid"
                pair["reasons"].append("method_changed_between_pair_rounds")
    return {"pairs": pairs, "unpaired_runs": unpaired}
