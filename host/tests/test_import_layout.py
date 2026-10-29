import importlib.util
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from zomboid_host.backup import make_archive
from zomboid_host.cli import backup_from_dir, main
from zomboid_host.settings import Settings

REPO = Path(__file__).resolve().parents[2]


def _members(archive: Path) -> list[str]:
    out = subprocess.run(["zstd", "-dc", "-q", str(archive)], capture_output=True, check=True)
    import io

    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tf:
        return sorted(tf.getnames())


@pytest.fixture
def friend_export(tmp_path):
    root = tmp_path / "export"
    (root / "Saves/Multiplayer/pz").mkdir(parents=True)
    (root / "Saves/Multiplayer/pz/map.bin").write_text("MAP")
    (root / "db").mkdir()
    (root / "db/pz.db").write_text("DB")
    (root / "Server").mkdir()
    for suffix in (".ini", "_SandboxVars.lua", "_spawnregions.lua"):
        (root / f"Server/pz{suffix}").write_text("X")
    return root


def test_import_archive_has_same_members_as_server_archive(friend_export, data_dir, s3, bucket, tmp_path):
    (data_dir / "Saves/Multiplayer/pz/map.bin").write_text("MAP")
    for suffix in ("_SandboxVars.lua", "_spawnregions.lua"):
        (data_dir / f"Server/pz{suffix}").write_text("X")
    server_archive = make_archive(data_dir, "pz", tmp_path / "server.tar.zst")

    key = backup_from_dir(s3, bucket, friend_export, Settings(server_name="pz"), "import")

    uploaded = tmp_path / "uploaded.tar.zst"
    s3.download_file(bucket, key, str(uploaded))
    assert key.endswith("-import.tar.zst")
    assert _members(uploaded) == _members(server_archive)


def _run_import(tmp_path, export, server_name="pz", wrap=False):
    """Run the real script in a copy of the repo layout, with stub cloud tools."""
    src = export
    if wrap:
        src = tmp_path / "wrapped"
        shutil.copytree(export, src / "Zomboid")
    zip_path = shutil.make_archive(str(tmp_path / "friend"), "zip", src)

    fake = tmp_path / "repo"
    (fake / "scripts").mkdir(parents=True)
    (fake / "config").mkdir()
    shutil.copy(REPO / "scripts/import_world.sh", fake / "scripts/")
    (fake / "config/settings.toml").write_text(f'server_name = "{server_name}"\n')
    (fake / ".venv").symlink_to(REPO / ".venv")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in ("terraform", "aws"):
        stub = stubs / tool
        stub.write_text("#!/bin/sh\necho stub $0 called >&2\nexit 99\n")
        stub.chmod(0o755)

    return subprocess.run(
        ["bash", str(fake / "scripts/import_world.sh"), zip_path],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"},
    )


def test_import_script_refuses_export_without_db(friend_export, tmp_path):
    (friend_export / "db/pz.db").unlink()
    result = _run_import(tmp_path, friend_export)
    assert result.returncode == 1
    assert "db/pz.db" in result.stderr


def test_import_script_refuses_wrapped_export_without_db(friend_export, tmp_path):
    (friend_export / "db/pz.db").unlink()
    result = _run_import(tmp_path, friend_export, wrap=True)
    assert result.returncode == 1
    assert "missing db/pz.db" in result.stderr


def test_import_script_stops_on_change_me(friend_export, tmp_path):
    result = _run_import(tmp_path, friend_export, server_name="CHANGE_ME")
    assert result.returncode == 1
    assert "CHANGE_ME" in result.stderr
    assert "config/settings.toml" in result.stderr


def test_register_payload_has_the_three_commands():
    spec = importlib.util.spec_from_file_location("register_commands", REPO / "scripts/register_commands.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    payload = mod.build_payload()
    assert [c["name"] for c in payload] == ["start", "status", "stop"]
    assert payload[2]["description"] == "Stop the server (admins only)."
    req = mod.build_request("A", "G", "tok")
    assert req.get_method() == "PUT"
    assert req.full_url.endswith("/applications/A/guilds/G/commands")
    assert req.get_header("Authorization") == "Bot tok"


def test_deploy_script_stops_on_change_me(tmp_path):
    fake = tmp_path / "repo"
    (fake / "scripts").mkdir(parents=True)
    (fake / "config").mkdir()
    shutil.copy(REPO / "scripts/deploy.sh", fake / "scripts/")
    (fake / "config/settings.toml").write_text('server_name = "CHANGE_ME"\n')
    (fake / ".venv").symlink_to(REPO / ".venv")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in ("terraform", "aws"):
        stub = stubs / tool
        stub.write_text("#!/bin/sh\necho stub $0 called >&2\nexit 99\n")
        stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(fake / "scripts/deploy.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1
    assert "CHANGE_ME" in result.stderr
    assert "config/settings.toml" in result.stderr
    assert "stub" not in result.stderr
    assert not (fake / "build").exists()


def test_upload_only_needs_a_region(tmp_path, monkeypatch):
    monkeypatch.delenv("REGION", raising=False)
    with pytest.raises(SystemExit, match="set --region"):
        main(["backup", "--from", str(tmp_path), "--upload-only", "--bucket", "b"])
