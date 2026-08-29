FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep patch nodejs npm ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 coder \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh coder \
    && mkdir -p /workspace/repo /artifacts /skills \
    && chown -R 10001:10001 /workspace /artifacts /skills /home/coder

USER 10001:10001
WORKDIR /workspace/repo
ENV HOME=/home/coder PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 CI=true

CMD ["sleep", "infinity"]
