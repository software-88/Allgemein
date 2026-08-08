# Claude Code + FastAPI-Bridge in einem Image.
# Node fuer die Claude-CLI, Python fuer die HTTP-Fassade.
FROM node:22-bookworm-slim

# Auf der Synology muessen die Dateirechte im Share passen: PUID/PGID auf den
# Besitzer von /volume1/docker/claude setzen (`id <benutzer>` auf der NAS).
ARG PUID=1000
ARG PGID=1000
ARG CLAUDE_CODE_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv git ripgrep curl ca-certificates jq less openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /srv/bridge
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN groupmod -o -g "${PGID}" node \
    && usermod -o -u "${PUID}" -g "${PGID}" node \
    && mkdir -p /workspace /data/jobs /config /home/node/.claude \
    && chown -R "${PUID}:${PGID}" /data /home/node

ENV HOME=/home/node \
    WORKSPACE_ROOT=/workspace \
    JOBS_DIR=/data/jobs \
    PROJECTS_FILE=/config/projects.yaml \
    BASE_SYSTEM_PROMPT_FILE=/config/systemprompt.de.md \
    DISABLE_AUTOUPDATER=1 \
    npm_config_update_notifier=false \
    PYTHONUNBUFFERED=1

USER node
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8790/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8790"]
