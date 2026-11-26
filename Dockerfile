# Nakama fleet runner. Code is baked in; data dirs are bind-mounted (see compose.yaml).
FROM python:3.12-slim

# Node.js is only needed for stdio MCP servers launched via `npx`. Build with --build-arg WITH_NODE=false to skip.
ARG WITH_NODE=true
RUN if [ "$WITH_NODE" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# Run as non-root: Claude Code refuses some modes as root, and bind-mounted files stay owned by you.
ARG UID=1000
ARG GID=1000
# Reuse an existing group if GID is taken (e.g. Synology users are often GID 100 = "users" in Debian).
RUN (getent group ${GID} >/dev/null || groupadd -g ${GID} nakama) \
    && useradd -m -u ${UID} -g ${GID} nakama

WORKDIR /app
COPY pyproject.toml ./
COPY nakama ./nakama
RUN pip install --no-cache-dir .

COPY agents/_shared ./agents/_shared
COPY templates ./templates
COPY samples ./samples
RUN chown -R ${UID}:${GID} /app

USER nakama
ENV NAKAMA_HOME=/app PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "nakama"]
CMD ["serve"]
