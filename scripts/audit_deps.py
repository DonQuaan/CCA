"""Audit EVERY version pinned in uv.lock, whatever its environment markers say.

``pip-audit -r`` evaluates each requirement's marker in the auditing interpreter and silently
skips the ones that do not match (e.g. ``colorama ; sys_platform == 'win32'`` on Linux, or the
``numpy`` pin used only on Python 3.11). This script exports the universal lock, strips the
markers, splits pins that conflict (same name, different version) into separate files, and
runs ``pip-audit --no-deps --disable-pip`` on each file. Exit code 1 if anything is vulnerable.

Usage::

    uv run python scripts/audit_deps.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"  # keep pip-audit's cache and temp files inside the repo (gitignored)

# Accepted advisories. Every entry needs a reason tied to how CCA uses the package and an
# expiry date; an expired entry fails the audit so the decision is re-made, not forgotten.
# All seven are in torch 2.8.0, pulled only by the optional `maia2` extra (maia2 0.11 caps
# torch < 2.9). Assessed 2026-09-26 against the OSV records.
REVIEW_BY = date(2027, 3, 31)
ACCEPTED = {
    "PYSEC-2026-2286": "CVE-2026-24747 weights_only unpickler: mitigated, CCA verifies the pinned "
    "SHA-256 of the official Maia-2 checkpoint before torch loads it (and maia2 verifies again)",
    "PYSEC-2026-139": "CVE-2026-4538 pt2 loading handler: CCA never loads pt2/torch.export files",
    "PYSEC-2025-193": "CVE-2025-2999 rnn.unpack_sequence: not used by Maia-2 (CNN + ViT)",
    "PYSEC-2025-194": "CVE-2025-3000 torch.jit.script: CCA does not script models",
    "PYSEC-2025-195": "CVE-2025-3001 torch.lstm_cell: Maia-2 has no LSTM",
    "PYSEC-2025-203": "CVE-2025-55551 linalg.lu DoS: not used by Maia-2",
    "PYSEC-2025-204": "CVE-2025-55552 rot90 + randn_like: not used by Maia-2",
    "PYSEC-2025-206": "CVE-2025-55554 nan_to_num().long() overflow: not used by Maia-2 or CCA",
}
# The "not used" claims were checked by grepping maia2 0.11.0 and src/cca for every affected
# API (unpack_sequence, jit.script, lstm, linalg.lu, rot90, randn_like, nan_to_num): no hits.


def exported_pins() -> list[Requirement]:
    out = subprocess.run(
        [  # noqa: S607 - fixed argument list
            "uv",
            "export",
            "--frozen",
            "--all-extras",
            "--all-groups",
            "--no-emit-project",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    pins = []
    for raw in out.splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "-")):
            pins.append(Requirement(line))
    return pins


def split_forks(pins: list[Requirement]) -> list[dict[str, str]]:
    """Distribute pins over files so that no file names a package twice."""
    files: list[dict[str, str]] = []
    for req in pins:
        pin = f"{req.name}{req.specifier}"
        for f in files:
            if f.get(req.name, pin) == pin:
                f[req.name] = pin
                break
        else:
            files.append({req.name: pin})
    return files


def main() -> int:
    if date.today() > REVIEW_BY:
        print(
            f"accepted-advisory list expired on {REVIEW_BY}: re-assess every entry", file=sys.stderr
        )
        return 1
    ignore = [arg for vuln in ACCEPTED for arg in ("--ignore-vuln", vuln)]
    pins = exported_pins()
    files = split_forks(pins)
    print(f"{len(pins)} locked pins -> {len(files)} audit file(s)")
    worst = 0
    CACHE.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        for i, f in enumerate(files):
            path = Path(tmp) / f"requirements-{i}.txt"
            path.write_text("\n".join(sorted(f.values())) + "\n", encoding="utf-8")
            code = subprocess.run(  # noqa: S603 - fixed argument list, no shell
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "-r",
                    str(path),
                    "--no-deps",
                    "--disable-pip",
                    "--cache-dir",
                    str(CACHE / "pip-audit"),
                    "--strict",
                    "--progress-spinner",
                    "off",
                    *ignore,
                ],
                check=False,
            ).returncode
            worst = max(worst, code)
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main())
