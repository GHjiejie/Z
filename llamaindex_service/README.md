# LlamaIndex 企业知识库服务

面向企业知识库、文档问答和 RAG 的独立后端服务，计划通过 HTTP API 为业务系统和 Agent 提供文档管理、权限检索、带引用的问答能力。

**当前状态：方案待 review，尚未实现服务。**

- [架构与实施方案](docs/architecture.md)：范围、技术选型、LlamaIndex 职责、文档生命周期、权限、API、部署及验收。
- 推荐路线：FastAPI + LlamaIndex + PostgreSQL/pgvector + 独立摄取 Worker。
- 一期围绕「创建知识库 → 上传文档 → 建立索引 → 检索/问答 → 查看引用 → 更新/删除」交付。

遵循仓库统一 Python 环境约定，后续依赖加入根目录 `pyproject.toml`，使用根目录 `uv.lock` 和 `.venv`。本目录不创建独立 Python 项目或虚拟环境。

本次仅创建方案文档，方案中的模块目录、接口、数据库表与部署文件均为规划。
