"""Every exemption from a quality check is narrow, explained and capped.

Raising a cap below is allowed, but it has to be a visible, reviewed change to this file.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_SUPPRESSIONS = 18  # src/ + scripts/, counted 2026-09-26
SUPPRESSION = re.compile(r"#\s*(noqa|type:\s*ignore)\b(?P<rest>.*)$")


def _suppressions() -> list[tuple[str, int, str]]:
    files = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").glob("*.py")])
    return [
        (path.relative_to(ROOT).as_posix(), n, m.group(0))
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for m in SUPPRESSION.finditer(line)
    ]


def test_suppressions_are_capped() -> None:
    found = _suppressions()
    assert len(found) <= MAX_SUPPRESSIONS, "\n".join(f"{p}:{n}: {s}" for p, n, s in found)


def test_every_suppression_is_narrow_and_explained() -> None:
    for path, n, text in _suppressions():
        where = f"{path}:{n}: {text}"
        if text.lstrip("# ").startswith("noqa"):
            assert re.match(r"#\s*noqa: [A-Z]+[0-9]+(, [A-Z]+[0-9]+)* - \S", text), where
        else:
            assert re.match(r"#\s*type:\s*ignore\[[a-z-]+(,[a-z-]+)*\]", text), where
            assert " - " in text, f"{where}: say why on the same line"


def test_file_level_exemptions_are_the_declared_ones() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]
    in_src = {k: v for k, v in ignores.items() if k.startswith("src/")}
    assert in_src == {"src/cca/cli.py": ["PLC0415"]}  # lazy CLI dispatch; everything else per line
    pytest_cfg = cfg["tool"]["pytest"]["ini_options"]
    assert pytest_cfg["filterwarnings"] == [
        "error",
        "ignore:Call to deprecated method findChildren:DeprecationWarning",
    ]
    assert cfg["tool"]["coverage"]["run"]["omit"] == ["*/cca/engines/maia2_human.py"]
