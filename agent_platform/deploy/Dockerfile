FROM node:24-alpine AS web
WORKDIR /build
COPY agent_platform/apps/web/package*.json ./
RUN npm ci
COPY agent_platform/apps/web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY chat_models/ ./chat_models/
COPY agent_platform/ ./agent_platform/
COPY --from=web /build/dist ./agent_platform/apps/web/dist
RUN useradd --create-home --uid 10001 platform && \
    mkdir -p /app/agent_platform/.data && chown -R platform:platform /app/agent_platform/.data
USER platform
EXPOSE 8000
CMD ["python", "-m", "agent_platform", "serve", "--host", "0.0.0.0", "--port", "8000"]
