.PHONY: install api web build test verify clean-data

PYTHON ?= python3.13
VENV_PYTHON ?= .venv/bin/python3.13

install:
	$(PYTHON) -m venv .venv
	$(VENV_PYTHON) -m pip install -r requirements.txt
	cd apps/web && npm install

api:
	$(VENV_PYTHON) -m uvicorn apps.platform_api.main:app --reload --host 0.0.0.0 --port 8000

web:
	cd apps/web && npm run dev

build:
	cd apps/web && npm run build

test:
	$(VENV_PYTHON) -m pytest -q

verify: test build

clean-data:
	rm -f data/deepagent.db data/deepagent.db-shm data/deepagent.db-wal
