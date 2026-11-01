#!/usr/bin/env python3
"""C8: scan the import graph and fail on cycles or reverse dependencies.

Rules (architecture document, section 4):

    core      imports nothing but the standard library
    ports     imports core (and its own facade)
    adapters  imports core and ports, never another adapter
    app       imports anything; it is the only assembly point

The check is transitive by construction: every import edge is classified by the
layer of its target, so an indirect path through a helper module cannot hide a
reverse dependency.  Dynamic imports with a literal argument are treated as
edges too.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "xys_superagent"
LAYERS = ("core", "ports", "adapters", "app")

#: Which layers each layer may import.
ALLOWED = {
    "core": {"core"},
    "ports": {"core"},
    "adapters": {"core", "ports"},
    "app": {"core", "ports", "adapters", "app"},
}

#: Layers whose modules may not import a sibling module directly.
NO_SIBLINGS = {"ports", "adapters"}


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    path: str
    line: int

    @property
    def source_layer(self) -> str:
        return self.source.split(".")[1]

    @property
    def target_layer(self) -> str:
        parts = self.target.split(".")
        return parts[1] if len(parts) > 1 else ""


def module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(name: str, is_package: bool) -> str:
    return name if is_package else name.rsplit(".", 1)[0]


def _resolve_relative(node: ast.ImportFrom, current: str, is_package: bool) -> str:
    package = _package_of(current, is_package).split(".")
    drop = node.level - 1
    if drop > len(package):
        return ""
    base = package[: len(package) - drop]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _literal_dynamic(node: ast.Call) -> str | None:
    func = node.func
    is_dynamic = (
        (isinstance(func, ast.Name) and func.id == "__import__")
        or (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        )
    )
    if not is_dynamic or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def collect_edges(path: Path, root: Path) -> list[Edge]:
    name = module_name(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise SystemExit(f"cannot parse {path}: {exc}") from exc
    is_package = path.name == "__init__.py"
    edges: list[Edge] = []

    def add(target: str, line: int) -> None:
        if target:
            edges.append(Edge(name, target, str(path), line))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(node, name, is_package)
            else:
                base = node.module or ""
            if not base:
                continue
            add(base, node.lineno)
            for alias in node.names:
                if alias.name == "*":
                    continue
                add(f"{base}.{alias.name}", node.lineno)
        elif isinstance(node, ast.Call):
            dynamic = _literal_dynamic(node)
            if dynamic:
                add(dynamic, node.lineno)
    return edges


def stdlib(name: str) -> bool:
    root = name.split(".")[0]
    return root in sys.stdlib_module_names


def find_cycles(modules: set[str], edges: list[Edge]) -> list[list[str]]:
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for edge in edges:
        if edge.target in modules and edge.target != edge.source:
            graph[edge.source].add(edge.target)
    cycles: list[list[str]] = []
    state: dict[str, int] = {module: 0 for module in modules}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for neighbour in sorted(graph[node]):
            if state[neighbour] == 0:
                visit(neighbour)
            elif state[neighbour] == 1:
                start = stack.index(neighbour)
                cycle = stack[start:] + [neighbour]
                if cycle not in cycles and list(reversed(cycle)) not in cycles:
                    cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for module in sorted(modules):
        if state[module] == 0:
            visit(module)
    return cycles


def check(root: Path) -> tuple[list[str], int, int]:
    package = root / PACKAGE
    if not package.is_dir():
        raise SystemExit(f"no {PACKAGE}/ package under {root}")
    files = sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts)
    modules = {module_name(path, root) for path in files}
    edges: list[Edge] = []
    violations: list[str] = []

    for path in files:
        for edge in collect_edges(path, root):
            source_layer = edge.source_layer
            if source_layer not in LAYERS:
                violations.append(
                    f"{edge.path}:{edge.line}: module {edge.source} is outside the four layers"
                )
                continue
            target = edge.target
            if target == PACKAGE or target.startswith(PACKAGE + "."):
                target_layer = edge.target_layer
                if target_layer not in LAYERS:
                    violations.append(
                        f"{edge.path}:{edge.line}: {edge.source} imports "
                        f"{target}, which is outside the four layers"
                    )
                    continue
                if target_layer not in ALLOWED[source_layer] and target_layer != source_layer:
                    violations.append(
                        f"{edge.path}:{edge.line}: reverse dependency "
                        f"{source_layer} -> {target_layer} ({edge.source} imports {target})"
                    )
                elif target_layer == source_layer and source_layer in NO_SIBLINGS:
                    facade = f"{PACKAGE}.{source_layer}"
                    if target != facade and edge.source != facade:
                        violations.append(
                            f"{edge.path}:{edge.line}: {source_layer} modules must not import "
                            f"each other ({edge.source} imports {target})"
                        )
                edges.append(edge)
            else:
                if source_layer in {"core", "ports", "adapters"} and not stdlib(target):
                    violations.append(
                        f"{edge.path}:{edge.line}: third-party import {target!r} in "
                        f"{source_layer}/ (standard library only)"
                    )

    for cycle in find_cycles(modules, edges):
        violations.append("import cycle: " + " -> ".join(cycle))

    return violations, len(modules), len(edges)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="directory that contains the package (default: project root)",
    )
    args = parser.parse_args(argv)
    violations, modules, edges = check(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"VIOLATION {violation}")
        print(f"FAILED: {len(violations)} violation(s) over {modules} modules")
        return 1
    print(f"scanned {modules} modules, {edges} internal edges")
    print("no cycles, no reverse dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
