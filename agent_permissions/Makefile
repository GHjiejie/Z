SHELL := /bin/sh
.DEFAULT_GOAL := help

# Keep UI builds, contract generation and tests ordered, including with make -j.
.NOTPARALLEL:

PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_ROOT := $(abspath $(PROJECT_DIR)/..)
CONSOLE_DIR := $(PROJECT_DIR)/console
DEPLOY_ENV := $(PROJECT_DIR)/deploy/.env
COMPOSE_FILE := $(PROJECT_DIR)/deploy/compose.yaml

UV ?= uv
NPM ?= npm
HOST ?= 127.0.0.1
PORT ?= 8000
UI_PORT ?= 5173
ADMIN_USERNAME ?= admin
ADMIN_DISPLAY_NAME ?= 管理员
PROMPT_PASSWORD ?= 0

# An explicit ENV_FILE= disables the file; process environment still applies.
ENV_FILE ?= $(wildcard $(DEPLOY_ENV))
ENV_ARGS = $(if $(strip $(ENV_FILE)),--env-file "$(abspath $(ENV_FILE))",)
PASSWORD_ARGS = $(if $(filter 1,$(PROMPT_PASSWORD)),--prompt-password,)

PYTHON_TESTS := api_tools.test_tools agent_permissions.test_tools \
	agent_permissions.test_chat_model agent_permissions.test_chat_api agent_permissions.test_tracing agent_permissions.test_hitl agent_permissions.test_agui \
	agent_permissions.test_api agent_permissions.test_rbac agent_permissions.test_agent_grants agent_permissions.test_console \
	agent_permissions.test_connection_pool.PoolConfigurationTests \
	agent_permissions.test_connection_pool.DatabaseLifecycleTests

.PHONY: help setup python-deps ui-deps model-env init-admin build ui-build tools \
	run start serve dev ui-dev check-env db-config db-up db-down db-status \
	check check-python check-ui test test-python test-ui

help:
	@printf '%s\n' \
		'Agent Console / Chat' \
		'用法：make -C agent_permissions <目标> [变量=值]' \
		'' \
		'  setup         安装根目录 Python 和 Console 依赖，准备本模块模型配置' \
		'  db-up         准备私有配置并启动 Docker PostgreSQL，等待健康检查' \
		'  init-admin    仅在空数据库中创建初始管理员，默认密码 12345' \
		'  run / start   构建 Console 和工具包，然后前台启动服务（Ctrl+C 停止）' \
		'  serve         直接启动服务，使用已构建的 Console' \
		'  dev           启动可自动重载的后端' \
		'  ui-dev        启动 Vite；其 API 代理连接本机 8000 端口' \
		'  build         构建 Console，再生成 API 工具包' \
		'  tools         生成并核对 API 工具包' \
		'  db-status     查看 Docker PostgreSQL 状态' \
		'  db-down       停止 Docker PostgreSQL，保留数据库卷' \
		'  check         Python / TypeScript / 格式检查' \
		'  test          后端离线测试和前端浏览器测试' \
		'  test-python   仅执行后端离线测试' \
		'  test-ui       构建前端并执行 Playwright 测试（需已安装 Chromium）' \
		'' \
		'变量：HOST=127.0.0.1 PORT=8000 UI_PORT=5173 ADMIN_USERNAME=admin' \
		'      ADMIN_DISPLAY_NAME=管理员 PROMPT_PASSWORD=1（交互设置初始密码）' \
		'      ENV_FILE=/绝对路径/配置文件；默认使用存在的 deploy/.env' \
		'      ENV_FILE= 不载入数据库配置文件；未设置数据库 URL 时使用 SQLite' \
		'模型配置：agent_permissions/.env；已有文件和凭据会保留'

setup: python-deps ui-deps model-env

python-deps:
	cd "$(REPO_ROOT)" && $(UV) sync --locked

ui-deps:
	cd "$(CONSOLE_DIR)" && $(NPM) ci

model-env:
	@if [ -e "$(PROJECT_DIR)/.env" ]; then \
		printf '%s\n' '模型配置已存在，保留 agent_permissions/.env'; \
	else \
		(umask 077; set -C; cat "$(PROJECT_DIR)/.env.example" > "$(PROJECT_DIR)/.env") && \
		printf '%s\n' '已创建 agent_permissions/.env，请填写模型 API Key'; \
	fi

check-env:
	@if [ -n "$(strip $(ENV_FILE))" ] && [ ! -f "$(abspath $(ENV_FILE))" ]; then \
		printf '%s\n' 'ENV_FILE 指定的文件不存在，请检查路径或运行 make db-up'; \
		exit 1; \
	fi

init-admin: check-env
	cd "$(REPO_ROOT)" && $(UV) run python -m agent_permissions init-admin \
		--username "$(ADMIN_USERNAME)" --display-name "$(ADMIN_DISPLAY_NAME)" \
		$(ENV_ARGS) $(PASSWORD_ARGS)

ui-build:
	cd "$(CONSOLE_DIR)" && $(NPM) run build

tools:
	cd "$(REPO_ROOT)" && $(UV) run python -m agent_permissions.tools build
	cd "$(REPO_ROOT)" && $(UV) run python -m agent_permissions.tools check

build: ui-build tools

run: build serve

start: run

serve: check-env
	@test -f "$(CONSOLE_DIR)/dist/index.html" || \
		{ printf '%s\n' 'Console 尚未构建，请先执行 make build 或 make run'; exit 1; }
	@printf '%s\n' 'Console: http://$(HOST):$(PORT)/console/' \
		'Chat:    http://$(HOST):$(PORT)/console/#chat'
	cd "$(REPO_ROOT)" && exec $(UV) run uvicorn agent_permissions.main:app \
		--host "$(HOST)" --port "$(PORT)" --no-proxy-headers $(ENV_ARGS)

dev: check-env
	cd "$(REPO_ROOT)" && exec $(UV) run uvicorn agent_permissions.main:app \
		--host "$(HOST)" --port "$(PORT)" --no-proxy-headers $(ENV_ARGS) \
		--reload --reload-dir "$(PROJECT_DIR)" --reload-dir "$(REPO_ROOT)/api_tools"

ui-dev:
	cd "$(CONSOLE_DIR)" && exec $(NPM) run dev -- --port "$(UI_PORT)" --strictPort

db-config:
	cd "$(REPO_ROOT)" && $(UV) run python -m agent_permissions.setup_postgres

db-up: db-config
	cd "$(REPO_ROOT)" && docker compose --env-file "$(DEPLOY_ENV)" \
		-f "$(COMPOSE_FILE)" up -d --wait

db-down:
	cd "$(REPO_ROOT)" && docker compose --env-file "$(DEPLOY_ENV)" \
		-f "$(COMPOSE_FILE)" down

db-status:
	cd "$(REPO_ROOT)" && docker compose --env-file "$(DEPLOY_ENV)" \
		-f "$(COMPOSE_FILE)" ps

check: check-python check-ui

check-python:
	cd "$(REPO_ROOT)" && $(UV) run ruff check api_tools agent_permissions
	cd "$(REPO_ROOT)" && $(UV) run ruff format --check api_tools agent_permissions

check-ui:
	cd "$(CONSOLE_DIR)" && $(NPM) run typecheck
	cd "$(CONSOLE_DIR)" && $(NPM) run format:check

test: test-python test-ui

test-python:
	cd "$(REPO_ROOT)" && $(UV) run python -m unittest $(PYTHON_TESTS) -v

test-ui:
	cd "$(CONSOLE_DIR)" && $(NPM) run test:e2e
