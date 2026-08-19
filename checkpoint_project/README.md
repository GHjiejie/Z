# LangGraph SQLite Checkpoint 综合终端

这是一个将仓库内 checkpoint demos 组合成可交互项目的版本。它提供持久化多轮
会话、文件工具人工审批、checkpoint 历史/分支、失败恢复，并把 graph checkpoint
和会话目录都保存在本地 SQLite 中。

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
├── cli.py                      # 终端交互、会话/历史/审批命令
├── graph.py                    # StateGraph、interrupt、SQLite checkpointer、fork
├── file_tools.py               # 受工作区约束的读/写/删/列目录工具
├── session_store.py            # SQLite 会话目录与分支来源
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

## 启动

在仓库根目录执行：

```bash
cp checkpoint_project/.env.example .env
# 编辑 .env，至少填入 OPENAI_API_KEY
uv sync
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
uv run ruff check checkpoint_project
```

测试覆盖：多轮 memory 与重启恢复、写入批准、删除拒绝、从旧 checkpoint 派生
独立会话，以及节点失败后从最后成功 checkpoint 重试。

## 需要注意的边界

- SQLite 方案适合本地单机 demo。多进程高并发或生产部署应换成 Postgres 等后端。
- 人工批准后，文件删除是真实且不可撤销的；终端会显示精确相对路径，默认答案为
  拒绝。重要文件仍应使用版本控制或额外备份。
- checkpoint 会长期保存完整消息和工具结果；生产环境应增加保留期限、敏感信息
  脱敏、数据库加密和会话访问控制。
