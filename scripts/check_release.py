"""Release gate: a tag ``vX.Y.Z[{a|b|rc}N]`` must equal the package version and be in CHANGELOG.

Usage::

    python scripts/check_release.py v0.1.0            # exit 1 on any mismatch
    python scripts/check_release.py v0.1.0 --notes    # print that version's CHANGELOG section
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^v(?P<v>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)((a|b|rc)(0|[1-9]\d*))?)$")


def package_version() -> str:
    text = (ROOT / "src" / "cca" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if not m:
        raise SystemExit("__version__ not found")
    return m.group(1)


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not m:
        raise SystemExit(f"CHANGELOG.md has no '## [{version}]' section")
    return m.group(1).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--notes", action="store_true")
    args = ap.parse_args()
    m = TAG.match(args.tag)
    if not m:
        raise SystemExit(f"tag {args.tag!r} is not vMAJOR.MINOR.PATCH[aN|bN|rcN]")
    version = m.group("v")
    if version != package_version():
        raise SystemExit(f"tag {version} != package version {package_version()}")
    notes = changelog_section(version)
    if args.notes:
        print(notes)
    else:
        print(f"release {version} ok")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    raise SystemExit(main())
