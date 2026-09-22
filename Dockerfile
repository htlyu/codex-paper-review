FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.155.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        poppler-utils \
        python3 \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && python3 -m venv /opt/venv \
    && groupadd --gid 10001 reviewer \
    && useradd --uid 10001 --gid reviewer --no-create-home reviewer \
    && mkdir -p /app /work \
    && chown reviewer:reviewer /work

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    CODEX_HOME=/codex-home \
    XDG_CACHE_HOME=/tmp/cache \
    npm_config_cache=/tmp/npm-cache

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY paper_review_service/ ./paper_review_service/

# The host runner normally overrides this with its own UID:GID for /work.
USER reviewer:reviewer
WORKDIR /work

CMD ["python", "-m", "paper_review_service.container_job"]
