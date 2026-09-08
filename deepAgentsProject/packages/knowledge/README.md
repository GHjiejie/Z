# Knowledge / RAG 架构

本目录实现项目内的知识库控制面、文档摄取管线和运行时检索工具。原始文件保存在对象存储；数据库只保存稳定对象定位信息、解析结果、索引、版本快照和审计记录。

## 组件边界

```text
React Knowledge Console
        │
        ▼
FastAPI Knowledge API
        │
        ├── ObjectStorage port ── Local adapter / Alibaba Cloud OSS V2
        ├── Ingestion queue ───── Parse → Chunk → Embed → Index → Publish revision
        ├── SQLite repository ─── Metadata, chunks, immutable revisions, jobs, audit
        └── builtin_rag Agent ─── Auto route → Revision pin → ACL → Hybrid RRF → Citation
                                      │
                                      ▼
                              Agent runtime events/output
```

目录职责：

- `storage/`：稳定 object key、默认凭据链、短期 PUT/GET 签名、本地等价适配；
- `ingestion/`：PDF、DOCX、Markdown、HTML、JSON、CSV、纯文本解析和结构化分块；
- `embedding.py`：确定性本地 Embedding 与 OpenAI-compatible 模型网关适配；
- `agent.py`：内置 RAG Agent，按请求类型和可靠证据自动选择 `knowledge` 或 `model_only`；
- `service.py`：知识库、文档、摄取任务、不可变版本、混合检索、ACL、审计和领域事件；
- `tool.py`：只从 `ResolvedExecutionPlan` 读取已锁定 Revision 的 `knowledge_search`；
- `models.py` / `ports.py`：API 输入模型和可替换基础设施接口。

## 文件与数据库的关系

数据库不会长期保存预签名 URL。每个文档版本保存：

- `storage_provider`、`bucket`、`region`；
- 不可变 `object_key` 和可选 OSS `object_version_id`；
- 稳定 `canonical_uri`，例如 `oss://jie-agent-file/rag/.../source.pdf`；
- ETag、SHA-256、Content-Type、大小和存储类型；
- Parser、Chunker、Embedding 版本及索引完成时间。

浏览器先计算强制 SHA-256，API 再生成有效期 15 分钟的 PUT URL。上传后浏览器调用 complete，服务端通过 HeadObject 校验对象大小、类型和可选 ETag，摄取时强制复核 SHA-256。下载时才生成短期 GET URL。AK/SK、STS Token 和签名 URL 都不会进入数据库、Execution Plan、Checkpoint 或事件。

Schema 20 将准确文件长度绑定到 OSS 签名，并加入上传意图期限、逻辑保留字节额度和摄取执行并发限制。complete 的 202 仅表示校验任务入队：完整文件只在 Worker 内下载、校验 SHA-256 并扫描，扫描失败不会解析/索引或获得下载权限。完整契约及未完成的物理对象回收见 [上传治理](../../docs/upload-governance.md)。

Object key 格式：

```text
rag/{environment}/{tenant}/{project}/{knowledge_base}/documents/{document_version}/source.{ext}
```

各层 ID 会被限制为安全字符，文件名只用于保留后缀，避免路径穿越和名称冲突。

## 摄取状态机

```text
PENDING_UPLOAD
    → UPLOADED
    → QUEUED
    → DOWNLOADING
    → SECURITY_SCAN
    → PARSING
    → EMBEDDING
    → INDEXING
    → COMPLETED / FAILED
```

每次成功摄取会发布一个 `KnowledgeBaseRevision`：

- Manifest 固定所有 `document_version_id`；
- 固定 Parser、Chunker、Embedding 模型/维度和 Retrieval Profile；
- 通过文档 SHA、Chunk 内容哈希、向量哈希和规范化配置生成 `index_hash`；
- 新版本激活时旧版本标为 `DEPRECATED`，但已发布 Agent 仍可使用旧 Revision；
- Agent 发布时只允许选择当前 `ACTIVE` Revision，并把完整检索快照锁入执行计划。

