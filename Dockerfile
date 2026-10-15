FROM node:24-bookworm-slim AS webui-builder

WORKDIR /app
COPY webui/package.json webui/package-lock.json ./webui/
WORKDIR /app/webui
RUN npm ci
COPY webui/ ./
# Channel-owned frontend modules are imported through Vite's glob from outside
# webui/. The builder must see the same plugin tree as a repository build.
COPY nanobot/channels/ /app/nanobot/channels/
RUN mkdir -p /app/nanobot/web && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git gh bubblewrap openssh-client libmagic1 && \
    rm -rf /var/lib/apt/lists/*

# MCP runtime dependencies. Reuse the Node.js runtime from the WebUI build
# stage, preinstall the maintained reference servers, and install GitHub's
# official Go-based MCP server instead of the deprecated npm package.
COPY --from=webui-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=webui-builder /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    npm install -g \
        @modelcontextprotocol/server-filesystem@2026.7.10 \
        @modelcontextprotocol/server-memory@2026.7.4 && \
    npm cache clean --force

ARG GITHUB_MCP_VERSION=1.6.0
# This project currently deploys on x86_64 WSL/Docker. The official API can
# serve release assets when the github.com download frontend is unreachable.
# Keep the versioned download in a cached RUN layer and verify its published digest.
RUN python - "$GITHUB_MCP_VERSION" <<'PYTHON'
import hashlib
import json
import re
import sys
import urllib.request

version = sys.argv[1]
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise ValueError("invalid GitHub MCP version")
base = "https://api.github.com/repos/github/github-mcp-server"
headers = {"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nanobot-image-build"}
try:
    request = urllib.request.Request(f"{base}/releases/tags/v{version}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    asset = next(item for item in release["assets"]
                 if item["name"] == "github-mcp-server_Linux_x86_64.tar.gz")
    expected = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("missing published GitHub MCP digest")
    request = urllib.request.Request(
        f"{base}/releases/assets/{int(asset['id'])}",
        headers={**headers, "Accept": "application/octet-stream"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read()
    if len(archive) != asset["size"] or "sha256:" + hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("GitHub MCP archive checksum mismatch")
    with open("/tmp/github-mcp-server.tar.gz", "wb") as destination:
        destination.write(archive)
except Exception:
    raise RuntimeError("verified GitHub MCP dependency download failed") from None
PYTHON
RUN tar -xzf /tmp/github-mcp-server.tar.gz -C /usr/local/bin github-mcp-server && \
    chmod +x /usr/local/bin/github-mcp-server && \
    rm -f /tmp/github-mcp-server.tar.gz

WORKDIR /app

# Keep the runtime environment writable by the non-root nanobot user. Enabled
# channels may install their manifest-declared dependencies at startup.
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv --seed "$VIRTUAL_ENV"

# Install Python dependencies first (cached layer). Hatch reads the custom build
# hook from hatch_build.py even for this metadata-only install.
ARG NANOBOT_EXTRAS=
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
RUN mkdir -p nanobot && touch nanobot/__init__.py && \
    if [ -n "$NANOBOT_EXTRAS" ]; then \
        NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache ".[${NANOBOT_EXTRAS}]"; \
    else \
        NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache .; \
    fi && \
    rm -rf nanobot

# Copy the full source and install
COPY nanobot/ nanobot/
COPY scripts/install_channel_dependencies.py scripts/
COPY --from=webui-builder /app/nanobot/web/dist/ nanobot/web/dist/
RUN NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache .

# Preinstall selected channel dependencies from their manifests. A comma-separated
# list keeps the image configurable while preserving WhatsApp in the default image.
ARG NANOBOT_CHANNELS=whatsapp
RUN for channel in $(printf '%s' "$NANOBOT_CHANNELS" | tr ',' ' '); do \
        python -m scripts.install_channel_dependencies "$channel"; \
    done

# Render deploy template (see render.yaml): committed gateway config that wires
# secrets through ${ANTHROPIC_API_KEY} / ${NANOBOT_WEB_TOKEN} env vars (resolved
# at startup). Lives in the code dir (/app), not the data dir, so a mounted disk
# won't shadow it. Only used when RENDER=true; ignored by local runs.
COPY render-config.json ./

# Create the non-root user and hand ownership of the writable virtualenv to it.
RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown -R nanobot:nanobot /home/nanobot /app/.venv

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Start as root so the entrypoint can chown the data dir (on Render, the
# freshly-mounted root-owned persistent disk) before dropping to the non-root
# nanobot user via setpriv. The entrypoint drops privileges on every root start
# and fails closed if it cannot, so the agent never runs as root (see
# entrypoint.sh).
USER root
ARG NANOBOT_BUILD_REF=unknown
ARG NANOBOT_BUILD_TIME=unknown
ENV HOME=/home/nanobot
ENV NANOBOT_BUILD_REF=${NANOBOT_BUILD_REF}
ENV NANOBOT_BUILD_TIME=${NANOBOT_BUILD_TIME}
# Ensure crash output reaches Render logs (app output is otherwise swallowed on
# non-graceful exit).
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

# Gateway health endpoint and optional WebUI/WebSocket channel ports
EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
