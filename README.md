# Decentralized Storage System Benchmark

Python/uv benchmark tools for S3-compatible object storage systems. The current
phase implements lightweight basic IO workloads and COG/GDAL read workloads for
MinIO on a lab VPS, while keeping the S3 access layer compatible with future
Ceph RGW testing.

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

## Tests

```bash
uv run pytest
```
