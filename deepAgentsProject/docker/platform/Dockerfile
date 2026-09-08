# Build context must be deepAgentsProject, never its parent workspace.
# Base index digests verified against Docker Hub on 2026-08-31.
FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca AS python-base

FROM python-base AS wheels
WORKDIR /build
COPY requirements.prod.lock requirements.txt requirements.build.lock ./
COPY docker/platform/wheelhouse.py ./
RUN python -m pip install --require-hashes --only-binary=:all: -r requirements.build.lock
# Only this audited dependency is source-only. Build tools are already locked;
# no dynamic build isolation or unpinned build dependencies may be downloaded.
RUN python -m pip wheel --no-build-isolation --require-hashes --only-binary=:all: --no-binary=alibabacloud-tea --wheel-dir /wheels -r requirements.prod.lock \
    && python wheelhouse.py record /wheels \
    && python -m pip wheel --no-build-isolation --require-hashes --only-binary=:all: --no-binary=alibabacloud-tea --wheel-dir /test-wheels -r requirements.txt \
    && python wheelhouse.py record /test-wheels

FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend
WORKDIR /build
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/web/ ./
RUN npm test && npm run build

FROM python-base AS app-runtime
COPY docker/platform/debian.sources /etc/apt/sources.list.d/debian.sources
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 deepagent \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/deepagent --shell /usr/sbin/nologin deepagent \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/deepagent \
    && python -m venv /opt/venv
COPY requirements.prod.lock /opt/release/requirements.prod.lock
COPY docker/platform/wheelhouse.py /opt/release/wheelhouse.py
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python /opt/release/wheelhouse.py verify /wheels \
    && /opt/venv/bin/python -m pip install --no-index --no-deps /wheels/*.whl \
    && cp /wheels/wheelhouse.json /opt/release/production-wheelhouse.json \
    && /opt/venv/bin/python -m pip check
WORKDIR /app
COPY --chown=0:0 packages/ packages/
COPY --chown=0:0 builtin_plugins/ builtin_plugins/
COPY --chown=0:0 apps/platform_api/ apps/platform_api/
COPY --chown=0:0 apps/platform_worker/ apps/platform_worker/
COPY --chown=0:0 apps/sandbox_service/ apps/sandbox_service/
COPY --chown=0:0 apps/production_entrypoint.py apps/production_entrypoint.py
COPY --chown=0:0 apps/logging.json apps/logging.json
COPY --chown=0:0 pyproject.toml uv.lock ./
ARG SOURCE_REVISION=uncommitted
LABEL org.opencontainers.image.title="DeepAgent production runtime" \
      org.opencontainers.image.revision=$SOURCE_REVISION
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DEEPAGENT_ENVIRONMENT=production DEEPAGENT_AUTO_MIGRATE=false \
    DEEPAGENT_ALLOW_DEMO_IDENTITY=false DEEPAGENT_SESSION_COOKIE_SECURE=true \
    DEEPAGENT_CSRF_ENABLED=true DEEPAGENT_DATA_DIR=/var/lib/deepagent \
    DEEPAGENT_SANDBOX_PROVIDER=remote
USER 10001:10001
ENTRYPOINT ["/opt/venv/bin/python", "-m", "apps.production_entrypoint"]

FROM app-runtime AS api
COPY --from=frontend --chown=0:0 /build/dist/ /app/apps/web/dist/
EXPOSE 8000
CMD ["api"]

FROM app-runtime AS worker
CMD ["worker"]

FROM app-runtime AS migrate
CMD ["migrate"]

FROM app-runtime AS sandbox-service
EXPOSE 8443
CMD ["sandbox-service"]

# Test-only image. Never publish it as an API or Worker release artifact.
# All application versions are the same locked versions as app-runtime.
FROM app-runtime AS acceptance
USER 0:0
COPY requirements.txt /opt/release/requirements.test.lock
RUN --mount=type=bind,from=wheels,source=/test-wheels,target=/wheels \
    python /opt/release/wheelhouse.py verify /wheels \
    && /opt/venv/bin/python -m pip install --no-index --no-deps /wheels/*.whl \
    && /opt/venv/bin/python -m pip check
COPY --chown=0:0 tests/ /app/tests/
USER 10001:10001
ENTRYPOINT ["/opt/venv/bin/python", "-m", "packages.operations.release_checks"]
CMD []
