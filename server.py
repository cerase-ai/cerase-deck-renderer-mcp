#!/usr/bin/env python3
"""Cerase Deck Renderer — MCP server.

Exposes a single tool `render(markdown_content, output_filename?, template?,
template_css?, dark?, orientation?, paper?)` that takes md2-flavoured markdown
and writes the deck into the agent workspace (via the control-plane broker),
returning a `{path}` handle: a PDF, or the HTML deck itself when
`output_filename` ends in `.html`. The markdown→HTML conversion is delegated to
the md2 CLI (md2-presenter on PyPI); the HTML→PDF step shells out to headless
chromium.

Page box: md2's HTML names no page size, and Chromium prints such a page on US
Letter portrait, so every deck came out on Letter portrait. The page size is
injected here, A4 landscape unless the call says otherwise, together with two
defensive print rules (columns stay side by side, tables wrap instead of
scrolling) for a print viewport narrow enough to match md2's mobile layout. A PDF answer carries its page count, which is how
the deck skill tells a slide that spilled onto a second page.

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

_PAPERS = {"a4": "A4", "letter": "letter"}
_ORIENTATIONS = ("landscape", "portrait")


def _page_css(paper: str, orientation: str) -> str:
    """The page box, and two defensive print rules: columns keep their row in
    print, and a table wraps instead of scrolling (charts excepted, whose bars
    size themselves from the table)."""
    return (
        "<style>"
        f"@page {{ size: {paper} {orientation}; margin: 12mm; }} "
        "@media print { .md2-columns { flex-direction: row !important; gap: 20px; } "
        ".slide table:not(.charts-css) { display: table !important; overflow-x: visible !important; "
        "white-space: normal !important; width: auto !important; max-width: 100% !important; "
        "margin: 30px auto !important; } }"
        "</style>"
    )


def _with_page_css(html: str, css: str) -> str:
    """Last in the head, so it wins over md2's own `@page { margin }`."""
    match = re.search(r"</head>", html, flags=re.IGNORECASE)
    if match:
        return html[: match.start()] + css + html[match.start():]
    match = re.search(r"<html[^>]*>", html, flags=re.IGNORECASE)
    if match:
        return html[: match.end()] + "<head>" + css + "</head>" + html[match.end():]
    return css + html


def _pdf_page_count(pdf: bytes) -> int:
    """Pages in a PDF Chromium printed: its page objects are written uncompressed."""
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf))


def _write_workspace_file(
    agent_id: str | None, path: str, data: bytes, binding: str = ""
) -> bool:
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
    # M-SEC-TOKEN-BINDING-1: the broker requires the calling agent's binding
    # (gateway-injected tool arg) besides the shared bearer.
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/octet-stream",
    }
    if binding:
        headers["X-Cerase-Agent-Binding"] = binding
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        data=data,
        method="PUT",
        headers=headers,
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


def _load_workspace_bytes(agent_id: str | None, path: str, binding: str = "") -> bytes:
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
    headers = {"Authorization": f"Bearer {secret}"}
    if binding:
        headers["X-Cerase-Agent-Binding"] = binding
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers=headers,
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


def _deliver(agent_id: str | None, filename: str, data: bytes, binding: str, extra: dict) -> dict:
    """M-WORKSPACE-WRITE-BROKER-1: write into the agent's workspace and return a
    small {path} handle — avoids the federated 1 MB base64 truncation that
    silently corrupts non-trivial decks, plus the model-context bloat. Falls back
    to base64 when the broker isn't configured (dev / a non-agent call)."""
    rel = f"outputs/{filename}"
    if _write_workspace_file(agent_id, rel, data, binding):
        return {"path": rel, "filename": filename, "size_bytes": len(data), **extra}
    return {
        "filename": filename,
        "size_bytes": len(data),
        **extra,
        "contents_base64": base64.b64encode(data).decode("ascii"),
    }


@mcp.tool()
def render(
    markdown_content: str,
    output_filename: str = "presentation.pdf",
    agent_id: str | None = None,
    template: str | None = None,
    template_css: str | None = None,
    template_path: str | None = None,
    dark: bool = False,
    orientation: str = "landscape",
    paper: str = "A4",
    agent_binding: str = "",
) -> dict:
    """Render md2-flavoured markdown to a deck: a PDF, or the HTML deck when `output_filename` ends in `.html`.

    Args:
        markdown_content: the full markdown source. Frontmatter (+++ TOML) and slide separators (--- on its own line) follow md2 syntax — see the deck skill for the cheatsheet.
        output_filename: the produced file's name, written under `outputs/` in your workspace. Ending in `.html` returns the HTML deck, one self-contained file that opens in any browser; any other name returns a PDF.
        agent_id: injected by the platform — do not set it.
        template: optional NAME of an installed md2 template (under `~/.md2/templates/`) — e.g. a brand template. Ignored when `template_css` / `template_path` is given.
        template_css: optional brand CSS applied on top of the default theme (colours / fonts / logo positioning) — passed by value, no file needed. Wins over `template_path` and `template`.
        template_path: optional workspace path to a brand-CSS file, resolved by the read broker — the by-reference form of `template_css`, for overrides too big to inline (e.g. embedded `@font-face` data URIs). Its content is applied exactly like `template_css`. Ignored when an explicit `template_css` is also given.
        dark: render on md2's dark theme.
        orientation: `landscape` (default) or `portrait`, the printed page.
        paper: `A4` (default) or `letter`.
        agent_binding: injected by the platform (M-SEC-TOKEN-BINDING-1 second factor for the workspace-file broker) — do not set it.

    Returns:
        Normally `{path, filename, size_bytes, format}` — the file is written into your workspace at `path`; send it with `[[attach: <path>]]`. A PDF answer also carries `pages`, the printed page count: one cover plus one page per `## ` slide, and more means a slide spilled onto a second page. If the workspace broker isn't configured (dev), falls back to `{filename, size_bytes, format, contents_base64}`.
    """
    if not markdown_content.strip():
        raise ValueError("markdown_content is empty")
    paper_key = (paper or "A4").strip().lower()
    if paper_key not in _PAPERS:
        raise ValueError(f"paper must be A4 or letter — not {paper!r}")
    orient = (orientation or "landscape").strip().lower()
    if orient not in _ORIENTATIONS:
        raise ValueError(f"orientation must be landscape or portrait — not {orientation!r}")
    as_html = output_filename.lower().endswith((".html", ".htm"))

    # By-reference brand override: read the workspace file and treat its content
    # as `template_css`. An explicit by-value `template_css` wins.
    if template_path and not (template_css and template_css.strip()):
        template_css = _load_workspace_bytes(agent_id, template_path, agent_binding).decode("utf-8")

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
        with open(html_path, encoding="utf-8") as f:
            html = _with_page_css(f.read(), _page_css(_PAPERS[paper_key], orient))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        if as_html:
            return _deliver(agent_id, output_filename, html.encode("utf-8"), agent_binding, {"format": "html"})

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
                # Chromium 154 reads this one; it ignored the older
                # --print-to-pdf-no-header and printed the date and the temp
                # file's URL on every page.
                "--no-pdf-header-footer",
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

        return _deliver(
            agent_id, output_filename, pdf_bytes, agent_binding,
            {"format": "pdf", "pages": _pdf_page_count(pdf_bytes)},
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if cleanup_template:
            shutil.rmtree(cleanup_template, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
