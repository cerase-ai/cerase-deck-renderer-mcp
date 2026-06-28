#!/usr/bin/env python3
"""Cerase Deck Renderer — MCP server.

Exposes a single tool `render(markdown_content, output_filename?, template?,
template_css?, dark?)` that takes md2-flavoured markdown and writes a deck PDF
into the agent workspace (via the control-plane broker), returning a `{path}`
handle. The markdown→HTML conversion is delegated to the md2 CLI (md2-presenter
on PyPI); the HTML→PDF step shells out to headless chromium.

Theming (M-DECK-CUSTOM-TEMPLATE-1): md2 0.2.0 supports named templates under
`~/.md2/templates/` (`--template NAME`) + a `--dark` default. We expose those,
plus a by-value `template_css` brand override: it derives a one-shot template
from `default` with the supplied CSS appended (last-wins cascade), renders with
it, then removes it. A full multi-file template is a named/marketplace template.

Follow-on: the same brand CSS can be supplied BY REFERENCE as `template_path` —
a workspace file the read broker resolves (for overrides too big to inline, e.g.
embedded `@font-face` data URIs). Its content is treated exactly like
`template_css`; an explicit by-value `template_css` still wins.

Cerase plumbing: this server speaks the MCP stdio protocol; the container's
entrypoint pipes it through mcp-proxy which exposes /sse over HTTP on port 3000.
"""
from __future__ import annotations

import base64
import os
import re
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

_TEMPLATES_ROOT = os.path.expanduser("~/.md2/templates")
_SAFE_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


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


# ─── Read broker (mirror office-converter) — resolve a by-reference template ──

def _safe_local_path(path: str) -> str:
    """Resolve a workspace path, refusing anything that escapes the shared
    workspace root (path-traversal guard — the agent supplies `path`, so a
    crafted `../../etc/passwd` must not read host files)."""
    root = os.path.realpath(os.environ.get("CERASE_TOOL_WORKSPACE_ROOT", "/workspace"))
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError("path escapes the workspace root")
    return resolved


def _load_workspace_bytes(agent_id: str | None, path: str) -> bytes:
    """Read a workspace file's CONTENT (M-DECK-CUSTOM-TEMPLATE-1 follow-on — a
    by-reference `template_path`). Try a local mount first (dev/test where
    CERASE_TOOL_WORKSPACE_ROOT IS the agent's workspace), then fall back to the
    control-plane internal API (it owns workspace access via docker exec) scoped
    to (agent_id, path)."""
    try:
        local = _safe_local_path(path)
        if os.path.isfile(local):
            with open(local, "rb") as f:
                return f.read()
    except ValueError:
        pass  # not a safe local path → let the control-plane re-guard + serve

    cp = os.environ.get("CERASE_CONTROL_PLANE_URL", "").rstrip("/")
    secret = os.environ.get("CERASE_INTERNAL_SECRET", "")
    if not agent_id or not cp or not secret:
        raise ValueError(
            "workspace `template_path` given but no local file and no control-plane "
            "configured (agent_id / CERASE_CONTROL_PLANE_URL / CERASE_INTERNAL_SECRET)"
        )
    qs = urlencode({"path": path})
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — internal API
        return r.read()


def _ensure_default_template() -> bool:
    """Make sure `~/.md2/templates/default/` exists (md2 --init-templates).
    Returns True when a usable default template dir is present."""
    default_dir = os.path.join(_TEMPLATES_ROOT, "default")
    if not os.path.isdir(default_dir):
        subprocess.run([MD2_BIN, "--init-templates"], capture_output=True, text=True, timeout=30)
    return os.path.isdir(default_dir)


