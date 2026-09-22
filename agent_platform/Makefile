ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ENV_FILE ?= $(ROOT)/agent_platform/.env
PORT ?= 8000
HOST ?= 127.0.0.1
STATE_DIR ?= $(ROOT)/agent_platform/.data/local
ENV_PATH := $(abspath $(ENV_FILE))
STATE_PATH := $(abspath $(STATE_DIR))
COMPOSE := docker compose --env-file "$(ENV_PATH)" -f "$(ROOT)/agent_platform/deploy/compose.yml"
LOCAL := uv run --no-sync python -m agent_platform.scripts.local

.DEFAULT_GOAL := start

.PHONY: start run stop status help install migrate init dev worker web build test infra gateway-key up down

# Dependencies are prepared sequentially by the supervisor, including under -j.
start:
	@cd "$(ROOT)" && $(LOCAL) start --env-file "$(ENV_PATH)" --state-dir "$(STATE_PATH)" --host "$(HOST)" --port "$(PORT)"

run: start

stop:
	@cd "$(ROOT)" && $(LOCAL) stop --state-dir "$(STATE_PATH)"

status:
	@cd "$(ROOT)" && $(LOCAL) status --state-dir "$(STATE_PATH)"

help:
	@echo 'make start / make     一键准备并启动；Ctrl+C 停止'
	@echo 'make start PORT=8010  使用指定端口'
	@echo 'make status / stop    查看状态 / 停止本脚本启动的服务'
	@echo 'make dev / web        分别运行后端 / Vite 热更新前端'
	@echo 'make infra / gateway-key / up / down  Docker 部署命令'

install:
	cd "$(ROOT)" && uv sync --frozen --all-groups
	npm --prefix "$(ROOT)/agent_platform/apps/web" ci

migrate:
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" alembic -c agent_platform/alembic.ini upgrade head

init: migrate
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" python -m agent_platform init

dev:
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" python -m agent_platform serve --host "$(HOST)" --port "$(PORT)"

worker:
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" python -m agent_platform worker

web:
	PLATFORM_API_PROXY="http://$(HOST):$(PORT)" npm --prefix "$(ROOT)/agent_platform/apps/web" run dev

build:
	npm --prefix "$(ROOT)/agent_platform/apps/web" run build

test:
	cd "$(ROOT)" && uv run python -m unittest discover -s agent_platform/tests -v

infra:
	mkdir -p "$(ROOT)/agent_platform/.data"
	$(COMPOSE) up -d --wait postgres redis litellm

gateway-key:
	$(COMPOSE) exec -T -e GATEWAY_KEY_UID=$$(id -u) -e GATEWAY_KEY_GID=$$(id -g) litellm python /app/platform-bootstrap-key.py

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down
