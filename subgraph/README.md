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

This version uses `graph.stream_events(input, version="v3")` and consumes the
dedicated `stream.subgraphs` projection. Each discovered subgraph exposes its
own message stream.

The old `uv run python -m subgraph.main` command remains available and runs the
v2 example.
