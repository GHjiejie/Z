# LangGraph `interleave` 经典 demo

`interleave` 用于把多个事件投影合并到同一个循环中，并严格保留事件的真实到达顺序。
这个示例调用项目在 `chat_models/chat.py` 中配置的真实模型，并同时消费：

- `values`：每一步执行后的完整图状态；
- `messages`：聊天模型产生的消息流。

运行前需要在项目根目录的 `.env` 中配置：

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
MODEL=...
```

## 运行

在项目根目录进入 `interleave` 包对应的 demo：

```bash
uv run python -m interleave.main
```

输入问题后回车，输入 `exit` 退出。也可以直接执行一次：

```bash
uv run python -m interleave.main "用一句话解释 LangGraph interleave"
```

核心代码是：

```python
with graph.stream_events(input, version="v3") as stream:
    for projection, item in stream.interleave("messages", "values"):
        if projection == "messages":
            for text in item.text:
                print(text, end="", flush=True)
        elif projection == "values":
            print("状态更新", item)
```

与 `event_stream_v3/main.py` 中依次调用 `message_comsumer(stream)`、
`values_comsumer(stream)` 不同，`interleave` 会预先订阅两个投影，避免第一个消费者
排空底层事件流后，第二个消费者收不到内容。

当前项目使用的 LangGraph 版本可能提示 v3 streaming protocol 仍为 experimental，
这不影响示例运行。
