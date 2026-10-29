#!/usr/bin/env bash
# Import a world export (zip, see docs/world-export.md) into the back-up bucket as the newest back-up.
# Usage: scripts/import_world.sh <world.zip>
set -euo pipefail

cd "$(dirname "$0")/.."
ZIP_ARG=${1:-}
if [ -z "$ZIP_ARG" ] || [ ! -f "$ZIP_ARG" ]; then
    echo "usage: scripts/import_world.sh <world.zip>" >&2
    exit 1
fi
ZIP=$(cd "$(dirname "$ZIP_ARG")" && pwd)/$(basename "$ZIP_ARG")

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    echo "error: .venv is missing. Create it and install the dev extras first." >&2
    exit 1
fi

NAME=$("$PY" -c 'from pathlib import Path; from zomboid_host.settings import load_settings; print(load_settings(Path("config/settings.toml")).server_name)')
if [ "$NAME" = "CHANGE_ME" ]; then
    echo "error: server_name is still CHANGE_ME. Set it in config/settings.toml first." >&2
    exit 1
fi

rm -rf world-import
mkdir world-import
unzip -q "$ZIP" -d world-import

# The export has its files at the root, or under one top-level Zomboid folder.
ROOT=world-import
if [ ! -e "$ROOT/Saves" ] && [ -d world-import/Zomboid ]; then
    ROOT=world-import/Zomboid
fi

for path in "Saves/Multiplayer/$NAME" "db/$NAME.db" "Server/$NAME.ini"; do
    if [ ! -e "$ROOT/$path" ]; then
        echo "error: the export is missing $path" >&2
        exit 1
    fi
done

mkdir -p config/server
cp "$ROOT/Server/$NAME.ini" config/server/
for extra in "${NAME}_SandboxVars.lua" "${NAME}_spawnregions.lua" "${NAME}_spawnpoints.lua"; do
    if [ -f "$ROOT/Server/$extra" ]; then cp "$ROOT/Server/$extra" config/server/; fi
done
"$PY" - "config/server/$NAME.ini" <<'PYEOF'
import sys
from pathlib import Path

from zomboid_host.settings import render_ini

path = Path(sys.argv[1])
path.write_text(render_ini(path.read_text(), {"Password": "", "RCONPassword": ""}))
PYEOF

# Give Terraform and boto3 plain credentials. They may not read an `aws login` profile.
# Assign first: a failure inside eval "$(...)" does not stop the script.
CREDS=$(aws configure export-credentials --format env) || {
    echo "error: no AWS credentials. Run 'aws login --profile zomboid', then 'export AWS_PROFILE=zomboid'." >&2
    exit 1
}
eval "$CREDS"
BUCKET=$(terraform -chdir=infra/data output -raw backup_bucket)
REGION=$(terraform -chdir=infra/data output -raw region)
KEY=$("$PY" -m zomboid_host.cli backup --from "$ROOT" --reason import --upload-only \
    --bucket "$BUCKET" --region "$REGION")
aws ssm put-parameter --name /zomboid/restore_key --type String --overwrite \
    --region "$REGION" --value "$KEY" >/dev/null

echo "Imported the world as $KEY."
echo "Run make deploy, then type /start in Discord."
