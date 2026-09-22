.DEFAULT_GOAL := help

UV ?= uv
override TASK := $(value TASK)
override INPUT := $(value INPUT)
override OUTPUT_DIR := $(value OUTPUT_DIR)
export TASK INPUT OUTPUT_DIR
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv

.PHONY: help install setup run start analyze check check-analyze test

help:
	@printf '%s\n' \
	  'make setup                 安装依赖并创建本目录 .env（已有文件不覆盖）' \
	  'make run                   运行默认示例任务' \
	  'make run TASK="你的任务"    运行指定任务' \
	  'make start                 等同于 make run' \
	  'make analyze               分析内置示例需求并生成报告' \
	  'make analyze INPUT=文件.md  分析本地需求文档' \
	  'make analyze TASK="需求"    分析直接输入的需求' \
	  'OUTPUT_DIR=目录            指定报告保存目录（默认 outputs）' \
	  'make check                 检查配置和 Agent 构造，不调用模型' \
	  'make check-analyze         检查三角色流程，不调用模型' \
	  'make test                  运行离线测试，无需密钥' \
	  'make install               按本目录锁文件同步依赖'

install:
	@$(UV) sync --locked

setup: install
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@printf '%s\n' '请在 crewai_agent/.env 填写 OPENAI_API_KEY 和 MODEL，然后执行 make run。'

run start: install
	@$(UV) run --locked python main.py

check: install
	@$(UV) run --locked python main.py --check

analyze: install
	@$(UV) run --locked python main.py --mode requirements

check-analyze: install
	@$(UV) run --locked python main.py --mode requirements --check

test: install
	@$(UV) run --locked python -m unittest discover -s tests -v
