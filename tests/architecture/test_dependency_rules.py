"""Enforce the hexagonal dependency rule with an AST walk over ``fusion/``.

Every ``import``/``from ... import`` (including lazy ones inside function
bodies) is classified as stdlib, first-party (``fusion.*``) or third-party and
checked against the layer's allowance.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "fusion"
STDLIB = set(sys.stdlib_module_names)

# layer package -> (allowed first-party prefixes, third-party allowed)
RULES: dict[str, tuple[tuple[str, ...], bool]] = {
    "fusion.domain": (("fusion.domain",), False),
    "fusion.ports": (("fusion.domain", "fusion.ports"), False),
    "fusion.application": (("fusion.domain", "fusion.ports", "fusion.application"), False),
    "fusion.observability": (("fusion.domain", "fusion.application"), False),
    "fusion.adapters.outbound": (
        ("fusion.domain", "fusion.ports", "fusion.adapters.outbound"),
        True,
    ),
    "fusion.adapters.inbound": (
        (
            "fusion.domain",
            "fusion.ports",
            "fusion.application",
            "fusion.observability",
            "fusion.adapters.inbound",
            "fusion.bootstrap",
            "fusion",  # version string only
        ),
        True,
    ),
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                found.append(f"{base}.{node.module}" if node.module else base)
            else:
                found.append(node.module or "")
    return found


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _layer_files(layer: str) -> list[Path]:
    directory = PACKAGE_ROOT.joinpath(*layer.split(".")[1:])
    return sorted(directory.rglob("*.py")) if directory.exists() else []


@pytest.mark.parametrize("layer", sorted(RULES))
def test_layer_imports_respect_dependency_rule(layer: str) -> None:
    allowed_prefixes, third_party_ok = RULES[layer]
    violations: list[str] = []
    for path in _layer_files(layer):
        for name in _imports(path):
            top = name.split(".", 1)[0]
            if top in STDLIB:
                continue
            if top == "fusion":
                if _matches(name, allowed_prefixes):
                    continue
                violations.append(f"{path.relative_to(PACKAGE_ROOT.parent)} -> {name}")
            elif not third_party_ok:
                violations.append(f"{path.relative_to(PACKAGE_ROOT.parent)} -> {name}")
    assert not violations, "\n".join(violations)


def test_domain_and_ports_exist() -> None:
    assert _layer_files("fusion.domain"), "fusion/domain must exist"
    assert _layer_files("fusion.ports"), "fusion/ports must exist"
