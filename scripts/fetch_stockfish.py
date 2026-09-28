"""Download an official Stockfish release into ./engines, verified against a pinned SHA-256.

Usage::

    python scripts/fetch_stockfish.py                 # sf_19 for this OS (x86-64, universal)
    python scripts/fetch_stockfish.py --os linux      # another platform
    python scripts/fetch_stockfish.py --list          # list release assets (GitHub API)

Stockfish 19 ships only *universal* binaries: one executable that picks the best build for the
CPU at run time (x86-64, sse41-popcnt, avx2, bmi2, avxvnni, avx512, vnni512, avx512icl).

Integrity: ``engines/stockfish.lock.json`` (committed) pins the SHA-256 of each asset; the
values were cross-checked against GitHub's own asset digests. A download whose hash differs
from the pin is deleted and the script aborts. For an asset without a pin, GitHub's API digest
is required and then pinned. Stockfish is GPL-3.0-or-later and is *downloaded*, never
committed or bundled; the official archive already contains its licence and full source.

Set ``GITHUB_TOKEN`` to lift the anonymous API rate limit (only needed for unpinned assets).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com/repos/official-stockfish/Stockfish/releases/tags/{tag}"
DOWNLOAD = "https://github.com/official-stockfish/Stockfish/releases/download/{tag}/{name}"
ROOT = Path(__file__).resolve().parent.parent
ENGINES = ROOT / "engines"
LOCK = ENGINES / "stockfish.lock.json"
ALLOWED_PREFIXES = ("https://github.com/", "https://api.github.com/")


def _request(url: str) -> urllib.request.Request:
    if not url.startswith(ALLOWED_PREFIXES):
        raise SystemExit(f"refusing non-GitHub URL: {url}")
    headers = {"User-Agent": "cca-fetch-stockfish", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    # Only the API needs the token; release downloads redirect to other hosts and urllib
    # would forward the Authorization header there.
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)  # noqa: S310 - prefix checked above


def api_assets(tag: str) -> list[dict[str, object]]:
    req = _request(API.format(tag=tag))  # GitHub-only: prefix checked in _request
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - checked URL
        data = json.load(resp)
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise SystemExit("unexpected API response (rate limited? set GITHUB_TOKEN)")
    return assets


def default_os() -> str:
    return {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
        platform.system(), "linux"
    )


def asset_name(os_name: str, arch: str, flavour: str) -> str:
    if os_name == "macos":
        return f"stockfish-macos-{flavour}.tar.gz"
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"stockfish-{os_name}-{arch}-{flavour}.{ext}"


def expected_digest(tag: str, name: str, lock: dict[str, str]) -> str:
    key = f"{tag}/{name}"
    if key in lock:
        return lock[key]
    print(f"{key} is not pinned; asking the GitHub API for its digest")
    for a in api_assets(tag):
        if a["name"] == name:
            digest = str(a.get("digest") or "").removeprefix("sha256:")
            if not digest:
                raise SystemExit(
                    "no published digest for this asset; refusing an unverified binary"
                )
            return digest
    raise SystemExit(f"release {tag} has no asset {name} (try --list)")


def download_verified(url: str, target: Path, expected: str) -> None:
    sha = hashlib.sha256()
    req = _request(url)
    with (
        urllib.request.urlopen(req, timeout=300) as resp,  # noqa: S310 - checked URL
        target.open("wb") as fh,
    ):
        while chunk := resp.read(1 << 20):
            sha.update(chunk)
            fh.write(chunk)
    if sha.hexdigest() != expected:
        target.unlink()
        raise SystemExit(f"SHA-256 mismatch: got {sha.hexdigest()} expected {expected}")
    print(f"sha256 ok {expected}")


def extract(target: Path) -> Path:
    name = target.name
    dest = ENGINES / name.removesuffix(".zip").removesuffix(".tar.gz")
    if name.endswith(".zip"):
        with zipfile.ZipFile(target) as zf:
            for member in zf.namelist():  # zip-slip guard
                if not (dest / member).resolve().is_relative_to(dest.resolve()):
                    raise SystemExit(f"unsafe path in archive: {member}")
            zf.extractall(dest)  # noqa: S202 - every member checked above
    else:
        with tarfile.open(target) as tf:
            # filter="data" rejects absolute paths, ".." members and links leaving dest
            tf.extractall(dest, filter="data")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tag", default="sf_19")
    ap.add_argument("--os", default=default_os(), choices=["windows", "linux", "macos"])
    ap.add_argument("--arch", default="x86-64", help="x86-64 | arm64 (ignored on macos)")
    ap.add_argument("--flavour", default="universal")
    ap.add_argument("--list", action="store_true", help="list assets via the GitHub API")
    args = ap.parse_args()

    if args.list:
        for a in api_assets(args.tag):
            print(f"{a['name']:<50} {int(str(a['size'])) / 1e6:7.1f} MB  {a.get('digest') or ''}")
        return 0

    name = asset_name(args.os, args.arch, args.flavour)
    lock: dict[str, str] = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.exists() else {}
    expected = expected_digest(args.tag, name, lock)

    ENGINES.mkdir(exist_ok=True)
    target = ENGINES / name
    url = DOWNLOAD.format(tag=args.tag, name=name)
    print(f"downloading {name} from {url}")
    download_verified(url, target, expected)
    dest = extract(target)
    binaries = sorted(
        p for p in dest.rglob("stockfish-*") if p.is_file() and p.suffix in {"", ".exe"}
    )
    print(f"extracted to {dest}")
    for b in binaries:
        print(f"binary: {b}")
    lock[f"{args.tag}/{name}"] = expected
    # newline="\n": the lock is committed; Windows text mode would otherwise write CRLF.
    LOCK.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print("next: run `cca doctor` (a source checkout finds engines/ by itself)")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
