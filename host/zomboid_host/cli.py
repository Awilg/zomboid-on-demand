import argparse
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import boto3

from zomboid_host import lifecycle, watchdog
from zomboid_host.backup import backup_key, make_archive, upload_and_prune
from zomboid_host.lifecycle import HostContext, Paths
from zomboid_host.settings import load_settings

log = logging.getLogger(__name__)
PATHS = Paths()


def _read_env_file(path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def _base_context(paths: Paths) -> HostContext:
    """A context with AWS clients but no settings or secrets yet."""
    bucket = _read_env_file(paths.host_env)["BUCKET"]
    region = lifecycle.imds_get("placement/region")
    return HostContext(
        settings=None,
        s3=boto3.client("s3", region_name=region),
        ssm=boto3.client("ssm", region_name=region),
        bucket=bucket,
        paths=paths,
    )


def load_context() -> HostContext:
    ctx = _base_context(PATHS)
    ctx.settings = load_settings(PATHS.bundle / "config/settings.toml")
    lifecycle.load_secrets(ctx)
    return ctx


def backup_from_dir(s3, bucket: str, source: Path, settings, reason: str) -> str:
    """Archive a data dir that is not the live server's, and upload it."""
    key = backup_key(datetime.now(UTC), reason)
    with tempfile.TemporaryDirectory() as tmp:
        archive = make_archive(source, settings.server_name, Path(tmp) / "world.tar.zst")
        upload_and_prune(s3, bucket, archive, key, settings.backup_retain)
    return key


def _upload_only(args) -> str:
    """Run on the owner's Mac: no IMDS, host.env, SSM secrets, or /opt."""
    bucket = args.bucket or os.environ.get("BUCKET")
    if not bucket:
        sys.exit("error: set --bucket or the BUCKET environment variable")
    settings_path = Path(__file__).resolve().parents[2] / "config/settings.toml"
    region = args.region or os.environ.get("REGION")
    if not region:
        sys.exit("error: set --region or the REGION environment variable")
    settings = load_settings(settings_path)
    s3 = boto3.client("s3", region_name=region)
    return backup_from_dir(s3, bucket, Path(args.source), settings, args.reason)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="zomboid-host")
    sub = parser.add_subparsers(dest="command", required=True)
    p_boot = sub.add_parser("boot")
    p_boot.add_argument(
        "--skip-fetch", action="store_true", help="use the bundle that bootstrap.sh fetched"
    )
    sub.add_parser("watchdog")
    p_shutdown = sub.add_parser("shutdown")
    p_shutdown.add_argument("--reason", default="admin")
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--reason", default="manual")
    p_backup.add_argument("--from", dest="source", help="archive this data dir instead of the server's")
    p_backup.add_argument("--upload-only", action="store_true", help="skip IMDS and host files")
    p_backup.add_argument("--bucket", help="back-up bucket (default: env BUCKET)")
    p_backup.add_argument("--region", help="bucket region (default: env REGION)")
    args = parser.parse_args(argv)
    if args.command == "backup" and (args.upload_only or args.source):
        if not (args.upload_only and args.source):
            parser.error("--from and --upload-only go together")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "boot":
        try:
            ctx = _base_context(PATHS)
        except Exception:
            log.exception("cannot start boot")
            lifecycle.power_off_unless_held(lifecycle.run_command, PATHS)
            sys.exit(1)
        lifecycle.boot(ctx, fetch=not args.skip_fetch)
    elif args.command == "watchdog":
        watchdog.run_once(load_context())
    elif args.command == "shutdown":
        try:
            ctx = load_context()
        except Exception:
            # The Discord /stop runs this command. The instance must stop anyway.
            log.exception("cannot load the context; power off without a back-up")
            lifecycle.run_command(["systemctl", "poweroff"])
            sys.exit(1)
        lifecycle.shutdown(ctx, args.reason)
    elif args.command == "backup":
        if args.upload_only:
            print(_upload_only(args))
        else:
            print(lifecycle.backup(load_context(), args.reason))
