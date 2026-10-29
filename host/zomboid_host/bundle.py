"""Fetch the config and code bundle. Run by bootstrap.sh as `python -m zomboid_host.bundle BUCKET DEST`."""

import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import boto3

BUNDLE_KEY = "bundle/current.tar.gz"


def fetch_bundle(s3, bucket: str, dest: Path) -> None:
    """Download and unpack the bundle to a staging dir, then swap it into dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        archive = Path(tmp) / "bundle.tar.gz"
        s3.download_file(bucket, BUNDLE_KEY, str(archive))
        staging = Path(tmp) / "bundle"
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(staging, filter="data")
        shutil.rmtree(dest, ignore_errors=True)
        staging.rename(dest)


def main(argv: list[str] | None = None) -> None:
    from zomboid_host.lifecycle import imds_get

    bucket, dest = argv if argv is not None else sys.argv[1:]
    region = imds_get("placement/region")
    fetch_bundle(boto3.client("s3", region_name=region), bucket, Path(dest))


if __name__ == "__main__":
    main()
