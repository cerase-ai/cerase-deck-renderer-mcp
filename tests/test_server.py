"""cerase-deck-renderer MCP — the properties that must not regress.

The weight is on the `{path}` handle. A rendered deck goes back into the calling
agent's workspace through the write broker and the tool answers with a handle;
the PDF bytes never travel in the MCP result. The federation truncates an
inlined base64 payload at 1 MB, and a truncated PDF is a corrupt PDF that still
reads as a successful render — so the large-deck case is asserted here byte for
byte, not by size alone.

The branch between a handle and inline base64 is the BROKER CONFIGURATION
(agent_id + control-plane URL + internal secret), never the deck's size. Both
sizes are covered below in both configurations so that a future size threshold
cannot be introduced unnoticed.

Nothing here runs md2, chromium or the network: `subprocess.run` is replaced by
a fake that writes the file each real binary would have produced and records the
argv, and `urlopen` by a fake broker that records the request.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import base64
import os
import subprocess

import pytest

import server


# A byte pattern rather than random data, so an assertion failure shows where a
# payload was cut instead of an unreadable diff.
def _payload(size: int) -> bytes:
    return (b"%PDF-1.7 CERASE-DECK-" * (size // 21 + 1))[:size]


LARGE = _payload(1_500_000)  # over the 1 MB federation truncation point
SMALL = _payload(512)

DECK_MD = "+++\ntitle = \"Q3\"\n+++\n\n# One\n\n---\n\n# Two\n"
AGENT = "agent-7"
BINDING = "binding-token"


# ─── Fakes: md2 / chromium and the control-plane broker ──────────────────

class FakeBinaries:
    """Stands in for md2 and chromium: records each argv and writes the file the
    real binary would have produced, so the server's own argv assembly, template
    handling and post-render handling all still run."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.payload = SMALL
        self.md2_writes_html = True
        self.md2_returncode = 0
        self.chromium_writes_pdf = True
        self.chromium_returncode = 0
        # Called with each argv before the fake acts on it. A derived template
        # exists only for the duration of the md2 call, so a test that wants to
        # read it has to look from in here.
        self.observer = None

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.observer is not None:
            self.observer(argv)
        if argv[0] == server.MD2_BIN:
            if "--init-templates" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if self.md2_writes_html:
                html = os.path.splitext(argv[-1])[0] + ".html"
                with open(html, "w", encoding="utf-8") as f:
                    f.write("<html><body>slides</body></html>")
            return subprocess.CompletedProcess(argv, self.md2_returncode, "", "md2 stub stderr")
        if self.chromium_writes_pdf:
            out = next(a for a in argv if a.startswith("--print-to-pdf="))
            with open(out.split("=", 1)[1], "wb") as f:
                f.write(self.payload)
        return subprocess.CompletedProcess(argv, self.chromium_returncode, "", "chromium stub stderr")

    def argv_for(self, binary: str) -> list[str]:
        for argv in self.calls:
            if argv[0] == binary and "--init-templates" not in argv:
                return argv
        raise AssertionError(f"{binary} was never invoked; calls={self.calls}")


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeBroker:
    """Records every control-plane call so a test can assert what crossed the
    wire — the point of the write broker is that the bytes go THERE."""

    def __init__(self, body: bytes = b"", status: int = 200):
        self.requests = []
        self.body = body
        self.status = status

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse(self.status, self.body)

    @property
    def last(self):
        assert self.requests, "the broker was never called"
        return self.requests[-1]


def headers_of(req) -> dict:
    # urllib capitalises header names on the way in.
    return {k.lower(): v for k, v in req.headers.items()}


@pytest.fixture
def binaries(monkeypatch):
    fake = FakeBinaries()
    monkeypatch.setattr(server.subprocess, "run", fake)
    return fake


@pytest.fixture
def broker(monkeypatch):
    fake = FakeBroker()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def broker_configured(monkeypatch):
    monkeypatch.setenv("CERASE_CONTROL_PLANE_URL", "http://cerase-control-plane")
    monkeypatch.setenv("CERASE_INTERNAL_SECRET", "internal-secret")


