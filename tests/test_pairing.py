import csv
import json
from dataclasses import replace

import pytest

from test_content_validation import io_run, cog_run, reference_cog
from storage_benchmark.pairing import validate_pairs
from storage_benchmark.reporting import generate_compare_report, load_run_result


def pair_runs(tmp_path):
    return [load_run_result(io_run(tmp_path, side)[2].output_dir) for side in ("rep", "ec")]


def test_verified_pair_generates_labeled_report_and_paired_artifacts(tmp_path):
    runs = pair_runs(tmp_path)
    output = tmp_path / "report"
    paths = generate_compare_report([r.result_dir for r in runs], output)
    result = json.loads((output / "pair_validation.json").read_text())
    assert result["pairs"][0]["status"] == "valid"
    with (output / "paired_metrics.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert {r["storage_policy"] for r in rows} == {"rep3", "ec4_2"}
    assert {r["bucket"] for r in rows} == {"zhang_rep", "zhang_ec"}
    text = (output / "summary.md").read_text()
    assert "MiB/s" in text and "decoded arrays" in text and "round 1" in text
    assert "Valid Paired Workload Summary" in text
    assert (output / "paired_throughput.png").stat().st_size > 0
    assert all(p.is_file() for p in paths)


@pytest.mark.parametrize("damage,reason", [
    ("incomplete", "run_not_completed"), ("counts", "sample_count_mismatch"),
    ("seed", "different:seed"), ("content", "different:io_content"),
    ("missing_verification", "io_verification_count_mismatch"),
    ("cleanup", "cleanup_not_successful"), ("environment", "environment_unknown:client_options"),
    ("same_bucket", "same_bucket_on_both_sides"), ("sequence", "different:access_sequence"),
    ("metrics", "metric_sample_mismatch"),
])
def test_invalid_evidence_never_becomes_a_valid_pair(tmp_path, damage, reason):
    runs = pair_runs(tmp_path)
    bad = runs[1]
    if damage == "incomplete": bad.metadata["status"] = "failed"
    if damage == "counts": bad.samples.pop()
    if damage == "seed": bad.config["run"]["seed"] += 1
    if damage == "content": bad.metadata["io_verification"][0]["sha256"] = "0" * 64
    if damage == "missing_verification": bad.metadata["io_verification"] = []
    if damage == "cleanup": bad.metadata["cleanup_status"] = "failed"
    if damage == "environment": bad.metadata["environment"]["client_options"] = None
    if damage == "same_bucket": bad.metadata["bucket"] = runs[0].metadata["bucket"]
    if damage == "sequence": bad.samples[-1]["details"] = bad.samples[-2]["details"]
    if damage == "metrics": bad.metrics[0]["throughput_mb_s"] = "999999"
    result = validate_pairs(runs)
    pair, = result["pairs"]
    assert pair["status"] == "invalid"
    assert any(reason in r for r in pair["reasons"]), pair["reasons"]


def test_missing_side_duplicate_side_and_legacy_results_are_not_inferred_pairs(tmp_path):
    runs = pair_runs(tmp_path)
    assert validate_pairs(runs[:1])["pairs"][0]["status"] == "invalid"
    assert validate_pairs([runs[0], runs[0], runs[1]])["pairs"][0]["status"] == "invalid"
    legacy = replace(runs[1], metadata=None)
    result = validate_pairs([runs[0], legacy])
    assert result["unpaired_runs"] and result["pairs"][0]["status"] == "invalid"


def test_cog_reference_is_required_for_pairing(tmp_path, reference_cog):
    rep = cog_run(tmp_path, reference_cog, "rep")[2]
    ec = cog_run(tmp_path, reference_cog, "ec", validate=False)[2]
    runs = [load_run_result(j.output_dir) for j in (rep, ec)]
    result = validate_pairs(runs)
    assert any("cog_reference_not_verified" in r for r in result["pairs"][0]["reasons"])


def test_one_experiment_cannot_change_method_between_rounds(tmp_path):
    runs = pair_runs(tmp_path / "round1")
    for side in ("rep", "ec"):
        journal = io_run(tmp_path / "round2", side, seed=99, run_id_suffix="-round2")[2]
        run = load_run_result(journal.output_dir)
        run.metadata["pair_index"] = run.config["experiment"]["pair_index"] = 2
        runs.append(run)
    result = validate_pairs(runs)
    assert len(result["pairs"]) == 2
    assert all(p["status"] == "invalid" and "method_changed_between_pair_rounds" in p["reasons"] for p in result["pairs"])


def test_failed_and_empty_runs_are_reported_but_not_aggregated(tmp_path):
    runs = pair_runs(tmp_path)
    for run in runs:
        meta = run.metadata
        meta["status"] = "failed"
        meta["error"] = {"step": "upload", "code": "AccessDenied"}
        (run.result_dir / "run_metadata.json").write_text(json.dumps(meta))
        for name in ("metrics.csv", "samples.csv"):
            path = run.result_dir / name
            path.write_text(path.read_text().splitlines()[0] + "\n")
    output = tmp_path / "failed-report"
    generate_compare_report([r.result_dir for r in runs], output)
    assert "AccessDenied" in (output / "summary.md").read_text()
    with (output / "combined_metrics.csv").open() as stream:
        assert list(csv.DictReader(stream)) == []
    with (output / "paired_metrics.csv").open() as stream:
        assert list(csv.DictReader(stream)) == []
