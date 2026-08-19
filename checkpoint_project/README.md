# Checkpoint Studio：LangGraph + FastAPI + React

这是一个将仓库内 checkpoint demos 组合成完整应用的版本。它同时提供 React Web
界面和交互终端，支持持久化多轮会话、文件工具人工审批、checkpoint 历史/分支、
失败恢复，并把 graph checkpoint 和会话目录都保存在本地 SQLite 中。

## 已实现能力

- **Memory**：`MessagesState` 使用 `add_messages` reducer；同一 `thread_id` 的历史
  会在后续调用中自动恢复，程序退出后也不会丢失。
- **Human-in-the-loop**：`write_file`、`delete_file` 在产生副作用之前调用
  `interrupt()`。审批本身也是 checkpoint 状态，退出终端后重新启动仍会再次显示。
- **Time travel / fork**：`/history` 可看到任意 checkpoint；`/fork` 把所选时点的
  state 复制到新的 `thread_id`。源会话和新会话从此独立演进。
- **Fault tolerance**：节点异常时，SQLite 保留最后一个成功的 super-step；修复
  外部原因后可用 `/retry` 从该处继续，不需要重跑整段对话。
- **Pending-write 友好**：敏感工具节点会先收集全部审批，再执行工具；模型也被
  配置为一次只生成一个工具调用，避免恢复审批时重复部分文件副作用。
- **本地文件安全边界**：所有文件路径都限制在 `--workspace` 下；读取/列目录无需
  审批，写入、覆盖和删除单个文件必须审批，目录删除被禁止。

## 结构

```text
checkpoint_project/
├── api.py                      # FastAPI 会话、消息、审批和分支接口
├── cli.py                      # 终端交互、会话/历史/审批命令
├── graph.py                    # StateGraph、interrupt、SQLite checkpointer、fork
├── model.py                    # 终端和 API 共用的模型配置
├── file_tools.py               # 受工作区约束的读/写/删/列目录工具
├── session_store.py            # SQLite 会话目录与分支来源
├── frontend/                   # React + TypeScript + Vite 单页应用
├── test_api.py                 # FastAPI 离线 HTTP 集成测试
├── test_checkpoint_project.py  # 不访问网络的集成测试
└── .env.example
```

运行数据默认写入：

```text
checkpoint_project/data/checkpoints.sqlite
checkpoint_project/workspace/
```

SQLite 内既有 LangGraph 自动建立的 checkpoint/write 表，也有项目建立的
`chat_sessions` 表。后者只保存会话 ID 和分支来源，实际 memory 仍由 LangGraph
checkpoint 管理。

## 启动 Web 应用

在仓库根目录执行：

```bash
cp checkpoint_project/.env.example .env
# 编辑 .env，至少填入 OPENAI_API_KEY
uv sync
cd checkpoint_project/frontend
npm install
npm run build
cd ../..
uv run uvicorn checkpoint_project.api:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。FastAPI 会在启动时检测 `frontend/dist` 并以同一
端口托管构建后的 React 应用，同时在 <http://127.0.0.1:8000/docs> 提供 OpenAPI
交互文档。

前端开发模式可以分别启动后端和 Vite；Vite 会把 `/api` 代理到 8000 端口：

```bash
# 终端 1：后端
uv run uvicorn checkpoint_project.api:app --reload

# 终端 2：前端热更新
cd checkpoint_project/frontend
npm run dev
```

访问 <http://127.0.0.1:5173>。

可选运行变量：

```text
CHECKPOINT_DB=/path/to/checkpoints.sqlite
CHECKPOINT_WORKSPACE=/path/to/workspace
```

Web 界面包括会话侧栏、消息与工具结果、人工审批卡、失败恢复入口、checkpoint
时间线和 fork 弹窗，并为桌面和移动端提供响应式布局。

## FastAPI 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/health` | 检查服务和本地存储路径 |
| `GET/POST` | `/api/sessions` | 列出或创建会话 |
| `GET` | `/api/sessions/{id}` | 获取完整会话状态和待审批项 |
| `POST` | `/api/sessions/{id}/messages` | 发送消息并执行 graph |
| `POST` | `/api/sessions/{id}/approval` | 批准或拒绝 pending 文件操作 |
| `GET` | `/api/sessions/{id}/checkpoints` | 获取 checkpoint 时间线 |
| `POST` | `/api/sessions/{id}/fork` | 从指定 checkpoint 创建分支 |
| `POST` | `/api/sessions/{id}/retry` | 从失败节点恢复 |

## 启动终端版本

在仓库根目录执行：

```bash
uv run python -m checkpoint_project
```

也可以指定存储位置和启动会话：

```bash
uv run python -m checkpoint_project \
  --db ./local/checkpoints.sqlite \
  --workspace ./local/workspace \
  --thread alice
```

项目兼容 `OPENAI_BASE_URL` 指向的 OpenAI-compatible 服务，并从 `MODEL` 选择模型；
所选模型必须支持 tool calling。

## 终端操作示例

```text
[main] 你> 请把“hello checkpoint”写入 hello.txt

--- 需要人工确认 ---
操作: write_file
路径: hello.txt
字符数: 16  覆盖: False
内容预览:
hello checkpoint
批准此操作？[y/N] y
助手> 文件已写入。

[main] 你> /history
# 0 是最新 checkpoint；数字越大越早

[main] 你> /fork 3 alternative
已从 main@... 派生并切换到: alternative

[alternative] 你> 基于刚才的上下文，改走另一种方案
```

常用命令：

```text
/history
/fork <历史序号|checkpoint_id> [新会话ID]
/new [会话ID]
/switch <会话ID>
/sessions
/state
/graph
/retry
/help
/quit
```

`/history` 按“最新在前”显示，所以序号 `0` 代表当前最新 checkpoint。分支复制
消息 memory 和普通 state，但有意不复制旧时点的 pending task/interrupt；若该时点
恰好停在工具审批处，新分支会为待执行 tool call 写入一条“已取消、未执行”的
`ToolMessage`，保证消息协议完整且不会产生副作用。新分支随后可立即接收用户消息。
如果希望处理原会话的 pending 审批，应切回源会话。

## 验证

测试完全使用本地脚本模型，不消耗 API：

```bash
uv run python -m unittest -v checkpoint_project.test_checkpoint_project
uv run python -m unittest -v checkpoint_project.test_api
uv run ruff check checkpoint_project
cd checkpoint_project/frontend && npm run build
```

测试覆盖：多轮 memory 与重启恢复、写入批准、删除拒绝、从旧 checkpoint 派生
独立会话、节点失败后从最后成功 checkpoint 重试，以及完整 HTTP
“会话 → 消息 → 审批 → checkpoint → fork”流程。

## 需要注意的边界

- SQLite 方案适合本地单机 demo。多进程高并发或生产部署应换成 Postgres 等后端。
- 人工批准后，文件删除是真实且不可撤销的；终端会显示精确相对路径，默认答案为
  拒绝。重要文件仍应使用版本控制或额外备份。
- checkpoint 会长期保存完整消息和工具结果；生产环境应增加保留期限、敏感信息
  脱敏、数据库加密和会话访问控制。
