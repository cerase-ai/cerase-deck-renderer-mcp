#!/usr/bin/env python3
"""Cerase Deck Renderer — MCP server.

Exposes a single tool `render(markdown_content, output_filename?)` that
takes md2-flavoured markdown and returns a base64-encoded PDF. The
markdown→HTML conversion is delegated to the md2 CLI (md2-presenter on
PyPI); the HTML→PDF step shells out to headless chromium.

Cerase plumbing: this server speaks the MCP stdio protocol; the
container's entrypoint pipes it through mcp-proxy which exposes
/sse over HTTP on port 3000 — the contract every Cerase MCP container
honours (see agent-runtime/mcp-runner/docker/entrypoint.sh for the parallel
pattern).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cerase-deck-renderer")

CHROMIUM_BIN = (
    shutil.which("chromium")
    or shutil.which("chromium-browser")
    or "/usr/bin/chromium"
)
MD2_BIN = shutil.which("md2") or "/usr/local/bin/md2"


def _write_workspace_file(agent_id: str | None, path: str, data: bytes) -> bool:
    """M-WORKSPACE-WRITE-BROKER-1 — write a produced artifact back into the
    calling agent's workspace via the control-plane broker (this runner mounts
    no agent volume). The control-plane owns workspace access (docker exec),
    scopes the write to (agent_id, path), and caps it. Returns True on success,
    False when not configured — the caller then falls back to base64 (dev / a
    non-agent call).
    """
    cp = os.environ.get("CERASE_CONTROL_PLANE_URL", "").rstrip("/")
    secret = os.environ.get("CERASE_INTERNAL_SECRET", "")
    if not agent_id or not cp or not secret:
        return False
    qs = urlencode({"path": path})
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — internal API
        return 200 <= r.status < 300


@mcp.tool()
def render(
    markdown_content: str,
    output_filename: str = "presentation.pdf",
    agent_id: str | None = None,
) -> dict:
    """Render md2-flavoured markdown to a deck PDF.

    Args:
        markdown_content: the full markdown source. Frontmatter (+++ TOML)
            and slide separators (--- on its own line) follow md2 syntax —
            see the deck skill in cerase-ai/cerase-skills for the cheatsheet.
        output_filename: the filename for the produced PDF (written under
            `outputs/` in your workspace).
        agent_id: injected by the platform — do not set it.

    Returns:
        Normally `{path, filename, size_bytes}` — the PDF is written into your
        workspace at `path`; send it with `[[attach: <path>]]`. If the workspace
        broker isn't configured (dev), falls back to `{filename, size_bytes,
        contents_base64}`.
    """
    if not markdown_content.strip():
        raise ValueError("markdown_content is empty")

    workdir = tempfile.mkdtemp(prefix=f"deck-{uuid.uuid4().hex[:8]}-")
    md_path = os.path.join(workdir, "input.md")
    html_path = os.path.join(workdir, "input.html")
    pdf_path = os.path.join(workdir, output_filename)

    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        # md2 reads <name>.md and writes <name>.html next to it.
        md2_result = subprocess.run(
            [MD2_BIN, md_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if md2_result.returncode != 0:
            raise RuntimeError(
                f"md2 failed (exit {md2_result.returncode}):\n"
                f"stdout: {md2_result.stdout}\nstderr: {md2_result.stderr}"
            )
        if not os.path.exists(html_path):
            raise RuntimeError(
                f"md2 did not produce {html_path}; stdout: {md2_result.stdout}"
            )

        # chromium headless → PDF. --no-sandbox required because the
        # container runs in a minimal Linux environment without the
        # user-namespacing chromium expects (OPT-14: container now runs
        # as a non-root `appuser`, but the sandbox still depends on
        # CAP_SYS_ADMIN + namespaces unavailable here).
        chrome_result = subprocess.run(
            [
                CHROMIUM_BIN,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--print-to-pdf-no-header",
                f"--print-to-pdf={pdf_path}",
                f"file://{html_path}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if chrome_result.returncode != 0 or not os.path.exists(pdf_path):
            raise RuntimeError(
                f"chromium failed (exit {chrome_result.returncode}):\n"
                f"stderr: {chrome_result.stderr}"
            )

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # M-WORKSPACE-WRITE-BROKER-1: write into the agent's workspace and return
        # a small {path} handle — avoids the federated 1 MB base64 truncation that
        # silently corrupts non-trivial decks, plus the model-context bloat. Falls
        # back to base64 when the broker isn't configured (dev / a non-agent call).
        rel = f"outputs/{output_filename}"
        if _write_workspace_file(agent_id, rel, pdf_bytes):
            return {"path": rel, "filename": output_filename, "size_bytes": len(pdf_bytes)}
        return {
            "filename": output_filename,
            "size_bytes": len(pdf_bytes),
            "contents_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
