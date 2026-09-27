"""Fail when a built wheel or sdist leaves out a file of the package's source tree.

Usage (from the repository root)::

    python docker/check_dist.py [--package src/cca] DIST [DIST ...]

DIST is a wheel (``*.whl``) or an sdist (``*.tar.gz``). hatchling selects files with the root
``.gitignore``, so an ignore rule can silently drop a file that the package needs at run time:
the root rule ``dist/`` once dropped ``cca/play/static/vendor/chess.js/dist/esm/chess.js`` and
the web simulator's board could not load. Every file under ``--package`` (except
``__pycache__`` and compiled bytecode) must therefore be in every DIST: in a wheel under the
package's own name (``cca/...``), in an sdist under its top directory (``cca_chess-X/src/cca/...``).

Standard library only: the Dockerfile's build stage and release.yml run it with a bare Python.
Exit status 0 when nothing is missing, 1 otherwise, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from collections.abc import Sequence

SKIPPED_DIRS = frozenset({"__pycache__"})
SKIPPED_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_LISTED = 30


class DistError(Exception):
    """A distribution cannot be read or is of an unknown kind."""


def source_files(package: Path) -> set[str]:
    """Files under ``package``, as POSIX paths relative to the directory that holds it."""
    found: set[str] = set()
    for path in package.rglob("*"):
        rel = path.relative_to(package.parent)
        if SKIPPED_DIRS.intersection(rel.parts) or path.suffix in SKIPPED_SUFFIXES:
            continue
        if path.is_file():
            found.add(rel.as_posix())
    return found


def dist_files(dist: Path, package: Path) -> set[str]:
    """Members of ``dist`` that belong to ``package``, in :func:`source_files` form."""
    if dist.name.endswith(".whl"):
        try:
            with zipfile.ZipFile(dist) as wheel:
                return {name for name in wheel.namelist() if not name.endswith("/")}
        except (OSError, zipfile.BadZipFile) as exc:
            raise DistError(f"{dist}: not a readable wheel ({exc})") from exc
    if dist.name.endswith(".tar.gz"):
        # sdist members are <top>/<path from the project root>: strip <top> and the package's
        # parent directory (src/), keep what lies under the package.
        prefix = PurePosixPath(*package.parent.parts) if package.parent.parts else None
        try:
            with tarfile.open(dist, "r:gz") as sdist:
                members = [m.name for m in sdist.getmembers() if m.isfile()]
        except (OSError, tarfile.TarError) as exc:
            raise DistError(f"{dist}: not a readable sdist ({exc})") from exc
        found: set[str] = set()
        for name in members:
            rel = PurePosixPath(*PurePosixPath(name).parts[1:])
            if prefix is not None:
                if not rel.is_relative_to(prefix):
                    continue
                rel = rel.relative_to(prefix)
            found.add(rel.as_posix())
        return found
    raise DistError(f"{dist}: neither a wheel (*.whl) nor an sdist (*.tar.gz)")


def missing_files(dist: Path, package: Path, expected: set[str]) -> list[str]:
    """Files of ``expected`` that ``dist`` does not contain, sorted."""
    return sorted(expected - dist_files(dist, package))


def check(dists: Sequence[Path], package: Path, out: TextIO) -> int:
    """Report on each distribution; return the exit status."""
    expected = source_files(package)
    if not expected:
        print(f"no files under {package.as_posix()}: wrong --package?", file=out)
        return 2
    status = 0
    where = package.as_posix()
    for dist in dists:
        try:
            missing = missing_files(dist, package, expected)
        except DistError as exc:
            print(f"FAIL {exc}", file=out)
            status = 1
            continue
        if not missing:
            print(f"ok   {dist.name}: all {len(expected)} files under {where}", file=out)
            continue
        status = 1
        print(
            f"FAIL {dist.name} leaves out {len(missing)} of the {len(expected)} files under "
            f"{where} (an ignore rule in .gitignore, or the build config):",
            file=out,
        )
        for rel in missing[:MAX_LISTED]:
            print(f"  {rel}", file=out)
        if len(missing) > MAX_LISTED:
            print(f"  ... and {len(missing) - MAX_LISTED} more", file=out)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(
        prog="check_dist.py", description="Check that built distributions hold the whole package."
    )
    ap.add_argument(
        "--package",
        type=Path,
        default=Path("src/cca"),
        help="package directory, relative to the project root (default: src/cca)",
    )
    ap.add_argument("dists", nargs="+", type=Path, metavar="DIST", help="wheel or sdist to check")
    ns = ap.parse_args(argv)
    if ns.package.is_absolute():
        ap.error("--package must be relative to the project root (the current directory)")
    return check(ns.dists, ns.package, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
