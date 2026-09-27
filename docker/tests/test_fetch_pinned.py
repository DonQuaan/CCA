"""Tests for docker/fetch_pinned.py with a scripted opener (no network)."""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.client import HTTPMessage
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import fetch_pinned
import pytest
from fetch_pinned import FetchError

if TYPE_CHECKING:
    from collections.abc import Iterator

URL = "https://example.org/tini-amd64"
BODY = b"\x7fELF" + b"tini" * 5000
PIN = hashlib.sha256(BODY).hexdigest()


class Broken(io.BytesIO):
    """A response that dies after its first chunk."""

    def read(self, size: int | None = -1) -> bytes:
        if self.tell():
            raise ConnectionResetError("connection reset by peer")
        return super().read(size)


class Opener:
    """Scripted responses: bytes are served, exceptions raised, one per call."""

    def __init__(self, *answers: bytes | Exception | BinaryIO) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> BinaryIO:
        self.calls.append((url, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer) if isinstance(answer, bytes) else answer


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "error", Message(), io.BytesIO())


def test_a_matching_download_lands_with_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[tuple[str, int]] = []
    real_chmod = Path.chmod

    def spy(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        modes.append((self.name, mode))
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", spy)
    dest = tmp_path / "out" / "bin" / "tini"
    opener = Opener(BODY)
    fetch_pinned.fetch(URL, PIN, dest, mode=0o755, opener=opener)
    assert dest.read_bytes() == BODY
    assert modes == [("tini.part", 0o755)]
    assert opener.calls == [(URL, fetch_pinned.TIMEOUT_S)]
    assert sorted(p.name for p in dest.parent.iterdir()) == ["tini"]


def test_a_mismatch_leaves_nothing_behind_and_keeps_an_old_file(tmp_path: Path) -> None:
    dest = tmp_path / "tini"
    dest.write_bytes(b"old")
    with pytest.raises(FetchError, match=f"SHA-256 is {hashlib.sha256(b'evil').hexdigest()}"):
        fetch_pinned.fetch(URL, PIN, dest, opener=Opener(b"evil"))
    assert dest.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tini"]


@pytest.mark.parametrize(
    ("url", "pin", "problem"),
    [
        ("http://example.org/tini", PIN, "only https:// URLs"),
        ("file:///etc/passwd", PIN, "only https:// URLs"),
        (URL, PIN.upper(), "is not a lower-case hex SHA-256"),
        (URL, PIN[:-1], "is not a lower-case hex SHA-256"),
    ],
)
def test_bad_arguments_are_refused_before_any_download(
    tmp_path: Path, url: str, pin: str, problem: str
) -> None:
    opener = Opener()
    with pytest.raises(FetchError, match=problem):
        fetch_pinned.fetch(url, pin, tmp_path / "x", opener=opener)
    assert opener.calls == []
    assert list(tmp_path.iterdir()) == []


def test_transient_errors_are_retried_from_scratch(tmp_path: Path) -> None:
    sleeps: list[float] = []
    opener = Opener(
        urllib.error.URLError("temporary failure in name resolution"),
        Broken(BODY),  # half a file first: the retry must not append to it
        BODY,
    )
    fetch_pinned.fetch(URL, PIN, tmp_path / "tini", opener=opener, sleep=sleeps.append)
    assert (tmp_path / "tini").read_bytes() == BODY
    assert sleeps == [2.0, 4.0]


def test_server_errors_are_retried_but_client_errors_are_not(tmp_path: Path) -> None:
    fetch_pinned.fetch(
        URL, PIN, tmp_path / "a", opener=Opener(http_error(503), BODY), sleep=lambda _s: None
    )
    opener = Opener(http_error(404), BODY)
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_pinned.fetch(URL, PIN, tmp_path / "b", opener=opener, sleep=lambda _s: None)
    assert len(opener.calls) == 1


def test_persistent_errors_give_up(tmp_path: Path) -> None:
    errors = [TimeoutError("timed out") for _ in range(fetch_pinned.ATTEMPTS)]
    with pytest.raises(FetchError, match="timed out \\(after 3 attempts\\)"):
        fetch_pinned.fetch(URL, PIN, tmp_path / "t", opener=Opener(*errors), sleep=lambda _s: None)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def local_server() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/to/"):  # /to/<url>: a redirect to <url>
                self.send_response(302)
                self.send_header("Location", self.path.removeprefix("/to/"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, format: str, *args: object) -> None:
            """Keep test output quiet."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/tini"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_default_opener_reads_the_url(local_server: str) -> None:
    with fetch_pinned._open(local_server, 10.0) as response:
        assert response.read() == BODY


def test_default_opener_refuses_a_redirect_that_leaves_https(local_server: str) -> None:
    # fetch() only starts from https://, which this local server cannot offer; the redirect
    # handler is what matters here: any hop to a non-https URL stops the download.
    with pytest.raises(FetchError, match=r"refusing the redirect from .* to 'http://127\.0\.0\.1"):
        fetch_pinned._open(local_server.replace("/tini", "/to/" + local_server), 10.0)


@pytest.mark.parametrize(
    ("newurl", "allowed"),
    [
        ("https://objects.example.org/tini-amd64?sig=1", True),
        ("HTTPS://objects.example.org/tini-amd64", True),
        ("http://objects.example.org/tini-amd64", False),
        ("ftp://objects.example.org/tini-amd64", False),
    ],
)
def test_redirects_stay_on_https(newurl: str, allowed: bool) -> None:
    handler = fetch_pinned._HttpsOnlyRedirects()
    req = urllib.request.Request(URL)
    if allowed:
        new = handler.redirect_request(req, io.BytesIO(), 302, "Found", HTTPMessage(), newurl)
        assert new is not None
        assert new.full_url == newurl
    else:
        with pytest.raises(FetchError, match="refusing the redirect"):
            handler.redirect_request(req, io.BytesIO(), 302, "Found", HTTPMessage(), newurl)


def test_a_refused_redirect_fails_the_download_at_once(tmp_path: Path) -> None:
    refused = FetchError(f"refusing the redirect from {URL} to 'http://example.org/x'")
    opener = Opener(refused, BODY)
    with pytest.raises(FetchError, match="refusing the redirect"):
        fetch_pinned.fetch(URL, PIN, tmp_path / "tini", opener=opener, sleep=lambda _s: None)
    assert len(opener.calls) == 1  # not retried
    assert list(tmp_path.iterdir()) == []


def test_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_open(url: str, timeout: float) -> BinaryIO:
        calls.append((url, timeout))
        return io.BytesIO(BODY)

    monkeypatch.setattr(fetch_pinned, "_open", fake_open)
    dest = tmp_path / "tini"
    assert fetch_pinned.main([URL, PIN, str(dest), "--mode", "755"]) == 0
    assert dest.read_bytes() == BODY
    assert capsys.readouterr().out.strip() == f"{dest}: {PIN}"
    assert fetch_pinned.main([URL, "0" * 64, str(tmp_path / "bad")]) == 1
    assert "fetch_pinned: https://example.org/tini-amd64: SHA-256 is" in capsys.readouterr().err
    assert len(calls) == 2
    with pytest.raises(SystemExit):
        fetch_pinned.main([URL, PIN, str(dest), "--mode", "9z"])
