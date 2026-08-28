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

## Frontend SSE service

`interleave.api` consumes every frontend-relevant projection exposed by the
graph (`messages`, `values`, `lifecycle`, `tools`, and `subgraphs`) and converts
it into a normalized event stream. Start the API with:

```bash
uv run uvicorn interleave.api:create_api --factory --reload
```

The endpoints are:

```text
GET  /api/health
POST /api/chat/stream
```

Send a chat request as JSON:

```json
{"message": "Please introduce this project."}
```

The SSE response can contain the following events in their actual arrival
order:

| Event | Frontend use |
| --- | --- |
| `run.started` | Create a new execution timeline |
| `reasoning.delta` | Append Thought/reasoning text |
| `text.delta` | Append final-answer tokens |
| `tool_call.delta` | Show the tool call while the model constructs it |
| `tool.started` / `tool.completed` | Show Read, Write, and other tool steps |
| `state.updated` | Update the current graph state |
| `run.completed` / `run.failed` | Finish the timeline or show an error |

Because the endpoint is a `POST`, consume it with streaming `fetch()` rather
than the browser's GET-only `EventSource` API:

```javascript
const response = await fetch("http://localhost:8000/api/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ message: userInput }),
});

const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += value;

  const frames = buffer.split("\n\n");
  buffer = frames.pop() ?? "";
  for (const frame of frames) {
    const data = frame
      .split("\n")
      .find((line) => line.startsWith("data: "));
    if (data) renderEvent(JSON.parse(data.slice(6)));
  }
}
```

Every event contains a `run_id`, a strictly increasing `sequence`, and a
`timestamp`. The frontend can group events by `run_id`, order them by
`sequence`, and calculate Thought duration from the timestamps.

## React timeline UI

The React 19 + Vite application lives in `interleave/frontend`. It renders the
SSE protocol as a dark execution timeline with expandable Thought blocks, tool
steps, streamed Markdown answers, run status, state snapshot counts, and a raw
event inspector.

For local development, run the backend and frontend in separate terminals:

```bash
uv run uvicorn interleave.api:create_api --factory --reload
```

```bash
cd interleave/frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` requests to the backend on
port `8000`.

For a single production-style service, build the frontend first and then start
FastAPI:

```bash
cd interleave/frontend
npm run build
cd ../..
uv run uvicorn interleave.api:create_api --factory
```

FastAPI detects `interleave/frontend/dist` and serves the built React app from
`http://localhost:8000` while preserving the `/api/*` routes.
