"""Guards the suite's isolation from the add-on's real ``/data`` volume.

conftest.py's autouse ``data_dir`` fixture redirects every /data path the
package holds, found by walking its modules and classes. That only works
while each such path is a module- or class-level constant looked up at call
time. A literal inside a function, or a constant bound as a default
argument when the function is defined, is out of any patch's reach and
goes back to writing the real /data. Those leaks break unrelated tests on
any machine that can create /data (root, a Home Assistant devcontainer),
and CI never sees them because its runners can't create /data at all.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

from blink_downloader import app, downloader
from blink_downloader.media_server import library
from tests.conftest import (
    attribute_owners,
    data_path_attributes,
    is_data_path,
    package_modules,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _PROJECT_ROOT / "blink_downloader"


def _constant_value_nodes(tree: ast.Module) -> set[int]:
    """Ids of every node inside a module- or class-level assignment's value."""
    bodies = [tree.body] + [
        node.body for node in tree.body if isinstance(node, ast.ClassDef)
    ]
    return {
        id(node)
        for body in bodies
        for stmt in body
        if isinstance(stmt, ast.Assign | ast.AnnAssign) and stmt.value is not None
        for node in ast.walk(stmt.value)
    }


def test_every_data_path_literal_is_a_module_or_class_constant() -> None:
    offenders = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _constant_value_nodes(tree)
        offenders.extend(
            f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and is_data_path(node.value)
            and id(node) not in constants
        )
    assert not offenders, (
        "A /data path written inline can't be redirected by conftest's "
        "data_dir fixture. Make it a module- or class-level constant:\n  "
        + "\n  ".join(offenders)
    )


def test_no_default_argument_is_a_data_path() -> None:
    offenders = []
    for module in package_modules():
        for owner in attribute_owners(module):
            for value in vars(owner).values():
                func = inspect.unwrap(getattr(value, "__func__", value))
                if not inspect.isfunction(func) or func.__module__ != module.__name__:
                    continue
                defaults = (
                    *(func.__defaults__ or ()),
                    *(func.__kwdefaults__ or {}).values(),
                )
                if any(is_data_path(default) for default in defaults):
                    offenders.append(f"{module.__name__}: {func.__qualname__}")
    assert not offenders, (
        "A default argument is fixed when the function is defined, so "
        "patching the constant it came from never reaches it. Default to "
        "None and fall back to the constant inside the function:\n  "
        + "\n  ".join(offenders)
    )


def test_the_walk_imports_every_module_in_the_package() -> None:
    """Every .py file must be walked, or its constants would go unpatched."""
    assert len(package_modules()) == len(list(_PACKAGE_ROOT.rglob("*.py")))


def test_nothing_in_the_package_still_points_at_data(data_dir: Path) -> None:
    assert data_path_attributes(), "the walk found no /data paths at all"
    leftovers = [
        f"{getattr(owner, '__qualname__', owner.__name__)}.{name} = {value!r}"
        for module in package_modules()
        for owner in attribute_owners(module)
        for name, value in vars(owner).items()
        if is_data_path(value)
    ]
    assert not leftovers


def test_redirected_paths_keep_their_names_under_data_dir(data_dir: Path) -> None:
    """The two leaks this fixture was written for, and the reason paths keep
    their place relative to /data: app polls the file the library touches."""
    assert downloader.CAMERA_IDENTITIES_FILE == data_dir / "camera_identities.json"
    assert app.TRIGGER_FILE == data_dir / "trigger_download"
    assert library._TRIGGER_FILE == app.TRIGGER_FILE


def test_standalone_server_redirects_every_data_path(tmp_path: Path) -> None:
    """The e2e backend runs beside pytest, so a /data file it writes is one a
    test can read. Run in a subprocess: the redirect rebinds the package's
    constants for good, and in this process they already point at data_dir."""
    probe = textwrap.dedent(
        f"""
        import runpy
        from pathlib import Path

        from tests.conftest import attribute_owners, is_data_path, package_modules

        script = runpy.run_path(
            {str(_PROJECT_ROOT / "scripts" / "standalone_server.py")!r},
            run_name="standalone_server",
        )
        script["_redirect_data_files"](Path({str(tmp_path)!r}))
        for module in package_modules():
            for owner in attribute_owners(module):
                for name, value in vars(owner).items():
                    if is_data_path(value):
                        print(f"{{module.__name__}}: {{name}} = {{value!r}}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", (
        "scripts/standalone_server.py's _redirect_data_files left these "
        "pointing at /data:\n" + result.stdout
    )
