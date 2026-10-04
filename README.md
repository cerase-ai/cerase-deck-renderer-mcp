# cerase-deck-renderer-mcp

An MCP server that turns a presentation written in md2 markdown into a PDF or
an HTML deck. It converts the markdown to an HTML deck with
[md2-presenter](https://pypi.org/project/md2-presenter/) 0.2.1 and prints that
HTML to PDF with headless Chromium. It calls no model.

## Tools

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `render` | Renders md2 markdown into a deck PDF, or into the HTML deck. | `markdown_content`; optional `output_filename` (default `presentation.pdf`), `template`, `template_css`, `template_path`, `dark`, `orientation` (`landscape` or `portrait`, default `landscape`), `paper` (`A4` or `letter`, default `A4`); `agent_id`, `agent_binding` | `{path, filename, size_bytes, format}`, plus `pages` for a PDF; `{filename, size_bytes, format, contents_base64}` without the workspace broker |

An `output_filename` ending in `.html` returns the HTML deck: one file with its
styles and scripts inline, which opens in any browser. Any other name returns a
PDF. Both carry the page box (`@page` size from `paper` and `orientation`, 12 mm
margins) and two print rules: columns stay side by side and tables wrap rather
than scroll. Without a page size, Chromium prints on US Letter portrait.

`pages` is the PDF's page count. A deck prints one page for the cover and one
for each `## ` slide, so a larger count means a slide ran onto a second page.

Theming:

- `template` is the name of an md2 template installed under
  `~/.md2/templates/` in the container (letters, digits, `_` and `-` only).
- `template_css` is CSS appended to md2's `default` template for this render
  only, so brand colours and fonts override the default theme.
- `template_path` is a file in the calling assistant's workspace whose content
  is used as `template_css`, for CSS too large to pass inline (for example
  fonts embedded as `data:` URIs).
- Precedence: `template_css`, then `template_path`, then `template`.
- `dark` renders on md2's dark theme.

The markdown syntax (`+++` TOML front matter, `---` between slides, charts,
two-column layouts, palettes) is documented in `md2-syntax.md` of the
[`deck` skill](https://github.com/cerase-ai/deck-skill), the Cerase skill that
writes a deck in this syntax and calls this tool.

Inside Cerase the gateway fills `agent_id` and
`agent_binding`; the model never sets them. The file is written into the
calling assistant's workspace at `outputs/<output_filename>` with
`PUT /api/internal/workspace-file/<agent_id>?path=…` on the Cerase
control-plane, presenting `CERASE_INTERNAL_SECRET` as a bearer and
`agent_binding` as `X-Cerase-Agent-Binding`, and the tool returns that path.
When `agent_id`, `CERASE_CONTROL_PLANE_URL` or `CERASE_INTERNAL_SECRET` is
missing, the file comes back inline as base64 instead. A `template_path` is
read locally when it exists under `CERASE_TOOL_WORKSPACE_ROOT`, otherwise
fetched from the same control-plane endpoint with `GET`.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `CERASE_CONTROL_PLANE_URL` | none | Control-plane base URL for writing the deck into the workspace and reading a `template_path`. |
| `CERASE_INTERNAL_SECRET` | none | Bearer token for those requests. |
| `CERASE_TOOL_WORKSPACE_ROOT` | `/workspace` | Directory a `template_path` is read from locally; a path resolving outside it is never opened. |

## Installation

The connector is published in the Cerase Marketplace as
`studio.guidance/cerase-deck-renderer`
([marketplace page](https://marketplace.cerase.ai/en/p/studio.guidance/cerase-deck-renderer)).
Every Cerase appliance installs it at boot, so its assistants have it without
an install step.

Each push to `main` runs the tests and publishes
`ghcr.io/cerase-ai/cerase-deck-renderer-mcp` (`.github/workflows/publish.yml`).

## Build and run locally

```sh
docker build -t cerase-deck-renderer-mcp .
docker run --rm -p 3000:3000 cerase-deck-renderer-mcp
```

`server.py` speaks MCP over stdio; the image runs it behind `mcp-proxy`, which
serves Streamable HTTP at `http://localhost:3000/mcp` and SSE at
`http://localhost:3000/sse`. Run without the control-plane variables, `render`
returns the file as base64. Chromium runs with `--no-sandbox`, because the
container lacks the namespaces its sandbox needs; the process runs as the
unprivileged user `appuser`.

The image's `HEALTHCHECK` runs `scripts/healthcheck.py`, an MCP client that
completes the handshake and lists the tools over `/mcp`; its
`CERASE_HEALTHCHECK_*` variables exist to point it at a stub in tests.

The tests fake md2, Chromium and the control-plane:

```sh
pip install -r requirements-dev.txt
python -m pytest tests/
```

## License

MIT. See [LICENSE](LICENSE).
