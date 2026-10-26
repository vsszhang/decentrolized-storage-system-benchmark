# Decentralized Storage System Benchmark

Python/uv benchmark tools for S3-compatible object storage systems. The current
phase implements lightweight basic IO and COG/GDAL read workloads, with MinIO
profiles and locally tested Ceph RGW paired-bucket configuration/access checks.
Real Ceph deployment and server-side acceptance remain separate steps.

## Setup

```bash
uv sync --dev
```

Set MinIO/S3 connection settings through environment variables:

```bash
export S3_ACCESS_KEY_ID="minioadmin"
export S3_SECRET_ACCESS_KEY="minioadmin"
```

The smoke config defaults to:

- endpoint: `http://127.0.0.1:9000`
- bucket: `benchmark`
- region: `us-east-1`
- SSL: disabled

`127.0.0.1` is correct only when MinIO is running on the VPS itself, or when a
VPN/SSH tunnel maps MinIO to the VPS local port. You can override config values:

```bash
export S3_ENDPOINT_URL="http://10.0.0.10:9000"
export S3_BUCKET="other-bucket"
export S3_REGION="us-east-1"
export S3_USE_SSL="false"
```

## Run Benchmarks

### Ceph paired buckets (local implementation; server acceptance pending)

The four `configs/ceph-{rep,ec}-{io,cog}-smoke.toml` profiles target `zhang-rep`
(rep3) and `zhang-ec` (ec4_2). Use these exact bucket names with hyphens.
Their endpoint and region are intentionally empty:
set `S3_ENDPOINT_URL` to the confirmed RGW HTTP/HTTPS URL and `S3_REGION` to the
confirmed region. Set `S3_USE_SSL` to match the URL scheme. The SSH login address
is not an RGW endpoint. Credentials remain environment-only as described above.
Unset a stale `S3_BUCKET`; a target override conflicting with a Ceph profile
is rejected before any data request.

```bash
uv run storage-benchmark check --config configs/ceph-rep-io-smoke.toml
uv run storage-benchmark check --config configs/ceph-ec-io-smoke.toml
uv run storage-benchmark run --config configs/ceph-rep-io-smoke.toml --pair-index 1
uv run storage-benchmark run --config configs/ceph-ec-io-smoke.toml --pair-index 1
```

`check` performs small-object PUT, HEAD, full GET, Range GET and byte validation,
then deletes only its disposable object and verifies its absence. Add `--list`
when prefix-listing permission is required by your workflow; otherwise that
step is explicitly skipped. It does not execute performance workloads or
require them to exist. Exit codes: 0 success, 1 request/check/cleanup failure,
2 configuration error. Check results are under `<output_dir>/check/<run-id>/`.

The optional `[experiment]` table supplies `experiment_id`, `backend`,
`storage_policy`, and `pair_index` (default 1). `run --pair-index` overrides the
configured round; matching REP/EC runs share experiment ID and round, while
every invocation gets its own run ID. Old profiles without `[experiment]`
continue to work; `--pair-index` requires that table.

Ceph IO/COG results use `results/ceph-rep/{io,cog}/<run-id>/` and
`results/ceph-ec/{io,cog}/<run-id>/`. Each run saves effective `run_config.toml`,
`run_metadata.json`, `created_objects.json`, `run.log`, and the standard metrics
and samples. Successful samples are appended to CSV immediately; on completion,
ordinary failure or KeyboardInterrupt, JSON and aggregates are exported too.
Partial/failed runs must not be interpreted as complete experiments. Reports now
list their state and errors and exclude incomplete measurements from aggregates.

IO keys are isolated below a run-owned prefix; `cleanup=true` attempts every
registered key even after a workload fails. Cleanup failures are recorded
separately and return nonzero without discarding successful measurements.
Existing COGs are never registered for automatic deletion. IO contents now use a
logical identity (generator version, seed, workload, object index, size and
generation chunk size), independent of bucket and run prefix. After all timed
workloads, each generated object is streamed again to verify its full length,
contents and SHA-256. This adds validation traffic, not performance samples;
`io_verification` and `verification_seconds` record the separate phase.
The generator repeats digest patterns; it is deterministic, not representative
of arbitrary compressibility. Record server compression/deduplication settings.

To validate COG identity and pixels against the trusted upload source, pass the
same reference file for both buckets:

```bash
uv run storage-benchmark run --config configs/ceph-rep-cog-smoke.toml \
  --cog-object-key cog/sample.tif --cog-reference /path/to/source.tif --pair-index 1
uv run storage-benchmark run --config configs/ceph-ec-cog-smoke.toml \
  --cog-object-key cog/sample.tif --cog-reference /path/to/source.tif --pair-index 1
```

