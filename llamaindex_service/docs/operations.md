# 部署与维护

## 环境和数据库角色

开发使用 README 中的 PostgreSQL + 本机 API/Worker。`deploy/compose.yaml` 的 `service` profile 可以容器化 API/Worker，容器网络来源不能使用仅限 loopback 的开发身份，因此该 profile 强制 JWT。先配置根目录 `.env` 的 JWT 参数并显式迁移，再运行 `docker compose -f llamaindex_service/deploy/compose.yaml --profile service up --build -d`。

生产设置 `RAG_ENVIRONMENT=production`、`RAG_AUTH_MODE=jwt`、`RAG_MODEL_MODE=live`、`RAG_STORAGE_BACKEND=s3`，禁用自动迁移。配置使用管理员允许的服务端模型/存储地址；用户输入不能覆盖端点。

使用三个独立数据库账号：迁移 owner、API LOGIN 账号（仅继承 `rag_api`）、Worker LOGIN 账号（仅继承 `rag_worker`）。迁移创建 NOLOGIN 权限组；数据库管理员自行创建 LOGIN 账号并通过受控密钥渠道配置密码，不能用共享超级账号运行生产服务。API 角色不能继承 Worker 权限；生产启动会拒绝 owner、superuser、BYPASSRLS 及角色错配。

API 与 Worker 进程分别设置自己的 `RAG_DATABASE_URL`。Compose 可用 `RAG_API_DATABASE_URL` 和 `RAG_WORKER_DATABASE_URL` 分别覆盖；默认 `rag` 账号仅用于本地集成。RLS 使用事务局部的 tenant/project，上下文不会残留到下一个连接池请求；Worker 权限跨租户用于处理队列，不能暴露为 HTTP 入口。

```bash
# 以迁移账号连接；命令不会加载模型或调用外部服务
uv run --group rag python -m llamaindex_service.migrations

# 以受控管理身份预置 JWT subject 对应的可信成员关系
uv run --group rag python -m llamaindex_service.admin add-member \
  --tenant tenant-a --project project-a --user employee-1 --role employee
```

JWT 验证签名、算法、issuer、audience、exp、iat 与 subject/scope 字段；roles 从数据库读取，不相信 token 自报角色。`RAG_JWT_PUBLIC_KEY` 可直接保存公钥文本或使用 `@/absolute/path/public-key.pem`。本地开发模式必须绑定 `127.0.0.1` 并关闭代理头信任。

## 模型配置与索引重建

聊天生成使用 `chat_models.chat.chat_model`，Embedding 使用单独的 OpenAI-compatible `/embeddings` 端点。当前一个服务部署对应一套默认 Embedding 配置；模型名、修订、维度、mock/live 必须与存量向量一致。mock 模式自动给模型身份添加 `mock:` 前缀，真实模式拒绝该模型标识，因此相同维度也不会静默混用测试向量。返回维度、数量、索引或数值错误时失败。

知识库 Owner 可以创建影子索引代次：

```text
POST /api/v1/knowledge-bases/{kb_id}/index-generations
{"embedding_model":"target-model","embedding_revision":"2","embedding_dimension":1024}

GET /api/v1/knowledge-bases/{kb_id}/index-generations/{generation_id}
POST /api/v1/knowledge-bases/{kb_id}/index-generations/{generation_id}/activate
POST /api/v1/knowledge-bases/{kb_id}/index-generations/{generation_id}/abort
```

创建时要求现有摄取任务已完成；重建期间冻结该库上传/重试，旧索引继续提供查询。启动使用目标模型配置的 Worker，它只领取匹配模型/维度的任务；重建片段写入影子代次。检查全部任务完成后，在维护窗口暂停问答流量，激活新代次，更新 API/普通 Worker 的模型配置并重启，健康检查与样例查询通过后恢复流量。同一部署下所有需要查询的知识库必须使用与 API 匹配的模型配置；本版没有每库动态模型路由。

失败时调用 abort 取消影子任务并恢复上传，旧索引不变。已激活的代次不能用 abort 回退；需要以旧模型重新构建并按相同步骤切换。不能仅修改环境变量而不处理已有向量。mock/live 切换也需要重新索引，推荐新建知识库避免混淆测试数据。

## 文件与维护

上传默认 20 MiB。PDF 按物理页计数，默认最多 500 页；DOCX 只可检查显式分页符，不能推断渲染排版页数，因此同时执行解压条目/大小/压缩比、提取字符数、CPU/内存与时间上限。解析进程屏蔽凭据、网络、子进程及写入；它不是完整的操作系统沙箱，生产应进一步使用受限容器和网络策略。

原文对象使用不可变 key。LocalStorage 仅用于开发共享目录；生产 S3 使用默认凭据链/IAM 角色及私有 bucket。无需把凭据写入数据库或代码。版本化 bucket 中 DeleteObject 会产生删除标记，非当前版本仍受 bucket 保留策略管理；需要非版本化主存储或明确配置非当前版本的到期策略，任务完成不代表备份/旧对象版本即时物理擦除。[AWS DeleteObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html)

Worker 每小时执行维护，默认会话保留 30 天；每轮最多扫描 500 个对象、回收 100 个。孤立对象至少保留 24 小时，通过数据库对象锁与引用复核回收；上传从对象创建到落库超过一小时拒绝，避免迟到引用与回收竞争。

```bash
uv run --group rag python -m llamaindex_service.workers --maintenance-only
```

任务失败会保留脱敏错误码，网络/429/5xx 有限重试；格式/维度错误需修正配置或文件后重试。Worker 异常退出后由租约恢复，过期领取者不得提交。模型外部调用可能重复计费，持久幂等不等于外部服务 exactly-once。

## 备份、恢复与监控

数据库与原文分别备份，备份中含企业数据，应采用与主存储一致的访问控制。数据库包含专用 Alembic 版本表、文档元数据、活动版本、索引代次、片段、任务、会话及审计。恢复到隔离环境后验证：schema 兼容、原文对象可读且哈希一致、活动代次/维度匹配、未完成任务可被恢复、权限和引用样例通过。向量可依据原文重建，不能仅保存向量数据库而丢失原文。

健康接口为 `/health/live` 和 `/health/ready`。请求日志只记录 request_id、路由模板、状态码与耗时，不记录正文。问答结果保存检索/生成/首 token 耗时及可用 usage；任务有阶段、完成数量和租约状态；审计记录资源操作及检索数量。当前没有独立 Prometheus exporter 或告警平台。

部署停止/升级前先停止接受新任务，等待在途任务结束或租约过期。模型切换使用维护窗口，不把配置错配错误作为普通无答案结果。
