test:
	pytest -q

deploy:
	scripts/deploy.sh

restore:
	scripts/restore.sh

import-world:
	@test -n "$(ZIP)" || { echo "usage: make import-world ZIP=<path to world zip>" >&2; exit 1; }
	scripts/import_world.sh "$(ZIP)"

destroy:
	creds=$$(aws configure export-credentials --format env) && eval "$$creds" && \
	terraform -chdir=infra/main destroy -var backup_bucket=$$(terraform -chdir=infra/data output -raw backup_bucket)

.PHONY: test deploy restore import-world destroy
