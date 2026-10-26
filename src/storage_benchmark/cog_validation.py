from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from storage_benchmark.cog_gdal import DatasetOpener, _dataset_details
from storage_benchmark.config import S3Settings
from storage_benchmark.gdal_client import open_raster
from storage_benchmark.run_state import RunJournal
from storage_benchmark.s3_client import S3Client


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def describe_cog(path: Path) -> dict:
    import rasterio

    with path.open("rb") as stream:
        sha256, size = _hash_stream(stream)
    with rasterio.open(path) as dataset:
        details = _dataset_details(dataset)
    return {**details, "sha256": sha256, "size_bytes": size}


def validate_cog(
    client: S3Client, settings: S3Settings, key: str, reference: Path,
    expected: dict, journal: RunJournal, *, opener: DatasetOpener = open_raster,
) -> None:
    import json
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    journal.step = "cog_verification"
    entry = next(item for item in journal.metadata["dataset_manifest"]
                 if item["kind"] == "cog" and item["bucket"] == client.bucket and item["object_key"] == key)
    entry.update(verification="running", reference_sha256=expected["sha256"], pixel_checks=0)
    journal.save()
    try:
        stream = client.get_object_stream(key)
        try:
            sha256, size = _hash_stream(stream)
        finally:
            stream.close()
        entry.update(sha256=sha256, size_bytes=size)
        if sha256 != expected["sha256"] or size != expected["size_bytes"]:
            entry["error"] = "file_identity_mismatch"
            raise ValueError("Remote COG bytes differ from local reference")
        with rasterio.open(reference) as local, opener(settings, client.bucket, key) as remote:
            observed = _dataset_details(remote)
            if observed != {name: expected[name] for name in observed}:
                entry["error"] = "raster_metadata_mismatch"
                raise ValueError("Remote raster metadata differs from local reference")
            entry.update(observed)
            # Check a bounded set of actual timed windows per band selection,
            # plus a small reference window for metadata/fullread-only profiles.
            selections: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
            for sample in journal.samples:
                if sample.object_key != key or sample.operation not in {"coginfo", "fullread", "randomwindow", "tilewindow"}:
                    continue
                details = json.loads(sample.details)
                if details["bucket"] != client.bucket:
                    continue
                bands = tuple(details["band_indexes"])
                windows = selections.setdefault(bands, [])
                window = details.get("window")
                coordinates = tuple(window[n] for n in ("col_off", "row_off", "width", "height")) if window else (0, 0, min(64, local.width), min(64, local.height))
                if coordinates not in windows and len(windows) < 4:
                    windows.append(coordinates)
            for bands, windows in selections.items():
                for coordinates in windows:
                    window = Window(*coordinates)
                    actual = remote.read(list(bands), window=window)
                    wanted = local.read(list(bands), window=window)
                    if actual.dtype != wanted.dtype or not np.array_equal(actual, wanted, equal_nan=True):
                        entry["error"] = "pixel_mismatch"
                        raise ValueError("Remote COG pixels differ from local reference")
                    entry["pixel_checks"] += 1
            if entry["pixel_checks"] == 0:
                raise ValueError("No COG performance samples available for reference checks")
        entry["verification"] = "passed"
    except (Exception, KeyboardInterrupt):
        entry["verification"] = "failed"
        raise
    finally:
        journal.save()
