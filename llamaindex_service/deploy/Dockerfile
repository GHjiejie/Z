FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca
COPY --from=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --group rag --no-dev
COPY chat_models ./chat_models
COPY llamaindex_service ./llamaindex_service
RUN useradd --create-home --uid 10001 rag && mkdir -p /app/llamaindex_service/data/objects && chown -R rag:rag /app/llamaindex_service/data
USER rag
EXPOSE 8090
CMD ["uv", "run", "--no-sync", "uvicorn", "llamaindex_service.app:app", "--host", "0.0.0.0", "--port", "8090", "--no-proxy-headers"]
