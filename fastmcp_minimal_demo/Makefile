SHELL := /bin/sh
.DEFAULT_GOAL := help

REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
UV ?= uv
UV_RUN := $(UV) run --locked --group mcp
API_COMMAND := $(UV_RUN) uvicorn fastmcp_minimal_demo.api:app --host 127.0.0.1 --port 8032
MCP_COMMAND := $(UV_RUN) python -m fastmcp_minimal_demo.server
OPENAPI_URL := http://127.0.0.1:8032/openapi.json

.PHONY: help start api mcp mcp-auth mcp-hitl client

help:
	@printf '%s\n' \
		'make start   启动 FastAPI 和 MCP，按 Ctrl+C 停止' \
		'make api     单独启动 FastAPI' \
		'make mcp     单独启动 MCP，需先启动 FastAPI' \
		'make mcp-auth 启动需要 Bearer Token 的 MCP，监听 8031' \
		'make mcp-hitl 启动人工确认 MCP，监听 8037，需先启动 FastAPI' \
		'make client  查看并调用 add 工具'

start:
	@set -e; \
		cd "$(REPO_ROOT)"; \
		$(UV) sync --locked --group mcp; \
		if curl --silent --max-time 1 --output /dev/null "$(OPENAPI_URL)"; then \
			printf '%s\n' 'FastAPI 端口 8032 已被占用。' >&2; \
			exit 1; \
		fi; \
		api_pid=; mcp_pid=; \
		cleanup() { \
			for pid in $$mcp_pid $$api_pid; do kill "$$pid" 2>/dev/null || :; done; \
			wait; \
		}; \
		trap cleanup EXIT; \
		trap 'exit 130' INT; \
		trap 'exit 143' TERM; \
		$(API_COMMAND) & api_pid=$$!; \
		attempts=0; \
		until curl --fail --silent --max-time 1 --output /dev/null "$(OPENAPI_URL)"; do \
			attempts=$$((attempts + 1)); \
			if ! kill -0 "$$api_pid" 2>/dev/null || [ "$$attempts" -ge 40 ]; then \
				printf '%s\n' 'FastAPI 启动失败，请检查上方日志。' >&2; \
				exit 1; \
			fi; \
			sleep 0.2; \
		done; \
		$(MCP_COMMAND) & mcp_pid=$$!; \
		wait "$$mcp_pid"

api:
	cd "$(REPO_ROOT)" && exec $(API_COMMAND)

mcp:
	cd "$(REPO_ROOT)" && exec $(MCP_COMMAND)

mcp-auth:
	cd "$(REPO_ROOT)" && exec $(UV_RUN) python -m fastmcp_minimal_demo.auth_server

mcp-hitl:
	cd "$(REPO_ROOT)" && exec $(UV_RUN) python -m fastmcp_minimal_demo.hitl_server

client:
	cd "$(REPO_ROOT)" && exec $(UV_RUN) python -m fastmcp_minimal_demo.client
