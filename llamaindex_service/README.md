# LlamaIndex 企业知识库服务

面向企业知识库、文档问答和 RAG 的独立后端服务，通过 HTTP API 为业务系统和 Agent 提供文档管理、权限检索、带引用的问答能力。

**当前状态：后端功能已实现，可本地启动；真实 Embedding 效果和生产环境验收待完成。**

- [已批准方案](docs/architecture.md)、[实现与验证记录](docs/implementation.md)
- [API 请求样例](docs/api-examples.md)、[生产配置与维护](docs/operations.md)
- [评估与并发探针](evaluation/README.md)
- 技术栈：FastAPI + LlamaIndex + PostgreSQL/pgvector + 独立摄取 Worker。
- 完整流程：创建知识库 → 上传文档 → 建立索引 → 检索/问答 → 查看引用 → 更新/删除。

## 本地启动

以下命令均从仓库根目录运行，依赖统一放在根目录 `rag` 依赖组，使用根目录 `uv.lock` 和 `.venv`。SQLite 仅用于隔离测试，本地服务使用 PostgreSQL。

```bash
uv sync --group rag
docker compose -f llamaindex_service/deploy/compose.yaml up -d postgres
uv run --group rag python -m llamaindex_service.migrations
```

分别在两个终端启动 API 和 Worker：

```bash
# 终端一：只允许本机开发身份，关闭代理头信任
RAG_MODEL_MODE=mock uv run --group rag uvicorn llamaindex_service.app:app --host 127.0.0.1 --port 8090 --no-proxy-headers
```

```bash
# 终端二：与 API 使用相同的模型、维度、数据库及文件目录
RAG_MODEL_MODE=mock uv run --group rag python -m llamaindex_service.workers
```

打开 [API 文档](http://127.0.0.1:8090/docs)，或使用 [请求样例](docs/api-examples.md)。默认 PostgreSQL 地址为 `127.0.0.1:55432`，本地账号/数据库均为 `rag`。若该端口已有本任务启动的 `z-rag-postgres`，可以复用，不再启动第二个数据库。

`mock` 使用确定性向量与带引用的原文摘录，仅用于验证功能，不用于语义问答效果评估。切换真实模式需要设置独立的 `RAG_EMBEDDING_MODEL`、`RAG_EMBEDDING_DIMENSION`、`RAG_EMBEDDING_BASE_URL` 与 `RAG_EMBEDDING_API_KEY`，以及根目录聊天模型使用的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL`。参考 [配置样例](deploy/.env.example)，将需要的变量合并到现有根目录 `.env` 或进程环境，勿覆盖原有配置。

切换 mock/live 或 Embedding 模型前，用新知识库重新导入或按维护文档重建索引；不要把测试向量当成真实模型的向量。

## 已实现功能

- 知识库与文档管理、TXT/Markdown/文本 PDF/DOCX 上传、处理进度和重试。
- 持久任务、租约心跳、Worker 重启恢复、原子版本切换、删除与回收。
- 两路 SQL 授权过滤、pgvector 精确召回、中文词法召回、RRF、可选重排。
- JSON/SSE 问答、来源引用、多轮会话、无证据反馈、幂等和断连取消。
- JWT 与服务端成员关系、租户/项目/知识库/文档权限、数据库 RLS。
- 索引代次重建/激活/取消、Local/S3 存储、保留期与孤立对象维护。

## 验证

```bash
# 离线功能测试；真实数据库用例未设置 URL 时明确跳过
uv run --group rag python -m unittest discover -s llamaindex_service/tests -v

# 使用专用测试库与拥有 CREATEDB 权限的测试账号，绝不能指向生产环境
RAG_TEST_DATABASE_URL=postgresql+psycopg://rag:rag@127.0.0.1:55432/rag_http_test uv run --group rag python -m unittest discover -s llamaindex_service/tests -v

uv run --group rag ruff check llamaindex_service
uv run --group rag ruff format --check llamaindex_service
```

真实效果验收需要带人工标注的企业问题集、可用 Embedding 模型及目标硬件。当前没有百万片段容量或生产问答准确率承诺，详见 [实现与验证记录](docs/implementation.md)。
