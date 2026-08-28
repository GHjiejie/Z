# LangGraph streaming protocol v2 and v3 examples

These examples run the same graph with two different LangGraph streaming
protocols. Both entry points print the graph workflow in the terminal before
processing user input:

```text
+-----------+
| __start__ |
+-----------+
      *
+----------+
| llm_call |
+----------+
      *
 +---------+
 | __end__ |
 +---------+
```

The graph calls the real chat model configured in `chat_models/chat.py`.

## Configuration

Add the following variables to `.env` in the project root:

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
MODEL=...
```

## Run version v2

```bash
uv run python -m interleave.v2
```

The v2 example asynchronously consumes callback-style `on_*` events with
`astream_events()` and prints graph, node, and chat-model lifecycle events in
their actual execution order.

To run it once without entering interactive mode:

```bash
uv run python -m interleave.v2 "Explain LangGraph streaming in one sentence."
```

## Run version v3

```bash
uv run python -m interleave.v3
```

The v3 example subscribes to the `messages` and `values` projections and uses
`interleave` to consume both projections in their actual arrival order:

```python
EVENT_CONSUMERS = {
    "messages": message_event_consumer,
    "values": values_event_consumer,
}

with graph.stream_events(input, version="v3") as stream:
    for event_type, event in stream.interleave(*EVENT_CONSUMERS):
        EVENT_CONSUMERS[event_type](event)
```

To run it once without entering interactive mode:

```bash
uv run python -m interleave.v3 "Explain LangGraph streaming in one sentence."
```

The original `python -m interleave.main` entry point is still available and
runs the v3 example.