@pytest.fixture
def broker_unconfigured(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


@pytest.fixture
def templates_root(monkeypatch, tmp_path):
    """A throwaway ~/.md2/templates with a `default` to derive from — the real
    one belongs to the container image and must not be touched by a test."""
    root = tmp_path / "templates"
    (root / "default").mkdir(parents=True)
    (root / "default" / "style.css").write_text(":root { --bg: white; }\n", encoding="utf-8")
    monkeypatch.setattr(server, "_TEMPLATES_ROOT", str(root))
    return root


# ─── The {path} handle — the guarantee with no coverage anywhere else ────

def test_a_large_deck_returns_a_path_handle_and_never_inlines_it(
    binaries, broker, broker_configured
):
    binaries.payload = LARGE
    result = server.render(
        DECK_MD, output_filename="q3.pdf", agent_id=AGENT, agent_binding=BINDING
    )
    assert result == {
        "path": "outputs/q3.pdf",
        "filename": "q3.pdf",
        "size_bytes": len(LARGE),
    }
    # An inlined payload is the defect: the federation would truncate it at 1 MB
    # and hand the model a corrupt PDF that reports success.
    assert "contents_base64" not in result


def test_broker_receives_every_byte_of_a_large_deck(binaries, broker, broker_configured):
    binaries.payload = LARGE
    server.render(DECK_MD, output_filename="q3.pdf", agent_id=AGENT)
    req = broker.last
    assert req.get_method() == "PUT"
    assert req.full_url.endswith("/api/internal/workspace-file/agent-7?path=outputs%2Fq3.pdf")
    assert len(req.data) > 1_000_000
    assert req.data == LARGE


def test_broker_request_carries_the_internal_secret_and_the_agent_binding(
    binaries, broker, broker_configured
):
    server.render(DECK_MD, agent_id=AGENT, agent_binding=BINDING)
    h = headers_of(broker.last)
    assert h["authorization"] == "Bearer internal-secret"
    assert h["x-cerase-agent-binding"] == BINDING
    assert h["content-type"] == "application/octet-stream"


def test_a_small_deck_takes_the_same_handle_path(binaries, broker, broker_configured):
    # Size does not select the branch — the broker's availability does. Without
    # this, a size threshold could be introduced and only the large case would
    # notice.
    binaries.payload = SMALL
    result = server.render(DECK_MD, output_filename="one-slide.pdf", agent_id=AGENT)
    assert result["path"] == "outputs/one-slide.pdf"
    assert "contents_base64" not in result


def test_without_a_broker_the_bytes_come_back_inline(
    binaries, broker, broker_unconfigured
):
    binaries.payload = SMALL
    result = server.render(DECK_MD, output_filename="q3.pdf", agent_id=AGENT)
    assert "path" not in result
    assert base64.b64decode(result["contents_base64"]) == SMALL
    assert result["size_bytes"] == len(SMALL)
    assert broker.requests == []


def test_a_call_with_no_agent_id_falls_back_inline(binaries, broker, broker_configured):
    # A non-agent caller (dev, a direct MCP client) has no workspace to write to.
    binaries.payload = SMALL
    result = server.render(DECK_MD, output_filename="q3.pdf")
    assert "path" not in result
    assert base64.b64decode(result["contents_base64"]) == SMALL


# ─── The guards the server states explicitly ─────────────────────────────

def test_empty_markdown_is_refused_before_anything_runs(binaries, broker):
    with pytest.raises(ValueError, match="markdown_content is empty"):
        server.render("   \n  ", agent_id=AGENT)
    assert binaries.calls == []


def test_a_template_name_that_could_escape_the_template_root_is_refused():
    with pytest.raises(ValueError, match="simple name"):
        server._md2_command("/tmp/in.md", "../../etc/passwd", None, False)


def test_md2_failing_is_reported_with_its_stderr(binaries, broker, broker_configured):
    binaries.md2_returncode = 3
    with pytest.raises(RuntimeError, match="md2 failed .exit 3."):
        server.render(DECK_MD, agent_id=AGENT)


def test_md2_producing_no_html_is_reported(binaries, broker, broker_configured):
    # md2 can exit 0 and write nothing; the missing HTML is the only evidence.
    binaries.md2_writes_html = False
    with pytest.raises(RuntimeError, match="did not produce"):
        server.render(DECK_MD, agent_id=AGENT)


def test_chromium_failing_is_reported(binaries, broker, broker_configured):
    binaries.chromium_returncode = 1
    binaries.chromium_writes_pdf = False
    with pytest.raises(RuntimeError, match="chromium failed"):
        server.render(DECK_MD, agent_id=AGENT)


# ─── Theming ─────────────────────────────────────────────────────────────

def test_dark_reaches_md2(binaries, broker, broker_configured):
    server.render(DECK_MD, agent_id=AGENT, dark=True)
    assert "--dark" in binaries.argv_for(server.MD2_BIN)


def test_a_named_template_is_passed_through(binaries, broker, broker_configured):
    server.render(DECK_MD, agent_id=AGENT, template="brand-2026")
    argv = binaries.argv_for(server.MD2_BIN)
    assert argv[argv.index("--template") + 1] == "brand-2026"


def _watch_derived_style(binaries, templates_root, captured):
    """Read the derived template's style.css while md2 is being invoked."""
    def observe(argv):
        if argv[0] == server.MD2_BIN and "--template" in argv:
            name = argv[argv.index("--template") + 1]
            captured["name"] = name
            captured["css"] = (templates_root / name / "style.css").read_text(encoding="utf-8")
    binaries.observer = observe


def test_brand_css_derives_a_one_shot_template_and_removes_it(
    binaries, broker, broker_configured, templates_root
):
    css = ".slide { background: #101010; }"
    captured = {}
    _watch_derived_style(binaries, templates_root, captured)

    server.render(DECK_MD, agent_id=AGENT, template_css=css)

    assert captured["css"].startswith(":root { --bg: white; }")  # default first
    assert captured["css"].endswith(css)                          # brand last wins
    # The derived template is per-render state; leaving it behind would grow the
    # container's template dir on every call and leak one deck's brand into the
    # names another render can select.
    assert sorted(p.name for p in templates_root.iterdir()) == ["default"]


def test_brand_css_wins_over_a_named_template(binaries, broker, templates_root):
    argv, cleanup = server._md2_command("/tmp/in.md", "brand-2026", ".slide {}", False)
    try:
        assert argv[argv.index("--template") + 1] != "brand-2026"
        assert cleanup is not None
    finally:
        server.shutil.rmtree(cleanup, ignore_errors=True)


def test_a_by_reference_template_is_read_through_the_broker_and_applied(
    binaries, broker, broker_configured, templates_root, monkeypatch, tmp_path
):
    css = ".slide { color: #123456; }"
    broker.body = css.encode()
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    captured = {}
    _watch_derived_style(binaries, templates_root, captured)

    server.render(DECK_MD, agent_id=AGENT, template_path="brand/deck.css")

    assert captured["css"].endswith(css)
    # The first broker call is the READ of the template, a GET, not the PDF write.
    assert broker.requests[0].get_method() == "GET"
    assert "path=brand%2Fdeck.css" in broker.requests[0].full_url


# ─── Workspace reads ─────────────────────────────────────────────────────

def test_a_path_escaping_the_workspace_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="escapes the workspace root"):
        server._safe_local_path(str(tmp_path / ".." / "etc" / "passwd"))


def test_a_workspace_path_with_no_local_file_and_no_broker_is_an_error(
    monkeypatch, tmp_path, broker_unconfigured
):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="no control-plane"):
        server._load_workspace_bytes(AGENT, str(tmp_path / "absent.css"))
