.PHONY: install migrate api worker sandbox-service web build test test-web test-parser-linux verify clean-data release-tools release-locks release-images release-scan test-integration test-docker

PYTHON ?= python3.13
VENV_PYTHON ?= .venv/bin/python3.13

install:
	$(PYTHON) -m venv .venv
	$(VENV_PYTHON) -m pip install --require-hashes --only-binary=:all: -r requirements.build.lock
	$(VENV_PYTHON) -m pip install --require-hashes --only-binary=:all: --no-binary=alibabacloud-tea --no-build-isolation -r requirements.txt
	cd apps/web && npm ci --ignore-scripts --no-audit --no-fund

migrate:
	$(VENV_PYTHON) -m packages.persistence.migrate

api:
	$(VENV_PYTHON) -m uvicorn apps.platform_api.main:app --reload --host 0.0.0.0 --port 8000

worker:
	DEEPAGENT_PROCESS_ROLE=worker $(VENV_PYTHON) -m apps.platform_worker.main

sandbox-service:
	$(VENV_PYTHON) -m apps.sandbox_service.main

web:
	cd apps/web && npm run dev

build:
	cd apps/web && npm run build

test:
	$(VENV_PYTHON) -m pytest -q

test-web:
	cd apps/web && npm test

test-parser-linux:
	$(VENV_PYTHON) -c 'import os,sys; sys.exit(0 if sys.platform.startswith("linux") and os.geteuid() != 0 else "Parser security acceptance requires an unprivileged Linux worker")'
	DEEPAGENT_PARSER_OS_SANDBOX=true $(VENV_PYTHON) -m pytest -q tests/test_parser_os_sandbox.py -k real_linux --tb=short

verify: test test-web build

release-tools:
	$(VENV_PYTHON) -m pip install --require-hashes --only-binary=:all: -r requirements.tools.lock

release-locks:
	$(VENV_PYTHON) scripts/release.py locks

release-images:
	$(VENV_PYTHON) scripts/release.py images --revision "$(or $(REVISION),uncommitted)"

release-scan:
	$(VENV_PYTHON) scripts/release.py scan --revision "$(REVISION)"

test-integration:
	$(VENV_PYTHON) -m packages.operations.integration_checks platform

test-docker:
	$(VENV_PYTHON) -m packages.operations.integration_checks docker

clean-data:
	rm -f data/deepagent.db data/deepagent.db-shm data/deepagent.db-wal
