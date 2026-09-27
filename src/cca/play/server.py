"""HTTP layer of ``cca play`` (standard library ``http.server``), with its security guards.

* Binds loopback by default; any other address gets a loud warning (no authentication).
* Every bind checks the ``Host`` header (421 otherwise), which defeats DNS rebinding (a hostile
  page resolving its own name to this server always carries the attacker's name). A loopback
  bind answers only ``localhost``, ``127.0.0.1``, ``[::1]`` and its own address; any other bind
  (0.0.0.0 inside a container, a LAN address) also answers IP literals and this machine's name.
* POST: foreign ``Origin`` or ``Sec-Fetch-Site: cross-site`` -> 403; a content type other than
  ``application/json`` -> 415 (so no form or "simple" cross-site request can reach the API);
  bodies above 64 KiB -> 413.
* Static files come only from the package's ``static`` directory, through importlib.resources,
  with strict path segments and a whitelist of extensions with explicit MIME types.
* Every response carries a strict Content-Security-Policy and ``nosniff``; errors are JSON,
  never HTML, and raw input is never echoed into markup.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import signal
import socket
import socketserver
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, TextIO
from urllib.parse import unquote, urlsplit

from cca.play.game import ApiError
from cca.play.serialize import dumps, loads_object

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from cca.play.app import PlayApp

MAX_BODY = 64 * 1024
_MAX_DRAIN = 1024 * 1024
# 'unsafe-inline' is limited to style *attributes*: cm-chessboard positions the dragged piece
# with setAttribute("style") and the Cburnett sprite uses style="" attributes (checked in a
# browser: with style-src 'self' alone both are refused). <style> elements stay forbidden.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}
# Licence files of vendored packages have no extension; they are served as plain text.
_PLAIN_NAMES = frozenset({"LICENSE"})
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
_HOST = re.compile(r"(?P<name>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?\Z")
_GAME = re.compile(
    r"/api/games/(?P<id>[A-Za-z0-9_-]{16,64})(?:/(?P<action>move|think|undo|resign|pgn|decisions))?\Z"
)
_POST_ACTIONS = frozenset({"move", "think", "undo", "resign"})


def static_file(path: str) -> tuple[bytes, str] | None:
    """Bytes and MIME type of ``static/<path>``, or ``None`` if not servable."""
    segments = path.split("/")
    if not all(_SEGMENT.match(s) for s in segments):
        return None  # empty, dot-leading ("..", ".x"), backslash, colon, %, NUL ...
    name = segments[-1]
    ctype = "text/plain; charset=utf-8" if name in _PLAIN_NAMES else None
    if "." in name:
        ctype = STATIC_TYPES.get(name[name.rindex(".") :].lower())
    if ctype is None:
        return None
    node = resources.files("cca.play") / "static"
    for segment in segments:
        node = node / segment
    if not node.is_file():
        return None
    return node.read_bytes(), ctype


def host_allowed(host: str, names: frozenset[str], *, any_ip: bool) -> bool:
    """Whether a ``Host`` header (any port) is one that DNS rebinding cannot produce.

    A rebinding page reaches this server under a DNS name its author controls. ``names`` (lower
    case, IPv6 in brackets as browsers send it) are always accepted; with ``any_ip`` every IP
    literal is accepted too (a non-loopback bind, reached by LAN or container address).
    """
    match = _HOST.match(host.strip())
    if match is None:
        return False
    name = match["name"].lower()
    if name in names:
        return True
    if not any_ip:
        return False
    try:
        ipaddress.ip_address(name.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return True


def host_literal(address: str) -> str:
    """``address`` as it appears in a ``Host`` header (IPv6 in brackets), lower case."""
    return f"[{address.lower()}]" if ":" in address else address.lower()


def is_loopback(host: str) -> bool:
    """Whether ``host`` (a bound address or name) is a loopback address."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class PlayHandler(BaseHTTPRequestHandler):
    """Routes requests to :class:`~cca.play.app.PlayApp` and applies the guards."""

    server_version = "cca-play"
    sys_version = ""
    timeout = 30

    # ------------------------------------------------------------------ plumbing
    @property
    def play_server(self) -> PlayServer:
        """The server this handler belongs to."""
        server = self.server
        if not isinstance(server, PlayServer):  # pragma: no cover - wiring error
            raise TypeError("PlayHandler needs a PlayServer")
        return server

    @property
    def app(self) -> PlayApp:
        """The application of the server this handler belongs to."""
        return self.play_server.app

    def version_string(self) -> str:
        """``Server`` header without the Python version."""
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: the simulator does not log every request."""

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str,
        *,
        cache: str = "no-store",
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"  # 0.9 has no headers: always send ours
        self.send_response(status)
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: object, extra: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(status, dumps(payload), "application/json", extra=extra)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Errors raised by the stdlib parser are JSON too (never HTML echoing the input)."""
        del explain
        self.close_connection = True
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "error"
        self._json(code, {"error": message or phrase})

    # ------------------------------------------------------------------ guards
    def _check_request(self) -> None:
        if self.request_version == "HTTP/0.9":
            raise ApiError(400, "HTTP/0.9 requests are not supported")
        server = self.play_server
        if not server.host_ok(self.headers.get("Host", "")):
            raise ApiError(421, f"unknown Host: use {server.host_hint} (DNS-rebinding guard)")

    def _same_origin(self, origin: str) -> bool:
        host = self.headers.get("Host", "").strip().lower()
        return bool(host) and origin.strip().lower() in {f"http://{host}", f"https://{host}"}

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return -1

    def _drain(self) -> None:
        """Discard an unread request body so the client can read our error response."""
        length = self._content_length()
        if length is None or length <= 0:
            return
        remaining = min(length, _MAX_DRAIN)
        try:
            self.connection.settimeout(1.0)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            return

    def _read_json(self) -> dict[str, object]:
        origin = self.headers.get("Origin")
        if origin is not None and not self._same_origin(origin):
            raise ApiError(403, "cross-origin request refused")
        if self.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
            raise ApiError(403, "cross-site request refused")
        media = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        if "Transfer-Encoding" in self.headers:
            raise ApiError(411, "send the body with a Content-Length (no chunked encoding)")
        length = self._content_length()
        if length is None:
            raise ApiError(411, "Content-Length required")
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise ApiError(413, f"request body larger than {MAX_BODY} bytes")
        self._body_read = True  # from here on nothing is left to drain
        try:
            raw = self.rfile.read(length)
        except TimeoutError:  # fewer bytes than Content-Length announced, then silence
            self.close_connection = True
            raise ApiError(408, "timed out waiting for the request body") from None
        if len(raw) != length:
            raise ApiError(400, "truncated request body")
        try:
            return loads_object(raw)
        except RecursionError:
            raise ApiError(400, "invalid JSON: nested too deeply") from None
        except (ValueError, TypeError) as exc:  # also UnicodeDecodeError, JSONDecodeError
            raise ApiError(400, f"invalid JSON: {exc}") from None

    # ------------------------------------------------------------------ dispatch
    def do_GET(self) -> None:
        """GET (static files and read-only API)."""
        self._handle("GET")

    def do_POST(self) -> None:
        """POST (API actions; JSON bodies only)."""
        self._handle("POST")

    def _handle(self, method: str) -> None:
        self._body_read = False
        try:
            self._check_request()
            path = unquote(urlsplit(self.path).path)
            body = self._read_json() if method == "POST" else {}
            self._route(method, path, body)
        except ApiError as exc:
            if method == "POST" and not self._body_read:
                self._drain()
            payload: dict[str, object] = {"error": exc.message}
            if exc.state is not None:
                payload["state"] = exc.state
            allow = (("Allow", exc.allow),) if isinstance(exc, _MethodNotAllowedError) else ()
            self._json(exc.status, payload, allow)
        except ConnectionError:
            return  # the client went away (e.g. closed the tab during a decision)
        except Exception:  # never leak a traceback to the client; keep it on stderr
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": "internal server error (see the server log)"})

    def _route(self, method: str, path: str, body: dict[str, object]) -> None:
        app = self.app
        if path in {"/", "/index.html"}:
            self._expect(method, "GET")
            self._static("index.html")
        elif path.startswith("/static/"):
            self._expect(method, "GET")
            self._static(path.removeprefix("/static/"))
        elif path == "/healthz":
            self._expect(method, "GET")
            status, payload = app.health()
            self._json(status, payload)
        elif path == "/api/info":
            self._expect(method, "GET")
            self._json(200, app.info())
        elif path == "/api/games":
            self._expect(method, "POST")
            self._json(201, app.create_game(body))
        elif path == "/api/analyse":
            self._expect(method, "POST")
            self._json(200, app.analyse(body))
        elif match := _GAME.match(path):
            self._game(method, match["id"], match["action"], body)
        else:
            raise ApiError(404, "not found")

    def _game(self, method: str, game_id: str, action: str | None, body: dict[str, object]) -> None:
        app = self.app
        self._expect(method, "POST" if action in _POST_ACTIONS else "GET")
        if action is None:
            self._json(200, app.game_state(game_id))
        elif action == "move":
            self._json(200, app.move(game_id, body))
        elif action == "think":
            self._json(200, app.think(game_id))
        elif action == "undo":
            self._json(200, app.undo(game_id))
        elif action == "resign":
            self._json(200, app.resign(game_id))
        elif action == "pgn":
            self._send(200, app.pgn(game_id).encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self._json(200, app.decisions(game_id))

    @staticmethod
    def _expect(method: str, allowed: str) -> None:
        if method != allowed:
            raise _MethodNotAllowedError(allowed)

    def _static(self, path: str) -> None:
        found = static_file(path)
        if found is None:
            raise ApiError(404, "not found")
        data, ctype = found
        self._send(200, data, ctype, cache="no-cache")


class _MethodNotAllowedError(ApiError):
    def __init__(self, allow: str) -> None:
        super().__init__(405, f"method not allowed (use {allow})")
        self.allow = allow


class PlayServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the application."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets another process bind the same port: keep it off there.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], app: PlayApp) -> None:
        self.app = app
        super().__init__(address, PlayHandler)
        bound = str(self.server_address[0])
        self.loopback = is_loopback(bound)
        # Host names a loopback bind answers: the three of the contract, plus the bound address
        # itself (--host 127.0.0.2). Nothing else, not even other IP literals.
        self.loopback_names = frozenset({"localhost", "127.0.0.1", "[::1]", host_literal(bound)})
        # A non-loopback bind (LAN, container) also answers any IP literal and this machine's
        # host name (a container's own name).
        self.host_names = frozenset(n for n in ("localhost", socket.gethostname().lower()) if n)

    def host_ok(self, host: str) -> bool:
        """The DNS-rebinding guard: whether this server answers ``Host: host``."""
        if self.loopback:
            return host_allowed(host, self.loopback_names, any_ip=False)
        return host_allowed(host, self.host_names, any_ip=True)

    @property
    def host_hint(self) -> str:
        """The Host names this server answers, for the 421 error message."""
        if self.loopback:
            return "localhost, 127.0.0.1 or [::1]"
        return "localhost, an IP address or this machine's name"

    def server_bind(self) -> None:
        """Bind without HTTPServer's reverse-DNS lookup of the host (slow, and not needed)."""
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Ignore clients that disconnect; report anything else."""
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class PlayServer6(PlayServer):
    """IPv6 variant (for ``--host ::1`` and the like)."""

    address_family = socket.AF_INET6


def make_server(host: str, port: int, app: PlayApp) -> PlayServer:
    """Bind a server (IPv6 when ``host`` contains a colon)."""
    cls = PlayServer6 if ":" in host else PlayServer
    return cls((host.strip("[]"), port), app)


def server_url(server: PlayServer) -> str:
    """URL to open in a browser (wildcard binds are reached through loopback)."""
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    wildcard = ipaddress.ip_address(host).is_unspecified
    if server.address_family == socket.AF_INET6:
        return f"http://[{'::1' if wildcard else host}]:{port}/"
    return f"http://{'127.0.0.1' if wildcard else host}:{port}/"


def exposure_warning(host: str) -> str:
    """The warning printed when binding a non-loopback address."""
    return (
        f"WARNING: cca play is listening on {host}, which other machines may reach.\n"
        "WARNING: there is no authentication: anyone who can reach this port can play, read\n"
        "WARNING: every game and keep your CPU busy. Use --host 127.0.0.1 unless this runs\n"
        "WARNING: inside a container whose port is published on 127.0.0.1 only. Requests\n"
        "WARNING: must name this server as localhost, by IP address or by this machine's\n"
        "WARNING: host name; other names are refused (DNS-rebinding guard)."
    )


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise KeyboardInterrupt


def run(
    app: PlayApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    out: TextIO | None = None,
    err: TextIO | None = None,
    on_listen: Callable[[PlayServer], None] | None = None,
) -> int:
    """Serve until Ctrl+C / SIGTERM; engines warm up in the background meanwhile.

    Returns 1, with an ``error: ...`` line, if the address cannot be bound (port in use,
    unknown host); the engines are not started then.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        server = make_server(host, port, app)
    except OSError as exc:  # includes socket.gaierror for an unresolvable host
        print(f"error: cannot listen on {host}:{port}: {exc}", file=err, flush=True)
        app.close()
        return 1
    restore: Callable[[], object] | None = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)  # docker stop -> clean exit
        restore = functools.partial(signal.signal, signal.SIGTERM, previous)
    try:
        if not server.loopback:
            print(exposure_warning(str(server.server_address[0])), file=err, flush=True)
        threading.Thread(target=app.warm_up, name="cca-play-warm-up", daemon=True).start()
        url = server_url(server)
        print(f"CCA simulator: {url}", file=out, flush=True)
        if open_browser:
            webbrowser.open(url)
        if on_listen is not None:
            on_listen(server)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("cca play: stopped", file=err, flush=True)
    finally:
        server.server_close()
        app.close()
        if restore is not None:
            restore()
    return 0
