#!/usr/bin/env bash
# Pick one of the newest back-ups. The server loads it at the next start.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    echo "error: .venv is missing. Create it and install the dev extras first." >&2
    exit 1
fi

# Give Terraform and boto3 plain credentials. They may not read an `aws login` profile.
# Assign first: a failure inside eval "$(...)" does not stop the script.
CREDS=$(aws configure export-credentials --format env) || {
    echo "error: no AWS credentials. Run 'aws login --profile zomboid', then 'export AWS_PROFILE=zomboid'." >&2
    exit 1
}
eval "$CREDS"
BUCKET=$(terraform -chdir=infra/data output -raw backup_bucket)
REGION=$(terraform -chdir=infra/data output -raw region)
KEYS=$(BUCKET="$BUCKET" REGION="$REGION" "$PY" -c '
import os, boto3
from zomboid_host.backup import list_backups
s3 = boto3.client("s3", region_name=os.environ["REGION"])
print("\n".join(list_backups(s3, os.environ["BUCKET"])[:14]))
')
if [ -z "$KEYS" ]; then
    echo "error: no back-ups found in s3://$BUCKET" >&2
    exit 1
fi

echo "$KEYS" | awk '{ printf "%2d) %s\n", NR, $0 }'
COUNT=$(echo "$KEYS" | wc -l | tr -d ' ')
read -r -p "Number to restore [1-$COUNT]: " NUM
case "$NUM" in
    ''|*[!0-9]*) echo "error: type a number." >&2; exit 1 ;;
esac
if [ "$NUM" -lt 1 ] || [ "$NUM" -gt "$COUNT" ]; then
    echo "error: the number must be from 1 to $COUNT." >&2
    exit 1
fi
KEY=$(echo "$KEYS" | sed -n "${NUM}p")

aws ssm put-parameter --name /zomboid/restore_key --type String --overwrite \
    --region "$REGION" --value "$KEY" >/dev/null
echo "Selected $KEY."
echo "The back-up loads at the next start. If the server runs now, type /stop first."
