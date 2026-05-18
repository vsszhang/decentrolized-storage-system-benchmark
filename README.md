# Decentralized Storage System Benchmark

Python/uv benchmark tools for S3-compatible object storage systems. The current
phase implements lightweight basic IO workloads for MinIO on a lab VPS, while
keeping the S3 access layer compatible with future Ceph RGW testing.

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

Each run writes:

- `results/<timestamp>/metrics.csv`
- `results/<timestamp>/metrics.json`
- `results/<timestamp>/run_config.toml`

## Tests

```bash
uv run pytest
```
