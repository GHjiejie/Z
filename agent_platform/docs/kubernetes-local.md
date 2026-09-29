# OrbStack Kubernetes 本地部署

本机已从 Python supervisor 与 Docker Compose 切换为 `orbstack` 集群管理，命名空间为 `agent-platform`。平台入口继续为 <http://127.0.0.1:8010>，LiteLLM 管理页为 <http://127.0.0.1:4000/ui>。部署脚本拒绝操作其他 context。

## 工作负载与数据

| 资源 | 用途 | 数据/配置 |
| --- | --- | --- |
| Deployment/api | HTTP、静态前端、SSE | platform-data、tenant-tombstones |
| Deployment/worker | 运行调度与模型调用 | 同上，运行凭据 |
| Deployment/gateway-sync | 网关开通、轮换、撤销 | 同上，另有独立控制 Secret |
| Deployment/maintenance | 导出与组织关闭 | 同上 |
| Deployment/litellm | 模型网关及管理页面 | 专用 Secret、ConfigMap、PostgreSQL |
| StatefulSet/postgres | LiteLLM 数据库 | data-postgres-0 |
| StatefulSet/redis | 速率与网关缓存 | data-redis-0 |
| Job/schema-* | 发布前 Alembic 迁移 | 运行完成保留 24 小时 |

四个 PVC 分别保存平台数据库及导出、独立删除记录、LiteLLM PostgreSQL、Redis。绑定后 PV 回收策略设为 `Retain`，StatefulSet 删除/缩容策略也为 Retain。日常停止不删除 namespace、PVC、Secret。

这份清单是**单节点本地配置**：平台数据库仍为 SQLite，各应用角色一个副本，更新策略为 Recreate。PostgreSQL 当前保存网关数据。不能仅增加 replicas 就将它变成可横向扩展的 SaaS；先按多租户运行手册迁入平台 PostgreSQL、启用运行角色/RLS、HTTPS，再设计共享导出存储与多节点部署。

API 的 readiness 检查数据库版本、Redis 和后台心跳；三个后台进程用各自心跳作为 readiness。PostgreSQL 检查实际业务库，Redis 检查 PING，LiteLLM 检查进程存活。容器退出后由控制器重建。集群工作负载不挂载 Kubernetes API 令牌；应用以 UID 10001 运行。

单副本更新或重建期间会短暂断连；Pod 就绪后，宿主机 Service 转发表还需短暂同步。本地配置不承诺无中断发布。

## 常用命令

在仓库根目录运行：

```sh
make -C agent_platform start                # 构建并部署；也可 make up
make -C agent_platform start KUBE_ARGS=--skip-build
make -C agent_platform status
make -C agent_platform k8s-logs
make -C agent_platform stop                 # 等待运行收尾，缩容为 0，保留数据
kubectl --context orbstack -n agent-platform get pods,pvc
kubectl --context orbstack -n agent-platform logs deployment/worker --tail=100
```

更新先构建镜像，再停止 API 准入，等待现有 Worker 的 queued/running/cancelling 运行结束。超时会恢复原 API 入口并停止更新；不会为了更新直接删除运行记录。之后停止应用、应用配置、执行迁移 Job、启动 LiteLLM 和四种应用角色。数据库保持其持久数据，迁移 Job 失败时应用保持停止以便处理。

`KUBE_CONTEXT` 默认 `orbstack`，`KUBE_NAMESPACE` 默认 `agent-platform`，`KUBE_IMAGE` 默认 `agent-platform:0.2.0-k8s`。本地镜像使用 `IfNotPresent`；部署前构建，缩容后创建新 Pod，避免沿用旧 Pod。OrbStack 的 Docker 与 Kubernetes 共用镜像引擎，不需要额外镜像仓库。

端口定义在 `deploy/kubernetes/stack.yaml` 的 LoadBalancer Service 中；`PORT` 仅影响旧 `local-start`。当前 OrbStack 可直接从 Mac 访问这两个服务，不依赖终端中的长期 `kubectl port-forward` 进程。

## 首次迁移已有本地实例

```sh
make -C agent_platform k8s-import
```

此命令只支持本项目默认路径的 SQLite 和现有 managed 网关，不支持覆盖已有目标 PVC。当前本机已经完成导入，不要重复执行。

