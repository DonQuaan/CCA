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

Public mode (:class:`ServerOptions`, all off by default) adds: Host names of the public
deployment (``allowed_hosts``; their ``https://`` origins also pass the POST origin check,
for proxies that rewrite ``Host``), the client address from ``X-Forwarded-For`` behind
``trusted_proxies`` proxies (never ``X-Real-IP``, ``Forwarded`` or ``X-Forwarded-Host``),
``frame_ancestors`` (CSP ``frame-ancestors`` lists them and ``X-Frame-Options`` is dropped,
its ``ALLOW-FROM`` being obsolete), ``Retry-After`` on 429/503 refusals of the limits, and one
log line per request (method, route template, status, duration: no address, no game id; a
request whose client went away before the answer is marked ``aborted``). With
``log_forwarded_hops`` each line also gives how many ``X-Forwarded-For`` entries the request
carried (a number, never an entry), so ``trusted_proxies`` can be calibrated after a deploy.

With ``max_connections`` (a limit, on in public mode) the server bounds its threads: at most
that many connections are handled at once. A connection's request head (request line and
headers) must arrive within :attr:`PlayHandler.HEAD_S` seconds in all, however slowly it
trickles in; and when every place is taken, a connection that has waited more than
:attr:`PlayServer.HEAD_GRACE_S` seconds for its head gives its place to the newcomer (it is
closed within :attr:`_GuardedReader.POLL_S` seconds; so does a connection still waiting for
its head from a TCP peer that holds more than its fair share of the places, for another
peer's newcomer), and a peer holding its fair share or more takes at most
:attr:`PlayServer.peer_slack` threads beyond the bound, leaving the rest to the others. So one
address, or a few, cannot keep others out with idle or trickling connections. Many addresses
each holding about its fair share can (a few dozen, with a place or two each), and so can one
client behind a proxy or port forwarder that passes idle connections through (everyone there
shares its address): against those, only a front that buffers request heads helps (a hosting
platform's edge, a reverse proxy). With no connection to replace, the newcomer is answered 503
at once, from the accepting thread; the refused connection is then kept open for up to
:attr:`_Lingerer.LINGER_S` seconds by one helper thread, which discards what the client still
sends, so that closing it does not reset the connection before the client read the 503.
No thread waits for a client's body unless that client is
counted for it: the route and method are checked before any body is read (404/405 at once);
one client has at most :attr:`PlayServer.API_REQUESTS_PER_CLIENT` API requests in progress
(one more: 429), whose bodies must arrive within :attr:`PlayHandler.BODY_S` seconds in all
(408 otherwise); the unread body of a refused request is drained for at most
:attr:`PlayHandler.DRAIN_S` seconds inside the client's API place, or inside one of its
:attr:`PlayServer.DRAINS_PER_CLIENT` drain places, and otherwise only what has already arrived
is discarded, without waiting. The server speaks HTTP/1.0 (:attr:`PlayHandler.protocol_version`):
one request per connection, closed after its answer even when the client asks for keep-alive,
so no connection idles between requests and there is no keep-alive timeout to bound; the only
idle connections are those still waiting for their first head, which the head deadline and the
places given to newcomers bound.

With ``max_connections_per_peer`` as well (a limit, off unless given) and no trusted proxy, one
TCP peer (an IPv6 /64 as one) has at most that many connections at once, whatever they are
doing (a head on its way, a body, an answer it reads slowly: each write waits at most
:attr:`PlayHandler.timeout` seconds); one more is answered 503 at once, from the accepting
thread. A place is given to a newcomer only by a connection still waiting for its head, so
this cap is what keeps one address from holding every thread past its heads. It is for a
server that clients reach directly: behind a proxy every connection has the proxy's address
(``trusted_proxies`` or not), so it never applies with ``trusted_proxies`` and must not be
turned on behind an untrusted proxy either. A hosting platform's edge (Hugging Face Spaces and
Render both put a proxy in front of the container) is then expected to buffer each request
head and body before passing it on (not verified here); the head and body deadlines bound
whatever it passes on slowly.
"""

from __future__ import annotations

import contextlib
import functools
import io
import ipaddress
import re
import select
import selectors
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, TextIO
from urllib.parse import unquote, urlsplit

from cca.play.game import ApiError
from cca.play.limits import client_id, forwarded_hops
from cca.play.serialize import dumps, loads_object

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

    from _typeshed import WriteableBuffer

    from cca.play.app import PlayApp

MAX_BODY = 64 * 1024
_MAX_DRAIN = 1024 * 1024
# 'unsafe-inline' is limited to style *attributes*: cm-chessboard positions the dragged piece
# with setAttribute("style") and the Cburnett sprite uses style="" attributes (checked in a
# browser: with style-src 'self' alone both are refused). <style> elements stay forbidden.
_CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "connect-src 'self'",
    "img-src 'self' data:",
    "style-src 'self'",
    "style-src-attr 'unsafe-inline'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
)


def content_security_policy(frame_ancestors: Sequence[str] = ()) -> str:
    """The CSP; ``frame-ancestors`` lists the given origins, or ``'none'`` (the default)."""
    ancestors = " ".join(frame_ancestors) if frame_ancestors else "'none'"
    return "; ".join([*_CSP_DIRECTIVES, f"frame-ancestors {ancestors}"])


CSP = content_security_policy()
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
_FIXED_ROUTES = frozenset(
    {"/", "/index.html", "/healthz", "/api/info", "/api/games", "/api/analyse"}
)
_LOGGED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# CSP host-char is ALPHA / DIGIT / "-" (no "_", no wildcard in an exact origin).
_ORIGIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN = re.compile(
    rf"https://(?P<host>{_ORIGIN_LABEL}(?:\.{_ORIGIN_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?\Z"
)
# A public host name: DNS labels (with "_", which some platforms put in generated names).
_NAME_LABEL = r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"{_NAME_LABEL}(?:\.{_NAME_LABEL})*\Z")
_MAX_HOST_NAME = 253


@dataclass(frozen=True, slots=True)
class ServerOptions:
    """How the HTTP layer faces the network (the defaults are the local, loopback behaviour)."""

    public: bool = False
    """Public mode: a start-up notice instead of the exposure warning, and a request log."""
    trusted_proxies: int = 0
    """Reverse proxies in front; the client is that many hops from the right of
    ``X-Forwarded-For`` (0: the header is ignored and the TCP peer is the client)."""
    frame_ancestors: tuple[str, ...] = ()
    """Exact ``https://host[:port]`` origins allowed to frame the page (none: ``DENY``)."""
    allowed_hosts: tuple[str, ...] = ()
    """Extra ``Host`` names answered (the public name(s) behind a proxy), lower case."""
    log_forwarded_hops: bool = False
    """Public mode: each request-log line also gives how many ``X-Forwarded-For`` entries the
    request carried (``xff=N``: a number, never an address), to calibrate ``trusted_proxies``
    behind a platform that does not document its proxy chain."""


def parse_frame_ancestor(text: str) -> str:
    """An exact ``https://host[:port]`` origin, normalised (lower case), or ``ValueError``."""
    match = _ORIGIN.match(text.strip().lower())
    port = int(match["port"]) if match is not None and match["port"] else None
    if (
        match is None
        or len(match["host"]) > _MAX_HOST_NAME
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            f"{text!r} is not an exact https origin such as https://huggingface.co "
            "(https://host[:port]: no path, query, user or wildcard)"
        )
    return f"https://{match['host']}" + (f":{port}" if port is not None else "")


def parse_allowed_host(text: str) -> str:
    """A host name for the ``Host`` guard, normalised (lower case), or ``ValueError``."""
    name = text.strip().lower()
    if not _HOST_NAME.match(name) or len(name) > _MAX_HOST_NAME:
        raise ValueError(
            f"{text!r} is not a host name such as owner-space.hf.space "
            "(no scheme, port, path or wildcard)"
        )
    return name


def security_headers(frame_ancestors: Sequence[str] = ()) -> tuple[tuple[str, str], ...]:
    """The headers of every response; framing is allowed from ``frame_ancestors`` only.

    With origins given, CSP ``frame-ancestors`` lists them and ``X-Frame-Options`` is left out
    (its ``ALLOW-FROM`` is obsolete and ignored, and ``DENY`` would still block the frame).
    """
    if not frame_ancestors:
        return SECURITY_HEADERS
    csp = content_security_policy(frame_ancestors)
    return tuple(
        (key, csp if key == "Content-Security-Policy" else value)
        for key, value in SECURITY_HEADERS
        if key != "X-Frame-Options"
    )


def log_line(
    method: str,
    route: str,
    status: int | None,
    took_ms: float | None,
    *,
    hops: int | None = None,
    hops_logged: bool = False,
    aborted: bool = False,
) -> str:
    """One request-log line: UTC time, method, route template, status, duration.

    ``status`` ``None``: no answer was attempted (``-``). With ``hops_logged``, ``xff=N`` gives
    the number of ``X-Forwarded-For`` entries (``xff=-``: no request head was read). ``aborted``
    marks a request whose client went away before its answer was sent.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    took = "-" if took_ms is None else f"{took_ms:.0f}"
    code = "-" if status is None else str(status)
    line = f"{stamp} {method} {route} {code} {took}ms"
    if hops_logged:
        line += f" xff={'-' if hops is None else hops}"
    return line + (" aborted" if aborted else "")


def route_method(path: str) -> str | None:
    """The one method a decoded path answers (``GET`` or ``POST``), or ``None``: no route."""
    if path in {"/", "/index.html", "/healthz", "/api/info"} or path.startswith("/static/"):
        return "GET"
    if path in {"/api/games", "/api/analyse"}:
        return "POST"
    match = _GAME.match(path)
    if match is None:
        return None
    return "POST" if match["action"] in _POST_ACTIONS else "GET"


def route_label(path: str) -> str:
    """The route template of a decoded path, for the request log (never raw input)."""
    if path in _FIXED_ROUTES:
        return path
    if path.startswith("/static/"):
        return "/static/..."
    match = _GAME.match(path)
    if match is not None:  # the game id is a bearer secret: never logged
        return "/api/games/:id" + (f"/{match['action']}" if match["action"] else "")
    return "(other)"


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


def _silent(request: object) -> bool:
    """Whether nothing has arrived on the socket ``request`` that its thread has yet to read.

    Only asks (``select`` with no wait): the socket's own thread keeps reading it. ``False``
    when that cannot be told (not a socket, closed meanwhile, a descriptor ``select`` cannot
    watch).
    """
    if not isinstance(request, socket.socket):
        return False
    try:
        readable, _, _ = select.select([request], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


def _peer_key(client_address: object) -> str:
    """The TCP peer of a connection as the limits key it (an IPv6 /64 as one address)."""
    if isinstance(client_address, tuple) and client_address:
        host = client_address[0]
        if isinstance(host, str):
            return client_id(host, (), 0)
    return ""


@dataclass(slots=True)
class _Conn:
    """A connection counted by ``max_connections`` (under the server's count lock)."""

    since: float
    """When it was accepted (``time.monotonic()``)."""
    head_deadline: float | None
    """When its request head must have arrived; ``None`` once it has. The server brings it
    forward to take the place of a connection that keeps it waiting (see ``HEAD_GRACE_S``)."""
    peer: str = ""
    """Its TCP peer (an IPv6 /64 as one), as :func:`~cca.play.limits.client_id` keys it."""


class _GuardedReader(io.RawIOBase):
    """The socket reads of one connection when ``max_connections`` bounds the threads.

    While the request head is awaited, no read waits past the head deadline, however slowly
    the head trickles in (each receive waits :attr:`POLL_S` seconds at most, then the deadline,
    which the server may have brought forward, is checked again); past it a ``TimeoutError``
    ends the connection quietly, as the stdlib's own timeout does. Once the head has arrived
    the reads are plain socket reads again (the body has its own deadline).
    """

    POLL_S = 0.5

    def __init__(self, sock: socket.socket, raw: io.RawIOBase, conn: _Conn) -> None:
        super().__init__()
        self._sock = sock
        self._raw = raw  # the stdlib's reader of the socket, closed with this one
        self._conn = conn

    def readable(self) -> bool:
        """A reader."""
        return True

    def readinto(self, buffer: WriteableBuffer, /) -> int | None:
        """Receive into ``buffer`` (``None``: nothing there on a non-blocking socket)."""
        while True:
            deadline = self._conn.head_deadline
            if deadline is None:
                try:
                    return self._sock.recv_into(buffer)
                except BlockingIOError:
                    return None
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise TimeoutError("the request head did not arrive in time")
            self._sock.settimeout(min(wait, self.POLL_S))
            try:
                return self._sock.recv_into(buffer)
            except TimeoutError:
                continue  # nothing yet: check the deadline again

    def close(self) -> None:
        """Close the stdlib's reader too (it keeps the socket's reference count)."""
        try:
            self._raw.close()
        finally:
            super().close()


class _Lingerer:
    """Keeps refused connections open briefly, discarding what they send, then closes them.

    A connection refused from the accepting thread gets its whole 503 (then end of stream) at
    once; its request often arrives just after. Closing a socket with unread data sends a
    reset, which can destroy the refusal before the client reads it, so the page would see a
    network error instead of a 503 with ``Retry-After``. One daemon thread keeps at most
    ``capacity`` such sockets for up to :attr:`LINGER_S` seconds each (until the client closes
    first), reading and dropping their data; beyond ``capacity`` a refusal is closed at once.
    """

    LINGER_S = 2.0

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._pending: list[socket.socket] = []
        self._held = 0
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def held(self) -> int:
        """Sockets kept open now."""
        with self._lock:
            return self._held

    def add(self, sock: socket.socket) -> bool:
        """Take ``sock`` (non-blocking, its reply sent); ``False``: full or closed, not taken."""
        with self._lock:
            if self._closed or self._held >= self._capacity:
                return False
            self._held += 1
            self._pending.append(sock)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="cca-play-linger", daemon=True
                )
                self._thread.start()
        self._wake.set()
        return True

    def close(self) -> None:
        """Close every socket kept and stop the thread."""
        with self._lock:
            self._closed = True
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(5)

    def _run(self) -> None:
        selector = selectors.DefaultSelector()
        until: dict[socket.socket, float] = {}

        def done(sock: socket.socket) -> None:
            selector.unregister(sock)
            del until[sock]
            with contextlib.suppress(OSError):
                sock.close()
            with self._lock:
                self._held -= 1

        try:
            while True:
                self._wake.clear()
                with self._lock:
                    closed, fresh, self._pending = self._closed, self._pending, []
                for sock in fresh:
                    selector.register(sock, selectors.EVENT_READ)
                    until[sock] = time.monotonic() + self.LINGER_S
                if closed:
                    for sock in list(until):
                        done(sock)
                    return
                if not until:
                    self._wake.wait()
                    continue
                wait = max(0.0, min(until.values()) - time.monotonic())
                for key, _ in selector.select(min(wait, 0.25)):
                    ready = key.fileobj
                    if not isinstance(ready, socket.socket):  # pragma: no cover - never
                        continue
                    try:
                        data = ready.recv(65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b""
                    if not data:  # the client closed (or reset): nothing more to protect
                        done(ready)
                now = time.monotonic()
                for sock in [s for s, end in until.items() if end <= now]:
                    done(sock)
        finally:
            selector.close()


class PlayHandler(BaseHTTPRequestHandler):
    """Routes requests to :class:`~cca.play.app.PlayApp` and applies the guards."""

    server_version = "cca-play"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    """One request per connection (the stdlib's default, pinned): no keep-alive, so with
    ``max_connections`` no thread waits on an idle connection between requests."""
    timeout = 30
    DRAIN_S = 2.0
    """With ``max_connections``: the longest an unread body is drained (a slow sender stops)."""
    BODY_S = 10.0
    """With ``max_connections``: the longest a request body may take to arrive (then 408).
    The page's bodies are a few hundred bytes, sent with the request head."""
    HEAD_S = 10.0
    """With ``max_connections``: the longest a request head (request line and headers) may
    take to arrive in all, from the connection's start; then the connection is closed without
    an answer. Browsers and proxies send a head at once."""
    # Per request, for the public-mode log (class defaults cover errors raised before
    # parse_request, e.g. an over-long request line).
    _started: float | None = None
    _label = "-"
    _logged = False
    _conn: _Conn | None = None  # with max_connections: this connection's count record

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
        """Quiet: the stdlib's own log (with the client address) is never written."""

    def setup(self) -> None:
        """With ``max_connections``: read the request head through a :class:`_GuardedReader`."""
        super().setup()
        conn = self.play_server.connection_record(self.request)
        if conn is not None and isinstance(self.rfile, io.BufferedReader):
            raw = self.rfile.detach()  # (nothing has been read yet)
            if not isinstance(raw, io.RawIOBase):  # pragma: no cover - the stdlib's SocketIO
                raise TypeError("unexpected reader")
            self.rfile = io.BufferedReader(_GuardedReader(self.connection, raw, conn))
            self._conn = conn

    def parse_request(self) -> bool:
        """Start the clock and reset the route label of a new request, then parse it."""
        self._started = time.monotonic()
        self._label = "-"
        self._logged = False
        try:
            return super().parse_request()
        finally:
            self._head_done()

    def _head_done(self) -> None:
        """The request head has been read (or refused): no head deadline from here on."""
        conn = self._conn
        if conn is not None and conn.head_deadline is not None:
            self.play_server.head_done(conn)
            self.connection.settimeout(self.timeout)

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
        try:
            self.send_response(status)
            for key, value in self.play_server.security_headers:
                self.send_header(key, value)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:  # the client went away: the request still gets its line
            self._log(status, aborted=True)
            raise
        self._log(status)

    def _log(self, status: int | None, *, aborted: bool = False) -> None:
        """One line per request in public mode: no address, no game id, no raw input."""
        server = self.play_server
        if server.log_stream is None or self._logged:
            return
        self._logged = True
        method = self.command if self.command in _LOGGED_METHODS else "-"
        took = None if self._started is None else (time.monotonic() - self._started) * 1000
        hops_logged = server.options.log_forwarded_hops
        hops = None
        headers = getattr(self, "headers", None)  # (absent when no head could be parsed)
        if hops_logged and headers is not None:
            hops = forwarded_hops(headers.get_all("X-Forwarded-For") or [])
        server.write_log(
            log_line(
                method,
                self._label,
                status,
                took,
                hops=hops,
                hops_logged=hops_logged,
                aborted=aborted,
            )
        )

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
        origin = origin.strip().lower()
        if host and origin in {f"http://{host}", f"https://{host}"}:
            return True
        # Behind a proxy that rewrites Host, the page's origin is still a public name of ours.
        return origin in self.play_server.public_origins

    def _client(self) -> str:
        """The requester's quota key (TCP peer, or X-Forwarded-For behind trusted proxies)."""
        return client_id(
            str(self.client_address[0]),
            self.headers.get_all("X-Forwarded-For") or [],
            self.play_server.options.trusted_proxies,
        )

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
        read: Callable[[int], bytes] = self.rfile.read
        deadline = None
        if self.play_server.max_connections is not None:
            read = self.rfile.read1  # one receive per call, so the deadline is checked often
            deadline = time.monotonic() + self.DRAIN_S
        try:
            self.connection.settimeout(1.0)
            while remaining > 0:
                if deadline is not None and time.monotonic() >= deadline:
                    break  # a slow sender: give up (bounded threads), the reply may be reset
                chunk = read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            return

    def _discard_arrived(self) -> None:
        """Discard the part of an unread body that has already arrived, without waiting."""
        length = self._content_length()
        if length is None or length <= 0:
            return
        remaining = min(length, _MAX_DRAIN)
        try:
            self.connection.setblocking(False)  # read1 then returns b"" when nothing is there
            try:
                while remaining > 0:
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            finally:
                self.connection.settimeout(self.timeout)  # (blocking again, for the reply)
        except OSError:
            return

    def _refuse_body(self, *, counted: bool) -> None:
        """With ``max_connections``: dispose of a refused request's unread body.

        Draining lets the client read the refusal (closing a socket with unread data resets
        the connection, which can destroy the reply). A request that holds one of its client's
        API places (``counted``) is drained within that place. Any other (refused before being
        counted: Host, route, method, or the per-client count itself) is drained only in one of
        its client's drain places (:attr:`PlayServer.DRAINS_PER_CLIENT`); without a free one,
        only what has already arrived is discarded (a flood's refusals may then be lost to a
        reset). So no client ties up more threads than its places, however many it sends.
        """
        if counted:
            self._drain()
            return
        server = self.play_server
        client = self._client()
        if not server.take_drain_place(client):
            self._discard_arrived()
            return
        try:
            self._drain()
        finally:
            server.drain_done(client)

    def _read_body(self, length: int) -> bytes:
        """The request body; with ``max_connections`` it must arrive within ``BODY_S``."""
        if self.play_server.max_connections is None:
            return self.rfile.read(length)
        deadline = time.monotonic() + self.BODY_S
        parts: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise TimeoutError
                self.connection.settimeout(wait)  # one receive per read1: never past the deadline
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    break
                parts.append(chunk)
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(parts)

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
            raw = self._read_body(length)
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
        server = self.play_server
        bounded = server.max_connections is not None
        admitted: str | None = None  # the client counted by server.admit, if any
        try:
            self._check_request()
            path = unquote(urlsplit(self.path).path)
            self._label = route_label(path)
            if bounded:
                self._resolve(method, path)  # 404/405 before any body is read or counted
                if path.startswith("/api/"):  # (every route with a body is an API route)
                    client = self._client()
                    server.admit(client)  # 429 when this client has too many API requests open
                    admitted = client
            body = self._read_json() if method == "POST" else {}
            self._route(method, path, body)
        except ApiError as exc:
            if method == "POST" and not self._body_read:
                if bounded:
                    self._refuse_body(counted=admitted is not None)
                else:
                    self._drain()
            self._api_error(exc)
        except ConnectionError:  # the client went away (e.g. closed the tab during a decision)
            self._log(None, aborted=True)  # (no-op if its answer was already logged)
        except Exception:  # never leak a traceback to the client; keep it on stderr
            server.report_error()
            self._json(500, {"error": "internal server error (see the server log)"})
        finally:
            if admitted is not None:
                server.release(admitted)

    def _api_error(self, exc: ApiError) -> None:
        """Answer a refusal: its JSON (message, state, limit) and headers (Allow, Retry-After)."""
        payload: dict[str, object] = {"error": exc.message}
        if exc.state is not None:
            payload["state"] = exc.state
        if exc.limit is not None:
            payload["limit"] = exc.limit
        extra: list[tuple[str, str]] = []
        if isinstance(exc, _MethodNotAllowedError):
            extra.append(("Allow", exc.allow))
        if exc.retry_after is not None:
            extra.append(("Retry-After", str(exc.retry_after)))
        self._json(exc.status, payload, tuple(extra))

    def _resolve(self, method: str, path: str) -> None:
        """404 when no route has ``path``, 405 when it takes another method (nothing is read)."""
        allowed = route_method(path)
        if allowed is None:
            raise ApiError(404, "not found")
        if method != allowed:
            raise _MethodNotAllowedError(allowed)

    def _route(self, method: str, path: str, body: dict[str, object]) -> None:
        self._resolve(method, path)
        app = self.app
        if path in {"/", "/index.html"}:
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path.removeprefix("/static/"))
        elif path == "/healthz":
            status, payload = app.health()
            self._json(status, payload)
        elif path == "/api/info":
            self._json(200, app.info())
        elif path == "/api/games":
            self._json(201, app.create_game(body, client=self._client()))
        elif path == "/api/analyse":
            self._json(200, app.analyse(body, client=self._client()))
        elif match := _GAME.match(path):
            self._game(match["id"], match["action"], body)
        else:  # pragma: no cover - _resolve refused every other path
            raise ApiError(404, "not found")

    def _game(self, game_id: str, action: str | None, body: dict[str, object]) -> None:
        app = self.app
        if action is None:
            self._json(200, app.game_state(game_id))
        elif action == "move":
            self._json(200, app.move(game_id, body))
        elif action == "think":
            self._json(200, app.think(game_id, client=self._client()))
        elif action == "undo":
            self._json(200, app.undo(game_id))
        elif action == "resign":
            self._json(200, app.resign(game_id))
        elif action == "pgn":
            pgn = app.pgn(game_id, client=self._client())
            self._send(200, pgn.encode("utf-8"), "text/plain; charset=utf-8")
        else:  # (serialised when the decisions were made, see PlayApp.decisions)
            self._send(200, app.decisions(game_id, client=self._client()), "application/json")

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
    API_REQUESTS_PER_CLIENT = 8
    """With ``max_connections``: API requests one client may have in progress (more: 429).
    The page itself keeps only a few open (a move, a position-lab analysis, a game read)."""
    DRAINS_PER_CLIENT = 2
    """With ``max_connections``: refused requests of one client (outside its API places) whose
    unread body may be drained at once, so the refusal reaches it; beyond, nothing is waited
    for. A page never has a request refused this way (no route, wrong method or Host, or more
    than :attr:`API_REQUESTS_PER_CLIENT` at once)."""
    FULL_RETRY_AFTER_S = 2
    """``Retry-After`` of a connection refused by ``max_connections`` (requests are short)."""
    HEAD_GRACE_S = 0.5
    """With ``max_connections``, when every place is taken: a connection still waiting for its
    request head after this long gives its place to a newcomer (a browser or a proxy sends
    the head with the connection; an idle or trickling one is holding a place, nothing more)."""
    FLOOD_GRACE_S = 0.05
    """The same, sooner, for a connection of a peer that holds more than its fair share of the
    places, when a newcomer of another peer finds none waiting longer (see
    :meth:`_stale_head`)."""

    def __init__(
        self, address: tuple[str, int], app: PlayApp, options: ServerOptions | None = None
    ) -> None:
        self.app = app
        self.options = options or ServerOptions()
        # Connections handled at once (None: no bound, the local default), and handled now.
        self.max_connections = app.settings.limits.max_connections
        self.connections = 0  # (counted only with max_connections)
        self._count_lock = threading.Lock()
        self._api_requests: dict[str, int] = {}  # API requests in progress, by client
        self._draining: dict[str, int] = {}  # refused bodies being drained, by client
        # With max_connections: the connections counted, oldest first; how many threads may
        # run beyond the bound while connections that gave up their place wind down (each
        # within _GuardedReader.POLL_S); and how many of those a TCP peer holding its fair
        # share of the places or more may use (half: flooders churn only their own places).
        self._conns: dict[object, _Conn] = {}
        cap = self.max_connections
        self.eviction_slack = 0 if cap is None else max(2, cap // 4)
        self.peer_slack = self.eviction_slack // 2
        # Connections one TCP peer may have at once: only with max_connections and no trusted
        # proxy (behind one, every connection has the proxy's address).
        per_peer = app.settings.limits.max_connections_per_peer
        direct = cap is not None and self.options.trusted_proxies == 0
        self.peer_cap = per_peer if direct else None
        self._lingerer = _Lingerer(capacity=cap or 0)
        super().__init__(address, PlayHandler)
        bound = str(self.server_address[0])
        self.loopback = is_loopback(bound)
        # Public names given with --allowed-host are answered by either kind of bind.
        self.allowed = frozenset(name.lower() for name in self.options.allowed_hosts)
        # Host names a loopback bind answers: the three of the contract, plus the bound address
        # itself (--host 127.0.0.2). Nothing else, not even other IP literals.
        self.loopback_names = (
            frozenset({"localhost", "127.0.0.1", "[::1]", host_literal(bound)}) | self.allowed
        )
        # A non-loopback bind (LAN, container) also answers any IP literal and this machine's
        # host name (a container's own name).
        self.host_names = (
            frozenset(n for n in ("localhost", socket.gethostname().lower()) if n) | self.allowed
        )
        # Origins of pages served under those public names (the POST origin check).
        self.public_origins = frozenset(f"https://{name}" for name in self.allowed)
        self.security_headers = security_headers(self.options.frame_ancestors)
        # Request log (public mode): run() points it at its error stream, and stops it (see
        # stop_logging) before it returns.
        self.log_stream: TextIO | None = sys.stderr if self.options.public else None
        self._log_lock = threading.Lock()
        self._log_stopped = False
        wait = self.FULL_RETRY_AFTER_S
        self._full_reply = b""
        self._peer_reply = b""
        if self.max_connections is not None:
            self._full_reply = self._refusal_bytes(
                f"busy: {self.max_connections} connections are open; try again in {wait} s",
                "max_connections",
            )
        if self.peer_cap is not None:
            self._peer_reply = self._refusal_bytes(
                f"too many connections from your address: at most {self.peer_cap} at once; "
                f"try again in {wait} s",
                "max_connections_per_peer",
            )

    # ------------------------------------------------------------------ thread bounds
    def _refusal_bytes(self, error: str, limit: str) -> bytes:
        """The whole 503 answer to a connection refused at accept (built once per limit)."""
        wait = self.FULL_RETRY_AFTER_S
        body = dumps({"error": error, "limit": limit})
        head = [
            "HTTP/1.0 503 Service Unavailable",
            f"Server: {PlayHandler.server_version}",
            *(f"{key}: {value}" for key, value in self.security_headers),
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            f"Retry-After: {wait}",
            "Connection: close",
        ]
        return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Handle a connection in its own thread; beyond ``max_connections``, refuse it.

        When every place is taken, the oldest connection that has waited longer than
        :attr:`HEAD_GRACE_S` for its request head gives its place to this one (see the module
        docstring); without one, this connection is refused. With :attr:`peer_cap`, a
        connection whose TCP peer already has that many is refused first, whatever the rest.
        """
        if self.max_connections is not None:
            peer = _peer_key(client_address)
            with self._count_lock:
                if self._peer_full(peer):
                    refusal = self._peer_reply
                elif self._admit_connection(request, peer):
                    refusal = b""
                else:
                    refusal = self._full_reply
            if refusal:
                self._refuse(request, refusal)
                return
        try:
            super().process_request(request, client_address)
        except BaseException:  # no thread was started: the connection is not handled
            self._connection_done(request)
            raise

    def _peer_full(self, peer: str) -> bool:
        """Whether ``peer`` has :attr:`peer_cap` connections counted already (under the lock).

        Every counted connection counts, including one that gave its place and has yet to
        notice (its thread still runs): so one peer never runs more threads than the cap.
        """
        cap = self.peer_cap
        if cap is None:
            return False
        return sum(conn.peer == peer for conn in self._conns.values()) >= cap

    def _admit_connection(self, request: object, peer: str) -> bool:
        """Count ``request`` in, taking a waiting head's place if need be (under the lock).

        A connection that gave its place keeps its thread until its reader notices (within
        :attr:`_GuardedReader.POLL_S`), so at most :attr:`eviction_slack` threads run beyond
        the bound. A newcomer whose TCP peer holds its fair share of the places or more (the
        bound divided by the peers holding places, its own counted) may take a place only
        while fewer than the bound plus :attr:`peer_slack` (half the slack) are counted: so
        one peer's connections never exceed that, and peers that flood a full server, one or
        a few, churn only their own places and leave the rest of the slack to the others.
        """
        cap = self.max_connections
        if cap is None:  # pragma: no cover - only called with a bound
            return True
        now = time.monotonic()
        if self.connections >= cap:
            held = Counter(conn.peer for conn in self._conns.values())
            stale = self._stale_head(now, peer, held)
            # (A peer holding places is one of the len(held) peers holding places.)
            slack = self.peer_slack if held[peer] * len(held) >= cap else self.eviction_slack
            if stale is None or self.connections >= cap + slack:
                return False
            stale.head_deadline = now  # its reader gives up within _GuardedReader.POLL_S
        self.connections += 1
        self._conns[request] = _Conn(since=now, head_deadline=now + PlayHandler.HEAD_S, peer=peer)
        return True

    def _stale_head(self, now: float, peer: str, held: Counter[str]) -> _Conn | None:
        """The connection that gives its place to a newcomer of ``peer`` on a full server.

        The oldest one that has waited longer than :attr:`HEAD_GRACE_S` for its request head;
        failing that, a connection of a peer that holds more than its fair share of the places
        (the bound divided by the peers holding places, the newcomer's counted; ``held``:
        connections by peer) and at least two more than the newcomer's peer: of the peer
        holding the most, its oldest connection that has waited :attr:`FLOOD_GRACE_S` at least
        for its head with nothing arrived that its thread has yet to read (a head sent at once
        by a proxy is never cut off). So addresses flooding the server, one or a few, cannot
        keep everyone else out by replacing their connections faster than they grow stale.
        """
        cap = self.max_connections or 0
        for conn in self._conns.values():  # oldest first
            deadline = conn.head_deadline
            if (
                deadline is not None
                and deadline > now  # (not already given up)
                and now - conn.since >= self.HEAD_GRACE_S - 1e-9
            ):
                return conn
        peers = len(held) + (peer not in held)  # the peers holding places, and the newcomer's
        least = held[peer] + 2  # (so never the newcomer's own peer, nor a ping-pong of two)
        found: _Conn | None = None
        most = 0
        for request, conn in self._conns.items():  # oldest first
            count = held[conn.peer]
            deadline = conn.head_deadline
            if (
                count > most
                and count * peers > cap
                and count >= least
                and deadline is not None
                and deadline > now
                and now - conn.since >= self.FLOOD_GRACE_S - 1e-9
                and _silent(request)
            ):
                found, most = conn, count
        return found

    def connection_record(self, request: object) -> _Conn | None:
        """The count record of a connection (``None`` without ``max_connections``)."""
        with self._count_lock:
            return self._conns.get(request)

    def head_done(self, conn: _Conn) -> None:
        """A counted connection's request head has arrived: it no longer has a head deadline."""
        with self._count_lock:
            conn.head_deadline = None

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """The thread of one connection (it frees its place when it ends)."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_done(request)

    def _connection_done(self, request: object) -> None:
        if self.max_connections is not None:
            with self._count_lock:
                self.connections -= 1
                self._conns.pop(request, None)

    def _refuse(self, request: socket.socket | tuple[bytes, socket.socket], reply: bytes) -> None:
        """Answer ``reply`` (a whole 503) at once, from the accepting thread (never blocks).

        The reply and the end of stream are sent at once; the socket is then left to the
        :class:`_Lingerer`, which closes it once the client has read and closed (or after a
        moment), so that its late request does not turn the close into a reset.
        """
        kept = False
        if isinstance(request, socket.socket):
            with contextlib.suppress(OSError):
                request.setblocking(False)
                with contextlib.suppress(OSError):  # (nothing has arrived yet)
                    request.recv(65536)  # read what came, so closing does not reset
                request.sendall(reply)
                request.shutdown(socket.SHUT_WR)  # the whole reply, then end of stream
                kept = self._lingerer.add(request)
        if not kept:
            self.shutdown_request(request)
        self.write_log(log_line("-", "-", 503, None, hops_logged=self.options.log_forwarded_hops))

    def server_close(self) -> None:
        """Close the listening socket and every refused connection still kept open."""
        super().server_close()
        self._lingerer.close()

    def admit(self, client: str) -> None:
        """Count an API request of ``client`` in progress; 429 when it has too many."""
        with self._count_lock:
            held = self._api_requests.get(client, 0)
            if held >= self.API_REQUESTS_PER_CLIENT:
                raise ApiError(
                    429,
                    "too many requests at once: wait for your previous ones; try again in 1 s",
                    retry_after=1,
                    limit="requests_at_once",
                )
            self._api_requests[client] = held + 1

    def release(self, client: str) -> None:
        """An API request of ``client`` counted by :meth:`admit` has ended."""
        with self._count_lock:
            left = self._api_requests.pop(client) - 1
            if left:
                self._api_requests[client] = left

    def api_requests(self, client: str) -> int:
        """API requests of ``client`` in progress (counted only with ``max_connections``)."""
        with self._count_lock:
            return self._api_requests.get(client, 0)

    def take_drain_place(self, client: str) -> bool:
        """Count a refused body of ``client`` being drained; ``False`` when it has too many."""
        with self._count_lock:
            held = self._draining.get(client, 0)
            if held >= self.DRAINS_PER_CLIENT:
                return False
            self._draining[client] = held + 1
            return True

    def drain_done(self, client: str) -> None:
        """A drain counted by :meth:`take_drain_place` has ended."""
        with self._count_lock:
            left = self._draining.pop(client) - 1
            if left:
                self._draining[client] = left

    def draining(self, client: str) -> int:
        """Refused bodies of ``client`` being drained now (only with ``max_connections``)."""
        with self._count_lock:
            return self._draining.get(client, 0)

    def write_log(self, line: str) -> None:
        """Write one log line (whole, even with concurrent requests); never raises.

        Nothing is written once :meth:`stop_logging` has returned.
        """
        with self._log_lock:  # (read under it: stop_logging may run while a thread waits)
            stream = self.log_stream
            if stream is None:
                return
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):  # a closed or broken stream must not fail a request
                return

    def report_error(self, heading: str | None = None) -> None:
        """Report the exception being handled on stderr: ``heading``, then its traceback.

        In public mode the report is written whole, under the log's lock, and not at all once
        :meth:`stop_logging` has returned. Locally it is printed as it always was.
        """
        if not self.options.public:
            if heading is not None:
                print(heading, file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return
        text = traceback.format_exc()
        if heading is not None:
            text = f"{heading}\n{text}"
        with self._log_lock:
            stream = sys.stderr
            if self._log_stopped or stream is None:
                return
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                return

    def stop_logging(self) -> None:
        """Write no more log lines or public-mode error reports, from any thread.

        Once this returns, no request's thread writes to the log or to stderr. Those threads
        are daemon threads that may outlive :func:`run`, and a write still in progress when
        the interpreter shuts down aborts the process ("Fatal Python error").
        """
        with self._log_lock:
            self.log_stream = None
            self._log_stopped = True

    def host_ok(self, host: str) -> bool:
        """The DNS-rebinding guard: whether this server answers ``Host: host``."""
        if self.loopback:
            return host_allowed(host, self.loopback_names, any_ip=False)
        return host_allowed(host, self.host_names, any_ip=True)

    @property
    def host_hint(self) -> str:
        """The Host names this server answers, for the 421 error message."""
        extra = "".join(f", {name}" for name in sorted(self.allowed))
        if self.loopback:
            return f"localhost, 127.0.0.1{extra} or [::1]"
        return f"localhost, an IP address{extra} or this machine's name"

    def server_bind(self) -> None:
        """Bind without HTTPServer's reverse-DNS lookup of the host (slow, and not needed)."""
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Ignore clients that disconnect; report anything else.

        In public mode the report leaves out the client address the stdlib would print.
        """
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        if self.options.public:
            self.report_error("cca play: error while handling a request:")
            return
        super().handle_error(request, client_address)


class PlayServer6(PlayServer):
    """IPv6 variant (for ``--host ::1`` and the like)."""

    address_family = socket.AF_INET6


def make_server(
    host: str, port: int, app: PlayApp, options: ServerOptions | None = None
) -> PlayServer:
    """Bind a server (IPv6 when ``host`` contains a colon)."""
    cls = PlayServer6 if ":" in host else PlayServer
    return cls((host.strip("[]"), port), app, options)


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


PUBLIC_NEEDS_A_PUBLIC_HOST = (
    "--public needs a non-loopback --host (e.g. --host 0.0.0.0 in a container): a loopback "
    "address cannot be reached from the Internet"
)
PUBLIC_NEEDS_A_HOST_NAME = (
    "--public needs --allowed-host NAME, the public host name visitors use (e.g. "
    "owner-space.hf.space): requests naming another host are refused (DNS-rebinding guard)"
)
LOG_HOPS_NEEDS_PUBLIC = "--log-forwarded-hops needs --public: only public mode writes a request log"
PEER_CAP_NEEDS_DIRECT = (
    "--max-connections-per-peer needs --trusted-proxies 0: behind a proxy every connection "
    "has the proxy's address, so it would cap the whole site"
)
PEER_CAP_NEEDS_A_BOUND = (
    "--max-connections-per-peer needs --max-connections (or --public, which sets it): only a "
    "server that bounds its connections counts them per address"
)


def _limit(value: int | None, unit: str = "") -> str:
    return "no limit" if value is None else f"{value}{unit}"


def _peer_notice(server: PlayServer) -> str:
    """The start-up line on connections per TCP peer (only with ``max_connections``)."""
    if server.peer_cap is not None:
        return (
            f"Connections per address: {server.peer_cap} at once, whatever they are doing "
            "(right only if clients reach this server directly: behind any proxy every "
            "connection has the proxy's address). One request per connection (HTTP/1.0)."
        )
    if server.options.trusted_proxies:
        return (
            "Connections per address: no cap (the TCP peer is the proxy): the platform's edge "
            "is expected to buffer request heads and bodies; the deadlines above bound what it "
            "passes on slowly. One request per connection (HTTP/1.0)."
        )
    return (
        "Connections per address: no cap (--max-connections-per-peer N sets one, for a server "
        "that clients reach directly, with no proxy in front). One request per connection "
        "(HTTP/1.0)."
    )


def public_notice(server: PlayServer) -> str:
    """What a public-mode server tells its operator at start-up (on the error stream)."""
    options = server.options
    limits = server.app.settings.limits
    names = ", ".join(sorted(server.allowed)) or "(none)"
    if options.trusted_proxies:
        who = (
            f"the X-Forwarded-For entry {options.trusted_proxies} hop(s) from the right "
            "(only right if exactly that many proxies stand in front)"
        )
    else:
        who = (
            "the TCP peer (X-Forwarded-For ignored). Behind a reverse proxy every visitor then "
            "shares ONE quota: add --trusted-proxies 1 for one proxy (e.g. Hugging Face Spaces)"
        )
    framing = ", ".join(options.frame_ancestors) or "nobody (X-Frame-Options: DENY)"
    think = limits.max_think_seconds
    app = server.app
    reclaim = (
        ""
        if limits.max_sessions_per_client is None
        else f" (when all are in use, a game in which nothing was played for "
        f"{app.RECLAIM_UNPLAYED_S / 60:g} min, {app.RECLAIM_THINKING_S / 60:g} min on the move "
        "of a player who has moved in it, goes to a newcomer; a player's move counts as play "
        "once CCA has played in the game, and a game started in place of the same client's "
        "older one keeps that one's time)"
    )
    lines = [
        f"PUBLIC MODE on {host_literal(str(server.server_address[0]))}:"
        f"{int(server.server_address[1])}: anyone who reaches it can play (no authentication).",
        f"Host names answered: {names}; also IP addresses and localhost (health checks).",
        f"Limits: games per client {_limit(limits.max_sessions_per_client)}; CCA decisions "
        f"per client and minute {_limit(limits.decisions_per_minute)}; idle games dropped "
        f"after {_limit(limits.session_idle_minutes, ' min')}; requests waiting for the engine "
        f"{_limit(limits.max_queue)}; games in total {app.settings.max_sessions}{reclaim}; "
        f"nodes per engine search {server.app.settings.nodes}.",
        f"Decisions: at most {'no limit' if think is None else f'{think:g} s'} each, once "
        "they have the engine (a shorter search when the cap is near: not reproducible); "
        "waiting clients take turns; reads of a game's decisions or PGN per client and minute "
        f"{_limit(limits.reads_per_minute)}.",
        f"Threads: connections at once {_limit(limits.max_connections)}"
        + (
            ""
            if limits.max_connections is None
            else f" (per client {server.API_REQUESTS_PER_CLIENT} API requests at once and "
            f"{server.DRAINS_PER_CLIENT} refused bodies drained; request heads within "
            f"{PlayHandler.HEAD_S:g} s, and a head late by {server.HEAD_GRACE_S:g} s gives "
            f"its place to a newcomer when all are taken; request bodies within "
            f"{PlayHandler.BODY_S:g} s; per game {server.app.GAME_WAITERS} waiting requests)"
        )
        + ".",
        *([] if limits.max_connections is None else [_peer_notice(server)]),
        f"Client address: {who}.",
        f"Framing allowed from: {framing}.",
        "One line per request follows: time, method, route, status, duration "
        "(no addresses, no game ids)"
        + (
            "; xff=N is the number of X-Forwarded-For entries: set --trusted-proxies to the "
            "smallest N of the page's own requests (/api/...), not of /healthz"
            if options.log_forwarded_hops
            else ""
        )
        + ".",
    ]
    return "\n".join(f"cca play: {line}" for line in lines)


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
    options: ServerOptions | None = None,
) -> int:
    """Serve until Ctrl+C / SIGTERM; engines warm up in the background meanwhile.

    Returns 1, with an ``error: ...`` line, if the address cannot be bound (port in use,
    unknown host) or if public mode is asked of a loopback address; the engines are not
    started then. In public mode the request log goes to ``err``, until this returns: a
    request still ending then is not logged (nor, in public mode, is an error it meets).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        server = make_server(host, port, app, options)
    except OSError as exc:  # includes socket.gaierror for an unresolvable host
        print(f"error: cannot listen on {host}:{port}: {exc}", file=err, flush=True)
        app.close()
        return 1
    if server.options.public and server.loopback:
        print(f"error: {PUBLIC_NEEDS_A_PUBLIC_HOST}", file=err, flush=True)
        server.server_close()
        app.close()
        return 1
    if server.log_stream is not None:
        server.log_stream = err
    restore: Callable[[], object] | None = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)  # docker stop -> clean exit
        restore = functools.partial(signal.signal, signal.SIGTERM, previous)
    try:
        if server.options.public:
            print(public_notice(server), file=err, flush=True)
        elif not server.loopback:
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
        # Request threads are daemon threads and may outlive this call (the process then
        # exits): none of them may be writing to stderr when the interpreter shuts down.
        server.stop_logging()
        if restore is not None:
            restore()
    return 0