Worker 通过原子 compare-and-set 独占 `QUEUED` 任务；只有超过租期的 `RUNNING` 任务会在重启后恢复。Chunk 写入、版本 READY、Revision 发布和 Job 完成位于同一事务中。

PENDING_UPLOAD 未在期限内完成会变为 EXPIRED；它只释放待上传数量，不代表对象已删除或字节额度已释放。生产领取还通过数据库事务锁检查全局和租户/项目/用户的 RUNNING 上限。

## 检索与 Citation

检索流程：

1. 校验 Revision 的 Tenant / Project、Embedding 模型/维度、Retrieval Profile、Plan Binding 和索引哈希；
2. 通过 `visibility`、`created_by`、`allowed_roles` 做文档级 ACL；
3. 同时计算向量相似度和词法匹配；
4. 用 Reciprocal Rank Fusion 合并候选，每份文档限制最多 3 个 Chunk；
5. 返回 `citation_id`、Chunk、文档版本、结构定位、内容哈希、稳定 URI 和授权下载入口；
6. 审计只保存 query SHA-256、Revision、命中 Chunk 与耗时，不记录原始查询内容。

运行时仅在 Plan 同时绑定 Knowledge Revision、`knowledge_search` 工具且预算允许时启动内置 RAG Agent。创作、改写等模型原生请求直接选择 `model_only`；事实型请求进行证据探测，只有达到锁定 Profile 阈值时才选择 `knowledge`。HITL 恢复会重新执行相同的锁定检索并再次检查 ACL。

Reference 实现把向量保存在 SQLite JSON 中，适合本地开发和契约测试。数据量增长后，`EmbeddingProvider` 和 Repository 边界可替换为模型网关与 PostgreSQL/pgvector，而 API、Agent Plan 和 Citation 契约不变。

## API

```text
GET/POST /api/v1/knowledge-bases
GET       /api/v1/knowledge-bases/{id}
GET       /api/v1/knowledge-bases/{id}/documents
GET       /api/v1/knowledge-bases/{id}/revisions
POST      /api/v1/knowledge-bases/{id}/documents:prepare-upload
PUT       /api/v1/knowledge-document-versions/{id}/content   # local adapter only
POST      /api/v1/knowledge-document-versions/{id}:complete
GET       /api/v1/knowledge-ingestion-jobs/{id}
POST      /api/v1/knowledge-ingestion-jobs/{id}:retry
POST      /api/v1/knowledge:search
GET       /api/v1/knowledge-events
```

## 阿里云 OSS 配置

```dotenv
KNOWLEDGE_OBJECT_STORE=oss
ALIYUN_OSS_BUCKET=jie-agent-file
ALIYUN_OSS_REGION=cn-beijing
ALIYUN_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
ALIYUN_OSS_INTERNAL_ENDPOINT=https://oss-cn-beijing-internal.aliyuncs.com
ALIYUN_OSS_USE_INTERNAL_ENDPOINT=true
```

生产 Worker 推荐通过 ECS/ACK RAM Role 或 OIDC/STS 获得最小权限凭据。开发机可使用阿里云默认凭据链支持的环境凭据，但不应把密钥写入 `.env` 或仓库。

最小 OSS 权限范围应限制到 `jie-agent-file/rag/*`，包含 PutObject、GetObject 和 HeadObject；Bucket 保持私有并配置 Web 控制台来源所需的 CORS `PUT/GET/HEAD` 与 `Content-Type/ETag` 暴露。

## 验证

```bash
.venv/bin/python -m pytest -q tests/test_knowledge.py
cd apps/web && npm run build
```

测试覆盖本地 OSS 等价上传、完整性校验、异步摄取、混合检索、Citation、下载、Tenant/Project/Role 隔离、Agent Revision Pin、Runtime Tool Event 和 OSS SDK 初始化。
