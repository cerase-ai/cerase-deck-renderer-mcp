# cerase-deck-renderer-mcp

MCP server that renders md2-flavoured markdown into a PDF deck using
[md2-presenter](https://pypi.org/project/md2-presenter/) and headless
Chromium.

This directory is the canonical source inside the [Cerase
repo](https://github.com/cerase-ai/cerase). The public mirror lives at
[`cerase-ai/cerase-deck-renderer-mcp`](https://github.com/cerase-ai/cerase-deck-renderer-mcp)
for standalone use by anyone building an agent that needs deck rendering.

## Build

From the Cerase repo root:

```bash
./cli.sh build deck-renderer
```

(Equivalent to `docker build -t cerase-deck-renderer-mcp:0.1.0-dev .`)

## Run standalone

```bash
docker run --rm -p 3000:3000 cerase-deck-renderer-mcp:0.1.0-dev
# MCP endpoint: http://localhost:3000/sse
```

## Tool surface

One tool: `render(markdown_content: str, output_filename: str = "presentation.pdf") -> dict`

Returns `{filename, size_bytes, contents_base64}`. The base64 payload is
the full PDF — the caller is expected to save it into its workspace or
attach it directly to the user reply.

## md2 syntax reference

The markdown follows md2 conventions (`+++` TOML frontmatter, `---` slide
separator). See the
[deck skill](https://github.com/cerase-ai/cerase-skills/tree/main/deck)
for the cheatsheet and the slide pattern library.

## Why a dedicated container

Pattern coherent with the rest of the Cerase MCP catalog: one container
per MCP, isolated, per-MCP resource accounting, no `bash:allow` leak
into the agent slot image. Slot agent containers stay light (no
chromium, no md2).

## License

MIT — see [LICENSE](./LICENSE) in the public mirror repo.
