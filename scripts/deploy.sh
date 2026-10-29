#!/usr/bin/env bash
# Build the host bundle and the bot package, then apply both Terraform stacks.
# This script never touches a running game. The new bundle applies at the next boot.
set -euo pipefail

cd "$(dirname "$0")/.."

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
"$PY" -m pytest -q

rm -rf build
mkdir -p build/stage build/bot

# Host bundle. The layout must match what bootstrap.sh and user data expect.
mkdir -p build/stage/host build/stage/config
cp pyproject.toml build/stage/
cp -R host/zomboid_host host/systemd host/bootstrap.sh build/stage/host/
cp config/settings.toml build/stage/config/
# config/server holds the game templates; it may not exist yet.
if [ -d config/server ]; then cp -R config/server build/stage/config/; fi
find build/stage -name __pycache__ -type d -prune -exec rm -rf {} +
find build/stage -name '*.pyc' -delete
tar -czf build/bundle.tar.gz -C build/stage .

# Bot package.
"$PY" -m pip install --quiet --platform manylinux2014_x86_64 --implementation cp \
    --python-version 3.12 --only-binary=:all: --target build/bot pynacl
cp -R bot/zomboid_bot build/bot/
find build/bot -name __pycache__ -type d -prune -exec rm -rf {} +
(cd build/bot && zip -qr ../bot.zip .)

# Give Terraform plain credentials. It may not read an `aws login` profile.
# Assign first: a failure inside eval "$(...)" does not stop the script.
CREDS=$(aws configure export-credentials --format env) || {
    echo "error: no AWS credentials. Run 'aws login --profile zomboid', then 'export AWS_PROFILE=zomboid'." >&2
    exit 1
}
eval "$CREDS"
terraform -chdir=infra/data init -input=false
terraform -chdir=infra/data apply
BUCKET=$(terraform -chdir=infra/data output -raw backup_bucket)

aws s3 cp build/bundle.tar.gz "s3://$BUCKET/bundle/current.tar.gz"

terraform -chdir=infra/main init -input=false
terraform -chdir=infra/main apply -var "backup_bucket=$BUCKET"

echo "Discord Interactions Endpoint URL:"
terraform -chdir=infra/main output -raw interactions_url
echo