迁移步骤：检查活动运行 → 构建镜像 → 停旧 supervisor/Compose → 备份 SQLite、导出、删除记录、Redis → 用只读 `pg_dump` 导出旧 LiteLLM 库 → 创建隔离的 Kubernetes Secret/PVC → 临时导入 Pod 装载文件 → 核对 SQLite SHA-256、完整性、外键和所有表行数 → 将目标模型部署地址改为 `http://litellm:4000/v1` 并递增配置版本 → 单事务恢复网关 PostgreSQL → 核对所有网关表行数 → 删除导入 Pod → 迁移 schema 并启动服务。

迁移保留用户、角色、组织、Agent、对话、钱包、账本、模型授权和凭据代际。历史运行快照不会被重新改写。Redis AOF 从停止后的旧卷复制。旧 Docker 卷和本机 SQLite 留作停写备份；新服务使用 PVC，不再写旧文件。

私有备份目录为 `.data/kubernetes/backup-<UTC 时间>/`，包含 `manifest.json`、`platform.db`、`litellm.dump`、Redis 文件和卷归档；文件内容不提交 Git。平台数据与网关数据库都校验完成后，`platform-release` ConfigMap 才记录 imported 状态。若后续启动失败，可修复后执行 `make start KUBE_ARGS=--skip-build`；导入完成之前失败则先检查私有备份及导入 Pod，不允许重新导入覆盖 PVC。

## 配置与凭据

`platform-config` 保存不敏感的应用配置。四个 Secret 分别保存：

- `platform-runtime-secret`：持久加密 Key 和原受限运行 Key。
- `gateway-control-secret`：网关控制 URL 和 master key，仅同步器使用。
- `litellm-secret`：上游模型、上游 Key、管理页面凭据、网关秘密及其数据库 URL。
- `postgres-secret`：数据库初始化凭据。

首次迁移从本机忽略的配置文件读取，使用 stdin 提交 Secret，不把秘密放在 YAML、命令参数或镜像中。后续发布使用集群现有 Secret，不从旧 Compose 配置重新生成或覆盖它们。更改数据库密码需要先在 PostgreSQL 执行对应轮换，再更新关联 Secret 与 Pod；仅改 `.env` 不会轮换现存数据库密码。

镜像构建排除 `.env`、`.data`、数据库、日志及依赖缓存。根 `.dockerignore` 同时保护旧版 Docker builder；Dockerfile 专用忽略文件进一步限制构建上下文。

## 备份与恢复边界

迁移前备份只覆盖切换时刻。投入新写入后，回退必须停止 Kubernetes，重新备份 PVC 中的 SQLite/导出、独立删除记录、当前网关 PostgreSQL/Redis，并核对外部网关状态；不能直接启动旧 SQLite 与旧 Docker 卷。恢复为旧 supervisor 时还需将活动部署地址改回旧网关地址，保留历史快照和财务事实。

旧命令改名为 `local-start/local-stop/local-status`。迁移完成后 `local-start` 默认拒绝启动旧数据；只有完成上述恢复之后，才可调用 `python -m agent_platform.scripts.local start --after-kubernetes-restore ...`。这是恢复标记，不会执行任何数据恢复。

删除 namespace/PVC 会移除绑定元数据和 Secret，即使 PV 为 Retain 也不等于可自动恢复。维护集群时保留全部 PVC、Secret、备份与加密 Key，禁止将删除命名空间当成重启方式。OrbStack 集群重置需要另行备份卷内容；本地 PV 不提供异机灾备。

## 本次部署记录

- 平台迁移版本：`0007_support_access`；9 个 Agent、3 个会话、4 个历史运行保留。
- SQLite 摘要/完整性/外键/行数及 LiteLLM 所有表行数校验通过。
- 五个 Deployment、两个 StatefulSet 均已就绪，四个 PVC 均为 Retain。
- 平台 `/api/v1/ready` 返回 ready；LiteLLM 管理页返回 200。
- 5 项部署边界测试通过；完整 `make start` 更新成功；API Pod 删除后由 Deployment 自动重建，入口恢复且原有数据行数保持一致。
- 此次未发送付费模型请求；SaaS 的 I5 验收仍见原多租户手册。

依据：[OrbStack Kubernetes 文档](https://docs.orbstack.dev/kubernetes/)；清单入口：`deploy/kubernetes/stack.yaml`，执行入口：`scripts/kubernetes.py`。