def _derive_template_from_css(template_css: str) -> str | None:
    """Build a one-shot md2 template: copy `default` and append the supplied
    brand CSS to its `style.css` (cascade — last wins). Returns the derived
    template dir path (caller removes it), or None when the default is absent."""
    if not _ensure_default_template():
        return None
    default_dir = os.path.join(_TEMPLATES_ROOT, "default")
    name = f"cerase-{uuid.uuid4().hex[:8]}"
    derived = os.path.join(_TEMPLATES_ROOT, name)
    shutil.copytree(default_dir, derived)
    with open(os.path.join(derived, "style.css"), "a", encoding="utf-8") as f:
        f.write("\n/* M-DECK-CUSTOM-TEMPLATE-1: per-render brand override */\n")
        f.write(template_css)
    return derived


def _md2_command(
    md_path: str,
    template: str | None,
    template_css: str | None,
    dark: bool,
) -> tuple[list[str], str | None]:
    """Assemble the md2 argv + return a derived-template dir to clean up (or
    None). `template_css` (by-value brand) wins over a named `template`."""
    args = [MD2_BIN]
    if dark:
        args.append("--dark")
    cleanup_dir: str | None = None
    name: str | None = None
    if template_css and template_css.strip():
        cleanup_dir = _derive_template_from_css(template_css)
        if cleanup_dir is not None:
            name = os.path.basename(cleanup_dir)
    elif template:
        if not _SAFE_TEMPLATE_NAME.match(template):
            raise ValueError(
                "template must be a simple name ([A-Za-z0-9_-]); it selects a "
                "template under ~/.md2/templates/"
            )
        name = template
    if name:
        args += ["--template", name]
    args.append(md_path)
    return args, cleanup_dir


@mcp.tool()
def render(
    markdown_content: str,
    output_filename: str = "presentation.pdf",
    agent_id: str | None = None,
    template: str | None = None,
    template_css: str | None = None,
    template_path: str | None = None,
    dark: bool = False,
) -> dict:
    """Render md2-flavoured markdown to a deck PDF.

    Args:
        markdown_content: the full markdown source. Frontmatter (+++ TOML)
            and slide separators (--- on its own line) follow md2 syntax —
            see the deck skill for the cheatsheet.
        output_filename: the filename for the produced PDF (written under
            `outputs/` in your workspace).
        agent_id: injected by the platform — do not set it.
        template: optional NAME of an installed md2 template (under
            `~/.md2/templates/`) — e.g. a brand template. Ignored when
            `template_css` / `template_path` is given.
        template_css: optional brand CSS applied on top of the default theme
            (colours / fonts / logo positioning) — passed by value, no file
            needed. Wins over `template_path` and `template`.
        template_path: optional workspace path to a brand-CSS file, resolved by
            the read broker — the by-reference form of `template_css`, for
            overrides too big to inline (e.g. embedded `@font-face` data URIs).
            Its content is applied exactly like `template_css`. Ignored when an
            explicit `template_css` is also given.
        dark: render on md2's dark theme.

    Returns:
        Normally `{path, filename, size_bytes}` — the PDF is written into your
        workspace at `path`; send it with `[[attach: <path>]]`. If the workspace
        broker isn't configured (dev), falls back to `{filename, size_bytes,
        contents_base64}`.
    """
    if not markdown_content.strip():
        raise ValueError("markdown_content is empty")

    # By-reference brand override: read the workspace file and treat its content
    # as `template_css`. An explicit by-value `template_css` wins.
    if template_path and not (template_css and template_css.strip()):
        template_css = _load_workspace_bytes(agent_id, template_path).decode("utf-8")

    workdir = tempfile.mkdtemp(prefix=f"deck-{uuid.uuid4().hex[:8]}-")
    md_path = os.path.join(workdir, "input.md")
    html_path = os.path.join(workdir, "input.html")
    pdf_path = os.path.join(workdir, output_filename)
    cleanup_template: str | None = None

    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        # md2 reads <name>.md and writes <name>.html next to it.
        md2_argv, cleanup_template = _md2_command(md_path, template, template_css, dark)
        md2_result = subprocess.run(
            md2_argv,
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
        if cleanup_template:
            shutil.rmtree(cleanup_template, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
