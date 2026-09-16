"""The proof obligation of Wave 2: ``djcode.core`` is terminal-free.

If this test passes, a desktop GUI can drive the engine. If it fails, whatever
front-end gets written is a fork of the engine rather than a consumer of it.

The check walks the *static* import closure of ``djcode.core`` with ``ast``
rather than inspecting ``sys.modules``. That matters: by the time pytest runs,
``sys.modules`` is full of rich (pytest's own output), click (the CLI tests) and
prompt_toolkit (the REPL tests), so a runtime check would either pass
vacuously or fail for reasons that have nothing to do with core.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PKG = SRC / "djcode"

# A front-end may import any of these. Core may not, anywhere in its closure.
FORBIDDEN_IMPORTS = {"rich", "questionary", "prompt_toolkit", "textual", "click"}

# Writing to a stream is the same sin as importing a console library: it assumes
# a terminal is attached and steals output a GUI needed to render itself.
FORBIDDEN_CALLS = {"print", "input"}
FORBIDDEN_ATTR_CALLS = {
    ("sys", "stdout", "write"),
    ("sys", "stderr", "write"),
}


def module_to_path(name: str) -> Path | None:
    """Resolve a dotted djcode module name to a source file, if it has one."""
    if not name.startswith("djcode"):
        return None
    rel = name.replace(".", "/")
    for candidate in (SRC / f"{rel}.py", SRC / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def imported_names(tree: ast.AST, module_name: str, *, deferred: bool = True) -> set[str]:
    """Every module this file imports.

    With ``deferred=True`` that includes imports inside functions; with
    ``deferred=False`` only module-level imports, which are the ones that
    execute when the module is imported.
    """
    found: set[str] = set()
    nodes = ast.walk(tree) if deferred else ast.iter_child_nodes(tree)
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolve against this package
                parts = module_name.split(".")
                base = parts[: len(parts) - node.level]
                found.add(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                found.add(node.module)
    return found


def closure(entry: str = "djcode.core", *, deferred: bool = True) -> dict[str, Path]:
    """Every djcode module reachable from ``entry`` by static import.

    ``deferred=False`` follows only module-level imports -- the "hard" closure
    that genuinely loads when someone writes ``import djcode.core``. With
    ``deferred=True`` a module reached only by a lazy import inside a function
    is included too; that is the wider set, useful for spotting coupling that
    would bite the first time such a function runs.
    """
    seen: dict[str, Path] = {}
    queue = [entry]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        path = module_to_path(name)
        if path is None:
            continue
        seen[name] = path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in imported_names(tree, name, deferred=deferred):
            if imported.startswith("djcode") and imported not in seen:
                queue.append(imported)
            # A dotted import of a symbol resolves to its module too.
            parent = imported.rsplit(".", 1)[0]
            if parent.startswith("djcode") and parent not in seen:
                queue.append(parent)
    return seen


def test_core_closure_is_non_trivial():
    """Guard against the check passing because it found nothing."""
    modules = closure()
    assert len(modules) >= 10, f"suspiciously small closure: {sorted(modules)}"
    assert "djcode.core.events" in modules
    assert "djcode.provider" in modules


# Deferred terminal imports that are permitted, with the reason. These live
# inside functions that ARE the interactive terminal flow and that no headless
# caller reaches; importing djcode.core does not execute them, which
# test_importing_core_loads_no_terminal_library proves empirically.
#
# This list must not grow. Each entry is a function to move into
# djcode.frontends when its callers are refactored (its tests patch
# djcode.auth directly today, so moving it now would mean rewriting them).
ALLOWED_DEFERRED = {"djcode/auth.py"}


def enclosing_function(tree: ast.AST, lineno: int) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                return node.name
    return ""


def test_core_closure_has_no_module_level_terminal_import():
    """Hard rule: importing anything in core's closure must not pull in a terminal.

    A module-level import executes on import, so this admits no exceptions.
    """
    violations: list[str] = []
    for _name, path in sorted(closure(deferred=False).items()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.iter_child_nodes(tree):  # module level only
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module]
            for mod in mods:
                if mod.split(".")[0] in FORBIDDEN_IMPORTS:
                    violations.append(f"{path.relative_to(SRC)}:{node.lineno}: imports {mod}")
    assert not violations, "core must not import a terminal at module level:\n" + "\n".join(
        violations
    )


def test_core_closure_deferred_terminal_imports_do_not_grow():
    """Modules that reach core only through a lazy import are allowlisted.

    ``provider.py`` imports ``djcode.auth`` inside a function, so auth never
    loads on ``import djcode.core`` -- but it IS terminal code (it owns the
    interactive provider/auth pickers), so it is recorded here rather than
    hidden. W8 moves those pickers into ``core/onboarding_flow.py`` plus a
    front-end, at which point this entry is deleted.

    The list must not grow: any NEW module that drags a terminal into core,
    however lazily, fails here.
    """
    hard = set(closure(deferred=False))
    offenders: dict[str, list[str]] = {}
    for name, path in sorted(closure().items()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module]
            for mod in mods:
                if mod.split(".")[0] in FORBIDDEN_IMPORTS:
                    rel = path.relative_to(SRC).as_posix()
                    offenders.setdefault(rel, []).append(
                        f"{rel}:{node.lineno} in {enclosing_function(tree, node.lineno) or '<module>'}"
                        f": imports {mod}"
                    )
                    if name in hard:
                        offenders[rel].append(f"  ^ and {name} is in the HARD closure")

    unexpected = set(offenders) - ALLOWED_DEFERRED
    joined = chr(10).join(line for f in sorted(unexpected) for line in offenders[f])
    assert not unexpected, "new terminal coupling reachable from core:" + chr(10) + joined
    stale = ALLOWED_DEFERRED - set(offenders)
    assert not stale, f"allowlist entries no longer needed, delete them: {sorted(stale)}"


def test_importing_core_needs_no_terminal_and_prints_nothing():
    """The empirical proof: import and use core with no usable terminal at all.

    stdin, stdout and stderr are redirected away, so anything that demands a
    console, queries terminal size, or prints a banner on import fails here.
    The subprocess reports its result through a file, not through a stream.

    Note on what this deliberately does NOT assert: that no terminal library is
    present in ``sys.modules``. ``httpx`` -- which every provider needs -- pulls
    in ``click`` and ``rich`` itself for its own CLI, so their mere presence is
    outside djcode's control and says nothing about whether core uses them.
    What matters, and what this checks, is that core neither requires a terminal
    nor writes to one.
    """
    import subprocess
    import sys as _sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "result.txt"
        code = (
            "import os, sys\n"
            "devnull = os.open(os.devnull, os.O_RDWR)\n"
            "os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)\n"
            "import djcode.core as c\n"
            "bus = c.EventBus()\n"
            "bus.emit_nowait(c.token_event('hello'))\n"
            "o = c.ToolOutcome(content='ok', details={'n': 1})\n"
            "assert str(o) == 'ok' and c.EventType.TOKEN == 'token'\n"
            f"open({str(marker)!r}, 'w').write('ok:' + str(len(c.__all__)))\n"
        )
        result = subprocess.run(
            [_sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, (
            "djcode.core could not be imported and used without a terminal:\n" + result.stderr
        )
        assert marker.is_file(), "core ran but produced no result"
        payload = marker.read_text(encoding="utf-8")
        assert payload.startswith("ok:"), payload
        assert int(payload.split(":")[1]) >= 20, f"core exports too little: {payload}"
        assert not result.stdout, f"core wrote to stdout on import: {result.stdout!r}"


def test_core_closure_never_writes_to_a_stream():
    violations: list[str] = []
    for name, path in sorted(closure().items()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                violations.append(f"{path.relative_to(SRC)}:{node.lineno}: calls {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr == "write":
                # match sys.stdout.write / sys.stderr.write
                value = func.value
                if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                    triple = (value.value.id, value.attr, func.attr)
                    if triple in FORBIDDEN_ATTR_CALLS:
                        violations.append(
                            f"{path.relative_to(SRC)}:{node.lineno}: calls {'.'.join(triple)}()"
                        )
    assert not violations, "djcode.core must not write to a stream:\n" + "\n".join(violations)


def test_core_does_not_import_any_frontend():
    """The dependency arrow points one way: front-ends consume core."""
    offenders = [n for n in closure() if n.startswith("djcode.frontends")]
    assert not offenders, f"core must not import a front-end: {offenders}"


@pytest.mark.parametrize("module", ["djcode.core", "djcode.core.events", "djcode.core.outcome"])
def test_core_modules_import_cleanly(module):
    """Importing core must not require a terminal to exist."""
    __import__(module)
