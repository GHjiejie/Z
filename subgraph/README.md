# LangGraph subgraph streaming demos

Both examples run the same parent graph and reusable research subgraph with the
real chat model configured in `chat_models/chat.py`.

## Workflows

```text
Parent: START -> normalize_topic -> research_subgraph -> END
                                      |
Child:                         research_topic
                                      |
                              write_learning_note
                                      |
                                     END
```

The programs print ASCII diagrams for both workflows in the terminal before
execution. `topic` and `learning_note` are shared state fields, while
`research_result` is private to the subgraph.

## Version 2: `graph.stream`

```bash
uv run python -m subgraph.v2 "LangGraph subgraphs"
```

This version uses:

```python
graph.stream(
    input,
    stream_mode=["updates", "messages", "values"],
    subgraphs=True,
    version="v2",
)
```

## Version 3: `graph.stream_events`

```bash
uv run python -m subgraph.v3 "LangGraph subgraphs"
```

This version uses `graph.stream_events(input, version="v3")` and calls
`interleave("lifecycle", "subgraphs", "messages", "values")` on the parent and
each discovered subgraph. Events are consumed in their actual arrival order.

`run(topic, feedback=handler)` also provides frontend-ready feedback events:

- `workflow`: the workflow or a nested stream was attached;
- `lifecycle`: a subgraph started, completed, or failed;
- `activity`: an agent node started or completed;
- `output`: one streamed model-output delta;
- `state`: graph state became available;
- `result`: the final user-facing output.

The default handler renders live terminal feedback. A server can pass an SSE or
WebSocket sender as `feedback` and forward the same serializable dictionaries to
the frontend.

The old `uv run python -m subgraph.main` command remains available and runs the
v2 example.
