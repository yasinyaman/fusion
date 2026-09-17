"""Packaging invariants: Dockerfile, version consistency, entry points, import cost."""

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from fusion import __version__
from fusion.application.tool_schemas import TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_cmd_has_no_unexpanded_variables():
    """Regression: exec-form CMD passed a literal '${WARP_URL:-...}' to fusion-rest."""
    cmds = []
    continued = False  # a line ending in "\\" continues the previous instruction
    for raw in (ROOT / "Dockerfile").read_text().splitlines():
        line = raw.strip()
        if not continued and line.startswith("CMD "):
            cmds.append(line)
        continued = line.endswith("\\")
    assert cmds, "Dockerfile has no CMD"
    for cmd in cmds:
        assert "${" not in cmd, cmd
        assert cmd.startswith("CMD ["), "CMD must stay in exec form"
        argv = json.loads(cmd[len("CMD ") :])
        assert argv[0] == "fusion-rest"


def test_version_is_consistent():
    assert __version__ == "1.1.0"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "fusion/__init__.py"
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text()


def test_ten_tools():
    assert len(TOOL_NAMES) == 10


def test_console_scripts_resolve():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert set(scripts) == {"fusion-rest", "fusion-mcp"}
    for target in scripts.values():
        module_name, attr = target.split(":")
        assert callable(getattr(importlib.import_module(module_name), attr))


def test_import_fusion_is_light():
    code = (
        "import sys, fusion; "
        "print(sorted(m for m in ('duckdb','sqlglot','requests','pandas','fastapi','mcp') "
        "if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_no_stale_env_keys_in_templates():
    for name in (".env.example", ".env.production.example"):
        text = (ROOT / name).read_text()
        assert "FUSION_ALLOWED_HOSTS" not in text
        assert "FUSION_RATE_LIMIT_BURST" not in text
