# Cerase Deck Renderer MCP — markdown → HTML → PDF via md2 + headless chromium.
#
# Exposed tool: render(markdown_content, output_filename?) → base64 PDF.
#
# Distribution: this Dockerfile is the canonical build target inside the
# Cerase repo (built locally via `./cli.sh build deck-renderer`). Mirrored
# verbatim to the public repo github.com/cerase-ai/cerase-deck-renderer-mcp
# for community standalone use.
#
# Image size budget: ~600MB unpacked (~250MB chromium binaries + ~150MB
# python + uv tool + md2 wheel + ~100MB system deps + ~100MB mcp/mcp-proxy).
FROM python:3.13.9-slim@sha256:326df678c20c78d465db501563f3492d17c42a4afe33a1f2bf5406a1d56b0e86

# System deps: chromium (the PDF rendering engine), font baseline (so deck
# text renders without missing-glyph squares), ca-certificates (https from
# mcp-proxy), and curl for diagnostics during build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-sandbox \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Cerase Python toolchain: uv for ephemeral installs, mcp + mcp-proxy
# for the stdio→HTTP bridge. Pinned via requirements.txt (OPT-15).
COPY requirements.txt requirements.lock /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/requirements.txt /tmp/requirements.lock

# md2-presenter — the markdown → HTML/PDF tool the renderer wraps.
# Pinned version aligns with what we publish on PyPI from
# github.com/guidance-studio/md2.
RUN uv tool install md2-presenter==0.2.0 && \
    ln -s /root/.local/bin/md2 /usr/local/bin/md2

# MCP server skeleton.
COPY server.py /app/server.py

# OPT-14: non-root runtime user. chromium runs with --no-sandbox per
# server.py (the comment around the chromium spawn references this).
RUN groupadd -r appuser \
 && useradd -r -g appuser -u 1000 -m -d /home/appuser -s /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app /root/.local
USER appuser
WORKDIR /home/appuser

EXPOSE 3000

# mcp-proxy bridges stdio MCP (server.py) → HTTP /sse on port 3000, which
# is the contract every Cerase MCP container speaks (catalog.yaml +
# McpServerOrchestrator probe both depend on this).
# M-CI-3: image-level liveness — runtime-spawned MCP containers have no
# compose healthcheck, this is the only signal `docker ps`/doctor sees.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 3000), timeout=5)" || exit 1

ENTRYPOINT ["sh", "-c", "exec mcp-proxy --port 3000 --host 0.0.0.0 --pass-environment -- python /app/server.py"]
