"""Download a file over HTTPS and keep it only if its SHA-256 is the pinned one.

Usage (Dockerfile build stages)::

    python fetch_pinned.py URL SHA256 DEST [--mode 755]

The file is written next to DEST under a temporary name and renamed into place only after its
SHA-256 matched, so DEST never holds unchecked bytes. Only https:// URLs are fetched, and a
redirect to any other scheme is refused. Transient network errors are retried; a checksum
mismatch is not. Standard library only (the base image's Python runs it).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import IO, TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from http.client import HTTPMessage

    Opener = Callable[[str, float], AbstractContextManager[BinaryIO]]

SHA256 = re.compile(r"[0-9a-f]{64}")
ATTEMPTS = 3
TIMEOUT_S = 60.0
CHUNK = 1 << 16


class FetchError(Exception):
    """The download failed or did not match its pin."""


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects like urllib does, except to a URL that is not https://.

    urllib alone would follow an https:// URL's redirect to http://. The SHA-256 pin still guards
    the bytes, but nothing needs a plain-text hop, so it is refused (and not retried).
    """

    def redirect_request(  # noqa: PLR0917 - urllib's HTTPRedirectHandler fixes this signature
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise FetchError(f"refusing the redirect from {req.full_url} to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float) -> AbstractContextManager[BinaryIO]:
    """Open ``url``; :func:`fetch` hands it https:// URLs only, redirects stay on https://."""
    response: AbstractContextManager[BinaryIO]
    response = urllib.request.build_opener(_HttpsOnlyRedirects()).open(url, timeout=timeout)
    return response


def fetch(
    url: str,
    sha256: str,
    dest: Path,
    *,
    mode: int = 0o644,
    opener: Opener | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download ``url`` to ``dest`` if and only if its SHA-256 equals ``sha256``."""
    if not SHA256.fullmatch(sha256):
        raise FetchError(f"{sha256!r} is not a lower-case hex SHA-256")
    if not url.startswith("https://"):
        raise FetchError(f"refusing {url!r}: only https:// URLs are fetched")
    opener = opener or _open
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        for attempt in range(1, ATTEMPTS + 1):
            digest = hashlib.sha256()
            try:
                with opener(url, TIMEOUT_S) as response, part.open("wb") as out:
                    while chunk := response.read(CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
                break
            except (urllib.error.URLError, OSError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    raise FetchError(f"{url}: HTTP {exc.code}") from exc
                if attempt == ATTEMPTS:
                    raise FetchError(f"{url}: {exc} (after {ATTEMPTS} attempts)") from exc
                sleep(2.0 * attempt)
        got = digest.hexdigest()
        if got != sha256:
            raise FetchError(f"{url}: SHA-256 is {got}, the pin is {sha256}")
        part.chmod(mode)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(
        prog="fetch_pinned.py",
        description="Download a file and keep it only if its SHA-256 matches.",
    )
    ap.add_argument("url")
    ap.add_argument("sha256")
    ap.add_argument("dest", type=Path)
    ap.add_argument("--mode", type=lambda text: int(text, 8), default=0o644, help="octal")
    ns = ap.parse_args(argv)
    try:
        fetch(ns.url, ns.sha256, ns.dest, mode=ns.mode)
    except FetchError as exc:
        print(f"fetch_pinned: {exc}", file=sys.stderr)
        return 1
    print(f"{ns.dest}: {ns.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
