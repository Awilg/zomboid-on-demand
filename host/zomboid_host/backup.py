import logging
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "backups/"


def backup_key(now: datetime, reason: str) -> str:
    return f"{PREFIX}{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{reason}.tar.zst"


def _world_paths(data_dir: Path, server_name: str) -> list[Path]:
    """Existing paths that make up the world, relative to data_dir."""
    paths = [
        Path("Saves/Multiplayer") / server_name,
        Path("db") / f"{server_name}.db",
    ]
    server_dir = data_dir / "Server"
    if server_dir.is_dir():
        paths += sorted(p.relative_to(data_dir) for p in server_dir.glob(f"{server_name}*"))
    return paths


def make_archive(data_dir: Path, server_name: str, out: Path) -> Path:
    data_dir, out = Path(data_dir), Path(out)
    world = data_dir / "Saves/Multiplayer" / server_name
    if not world.is_dir():
        raise FileNotFoundError(f"world directory not found: {world}")
    db = data_dir / "db" / f"{server_name}.db"
    if not db.is_file():
        # A world without its db is still better than no back-up.
        log.warning("world database not found: %s; the back-up has no db", db)

    proc = subprocess.Popen(
        ["zstd", "-q", "-f", "-T0", "-o", str(out)], stdin=subprocess.PIPE
    )
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
            for rel in _world_paths(data_dir, server_name):
                if (data_dir / rel).exists():
                    tf.add(data_dir / rel, arcname=str(rel))
    finally:
        proc.stdin.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"zstd failed with exit code {code}")
    return out


def list_backups(s3, bucket: str) -> list[str]:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        keys += [obj["Key"] for obj in page.get("Contents", [])]
    return sorted(keys, reverse=True)


def upload_and_prune(s3, bucket: str, archive: Path, key: str, retain: int) -> None:
    # If the upload raises, the exception propagates and nothing is pruned.
    s3.upload_file(str(archive), bucket, key)
    stale = list_backups(s3, bucket)[retain:]
    for i in range(0, len(stale), 1000):
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in stale[i : i + 1000]], "Quiet": True},
        )


def _extract(archive: Path, dest: Path) -> None:
    with open(archive, "rb") as src:
        proc = subprocess.Popen(["zstd", "-dc", "-q"], stdin=src, stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                tf.extractall(dest, filter="data")
        finally:
            proc.stdout.close()
            code = proc.wait()
    if code != 0:
        raise RuntimeError(f"zstd failed with exit code {code}")


def restore(s3, bucket: str, key: str, data_dir: Path, server_name: str) -> None:
    data_dir = Path(data_dir)
    with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
        tmp = Path(tmp)
        archive = tmp / "download.tar.zst"
        s3.download_file(bucket, key, str(archive))
        # Extract to staging first: an unsafe or corrupt archive fails
        # before the current world is touched.
        staging = tmp / "staging"
        staging.mkdir()
        _extract(archive, staging)

        trash = data_dir / "restore-trash" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        for rel in _world_paths(data_dir, server_name):
            src = data_dir / rel
            if src.exists():
                (trash / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, trash / rel)

        for staged in sorted(staging.iterdir()):
            for path in sorted(p for p in staged.rglob("*") if p.is_file()) or [staged]:
                rel = path.relative_to(staging)
                (data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(path, data_dir / rel)
