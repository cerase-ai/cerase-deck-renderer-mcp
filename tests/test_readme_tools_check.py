"""scripts/readme-tools-check.py — the check that README.md's tool table and
the tools server.py registers are the same list.

The script is identical in every connector repo; its tests live here, in one of
them. Each case writes a server.py and a README.md into a temporary directory
and runs the script on them as CI does, as a separate process.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "readme-tools-check.py"

SERVER = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("example")


@mcp.tool()
def alpha(x: str) -> str:
    return x


@mcp.tool()
async def beta(x: str) -> str:
    return x
'''


def _readme(*names: str, after: str = "") -> str:
    rows = "".join(f"| `{n}` | does a thing |\n" for n in names)
    return f"# example\n\n## Tools\n\n| Tool | What it does |\n|---|---|\n{rows}\n{after}"


def _run(tmp_path: Path, server: str, readme: str) -> subprocess.CompletedProcess:
    (tmp_path / "server.py").write_text(server, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--server", "server.py", "--readme", "README.md"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )


def test_matching_lists_pass(tmp_path):
    result = _run(tmp_path, SERVER, _readme("alpha", "beta"))
    assert result.returncode == 0, result.stderr
    assert "lists the 2 tools server.py registers: alpha, beta" in result.stdout


def test_a_tool_missing_from_the_readme_and_a_row_the_server_lacks_are_both_named(tmp_path):
    result = _run(tmp_path, SERVER, _readme("alpha", "gamma"))
    assert result.returncode == 1
    assert "`beta` is registered by server.py (line 12) and missing from README.md" in result.stderr
    assert "`gamma` is listed in README.md (line 8) and not registered by server.py" in result.stderr
    assert "alpha" not in result.stderr


def test_a_readme_without_a_tools_section_fails(tmp_path):
    result = _run(tmp_path, SERVER, "# example\n\n## Settings\n\n| Tool |\n|---|\n| `alpha` |\n")
    assert result.returncode == 1
    assert "README.md has no '## Tools' section" in result.stderr


def test_a_tools_section_with_no_tool_row_fails(tmp_path):
    result = _run(tmp_path, SERVER, "## Tools\n\nNone yet.\n")
    assert result.returncode == 1
    assert "holds no table row naming a tool" in result.stderr


def test_a_row_without_a_backticked_name_and_a_repeated_row_fail(tmp_path):
    readme = "## Tools\n\n| Tool | x |\n|---|---|\n| alpha | x |\n| `beta` | x |\n| `beta` | x |\n"
    result = _run(tmp_path, SERVER, readme)
    assert result.returncode == 1
    assert "line 5: the first column names no tool in backticks" in result.stderr
    assert "line 7: `beta` is listed twice (first on line 6)" in result.stderr


def test_a_table_in_the_next_section_is_not_read(tmp_path):
    later = "## Settings\n\n| Name | Default |\n|---|---|\n| `TIMEOUT` | 30 |\n"
    result = _run(tmp_path, SERVER, _readme("alpha", "beta", after=later))
    assert result.returncode == 0, result.stderr


def test_the_registered_name_wins_over_the_function_name(tmp_path):
    server = SERVER + '''

@mcp.tool(name="renamed")
def gamma() -> None: ...


@mcp.tool("positional")
def delta() -> None: ...


def epsilon() -> None: ...


def zeta() -> None: ...


mcp.add_tool(epsilon)
mcp.add_tool(zeta, name="added")
mcp.tool()(lambda_free)
mcp.remove_tool("beta")
'''
    names = ("alpha", "renamed", "positional", "epsilon", "added", "lambda_free")
    result = _run(tmp_path, server, _readme(*names))
    assert result.returncode == 0, result.stderr
    for absent in ("beta", "gamma", "delta", "zeta"):
        result = _run(tmp_path, server, _readme(*names, absent))
        assert f"`{absent}` is listed in README.md" in result.stderr


def test_a_name_computed_at_run_time_fails_rather_than_being_guessed(tmp_path):
    server = SERVER + '\nNAME = "x"\n\n@mcp.tool(name=NAME)\ndef gamma() -> None: ...\n'
    result = _run(tmp_path, server, _readme("alpha", "beta"))
    assert result.returncode == 1
    assert "the tool name is computed at run time" in result.stderr


def test_a_server_that_registers_nothing_fails(tmp_path):
    result = _run(tmp_path, "def alpha() -> None: ...\n", _readme("alpha"))
    assert result.returncode == 1
    assert "registers no tool" in result.stderr
