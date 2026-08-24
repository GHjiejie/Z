.PHONY: install api web build test verify clean-data

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	cd apps/web && npm install

api:
	.venv/bin/uvicorn apps.platform_api.main:app --reload --host 0.0.0.0 --port 8000

web:
	cd apps/web && npm run dev

build:
	cd apps/web && npm run build

test:
	.venv/bin/pytest -q

verify: test build

clean-data:
	rm -f data/deepagent.db data/deepagent.db-shm data/deepagent.db-wal
