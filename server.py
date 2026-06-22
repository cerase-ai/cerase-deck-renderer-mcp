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
import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cerase-deck-renderer")

CHROMIUM_BIN = (
    shutil.which("chromium")
    or shutil.which("chromium-browser")
    or "/usr/bin/chromium"
)
MD2_BIN = shutil.which("md2") or "/usr/local/bin/md2"


@mcp.tool()
def render(
    markdown_content: str,
    output_filename: str = "presentation.pdf",
) -> dict:
    """Render md2-flavoured markdown to a deck PDF.

    Args:
        markdown_content: the full markdown source. Frontmatter (+++ TOML)
            and slide separators (--- on its own line) follow md2 syntax —
            see the deck skill in cerase-ai/cerase-skills for the cheatsheet.
        output_filename: filename to report in the response payload. Does
            not affect on-disk artefacts (the PDF is returned as base64).

    Returns:
        A dict with `filename`, `size_bytes`, and `contents_base64` (the
        full PDF). The caller (agent skill) is expected to either save it
        into its workspace or surface it directly in the chat reply.
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

        return {
            "filename": output_filename,
            "size_bytes": len(pdf_bytes),
            "contents_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
