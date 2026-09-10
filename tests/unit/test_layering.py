"""Architectural guard: `livekit` may only be imported inside voiceagent/livekit/.

This is the framework's one hard layering rule. If this test fails, engine
types are leaking into the engine-neutral core.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "voiceagent"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_livekit_confined_to_integration_layer() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if "livekit" in path.parts:  # src/voiceagent/livekit/** is the allowed home
            continue
        roots = _imported_roots(path)
        if any(root.startswith("livekit") for root in roots):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"livekit imported outside voiceagent/livekit/: {offenders}"


def test_core_modules_do_not_import_serving_layers() -> None:
    """Core root modules must not depend on platform/channels (one-way layering)."""
    core_files = [p for p in SRC.glob("*.py")] + list((SRC / "memory").glob("*.py"))
    offenders = []
    for path in core_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(("voiceagent.platform", "voiceagent.channels"))
            ):
                offenders.append(f"{path.name} -> {node.module}")
    assert not offenders, f"core imports serving layers: {offenders}"
