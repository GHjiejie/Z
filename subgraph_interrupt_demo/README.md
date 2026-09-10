# 三子图 Interrupt 人工审核 Demo

该示例让内容、合规和发布三个一等子图分别调用项目配置的真实 `chat_model`，并发生成
不同职责的结构化审核方案。每个子图随后在内部调用
`interrupt()` 暂停；CLI 收集三个人工决定后，使用 interrupt ID 到决定的映射一次恢复
所有分支。父图显式等待三个子图结束，再执行一次汇总。

## 运行

```bash
uv run python -m subgraph_interrupt_demo.main --thread-id review-001
```

也可以提供实际审核内容：

```bash
uv run python -m subgraph_interrupt_demo.main \
  --thread-id review-002 \
  --topic "新功能发布" \
  --content "我们将在下周发布新的数据导出功能。" \
  --channel "官网公告" \
  --scheduled-at "2026-09-15 10:00"
```

三个模型调用读取根目录 `.env` 中的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL`。
模型必须为每个子图返回经过 `GeneratedProposal` 校验的结构化结果，不存在写死方案或
静态 fallback。

程序会依次展示三个待审核项目，可输入：

- `approve`：采用原方案；
- `edit`：输入替换值后采用人工修改；
- `reject`：输入拒绝原因并阻止最终发布。

如果程序在暂停后退出，使用相同命令和相同 `--thread-id` 即可重新显示原来的三个
interrupt 并继续。完成后的 thread ID 不会自动开始新审核；新审核应使用新 ID。

SQLite checkpoint 默认写入 `subgraph_interrupt_demo/checkpoints.sqlite`，该文件已被仓库
的全局 `*.sqlite` 规则忽略。也可以通过 `--database PATH` 指定位置。

## 测试

```bash
uv run python -m unittest subgraph_interrupt_demo.test_demo -v
```

单元测试通过依赖注入替换模型调用，保持离线和确定性；默认运行路径仍调用真实模型。
测试覆盖并发、三个唯一 interrupt、按 ID 恢复、审批/编辑/拒绝、显式 join、状态隔离，
以及关闭并重新打开 SQLite 连接后的跨进程式恢复。
