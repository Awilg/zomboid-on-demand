#!/bin/bash
# First boot stage, run by zomboid-boot.service as root.
# It updates the host code and systemd units from the bundle, then runs
# `zomboid-host boot`. Any failure powers off, unless state/hold exists.

# Failsafe first: power off after the default hard limit (480) + 20 minutes,
# even if every later step fails.
shutdown -P +500

set -euo pipefail

ROOT="${ZOMBOID_ROOT:-/opt/zomboid}"  # override only for tests
VENV="$ROOT/venv"

on_error() {
    echo "bootstrap failed at line $1" >&2
    if [ -e "$ROOT/state/hold" ]; then
        echo "$ROOT/state/hold exists; the instance stays on" >&2
    else
        systemctl poweroff
    fi
}
trap 'on_error $LINENO' ERR

BUCKET=$(sed -n 's/^BUCKET=//p' "$ROOT/state/host.env")
test -n "$BUCKET"

"$VENV/bin/python" -m zomboid_host.bundle "$BUCKET" "$ROOT/bundle"
"$VENV/bin/pip" install --quiet "$ROOT/bundle"
install -m 0755 "$ROOT/bundle/host/bootstrap.sh" "$ROOT/bootstrap.sh"
cp "$ROOT"/bundle/host/systemd/* /etc/systemd/system/
systemctl daemon-reload

# Not exec: a crash in zomboid-host (for example a bad install) must reach the ERR trap.
"$VENV/bin/zomboid-host" boot --skip-fetch
