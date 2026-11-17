SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

PYTHON ?= python3
ARGS ?=
PG_MAJOR ?=
PG_BIN_DIR ?= $(if $(PG_MAJOR),/usr/lib/postgresql/$(PG_MAJOR)/bin,)
SCHEMA_BIN_DIR ?=
IMAGE ?= azure-postgresql-toolbox:local
PLATFORM ?=
PLATFORMS ?= linux/amd64,linux/arm64
DATA_VOLUME ?= azure-postgresql-backups
NAMESPACE ?= postgres-migration
KUBECTL ?= kubectl

.PHONY: help backup restore validate pitr-backup pitr-restore test test-integration test-image-integration docker-build docker-publish docker-run docker-test aks-render aks-deploy aks-shell
help:
	@$(PYTHON) server_backup.py --help
	@echo ""
	@echo "Container: make docker-build docker-test docker-run (IMAGE=...)"
	@echo "Publish:   make docker-publish IMAGE=registry/image:tag"
	@echo "AKS:       make aks-render aks-deploy aks-shell (IMAGE=... NAMESPACE=...)"
	@echo "Clients:   PG_MAJOR=16 or 18 in the container; SCHEMA_BIN_DIR always uses 18"
	@echo "Azure PITR: make pitr-backup or pitr-restore ARGS='...' (Azure login required)"

backup restore validate:
	@$(PYTHON) server_backup.py $@ $(if $(PG_BIN_DIR),--bin-dir $(PG_BIN_DIR),) $(if $(SCHEMA_BIN_DIR),--schema-bin-dir $(SCHEMA_BIN_DIR),) $(ARGS)

pitr-backup pitr-restore:
	@$(PYTHON) azure_pitr.py $(patsubst pitr-%,%,$@) $(ARGS)

test:
	@$(PYTHON) -B -m unittest discover -s tests -v

test-integration:
	@PG_BACKUP_INTEGRATION=1 $(PYTHON) -B -m unittest discover -s tests -p test_integration.py -v

test-image-integration:
	@PG_BACKUP_INTEGRATION=1 PG_BACKUP_TEST_IMAGE="$(IMAGE)" $(PYTHON) -B -m unittest discover -s tests -p test_integration.py -v

docker-build:
	docker build $(if $(PLATFORM),--platform "$(PLATFORM)",) --tag "$(IMAGE)" .

docker-publish:
	@test "$(IMAGE)" != "azure-postgresql-toolbox:local" || { echo "Set IMAGE to your approved registry/image:tag before publishing" >&2; exit 1; }
	docker buildx build --platform "$(PLATFORMS)" --tag "$(IMAGE)" --push .

docker-run:
	docker run --rm -it --read-only --cap-drop=ALL --security-opt=no-new-privileges \
		--tmpfs /tmp:rw,noexec,nosuid,size=64m \
		--mount type=volume,source="$(DATA_VOLUME)",target=/data "$(IMAGE)" bash

docker-test:
	docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
		--tmpfs /tmp:rw,noexec,nosuid,size=64m \
		--tmpfs /data:rw,noexec,nosuid,uid=10001,gid=10001,mode=0700 \
		"$(IMAGE)" bash -c 'make test && python3 azure_pitr.py --help && az postgres flexible-server backup create --help >/dev/null && az postgres flexible-server restore --help >/dev/null'

aks-render:
	@$(KUBECTL) kustomize k8s | sed 's|example.azurecr.io/azure-postgresql-toolbox:replace-me|$(IMAGE)|g; s|namespace: postgres-migration|namespace: $(NAMESPACE)|g; s|name: postgres-migration|name: $(NAMESPACE)|g'

aks-deploy:
	@test "$(IMAGE)" != "azure-postgresql-toolbox:local" || { echo "Set IMAGE to your pushed registry image/digest" >&2; exit 1; }
	@$(MAKE) --no-print-directory aks-render | $(KUBECTL) apply -f -
	$(KUBECTL) -n "$(NAMESPACE)" rollout status deployment/postgres-toolbox --timeout=180s

aks-shell:
	$(KUBECTL) -n "$(NAMESPACE)" exec -it deployment/postgres-toolbox -- bash
