SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

PYTHON ?= python3
ARGS ?=
PG_MAJOR ?=
PG_BIN_DIR ?= $(if $(PG_MAJOR),/usr/lib/postgresql/$(PG_MAJOR)/bin,)
SCHEMA_BIN_DIR ?=
IMAGE ?= azure-postgresql-toolbox:local
IMAGE_TAGS ?=
MANIFEST_SOURCES ?=
PLATFORM ?=
PLATFORMS ?= linux/amd64,linux/arm64
DATA_VOLUME ?= azure-postgresql-backups
NAMESPACE ?= postgres-migration
KUBECTL ?= kubectl

.PHONY: help backup restore validate compare-servers upgrade-check azure-login pitr-backup pitr-restore test test-integration test-image-integration docker-build docker-publish docker-push docker-manifest docker-run docker-test aks-render aks-deploy aks-shell
help:
	@$(PYTHON) server_backup.py --help
	@echo ""
	@echo "Container: make docker-build docker-test docker-run (IMAGE=...)"
	@echo "Publish:   make docker-publish IMAGE=registry/image:tag"
	@echo "CI:        make docker-push IMAGE=...; make docker-manifest IMAGE=... MANIFEST_SOURCES='...'"
	@echo "AKS:       make aks-render aks-deploy aks-shell (IMAGE=... NAMESPACE=...)"
	@echo "Clients:   PG_MAJOR=16 or 18 in the container; SCHEMA_BIN_DIR always uses 18"
	@echo "Azure PITR: make pitr-backup or pitr-restore ARGS='...' (Azure login required)"
	@echo "Azure login: make azure-login ARGS='--tenant TENANT_UUID' (device code)"
	@echo "Compare: make compare-servers ARGS='--source-host ... --destination-host ... --source-user ... --report ... --quiesced'"
	@echo "Upgrade: make upgrade-check ARGS='--phase before|after --host ... --user ... --baseline ... --quiesced [--report ...]'"

backup restore validate:
	@$(PYTHON) server_backup.py $@ $(if $(PG_BIN_DIR),--bin-dir $(PG_BIN_DIR),) $(if $(SCHEMA_BIN_DIR),--schema-bin-dir $(SCHEMA_BIN_DIR),) $(ARGS)

compare-servers upgrade-check:
	@$(PYTHON) server_compare.py $@ $(if $(PG_BIN_DIR),--bin-dir $(PG_BIN_DIR),) $(if $(SCHEMA_BIN_DIR),--schema-bin-dir $(SCHEMA_BIN_DIR),) $(ARGS)

pitr-backup pitr-restore:
	@$(PYTHON) azure_pitr.py $(patsubst pitr-%,%,$@) $(ARGS)

azure-login:
	az login --use-device-code $(ARGS)

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

docker-push:
	@test "$(IMAGE)" != "azure-postgresql-toolbox:local" || { echo "Set IMAGE to your tested registry/image:tag before pushing" >&2; exit 1; }
	docker push "$(IMAGE)"

docker-manifest:
	@test "$(IMAGE)" != "azure-postgresql-toolbox:local" && test -n "$(MANIFEST_SOURCES)" || { echo "Set IMAGE and MANIFEST_SOURCES before publishing a manifest" >&2; exit 1; }
	docker buildx imagetools create --tag "$(IMAGE)" $(foreach tag,$(IMAGE_TAGS),--tag "$(tag)") $(MANIFEST_SOURCES)

docker-run:
	docker run --rm -it --read-only --cap-drop=ALL --security-opt=no-new-privileges \
		--tmpfs /tmp:rw,noexec,nosuid,size=64m \
		--mount type=volume,source="$(DATA_VOLUME)",target=/data "$(IMAGE)" bash

docker-test:
	docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
		--tmpfs /tmp:rw,noexec,nosuid,size=64m \
		--tmpfs /data:rw,noexec,nosuid,uid=10001,gid=10001,mode=0700 \
		"$(IMAGE)" bash -c 'make test && python3 azure_pitr.py --help && az postgres flexible-server backup create --help >/dev/null && az postgres flexible-server restore --help >/dev/null && az storage blob upload --help >/dev/null && az storage blob download --help >/dev/null'

aks-render aks-deploy: IMAGE = $(shell sed -n 's/^[[:space:]]*image: //p' k8s/deployment.yaml)

aks-render:
	@$(KUBECTL) kustomize k8s | sed 's|^\([[:space:]]*image: \).*|\1$(IMAGE)|; s|namespace: postgres-migration|namespace: $(NAMESPACE)|g; s|name: postgres-migration|name: $(NAMESPACE)|g'

aks-deploy:
	@test "$(IMAGE)" != "azure-postgresql-toolbox:local" || { echo "Set IMAGE to your pushed registry image/digest" >&2; exit 1; }
	@$(MAKE) --no-print-directory aks-render | $(KUBECTL) apply -f -
	$(KUBECTL) -n "$(NAMESPACE)" rollout status deployment/postgres-toolbox --timeout=180s

aks-shell:
	$(KUBECTL) -n "$(NAMESPACE)" exec -it deployment/postgres-toolbox -- bash
