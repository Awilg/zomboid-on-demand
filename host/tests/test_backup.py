import io
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from zomboid_host.backup import (
    backup_key,
    list_backups,
    make_archive,
    restore,
    upload_and_prune,
)

pytestmark = pytest.mark.skipif(
    shutil.which("zstd") is None, reason="zstd CLI is not installed"
)

KEY = "backups/20261027T000000Z-manual.tar.zst"


def test_backup_key_format():
    assert (
        backup_key(datetime(2026, 10, 27, 19, 30, tzinfo=UTC), "shutdown")
        == "backups/20261027T193000Z-shutdown.tar.zst"
    )


def test_round_trip_archive_and_restore(s3, bucket, data_dir, tmp_path):
    a = make_archive(data_dir, "pz", tmp_path / "a.tar.zst")
    upload_and_prune(s3, bucket, a, KEY, 14)
    (data_dir / "Saves/Multiplayer/pz/map.bin").write_text("CHANGED")
    restore(s3, bucket, KEY, data_dir, "pz")
    assert (data_dir / "Saves/Multiplayer/pz/map.bin").read_text() == "ORIGINAL"
    assert (data_dir / "db/pz.db").read_text() == "DB"
    assert (data_dir / "Server/pz.ini").read_text() == "INI"


def test_restore_moves_current_world_to_trash(s3, bucket, data_dir, tmp_path):
    a = make_archive(data_dir, "pz", tmp_path / "a.tar.zst")
    upload_and_prune(s3, bucket, a, KEY, 14)
    (data_dir / "Saves/Multiplayer/pz/map.bin").write_text("CHANGED")
    restore(s3, bucket, KEY, data_dir, "pz")
    trashed = list((data_dir / "restore-trash").glob("*/Saves/Multiplayer/pz/map.bin"))
    assert len(trashed) == 1 and trashed[0].read_text() == "CHANGED"


def test_make_archive_without_server_files(data_dir, tmp_path):
    (data_dir / "Server/pz.ini").unlink()
    out = make_archive(data_dir, "pz", tmp_path / "a.tar.zst")
    assert out.exists()


def test_make_archive_without_db_warns_and_archives_world(data_dir, tmp_path, caplog):
    (data_dir / "db/pz.db").unlink()
    out = make_archive(data_dir, "pz", tmp_path / "a.tar.zst")
    raw = subprocess.run(["zstd", "-dc", "-q", str(out)], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        names = tf.getnames()
    assert "Saves/Multiplayer/pz/map.bin" in names
    assert "db/pz.db" not in names
    assert "world database not found" in caplog.text


def test_make_archive_missing_world_raises(data_dir, tmp_path):
    shutil.rmtree(data_dir / "Saves/Multiplayer/pz")
    with pytest.raises(FileNotFoundError, match="world"):
        make_archive(data_dir, "pz", tmp_path / "a.tar.zst")


def test_restore_refuses_unsafe_members(s3, bucket, data_dir, tmp_path):
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    subprocess.run(["zstd", "-q", str(tar_path), "-o", str(tmp_path / "evil.tar.zst")], check=True)
    s3.upload_file(str(tmp_path / "evil.tar.zst"), bucket, KEY)
    with pytest.raises(Exception):
        restore(s3, bucket, KEY, data_dir, "pz")
    assert not (data_dir.parent / "escape.txt").exists()
    assert (data_dir / "Saves/Multiplayer/pz/map.bin").read_text() == "ORIGINAL"


def test_prune_keeps_newest_14(s3, bucket, tmp_path):
    for i in range(15):
        s3.put_object(Bucket=bucket, Key=f"backups/202610{i + 1:02d}T000000Z-manual.tar.zst", Body=b"x")
    archive = tmp_path / "a.tar.zst"
    archive.write_bytes(b"x")
    upload_and_prune(s3, bucket, archive, "backups/20261101T000000Z-new.tar.zst", 14)
    keys = list_backups(s3, bucket)
    assert len(keys) == 14 and keys[0].endswith("new.tar.zst")


def test_list_backups_paginates(s3, bucket):
    for i in range(1005):
        s3.put_object(Bucket=bucket, Key=f"backups/{i:08d}-manual.tar.zst", Body=b"")
    assert len(list_backups(s3, bucket)) == 1005


def test_failed_upload_prunes_nothing(bucket, tmp_path):
    failing_s3 = MagicMock()
    failing_s3.upload_file.side_effect = RuntimeError("boom")
    archive = tmp_path / "a.tar.zst"
    archive.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        upload_and_prune(failing_s3, bucket, archive, KEY, 14)
    failing_s3.delete_objects.assert_not_called()
