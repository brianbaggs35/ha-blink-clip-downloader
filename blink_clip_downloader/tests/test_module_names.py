"""Guards against a module name that shadows a package the code imports.

Pyright resolves a bare ``import <x>`` to a same-named file in the
*importing file's own directory* before falling back to site-packages, so a
module named after a third-party package silently hijacks that package's
name for type checking — but only on a machine where the real package is
not installed. That asymmetry is what makes the mistake expensive: it
passes every local check, then fails CI (or, worse, a contributor's
machine) with an error that looks nothing like a naming problem.

This is not hypothetical. ``analyzer/moondream.py`` doing ``import
moondream`` resolved to itself and failed CI's pyright step, while passing
locally where the optional GPU-only ``moondream`` package happens to be
installed. It is now ``analyzer/moondream_provider.py``.

Only the source tree is inspected — nothing is imported — so this test
costs nothing and works with or without the optional extras present.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "blink_downloader"

# Names that are ours and always will be: the top-level package itself, and
# stdlib-shadowing is caught by ruff's A005 rather than here.
_OURS = {"blink_downloader"}


def _module_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if p.name != "__init__.py")


def _absolute_imports(path: Path) -> set[str]:
    """Every top-level package name ``path`` imports absolutely."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names - _OURS


def test_no_module_shadows_a_package_imported_from_its_own_directory() -> None:
    """A module must not be named after a package its own directory imports.

    The hijack is directory-scoped, so only siblings matter: a
    ``detection.py`` would only shadow an ``import detection`` made from a
    file beside it, not from elsewhere in the package.
    """
    by_directory: dict[Path, set[str]] = {}
    for path in _module_files():
        by_directory.setdefault(path.parent, set()).update(_absolute_imports(path))

    collisions = [
        f"{path.relative_to(_PACKAGE_ROOT.parent)} shadows `import {path.stem}`"
        f" made from {path.parent.relative_to(_PACKAGE_ROOT.parent)}/"
        for path in _module_files()
        if path.stem in by_directory.get(path.parent, set())
    ]
    assert not collisions, (
        "Module name(s) shadow a package imported from the same directory; "
        "pyright will resolve the import to the file wherever the real "
        "package is not installed. Rename with a suffix, as the analyzer's "
        "provider modules do:\n  " + "\n  ".join(collisions)
    )


def test_no_module_shadows_a_package_imported_anywhere_in_the_tree() -> None:
    """Broader net: a module named after any package this codebase imports.

    Not itself a pyright failure unless the import shares the module's
    directory, but it is always a readability trap — ``import openai`` in
    one file and ``openai.py`` in another invites exactly the wrong
    assumption about which one a reader is looking at.
    """
    imported: set[str] = set()
    for path in _module_files():
        imported |= _absolute_imports(path)

    collisions = sorted(
        f"{path.relative_to(_PACKAGE_ROOT.parent)} vs `import {path.stem}`"
        for path in _module_files()
        if path.stem in imported
    )
    assert not collisions, (
        "Module name(s) collide with a third-party package this codebase "
        "imports:\n  " + "\n  ".join(collisions)
    )