After timed reads, validation compares the remote SHA-256/length, raster
metadata and a bounded selection of actual windows against that local file.
The manifest records compression, bands, types, tiles and overviews. Without a
reference, existing COG reads still work but verification remains pending and
the run cannot enter a verified REP/EC pair. This is reference validation, not
an OGC conformance certification. `overview_level` remains unsupported and an
explicit setting is rejected.

Window sample timing stops immediately after `dataset.read`; metadata recording
and reference verification are outside that timer. Full-read timing includes
open/read/close. The historical baseline can still be viewed descriptively;
the changed generator and timing method are not silently treated as identical.
Multipart support and actual Ceph deployment/acceptance remain later tasks.

### Existing MinIO profiles

Smoke profile:

```bash
uv run storage-benchmark run --config configs/minio-smoke.toml
```

VPS smoke profile:

```bash
uv run storage-benchmark run --config configs/minio-vps-smoke.toml
```

Full profile:

```bash
uv run storage-benchmark run --config configs/minio-full.toml
```

Matrix profile:

```bash
uv run storage-benchmark run --config configs/minio-matrix.toml
```

COG/GDAL smoke profile:

```bash
uv run storage-benchmark run --config configs/minio-cog-smoke.toml
```

The COG/GDAL profile expects an existing COG object in MinIO. The default config
reads `s3://benchmark/cog/sample.tif`, so upload or copy a COG to that key before
running the benchmark.

Override the COG object key at runtime when switching between existing COG files:

```bash
uv run storage-benchmark run \
  --config configs/minio-cog-smoke.toml \
  --cog-object-key cog/medium.tif
```

The override applies to all `[[cog_workloads]]`, and the copied
`run_config.toml` records the effective object key used by that run.

Each run writes:

- `results/io/<timestamp>/...` for basic IO-only configs
- `results/cog/<timestamp>/...` for COG/GDAL-only configs
- `results/mixed/<timestamp>/...` for configs containing both

Each result directory contains:

- `metrics.csv`
- `metrics.json`
- `samples.csv`
- `samples.json`
- `run_config.toml`

`metrics.*` contains aggregated workload metrics. `samples.*` contains every
individual read/write operation and includes `repeat_index` for repeat-run
analysis. COG/GDAL samples also include a `details` column for dataset metadata
and window coordinates. The smoke profiles run 3 repeats by default; the full
profile keeps `repeats = 1` to avoid accidentally multiplying large-object
traffic.

Use `smoke` for connectivity and quick validation, `full` for the practical
MinIO benchmark mix, and `matrix` when you explicitly need every object size to
cover sequential write/read and random write/read. The matrix profile includes
16KiB, 10MiB, and 4GiB workloads; the 10GiB large-object case remains only in
the full profile.

## Generate Plots

After a benchmark run, generate matplotlib PNG charts from the result directory:

```bash
uv run storage-benchmark plot --result-dir results/io/<timestamp>
```

This writes:

- `<result-dir>/plots/throughput_mb_s.png`
- `<result-dir>/plots/iops.png`
- `<result-dir>/plots/latency_summary_ms.png`
- `<result-dir>/plots/latency_distribution_ms.png`

For COG/GDAL result directories, the same command also writes:

- `<result-dir>/plots/cog_latency_by_operation_ms.png`
- `<result-dir>/plots/cog_read_size_mb.png`
- `<result-dir>/plots/cog_latency_over_time_ms.png`
- `<result-dir>/plots/cog_window_latency_scatter_ms.png`

## Compare Multiple Runs

After running the same profile multiple times, generate a cross-run comparison
report from existing result directories:

```bash
uv run storage-benchmark compare \
  --result-dir results/cog/20260520T100000Z \
  --result-dir results/cog/20260520T110000Z
```

Or select the latest valid result directories under a result root:

```bash
uv run storage-benchmark compare --result-root results/cog --latest 5
```

By default this writes a report under `reports/<type>-compare-<timestamp>/`
with:

- `combined_metrics.csv`
- `combined_samples.csv`
- `summary.md`
- cross-run throughput, IOPS, latency, and COG-specific PNG charts

New reports also include `pair_validation.json`, `paired_metrics.csv` and
`paired_samples.csv` (plus a paired throughput chart when valid pairs exist).
Only completed, correctly counted runs with matching methods, environment,
verified input contents and access sequence enter a REP/EC pair. Missing sides,
duplicates, inconsistent sample aggregates and precise differing fields are
reported. One experiment cannot silently change method between rounds.
Legacy results remain readable with unknown labels and do not become inferred
Ceph pairs. Plots and summaries use MiB/s while retaining `throughput_mb_s` in
CSV/JSON. COG throughput is decoded-array processing rate, not network traffic.

## Tests

```bash
uv run pytest
```
