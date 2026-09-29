"""``cca play --public``: client identity, limits, framing, host names, logging, CLI wiring.

Limits are tested on the app with an injected clock and over real HTTP (a ``PlayServer`` on
127.0.0.1 with forged headers; a "public bind" is simulated by clearing ``server.loopback``,
as test_play.py does, so no test ever listens on a public interface). The last sections pin
down that ``cca play`` without the new flags behaves exactly as before.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import http.client
import io
import json
import re
import select
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from email.message import Message
from pathlib import Path

import chess
import chess.engine
import pytest

import cca.play.serialize as play_serialize
import cca.play.server as play_server
from cca.agent import AgentConfig, CAIMEAgent
from cca.cli import build_parser, main
from cca.core.types import Clock, Decision, MoveEval
from cca.engines.stockfish import StockfishEngine
from cca.play.app import Engines, PlayApp, PlaySettings
from cca.play.game import ApiError
from cca.play.limits import (
    PUBLIC_LIMITS,
    EngineGate,
    Limits,
    RateLimiter,
    client_id,
    forwarded_client,
    forwarded_hops,
    resolve_limits,
)
from cca.play.server import (
    CSP,
    SECURITY_HEADERS,
    PlayHandler,
    PlayServer,
    ServerOptions,
    _GuardedReader,
    make_server,
    parse_allowed_host,
    parse_frame_ancestor,
    public_notice,
    route_label,
    run,
    security_headers,
)
from tests.conftest import FakeEngine, FakeHuman, make_launcher

STATIC = Path(play_server.__file__).resolve().parent / "static"
HF = "demo-space.hf.space"
HF_ORIGIN = f"https://{HF}"
WILDCARD = ".".join(["0"] * 4)  # only ever parsed or passed to a patched run(), never bound
# The CSP of cca play 0.1.0, verbatim: local mode must keep sending exactly this.
CSP_0_1_0 = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; style-src-attr 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


# ---------------------------------------------------------------- helpers
class FakeClock:
    """Injected monotonic clock: time only moves when a test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_app(
    limits: Limits | None = None,
    *,
    max_sessions: int = 16,
    now: Callable[[], float] = time.monotonic,
    nodes: int = 200_000,
    engine: FakeEngine | None = None,
) -> PlayApp:
    settings = PlaySettings(
        AgentConfig(), nodes=nodes, max_sessions=max_sessions, limits=limits or Limits()
    )
    eng = engine or FakeEngine()
    app = PlayApp(lambda: Engines(eng, FakeHuman(), "qre"), settings, now=now)
    app.warm_up()
    return app


def game(app: PlayApp, client: str | None, **body: object) -> str:
    game_id = app.create_game(dict(body), client=client)["game_id"]
    assert isinstance(game_id, str)
    return game_id


def refused(call: Callable[[], object]) -> ApiError:
    with pytest.raises(ApiError) as err:
        call()
    return err.value


def refused_at_once(call: Callable[[], object]) -> ApiError:
    """Like :func:`refused`, for a call that a missing guard would leave waiting for the engine:
    waiting more than a few seconds fails the test instead of hanging the suite."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call()
        except BaseException as exc:  # handed over to the test thread below
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert outcome, "the call waited instead of being refused at once"
    error = outcome[0]
    assert isinstance(error, ApiError), error
    return error


def alive(app: PlayApp, *game_ids: str) -> list[bool]:
    """Whether each game still exists (looking it up marks it used: call this last)."""
    statuses = []
    for game_id in game_ids:
        try:
            app.game_state(game_id)
        except ApiError as exc:
            statuses.append(exc.status)
        else:
            statuses.append(200)
    assert set(statuses) <= {200, 404}, statuses
    return [status == 200 for status in statuses]


def lab(app: PlayApp, client: str | None) -> dict[str, object]:
    return app.analyse({"fen": chess.STARTING_FEN}, client=client)


@dataclasses.dataclass
class Response:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, object]:
        data = json.loads(self.body.decode("utf-8"))
        assert isinstance(data, dict)
        return data


def call(
    port: int,
    method: str,
    path: str,
    body: object = None,
    *,
    headers: Mapping[str, str] | None = None,
    host: str | None = "127.0.0.1",
) -> Response:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs: dict[str, str] = {}
        if host is not None:
            hdrs["Host"] = f"{host}:{port}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        hdrs.update(headers or {})
        for key, value in hdrs.items():
            conn.putheader(key, value)
        conn.endheaders()
        if data:
            conn.send(data)
        resp = conn.getresponse()
        return Response(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


def analyse_over_http(port: int, forwarded: str | None = None, **kw: str) -> Response:
    headers = {"X-Forwarded-For": forwarded} if forwarded is not None else {}
    return call(port, "POST", "/api/analyse", {"fen": chess.STARTING_FEN}, headers=headers, **kw)


@contextlib.contextmanager
def serving(
    app: PlayApp, options: ServerOptions | None = None, *, public_bind: bool = False
) -> Iterator[PlayServer]:
    server = make_server("127.0.0.1", 0, app, options)
    if public_bind:
        server.loopback = False  # as if bound to 0.0.0.0 in a container
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        app.close()


def port_of(server: PlayServer) -> int:
    return int(server.server_address[1])


def _raw_exchange(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


# ---------------------------------------------------------------- client identity
PEER = "192.0.2.10"


def test_without_trusted_proxies_x_forwarded_for_is_ignored() -> None:
    for forwarded in ([], ["198.51.100.1"], ["6.6.6.6, 198.51.100.1"], ["a", "b"]):
        assert client_id(PEER, forwarded, 0) == PEER, forwarded
        assert forwarded_client(forwarded, 0) is None


@pytest.mark.parametrize(
    ("forwarded", "trusted", "expected"),
    [
        (["198.51.100.1"], 1, "198.51.100.1"),
        ([" 198.51.100.1 "], 1, "198.51.100.1"),
        (["6.6.6.6, 198.51.100.1"], 1, "198.51.100.1"),  # the spoofed left part is ignored
        (["6.6.6.6", "198.51.100.1"], 1, "198.51.100.1"),  # two header lines form one list
        (["6.6.6.6, 198.51.100.1, 10.0.0.2"], 2, "198.51.100.1"),
        (["6.6.6.6", "198.51.100.1, 10.0.0.2"], 2, "198.51.100.1"),
        (["6.6.6.6, 198.51.100.1", "10.0.0.2"], 2, "198.51.100.1"),  # hops span the lines
        (["198.51.100.1"], 2, PEER),  # fewer entries than trusted proxies
        ([], 1, PEER),
        (["6.6.6.6, not-an-ip"], 1, PEER),
        (["6.6.6.6, 198.51.100.1:443"], 1, PEER),  # a port is not a bare address
        (["6.6.6.6, unknown"], 1, PEER),
        (["6.6.6.6,"], 1, PEER),  # an empty last entry
        (["2001:db8::1"], 1, "2001:db8::/64"),
    ],
)
def test_behind_n_proxies_the_client_is_n_hops_from_the_right(
    forwarded: list[str], trusted: int, expected: str
) -> None:
    assert client_id(PEER, forwarded, trusted) == expected
    assert (forwarded_client(forwarded, trusted) is None) is (expected == PEER)


def test_only_a_bare_address_is_taken_from_x_forwarded_for() -> None:
    for entry in ("not-an-ip", "198.51.100.1:443", "[2001:db8::1]:443", "unknown", "", "a.b"):
        assert forwarded_client([f"6.6.6.6, {entry}"], 1) is None, entry
    assert forwarded_client(["6.6.6.6, 2001:DB8::1"], 1) == "2001:db8::1"


def test_ipv6_clients_are_counted_by_their_64_block() -> None:
    one = client_id("2001:db8:1:2:aaaa::1", [], 0)
    assert one == "2001:db8:1:2::/64"
    assert client_id("2001:db8:1:2:ffff:ffff:ffff:ffff", [], 0) == one
    assert client_id("2001:db8:1:3::1", [], 0) != one
    assert client_id("::ffff:198.51.100.7", [], 0) == "198.51.100.7"
    assert client_id("fe80::1%eth0", [], 0) == "fe80::/64"
    assert client_id("not an address", [], 0) == "not an address"


def test_spoofed_forwarded_for_buys_no_new_quota_over_http() -> None:
    for trusted in (0, 1, 2):
        app = make_app(Limits(decisions_per_minute=1))
        with serving(app, ServerOptions(trusted_proxies=trusted)) as server:
            port = port_of(server)
            if trusted == 0:  # every request comes from the peer 127.0.0.1: one budget
                assert analyse_over_http(port, "198.51.100.1").status == 200
                assert analyse_over_http(port, "198.51.100.2").status == 429
                assert analyse_over_http(port).status == 429
            elif trusted == 1:
                assert analyse_over_http(port, "198.51.100.1").status == 200
                assert analyse_over_http(port, "198.51.100.2").status == 200
                # a forged left part does not change the entry the proxy wrote
                assert analyse_over_http(port, "203.0.113.9, 198.51.100.1").status == 429
                assert analyse_over_http(port).status == 200  # no header: the peer
            else:
                assert analyse_over_http(port, "6.6.6.6, 198.51.100.3, 10.0.0.1").status == 200
                assert analyse_over_http(port, "7.7.7.7, 198.51.100.3, 10.0.0.2").status == 429
                assert analyse_over_http(port, "198.51.100.3, 10.0.0.1").status == 429


def _analyse_with_lines(port: int, forwarded_lines: Sequence[str]) -> int:
    """POST /api/analyse with one X-Forwarded-For header line per entry (raw socket)."""
    body = json.dumps({"fen": chess.STARTING_FEN}).encode("utf-8")
    head = [
        "POST /api/analyse HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *[f"X-Forwarded-For: {line}" for line in forwarded_lines],
    ]
    request = ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body
    raw = _raw_exchange(port, request)
    return int(raw.split(b" ", 2)[1])


def test_every_forwarded_for_line_counts_over_http() -> None:
    app = make_app(Limits(decisions_per_minute=1))
    with serving(app, ServerOptions(trusted_proxies=1)) as server:
        port = port_of(server)
        # A forged first line, then the line the proxy wrote: the client is 198.51.100.1.
        assert _analyse_with_lines(port, ["203.0.113.9", "198.51.100.1"]) == 200
        assert _analyse_with_lines(port, ["198.51.100.1"]) == 429  # same client, budget spent
        assert _analyse_with_lines(port, ["203.0.113.9"]) == 200  # a different client


# ---------------------------------------------------------------- framing
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://huggingface.co", "https://huggingface.co"),
        ("HTTPS://HuggingFace.CO", "https://huggingface.co"),
        (" https://huggingface.co ", "https://huggingface.co"),
        ("https://blog.example.org:8443", "https://blog.example.org:8443"),
        ("https://x.io:0443", "https://x.io:443"),
        ("https://localhost", "https://localhost"),
    ],
)
def test_frame_ancestor_accepts_exact_https_origins(text: str, expected: str) -> None:
    assert parse_frame_ancestor(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "http://huggingface.co",
        "https://*.hf.space",
        "*",
        "'self'",
        "'none'",
        "https://huggingface.co/",
        "https://huggingface.co/spaces",
        "https://huggingface.co?x=1",
        "https://huggingface.co#top",
        "https://user@huggingface.co",
        "https://huggingface.co:0",
        "https://huggingface.co:65536",
        "https://huggingface.co:",
        "https://",
        "",
        "https://-bad.example",
        "https://under_score.example",
        "https://[::1]",
        "huggingface.co",
        "https://a.example https://b.example",
        "https://a.example;script-src *",
        "data:",
    ],
)
def test_frame_ancestor_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="exact https origin"):
        parse_frame_ancestor(text)


# 253 characters is the longest DNS name; one more is refused.
NAME_253 = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
NAME_254 = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62])


def test_host_names_are_at_most_253_characters() -> None:
    assert (len(NAME_253), len(NAME_254)) == (253, 254)
    assert parse_frame_ancestor(f"https://{NAME_253}") == f"https://{NAME_253}"
    assert parse_allowed_host(NAME_253) == NAME_253
    with pytest.raises(ValueError, match="exact https origin"):
        parse_frame_ancestor(f"https://{NAME_254}")
    with pytest.raises(ValueError, match="not a host name"):
        parse_allowed_host(NAME_254)


def test_frame_ancestors_replace_deny_on_every_response() -> None:
    assert security_headers() is SECURITY_HEADERS
    origins = ("https://huggingface.co", "https://blog.example.org")
    app = make_app()
    options = ServerOptions(frame_ancestors=origins)
    with serving(app, options) as server:
        port = port_of(server)
        responses = [
            call(port, "GET", "/"),
            call(port, "GET", "/healthz"),
            call(port, "GET", "/nope"),
            call(port, "GET", "/healthz", host="evil.example"),
            call(port, "POST", "/api/games", {}),
        ]
    assert [r.status for r in responses] == [200, 200, 404, 421, 201]
    expected = CSP.replace(
        "frame-ancestors 'none'", "frame-ancestors https://huggingface.co https://blog.example.org"
    )
    for resp in responses:
        assert resp.headers["Content-Security-Policy"] == expected
        assert resp.headers["X-Frame-Options"] is None  # DENY would still block the frame
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert resp.headers["Cross-Origin-Resource-Policy"] == "same-origin"


def test_cli_validates_frame_ancestors(capsys: pytest.CaptureFixture[str]) -> None:
    args = build_parser().parse_args(
        [
            "play",
            "--frame-ancestor",
            "https://HuggingFace.co",
            "--frame-ancestor",
            "https://blog.example.org",
        ]
    )
    assert args.frame_ancestor == ["https://huggingface.co", "https://blog.example.org"]
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["play", "--frame-ancestor", "https://*.hf.space"])
    assert exit_info.value.code == 2
    assert "not an exact https origin" in capsys.readouterr().err


# ---------------------------------------------------------------- public host names
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("owner-space.hf.space", "owner-space.hf.space"),
        ("Owner-Space.HF.Space", "owner-space.hf.space"),
        ("my_space.example", "my_space.example"),
        ("demo", "demo"),
    ],
)
def test_allowed_host_accepts_host_names(text: str, expected: str) -> None:
    assert parse_allowed_host(text) == expected


@pytest.mark.parametrize(
    "text",
    ["https://x.hf.space", "x.hf.space:443", "*.hf.space", "x/y", "", "-x.com", "x..y", "a b"],
)
def test_allowed_host_refuses_urls_ports_and_wildcards(text: str) -> None:
    with pytest.raises(ValueError, match="not a host name"):
        parse_allowed_host(text)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["play", "--allowed-host", text])


def test_public_names_pass_the_host_guard_and_health_checks_still_do() -> None:
    app = make_app()
    with serving(app, ServerOptions(public=True, allowed_hosts=(HF,)), public_bind=True) as srv:
        port = port_of(srv)
        for host in (HF, HF.upper(), f"{HF}:443"):
            assert call(port, "GET", "/healthz", headers={"Host": host}, host=None).status == 200
        # The container HEALTHCHECK probes 127.0.0.1 with an IP-literal Host.
        assert call(port, "GET", "/healthz").status == 200
        for host in ("evil.example", f"{HF}.evil.example", f"x{HF}", "hf.space"):
            resp = call(port, "GET", "/api/info", headers={"Host": host}, host=None)
            assert resp.status == 421, host
            assert HF in str(resp.json()["error"])  # the hint names the public host


def test_a_loopback_bind_can_answer_a_public_name_too() -> None:
    # e.g. a reverse proxy on the same machine that keeps the visitor's Host
    app = make_app()
    with serving(app, ServerOptions(allowed_hosts=(HF,))) as server:
        port = port_of(server)
        assert server.loopback
        assert call(port, "GET", "/healthz", headers={"Host": HF}, host=None).status == 200
        assert call(port, "GET", "/healthz", host="10.0.0.1").status == 421


def test_the_origin_check_works_behind_a_proxy() -> None:
    app = make_app()
    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    with serving(app, options, public_bind=True) as server:
        server.log_stream = None
        port = port_of(server)

        def post(host: str, **headers: str) -> int:
            hdrs = {"Host": host, **{k.replace("_", "-"): v for k, v in headers.items()}}
            return call(port, "POST", "/api/games", {}, headers=hdrs, host=None).status

        assert post(HF, Origin=HF_ORIGIN) == 201  # the browser on the public name
        assert post(HF, Origin=HF_ORIGIN, Sec_Fetch_Site="same-origin") == 201
        assert post(HF) == 201  # no Origin (not a browser)
        for origin in (
            "https://evil.hf.space",
            f"https://{HF}.evil.example",
            "https://huggingface.co",  # the embedding page itself never posts
            "null",
        ):
            assert post(HF, Origin=origin) == 403, origin
        assert post(HF, Origin=HF_ORIGIN, Sec_Fetch_Site="cross-site") == 403
        # A proxy that rewrites Host: the page's origin is still our public name.
        assert post(f"127.0.0.1:{port}", Origin=HF_ORIGIN) == 201
        assert post(f"127.0.0.1:{port}", Origin="https://evil.example") == 403
        wrong_type = {"Host": HF, "Origin": HF_ORIGIN, "Content-Type": "text/plain"}
        resp = call(port, "POST", "/api/games", {}, headers=wrong_type, host=None)
        assert resp.status == 415


# ---------------------------------------------------------------- decision rate
def test_decisions_are_rate_limited_per_client() -> None:
    clock = FakeClock()
    app = make_app(Limits(decisions_per_minute=3), now=clock)
    black = game(app, "a", human_color="black")  # CCA (white) moves first
    lab(app, "a")
    lab(app, "a")
    app.think(black, client="a")  # /think counts like the position lab
    err = refused(lambda: lab(app, "a"))
    assert (err.status, err.retry_after) == (429, 20)  # one token per 20 s at 3 per minute
    assert "at most 3 CCA decisions per minute" in err.message
    assert refused(lambda: app.think(black, client="a")).status == 429
    for _ in range(3):
        lab(app, "b")  # another client has its own bucket
    clock.advance(10)  # half a token back: 10 s to go
    assert refused(lambda: lab(app, "a")).retry_after == 10
    clock.advance(10)
    lab(app, "a")
    assert refused(lambda: lab(app, "a")).retry_after == 20
    clock.advance(3600)  # never more than a minute's worth
    for _ in range(3):
        lab(app, "a")
    assert refused(lambda: lab(app, "a")).status == 429


def test_refused_or_invalid_requests_spend_nothing() -> None:
    app = make_app(Limits(decisions_per_minute=2))
    for _ in range(3):
        assert refused(lambda: app.analyse({"fen": "not a fen"}, client="a")).status == 400
        assert refused(lambda: app.analyse({"nodes": 10**9}, client="a")).status == 400
        assert refused(lambda: app.think("A" * 32, client="a")).status == 404
    lab(app, "a")
    lab(app, "a")
    assert refused(lambda: lab(app, "a")).status == 429


class BrokenEngine(FakeEngine):
    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        raise chess.engine.EngineError("engine crashed")


def test_a_decision_the_server_could_not_make_costs_no_token() -> None:
    app = make_app(Limits(decisions_per_minute=1, max_queue=0))
    app._engine_lock.acquire()  # busy
    try:
        for _ in range(3):
            assert refused_at_once(lambda: lab(app, "a")).status == 503
    finally:
        app._engine_lock.release()
    lab(app, "a")  # its one token is still there
    assert refused(lambda: lab(app, "a")).status == 429
    broken = make_app(Limits(decisions_per_minute=1), engine=BrokenEngine())
    for _ in range(3):
        err = refused(lambda: lab(broken, "a"))
        assert (err.status, err.retry_after) == (503, None)  # an engine error: no token spent
    # A refusal that is the client's doing is charged: it is the human's turn here.
    other = make_app(Limits(decisions_per_minute=1))
    white = game(other, "b")
    assert refused(lambda: other.think(white, client="b")).status == 409
    assert refused(lambda: lab(other, "b")).status == 429


def test_a_returned_token_never_overfills_a_bucket() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, now=clock)
    limiter.give_back("unknown")  # nothing to return
    assert limiter.tracked() == 0
    limiter.take("a")
    limiter.give_back("a")
    limiter.give_back("a")  # already full: stays at 2
    limiter.take("a")
    limiter.take("a")
    with pytest.raises(ApiError):
        limiter.take("a")


def test_rate_limiter_memory_is_bounded() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, now=clock, max_clients=4)  # beyond 4: pruned down to 3
    limiter.take("old")
    clock.advance(60)  # "old" is full again: forgotten first
    for client in ("a", "b", "c"):
        limiter.take(client)
    assert limiter.tracked() == 4
    limiter.take("d")  # "old" (full) goes, then "a" (least recently active): down to 3
    assert limiter.tracked() == 3
    limiter.take("a")  # a forgotten client starts with a full bucket
    limiter.take("a")
    with pytest.raises(ApiError):
        limiter.take("a")
    with pytest.raises(ValueError, match="at least 1"):
        RateLimiter(0)


def test_think_over_http_is_charged_to_the_caller() -> None:
    app = make_app(Limits(decisions_per_minute=1))
    with serving(app, ServerOptions(trusted_proxies=1)) as server:
        port = port_of(server)
        a = {"X-Forwarded-For": "198.51.100.1"}
        b = {"X-Forwarded-For": "198.51.100.2"}
        games = [
            str(
                call(port, "POST", "/api/games", {"human_color": "black"}, headers=h).json()[
                    "game_id"
                ]
            )
            for h in (a, b)
        ]
        assert analyse_over_http(port, "198.51.100.1").status == 200  # a's only decision
        resp = call(port, "POST", f"/api/games/{games[0]}/think", {}, headers=a)
        assert (resp.status, resp.headers["Retry-After"]) == (429, "60")
        assert call(port, "POST", f"/api/games/{games[1]}/think", {}, headers=b).status == 200


def test_rate_limiter_forgets_full_buckets_before_active_ones() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, now=clock, max_clients=4)  # one token back every 30 s
    limiter.take("busy")
    limiter.take("busy")  # empty at t=0
    clock.advance(1)
    limiter.take("idle-1")  # more recent than "busy", but full again 30 s later
    limiter.take("idle-2")
    clock.advance(30.5)
    limiter.take("new-1")
    limiter.take("new-2")  # five buckets: the two full ones go, not the least recent one
    assert limiter.tracked() == 3
    limiter.take("busy")  # ~1 token had come back: remembered, so only one decision is left
    with pytest.raises(ApiError):
        limiter.take("busy")


def test_rate_limiter_forgets_the_least_recently_active_client_first() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, now=clock, max_clients=4)  # nobody is full again below
    for client in ("a", "b", "c", "d", "a"):  # "a" is now the most recently active
        limiter.take(client)
    limiter.take("e")  # five buckets, none full: "b" and "c" go (down to 3), not "a"
    assert limiter.tracked() == 3
    limiter.take("a")  # remembered: its third and last token
    with pytest.raises(ApiError):
        limiter.take("a")
    for _ in range(3):
        limiter.take("b")  # forgotten: a full bucket again


def test_rate_limiter_prunes_rarely_under_a_flood_of_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Load-review finding: past MAX_TRACKED_CLIENTS every take scanned the whole table (about
    # 0.9 ms per request under the limiter's lock). Pruned to 3/4 in one pass, a scan now
    # runs once per quarter of the table's worth of new clients.
    clock = FakeClock()
    limiter = RateLimiter(20, now=clock, max_clients=8)  # beyond 8: down to 6
    scans = 0
    forget = RateLimiter._forget

    def counted(self: RateLimiter, now: float) -> None:
        nonlocal scans
        scans += 1
        forget(self, now)

    monkeypatch.setattr(RateLimiter, "_forget", counted)
    for i in range(100):  # 100 identities, one decision each, none full again meanwhile
        limiter.take(f"198.51.100.{i}")
    assert limiter.tracked() <= 8
    assert scans <= (100 - 8) // 3 + 1, scans  # (every take past the 8th, before: 92)


def test_too_many_requests_at_once_cost_no_decision_token() -> None:
    clock = FakeClock()
    app = make_app(Limits(decisions_per_minute=3, max_queue=6), now=clock)
    done: list[object] = []
    app._engine_lock.acquire()  # another decision runs
    try:
        workers = [
            threading.Thread(target=lambda: done.append(lab(app, "a")), daemon=True)
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        _wait_for(lambda: app._gate.waiting == 2)
        err = refused_at_once(lambda: lab(app, "a"))
        assert (err.status, err.limit) == (429, "requests_at_once")
    finally:
        app._engine_lock.release()
    for worker in workers:
        worker.join(10)
    assert len(done) == 2
    lab(app, "a")  # the refused request was not charged: the third token is still there
    err = refused(lambda: lab(app, "a"))
    assert (err.status, err.limit) == (429, "decisions_per_minute")


def test_rate_limit_over_http_sends_retry_after() -> None:
    app = make_app(Limits(decisions_per_minute=1))
    with serving(app) as server:
        port = port_of(server)
        assert analyse_over_http(port).status == 200
        resp = analyse_over_http(port)
        assert resp.status == 429
        assert resp.headers["Retry-After"] == "60"
        assert resp.headers["Content-Security-Policy"] == CSP
        assert "slow down" in str(resp.json()["error"])


# ---------------------------------------------------------------- engine queue
def test_gate_estimates_retry_after_from_hold_times() -> None:
    clock = FakeClock()
    lock = threading.Lock()
    gate = EngineGate(lock, 0, now=clock)
    with gate:
        clock.advance(10)
    assert not lock.locked()

    def enter() -> None:
        with gate:
            pytest.fail("entered a busy gate")

    lock.acquire()  # another decision runs
    try:
        err = refused_at_once(enter)
    finally:
        lock.release()
    # 0.7 * 2 s (initial estimate) + 0.3 * 10 s = 4.4 s -> 5
    assert (err.status, err.retry_after) == (503, 5)
    assert "busy" in err.message
    assert gate.waiting == 0
    with gate:  # free again: admitted at once
        assert lock.locked()


def test_a_local_gate_is_the_plain_lock() -> None:
    lock = threading.Lock()
    gate = EngineGate(lock, None)
    lock.acquire()
    entered = threading.Event()

    def enter() -> None:
        with gate:
            entered.set()

    worker = threading.Thread(target=enter, daemon=True)
    worker.start()
    assert not entered.wait(0.2)  # waits, however long the line: never refused
    lock.release()
    assert entered.wait(5)
    worker.join(5)


def _wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_requests_beyond_the_queue_get_503_busy() -> None:
    app = make_app(Limits(max_queue=1))
    results: list[object] = []
    app._engine_lock.acquire()  # a long decision is running
    waiter = threading.Thread(target=lambda: results.append(lab(app, "a")), daemon=True)
    try:
        waiter.start()
        _wait_for(lambda: app._gate.waiting == 1)
        limits = app.info()["limits"]
        assert isinstance(limits, dict)
        assert limits["waiting"] == 1
        err = refused_at_once(lambda: lab(app, "b"))
        assert err.status == 503
        assert err.retry_after is not None
        assert err.retry_after >= 1
        assert (
            refused_at_once(lambda: app.create_game({}, client="c")).status == 503
        )  # uses the lock
    finally:
        app._engine_lock.release()
    waiter.join(10)
    assert len(results) == 1  # the one in the line was served
    assert app._gate.waiting == 0
    lab(app, "b")


def test_one_client_cannot_fill_the_engine_queue() -> None:
    app = make_app(Limits(max_queue=6))
    done: list[object] = []

    def start(client: str) -> threading.Thread:
        worker = threading.Thread(target=lambda: done.append(lab(app, client)), daemon=True)
        worker.start()
        return worker

    workers: list[threading.Thread] = []
    app._engine_lock.acquire()  # a long decision is running
    try:
        workers += [start("a"), start("a")]
        _wait_for(lambda: app._gate.waiting == 2)
        err = refused_at_once(lambda: lab(app, "a"))
        assert err.status == 429
        assert err.retry_after is not None
        assert "too many requests at once" in err.message
        assert refused_at_once(lambda: app.create_game({}, client="a")).status == 429
        workers.append(start("b"))  # another client still gets a place in the line
        _wait_for(lambda: app._gate.waiting == 3)
    finally:
        app._engine_lock.release()
    for worker in workers:
        worker.join(10)
    assert len(done) == 3
    assert app._gate.waiting == 0
    lab(app, "a")  # its places were given back


def test_gate_slots_count_per_client_and_anonymous_entries_do_not() -> None:
    lock = threading.Lock()
    gate = EngineGate(lock, 1)
    with gate.slot("a"):
        assert lock.locked()
    lock.acquire()
    entered: list[str] = []

    def enter() -> None:
        gate.acquire("a")
        entered.append("in")

    worker = threading.Thread(target=enter, daemon=True)
    worker.start()
    _wait_for(lambda: gate.waiting == 1)
    assert refused_at_once(lambda: gate.acquire(None)).status == 503  # the line (1) is full
    lock.release()
    worker.join(10)
    assert entered == ["in"]
    gate.release()
    assert gate.waiting == 0


def test_busy_over_http_sends_retry_after() -> None:
    app = make_app(Limits(max_queue=0))
    with serving(app) as server:
        port = port_of(server)
        app._engine_lock.acquire()
        try:
            resp = analyse_over_http(port)
        finally:
            app._engine_lock.release()
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "2"  # the initial 2 s estimate
        assert "busy" in str(resp.json()["error"])
        assert resp.json()["limit"] == "max_queue"
        assert analyse_over_http(port).status == 200


def test_busy_retry_after_is_capped() -> None:
    clock = FakeClock()
    lock = threading.Lock()
    gate = EngineGate(lock, 0, now=clock)
    with gate:
        clock.advance(10_000)  # a pathological hold time
    lock.acquire()
    try:
        err = refused_at_once(lambda: gate.acquire("b"))
    finally:
        lock.release()
    assert (err.status, err.retry_after) == (503, EngineGate.MAX_RETRY_AFTER_S)
    assert EngineGate.MAX_RETRY_AFTER_S == 300


def test_think_waits_in_the_bounded_line() -> None:
    app = make_app(Limits(max_queue=0, decisions_per_minute=5))
    black = game(app, "a", human_color="black")  # CCA (white) to move
    app._engine_lock.acquire()  # another decision runs
    try:
        err = refused_at_once(lambda: app.think(black, client="a"))
    finally:
        app._engine_lock.release()
    assert (err.status, err.limit) == (503, "max_queue")
    assert err.retry_after is not None
    app.think(black, client="a")  # free again: served


def test_think_takes_one_of_its_clients_places() -> None:
    app = make_app(Limits(max_queue=6))
    black = game(app, "a", human_color="black")
    done: list[object] = []
    app._engine_lock.acquire()
    try:
        workers = [
            threading.Thread(target=lambda: done.append(lab(app, "a")), daemon=True)
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        _wait_for(lambda: app._gate.waiting == 2)
        err = refused_at_once(lambda: app.think(black, client="a"))
        assert (err.status, err.limit) == (429, "requests_at_once")
    finally:
        app._engine_lock.release()
    for worker in workers:
        worker.join(10)
    assert len(done) == 2


def test_places_in_the_line_are_given_back() -> None:
    app = make_app(Limits(max_queue=6))
    for _round in range(3):
        done: list[object] = []
        app._engine_lock.acquire()
        try:
            workers = [
                threading.Thread(target=lambda done=done: done.append(lab(app, "a")), daemon=True)
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            _wait_for(lambda: app._gate.waiting == 2)  # later rounds: still admitted, not 429
        finally:
            app._engine_lock.release()
        for worker in workers:
            worker.join(10)
        assert len(done) == 2


class PausingLock:
    """An engine lock whose takers pause before taking it (``go`` lets them through), as when
    a burst of requests is let into the line of a free engine that none of them holds yet."""

    def __init__(self) -> None:
        self.inner = threading.Lock()
        self.go = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.go.wait(10)
        return self.inner.acquire(blocking, timeout)

    def release(self) -> None:
        self.inner.release()

    def locked(self) -> bool:
        return self.inner.locked()


def test_a_burst_on_a_free_engine_cannot_overfill_the_line() -> None:
    def take(gate: EngineGate, client: str, results: list[object]) -> threading.Thread:
        def run() -> None:
            try:
                gate.acquire(client)
            except ApiError as exc:
                results.append(exc.status)
            else:
                results.append("in")
                gate.release()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker

    lock = PausingLock()
    gate = EngineGate(lock, 0)  # nobody may wait
    results: list[object] = []
    first = take(gate, "a", results)
    _wait_for(lambda: gate.waiting == 1)  # let in on a free engine, not holding it yet
    assert not lock.locked()
    err = refused_at_once(lambda: gate.acquire("b"))  # it would wait behind "a"
    assert (err.status, err.limit) == (503, "max_queue")
    lock.go.set()
    first.join(10)
    assert results == ["in"]

    lock = PausingLock()
    gate = EngineGate(lock, 1)  # one may wait: behind the one about to take the free engine
    results = []
    workers = [take(gate, "a", results)]
    _wait_for(lambda: gate.waiting == 1)
    workers.append(take(gate, "b", results))  # admitted: the line holds one
    _wait_for(lambda: gate.waiting == 2)
    err = refused_at_once(lambda: gate.acquire("c"))  # a second in the line: refused
    assert (err.status, err.limit) == (503, "max_queue")
    lock.go.set()
    for worker in workers:
        worker.join(10)
    assert results == ["in", "in"]

    lock = PausingLock()
    gate = EngineGate(lock, 6)
    results = []
    workers = [take(gate, "a", results), take(gate, "a", results)]
    _wait_for(lambda: gate.waiting == 2)
    assert not lock.locked()
    err = refused_at_once(lambda: gate.acquire("a"))  # a third place for "a": refused
    assert (err.status, err.limit) == (429, "requests_at_once")
    workers.append(take(gate, "b", results))  # another client still gets in
    _wait_for(lambda: gate.waiting == 3)
    lock.go.set()
    for worker in workers:
        worker.join(10)
    assert results == ["in"] * 3
    assert gate.waiting == 0
    with gate.slot("a"):  # a free engine with nobody about to take it admits at once
        assert lock.locked()


# ---------------------------------------------------------------- sessions
def test_a_client_keeps_at_most_its_quota_of_games() -> None:
    app = make_app(Limits(max_sessions_per_client=2))
    a1, a2 = game(app, "a"), game(app, "a")
    b1, b2 = game(app, "b"), game(app, "b")
    app.game_state(a1)  # a2 is now a's least recently used game
    a3 = game(app, "a")
    assert app.session_count() == 4
    assert alive(app, a1, a2, a3, b1, b2) == [True, False, True, True, True]


def test_a_full_table_first_drops_the_creators_own_game() -> None:
    app = make_app(Limits(max_sessions_per_client=3), max_sessions=4)
    a1, a2 = game(app, "a"), game(app, "a")
    b1, b2 = game(app, "b"), game(app, "b")
    a3 = game(app, "a")  # a is under its quota, but the table is full
    assert alive(app, a1, a2, a3, b1, b2) == [False, True, True, True, True]


def test_a_full_table_then_drops_a_finished_game() -> None:
    app = make_app(Limits(max_sessions_per_client=2), max_sessions=3)
    b1, c1, d1 = game(app, "b"), game(app, "c"), game(app, "d")
    app.resign(b1)  # finished (and now the most recently used game)
    e1 = game(app, "e")
    assert alive(app, b1, c1, d1, e1) == [False, True, True, True]


def test_a_full_table_then_takes_from_a_client_holding_its_whole_quota() -> None:
    app = make_app(Limits(max_sessions_per_client=2), max_sessions=3)
    c1 = game(app, "c")  # the least recently used game, but c is below its quota
    b1, b2 = game(app, "b"), game(app, "b")
    e1 = game(app, "e")
    assert alive(app, c1, b1, b2, e1) == [True, False, True, True]


def test_a_full_table_never_takes_games_in_play_of_clients_below_quota() -> None:
    assert PlayApp.RECLAIM_UNPLAYED_S == 300.0
    clock = FakeClock()
    limits = Limits(max_sessions_per_client=2, session_idle_minutes=30)
    app = make_app(limits, max_sessions=2, now=clock)
    b1 = game(app, "b", human_color="black")  # CCA's move (the page asks for it at once)
    clock.advance(100)
    c1 = game(app, "c", human_color="black")
    clock.advance(100)
    err = refused(lambda: game(app, "e"))
    assert (err.status, err.retry_after) == (503, 100)  # b1 may be reclaimed in 100 s
    assert "all 2 game slots are in use" in err.message
    assert app.session_count() == 2
    clock.advance(100)  # nothing played in b1 for 300 s: a newcomer may take it, not c1
    e1 = game(app, "e")
    assert alive(app, c1, e1) == [True, True]
    gone = refused(lambda: app.game_state(b1))
    assert gone.status == 404
    assert gone.message == (
        "no such game any more: every game slot was in use and this game went to a newcomer, "
        "as nothing had been played in it for 5 minutes"
    )
    no_idle = make_app(Limits(max_sessions_per_client=2), max_sessions=1)
    game(no_idle, "b", human_color="black")
    assert refused(lambda: game(no_idle, "e")).retry_after == 300  # when "b" is reclaimable
    short = make_app(Limits(max_sessions_per_client=2, session_idle_minutes=1), max_sessions=1)
    game(short, "b", human_color="black")
    assert refused(lambda: game(short, "e")).retry_after == 60  # it expires first


def test_identities_below_quota_must_keep_playing_to_keep_newcomers_out() -> None:
    # Load-review finding (S3): 24 identities x 2 games held all 48 slots of the Space with one
    # GET per game every 29 minutes; newcomers got 503 for hours. Now a game in which nothing
    # was played (no new game, no CCA decision, no move further than before) for
    # RECLAIM_UNPLAYED_S (CCA's move) goes to a newcomer, whoever owns it; reading a game does
    # not keep it. Holding the table takes playing or restarting every game within the rules'
    # times (see PlayApp._make_room for what that still allows).
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=48, now=clock)
    owners = [f"2001:db8:0:{i:x}::1" for i in range(24) for _ in range(2)]
    held = [game(app, owner, human_color="black") for owner in owners]
    clock.advance(200)
    app.think(held[0], client=owners[0])  # CCA plays in the first game: it is in play
    clock.advance(PlayApp.RECLAIM_UNPLAYED_S - 201)  # 299 s after the games began
    for game_id in held:  # every game read (the attack), which keeps none of them now
        app.game_state(game_id)
    assert refused(lambda: game(app, "newcomer")).retry_after == 1  # the first: in 1 s
    clock.advance(1)
    newcomers = [game(app, f"198.51.100.{i}") for i in range(3)]
    assert alive(app, *newcomers) == [True] * 3
    assert alive(app, held[0]) == [True]  # the game in play was not taken
    assert alive(app, *held[1:4]) == [False] * 3  # the longest unplayed ones went
    assert all(alive(app, *held[4:]))


def _in_play(app: PlayApp, client: str) -> str:
    """A new game of ``client`` in which it has moved and CCA has answered: its move now."""
    game_id = game(app, client, human_color="white")
    app.move(game_id, {"uci": "e2e4"})
    app.think(game_id, client=client)
    return game_id


def _thinking_victim(max_sessions: int) -> tuple[FakeClock, PlayApp, str]:
    """A public-mode app whose table is full, with the victim's game the least recently played:
    the victim moved, CCA answered, and the victim is now thinking about its next move."""
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=max_sessions, now=clock)
    victim = _in_play(app, "victim")
    clock.advance(200)
    for n in range(max_sessions - 1):  # other visitors fill the table, playing more recently
        _in_play(app, f"198.51.100.{n}")
    return clock, app, victim


def test_a_player_thinking_about_a_move_keeps_the_game() -> None:
    # Round-3 finding: a game was taken for a newcomer once CCA had not decided in it for
    # 300 s, so a visitor who thought 5 minutes about one move (the page open, reading the
    # state every 10 s) lost the game. While it is the human's move the game now keeps its
    # slot for RECLAIM_THINKING_S (15 minutes), and the human's move counts as play.
    assert PlayApp.RECLAIM_THINKING_S == 900.0
    clock, app, victim = _thinking_victim(1)  # (the table: this game alone)
    for _ in range(10):  # 301 s after CCA's move, the page reading the state meanwhile
        clock.advance(10.1)
        app.game_state(victim)
    err = refused(lambda: game(app, "newcomer"))
    assert (err.status, err.limit, err.retry_after) == (503, "max_sessions", 599)
    app.move(victim, {"uci": "d2d4"})  # the victim's game is still there
    clock.advance(PlayApp.RECLAIM_UNPLAYED_S - 1)  # its move counts: now CCA's move, 299 s on
    refused(lambda: game(app, "newcomer"))
    app.think(victim, client="victim")
    clock.advance(PlayApp.RECLAIM_THINKING_S - 1)  # thinking again, 899 s
    refused(lambda: game(app, "newcomer"))
    clock.advance(1)  # 15 minutes without a move: now the game may go to a newcomer
    newcomer = game(app, "newcomer")
    assert alive(app, newcomer) == [True]
    gone = refused(lambda: app.move(victim, {"uci": "g1f3"}))
    assert gone.status == 404
    assert "went to a newcomer, as nothing had been played in it for 15 minutes" in gone.message


def test_a_move_played_again_after_a_take_back_does_not_keep_a_game() -> None:
    # Only a move further than the game had been counts as play: a take-back and the same
    # move again (free, no engine time) would otherwise keep a game in the table for ever.
    clock, app, victim = _thinking_victim(2)
    session = app.session(victim)
    assert session.reached == 2
    clock.advance(PlayApp.RECLAIM_THINKING_S - 201)  # thinking for 899 s: kept
    refused(lambda: game(app, "newcomer"))
    for _ in range(3):  # take back e2e4 (and CCA's answer), play it again
        app.undo(victim)
        app.move(victim, {"uci": "e2e4"})
    assert session.reached == 2
    # CCA's move now, and nothing played for 899 s (more than RECLAIM_UNPLAYED_S): the game
    # may go to a newcomer. Had the moves counted, it would be kept for 300 s more.
    game(app, "newcomer")
    assert alive(app, victim) == [False]


def test_only_a_move_further_than_the_game_had_been_counts_as_play() -> None:
    app = make_app()
    session = app.session(game(app, None, human_color="white"))
    assert session.human_move("e2e4") is True  # 1 move: further than ever
    app.think(session.id)
    assert session.reached == 2  # CCA's move counts too
    assert session.human_move("d2d4") is True  # 3 moves
    session.undo()  # only d2d4 goes (CCA has not answered it): 2 moves
    assert session.human_move("d2d4") is False  # 3 moves again: no further
    session.undo()
    assert session.human_move("g1f3") is False  # another move, the same length
    session.undo()
    session.undo()  # e2e4 and CCA's answer go: no move left
    assert session.human_move("d2d4") is False
    assert session.reached == 3


def test_a_game_whose_decision_waits_for_the_engine_is_never_taken() -> None:
    # Round-3 finding: a game could be taken for a newcomer while its own /think waited in
    # the engine line; the decision was then made and charged for a game that returned 404.
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=2, now=clock)
    victim = game(app, "victim", human_color="black")  # CCA (white) to move
    clock.advance(PlayApp.RECLAIM_UNPLAYED_S + 1)  # e.g. its first /think was refused
    game(app, "other", human_color="white")  # the table is now full
    outcome: dict[str, object] = {}

    def create() -> None:
        try:
            outcome["newcomer"] = game(app, "newcomer")
        except ApiError as exc:
            outcome["newcomer"] = exc

    app._engine_lock.acquire()  # a decision runs
    try:
        creator = threading.Thread(target=create, daemon=True)
        creator.start()  # checked the table (the victim's game could go), waits for the engine
        _wait_for(lambda: app._gate.waiting == 1)
        thinker = threading.Thread(
            target=lambda: outcome.__setitem__("think", app.think(victim, client="victim")),
            daemon=True,
        )
        thinker.start()
        _wait_for(lambda: app._gate.waiting == 2)
    finally:
        app._engine_lock.release()
    creator.join(10)
    thinker.join(10)
    think = outcome["think"]
    assert isinstance(think, dict)
    assert think["move"]
    refusal = outcome["newcomer"]
    assert isinstance(refusal, ApiError)  # refused when it came to store its game
    assert (refusal.status, refusal.limit) == (503, "max_sessions")
    # When "other" may go (nobody has moved in it); the victim's game is in play.
    assert refusal.retry_after == 300
    assert alive(app, victim) == [True]
    assert app._thinking == {}  # (every /think is uncounted when it ends)


def test_a_game_nobody_has_moved_in_keeps_its_slot_for_five_minutes_only() -> None:
    # Round-4 finding (verifier probe P2): RECLAIM_THINKING_S also went to the game the page
    # starts for every visitor (the human's move, nobody has moved yet), so visitors who opened
    # the page and left held a slot for 15 minutes: with 12 slots, 2 such visitors a minute
    # were refused 60% of the time. Only a player who has moved gets 15 minutes on its move.
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    first = game(app, "visitor")  # the page's first game: white, the human's move
    assert refused(lambda: game(app, "newcomer")).retry_after == 300
    clock.advance(PlayApp.RECLAIM_UNPLAYED_S)
    newcomer = game(app, "newcomer")
    assert alive(app, first, newcomer) == [False, True]
    app.move(newcomer, {"uci": "e2e4"})  # CCA's move now: 5 minutes
    assert refused(lambda: game(app, "third")).retry_after == 300
    app.think(newcomer, client="newcomer")  # the move of a player who has moved: 15 minutes
    assert refused(lambda: game(app, "third")).retry_after == 900
    app.undo(newcomer)  # no move of the player's left: 5 minutes (from the last play)
    assert refused(lambda: game(app, "third")).retry_after == 300
    black = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    second = game(black, "b", human_color="black")
    black.think(second, client="b")  # CCA opened: the human's move, but it has not moved yet
    assert refused(lambda: game(black, "newcomer")).retry_after == 300
    black.move(second, {"uci": "e7e5"})
    black.think(second, client="b")
    assert refused(lambda: game(black, "newcomer")).retry_after == 900


def test_visitors_who_open_the_page_and_leave_do_not_fill_a_small_table() -> None:
    # Verifier probe P2 as a guard: 2 visitors a minute, each opening the page (which starts
    # a game) and leaving, on the Render Blueprint's 12 slots: none is refused (60% were).
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=12, now=clock)
    statuses = []
    for n in range(2 * 60):  # an hour
        clock.advance(30)
        try:
            game(app, f"10.0.{n // 256}.{n % 256}")
        except ApiError as exc:
            statuses.append(exc.status)
        else:
            statuses.append(201)
    assert statuses == [201] * len(statuses)


def test_restarting_games_never_keeps_newcomers_out() -> None:
    # Round-4 finding (verifier probe P1): a new game counted as play, and on a full table it
    # replaces its owner's own older game; so 6 identities (IPv6 /64s) with 2 games each held
    # the Render Blueprint's 12 slots for hours by starting a game every 440 s each (0.8
    # requests a minute, no engine time): 0 of 180 newcomers got in. A game started in place
    # of its owner's older one now keeps that game's time; only play keeps a slot. (Here each
    # identity starts a game every 2 minutes, which renews each of its games within 5 minutes.)
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=12, now=clock)
    ids = [f"2001:db8:0:{i:x}::1" for i in range(6)]
    for owner in ids:
        game(app, owner)
        game(app, owner)
    refusals: list[tuple[int, str | None] | None] = []
    for minute in range(1, 91):
        clock.advance(60)
        if minute % 2 == 0:  # every 2 minutes each identity starts a game again
            for owner in ids:
                with contextlib.suppress(ApiError):
                    game(app, owner)
        try:
            game(app, f"198.51.100.{minute}")
        except ApiError as exc:
            refusals.append((exc.status, exc.limit))
        else:
            refusals.append(None)
    assert refusals[:4] == [(503, "max_sessions")] * 4  # the first 5 minutes: all games new
    assert refusals[4:] == [None] * 86  # then every newcomer gets in


@pytest.mark.parametrize(("slots", "restart_s"), [(12, 145), (48, 145), (12, 60)])
def test_restarting_games_and_moving_first_never_keeps_newcomers_out(
    slots: int, restart_s: int
) -> None:
    # Round-5 finding (verifier probe vb_restart_move): the player's first move in a restarted
    # game counted as play, so restarting a game and playing e2e4 in it renewed its slot: 6
    # identities (IPv6 /64s) with 2 games each held the Render Blueprint's 12 slots, 24 the
    # Space's 48, for hours (0 of 180 newcomers admitted, 0 engine evaluations). A human move
    # now counts only in a game in which CCA has decided: holding a slot costs engine time.
    clock = FakeClock()
    engine = FakeEngine()
    app = make_app(PUBLIC_LIMITS, max_sessions=slots, now=clock, engine=engine)
    ids = [f"2001:db8:7:{i:x}::1" for i in range(slots // 2)]
    for owner in ids:
        game(app, owner)
        game(app, owner)
    evaluations = engine.calls
    refusals: list[tuple[int, str | None] | None] = []
    next_restart, next_newcomer = clock.t + restart_s, clock.t + 60
    for n in range(1, 46):  # a newcomer a minute for 45 minutes
        while next_restart <= next_newcomer:  # each identity starts a game, moves first in it
            clock.t = next_restart
            for owner in ids:
                with contextlib.suppress(ApiError):
                    app.move(game(app, owner), {"uci": "e2e4"})
            next_restart += restart_s
        clock.t = next_newcomer
        try:
            game(app, f"198.51.100.{n}")
        except ApiError as exc:
            refusals.append((exc.status, exc.limit))
        else:
            refusals.append(None)
        next_newcomer += 60
    assert refusals[:4] == [(503, "max_sessions")] * 4  # the first 5 minutes: all games new
    assert refusals[4:] == [None] * 41  # then every newcomer gets in
    assert engine.calls == evaluations  # (the attack cost no engine time: it no longer holds)


def test_a_players_move_counts_as_play_once_cca_has_decided_in_the_game() -> None:
    # A first move costs no engine time, so it keeps nothing; the page asks for CCA's reply at
    # once, and that decision counts. From then on the player's moves count as play too.
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    first = game(app, "player")
    clock.advance(200)
    app.move(first, {"uci": "e2e4"})  # (CCA's reply never asked for, or refused)
    assert refused(lambda: game(app, "newcomer")).retry_after == 100  # not 300
    clock.advance(100)
    game(app, "newcomer")
    gone = refused(lambda: app.game_state(first))
    assert gone.message.endswith("as CCA had not played in it for 5 minutes")
    app = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    second = game(app, "player")
    app.move(second, {"uci": "e2e4"})
    clock.advance(200)
    app.think(second, client="player")  # a decision: play, 15 minutes on the player's move
    assert refused(lambda: game(app, "newcomer")).retry_after == 900
    clock.advance(800)
    app.move(second, {"uci": "d2d4"})  # after CCA's decision, the player's move is play
    assert refused(lambda: game(app, "newcomer")).retry_after == 300
    black = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    third = game(black, "player", human_color="black")
    black.think(third, client="player")  # CCA opens: its decision counts
    clock.advance(200)
    black.move(third, {"uci": "e7e5"})  # the player's first move comes after it: play
    assert refused(lambda: game(black, "newcomer")).retry_after == 300


def test_a_game_started_in_place_of_an_older_one_keeps_its_time() -> None:
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=2, now=clock)
    older = game(app, "a")
    other = _in_play(app, "b")  # 15 minutes
    clock.advance(200)
    again = game(app, "a")  # the table is full: a's older game goes, and gives its time
    assert alive(app, older) == [False]
    assert refused(lambda: game(app, "newcomer")).retry_after == 100  # not 300
    clock.advance(100)
    game(app, "newcomer")
    gone = refused(lambda: app.game_state(again))
    assert gone.message == (
        "no such game any more: every game slot was in use and this game went to a newcomer, "
        "as nothing had been played in it, nor in the older game of its player's it replaced, "
        "for 5 minutes"
    )
    assert app._carried == set()  # (forgotten with the game)
    # A game replacing one played more recently than 5 minutes ago starts afresh: never more
    # than a new game's own 5 minutes. And once played, a game keeps no borrowed time.
    renewed = game(app, "b")  # replaces b's game in play, played 300 s ago (15 minutes)
    assert alive(app, other) == [False]
    assert refused(lambda: game(app, "third")).retry_after == 300
    clock.advance(200)
    later = game(app, "newcomer")  # replaces the newcomer's game, 200 s old: 100 s left
    assert app._carried == {later}
    app.move(later, {"uci": "e2e4"})  # a first move is no play (see the next tests)
    assert app._carried == {later}
    app.think(later, client="newcomer")  # CCA's reply is: a time of its own now
    assert app._carried == set()
    app.move(later, {"uci": "d2d4"})  # a move after CCA's: play, 5 minutes from here
    clock.advance(100)
    game(app, "third")  # takes renewed (5 minutes unplayed); later has 200 s left
    assert alive(app, renewed) == [False]
    assert refused(lambda: game(app, "fourth")).retry_after == 200
    clock.advance(200)
    game(app, "fourth")
    gone = refused(lambda: app.game_state(later))
    assert gone.message.endswith("as nothing had been played in it for 5 minutes")
    assert app._decided == set()  # (forgotten with the game)


def test_a_game_started_in_place_of_a_finished_one_keeps_that_ones_time() -> None:
    # Verifier mutant (finished_replaced_game_carries_nothing) as a guard: a player who
    # resigns and starts a new game on a full table keeps the time of the game it resigned
    # (here 15 minutes on its move from CCA's last decision, 700 s left: more than a new
    # game's 5 minutes, so the new game starts afresh). Had the finished game passed on no
    # time, the new game could go to the next newcomer at once.
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=2, now=clock)
    player = "198.51.100.1"
    resigned = _in_play(app, player)
    game(app, "198.51.100.2")  # the table is full
    clock.advance(200)
    app.resign(resigned)
    again = game(app, player)  # in place of the player's finished game
    assert app._carried == set()
    clock.advance(1)
    err = refused(lambda: game(app, "203.0.113.9"))
    assert (err.status, err.limit, err.retry_after) == (503, "max_sessions", 99)
    assert alive(app, again) == [True]


@pytest.mark.parametrize("refusal", [409, 429, 503])
def test_a_refused_think_does_not_keep_a_game(refusal: int) -> None:
    # Verifier mutant (failed_think_counts_as_play): only a decision made counts as play. A
    # /think refused (409: not CCA's move; 429: over the rate; 503: the engine failed) would
    # otherwise keep a game in the table for nothing.
    clock = FakeClock()
    limits = dataclasses.replace(PUBLIC_LIMITS, decisions_per_minute=1)
    engine = BrokenEngine() if refusal == 503 else FakeEngine()
    app = make_app(limits, max_sessions=1, now=clock, engine=engine)
    held = game(app, "holder", human_color="white" if refusal == 409 else "black")
    clock.advance(PlayApp.RECLAIM_UNPLAYED_S - 1)
    if refusal == 429:
        lab(app, "holder")  # the holder's one decision this minute
    assert refused(lambda: app.think(held, client="holder")).status == refusal
    clock.advance(1)
    game(app, "newcomer")  # nothing was played in the holder's game for 5 minutes
    assert alive(app, held) == [False]


def test_the_quota_rule_never_takes_a_game_whose_decision_is_in_progress() -> None:
    # Verifier mutant (quota_rule_takes_thinking_game): a client holding its whole share loses
    # its least recently used game to a newcomer, but never one whose /think is under way.
    app = make_app(PUBLIC_LIMITS, max_sessions=3)
    busy, idle, other = (game(app, "a", human_color="black") for _ in range(3))
    app._engine_lock.acquire()  # someone's decision holds the engine: busy's /think waits
    try:
        thinker = threading.Thread(target=lambda: app.think(busy, client="a"), daemon=True)
        thinker.start()
        _wait_for(lambda: app._gate.waiting == 1)
        app.game_state(idle)
        app.game_state(other)  # busy is now a's least recently used game
        with app._state_lock:
            plan = app._room_plan("b", app._now())
    finally:
        app._engine_lock.release()
    thinker.join(10)
    assert plan == [(idle, "its player had 3 games, the most one player may keep")]


def test_the_reasons_of_taken_games_are_remembered_for_a_bounded_number() -> None:
    # Verifier mutant (taken_unbounded): the reasons kept for 404s are bounded.
    clock = FakeClock()
    app = make_app(PUBLIC_LIMITS, max_sessions=1, now=clock)
    ids = [game(app, "c0")]
    for n in range(1, PlayApp.TAKEN_REMEMBERED + 2):  # each game taken by the next one
        clock.advance(PlayApp.RECLAIM_UNPLAYED_S)
        ids.append(game(app, f"c{n}"))
    assert len(app._taken) == PlayApp.TAKEN_REMEMBERED
    assert "went to a newcomer" not in refused(lambda: app.game_state(ids[0])).message
    assert "went to a newcomer" in refused(lambda: app.game_state(ids[1])).message


def test_a_full_table_refuses_a_new_game_before_taking_the_engine() -> None:
    # Round-2 finding: a creation took the engine (to build its agent) before the table could
    # refuse it. Now the refusal comes first: with the engine busy it still comes at once.
    app = make_app(Limits(max_sessions_per_client=3), max_sessions=2)
    kept = [game(app, "a"), game(app, "b")]  # both below their quota, unfinished
    app._engine_lock.acquire()  # someone else's decision holds the engine
    try:
        err = refused_at_once(lambda: app.create_game({}, client="c"))
    finally:
        app._engine_lock.release()
    assert (err.status, err.limit) == (503, "max_sessions")
    assert alive(app, *kept) == [True, True]  # the check dropped nothing
    own = game(app, "a")  # room is still made as before: here, a's own older game goes
    assert alive(app, *kept, own) == [False, True, True]


def test_room_is_made_again_when_the_game_is_stored() -> None:
    # The table is checked before the engine is taken and again when the game is stored: a
    # game that went idle while the creation waited for the engine is expired then, rather
    # than the victim the first check had in mind (here the creator's own older game).
    clock = FakeClock()
    app = make_app(
        Limits(max_sessions_per_client=3, session_idle_minutes=30, max_queue=6),
        max_sessions=2,
        now=clock,
    )
    other = game(app, "b")
    clock.advance(29 * 60)
    older = game(app, "a")  # the table is full; b's game expires in one minute
    created: list[dict[str, object]] = []
    app._engine_lock.acquire()
    try:
        worker = threading.Thread(
            target=lambda: created.append(app.create_game({}, client="a")), daemon=True
        )
        worker.start()
        _wait_for(lambda: app._gate.waiting == 1)  # checked (a's older game would go), waiting
        clock.advance(2 * 60)  # meanwhile b's game has been idle for 31 minutes
    finally:
        app._engine_lock.release()
    worker.join(10)
    newer = str(created[0]["game_id"])
    assert alive(app, other, older, newer) == [False, True, True]


def test_a_full_table_over_http_sends_retry_after() -> None:
    app = make_app(Limits(max_sessions_per_client=2, session_idle_minutes=5), max_sessions=1)
    with serving(app, ServerOptions(trusted_proxies=1)) as server:
        port = port_of(server)
        first = {"X-Forwarded-For": "198.51.100.1"}
        other = {"X-Forwarded-For": "198.51.100.2"}
        assert call(port, "POST", "/api/games", {}, headers=first).status == 201
        resp = call(port, "POST", "/api/games", {}, headers=other)  # first is below its quota
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "300"
        # The client holding the slot may replace its own game.
        assert call(port, "POST", "/api/games", {}, headers=first).status == 201


def test_spoofed_forwarded_for_does_not_escape_the_game_quota() -> None:
    app = make_app(Limits(max_sessions_per_client=2))
    with serving(app) as server:  # trusted_proxies 0
        port = port_of(server)
        ids = []
        for n in range(4):
            spoof = {"X-Forwarded-For": f"198.51.100.{n}"}
            ids.append(str(call(port, "POST", "/api/games", {}, headers=spoof).json()["game_id"]))
        assert app.session_count() == 2
        assert [call(port, "GET", f"/api/games/{g}").status for g in ids] == [404, 404, 200, 200]


# ---------------------------------------------------------------- idle expiry
def test_idle_games_expire_and_use_keeps_them() -> None:
    clock = FakeClock()
    app = make_app(Limits(session_idle_minutes=30), now=clock)
    kept, left = game(app, "a"), game(app, "a")
    clock.advance(30 * 60 - 0.001)
    app.game_state(kept)  # used just in time: its 30 minutes start again
    clock.advance(0.001)  # `left` is idle for exactly 30 minutes
    assert app.session_count() == 1
    err = refused(lambda: app.game_state(left))
    assert err.status == 404
    assert "unused for 30 minutes" in err.message
    clock.advance(30 * 60 - 1)
    app.move(kept, {"uci": "e2e4"})  # every game request counts as a use
    clock.advance(30 * 60 - 1)
    assert alive(app, kept) == [True]
    clock.advance(30 * 60)
    assert app.session_count() == 0


# ---------------------------------------------------------------- thread bounds
def _server_threads() -> int:
    return sum(1 for t in threading.enumerate() if "process_request_thread" in t.name)


def test_a_busy_game_lets_few_requests_wait_and_refuses_more_at_once() -> None:
    app = make_app(Limits(max_connections=16))
    gid = game(app, "a")
    session = app._sessions[gid]
    served: list[object] = []
    session.lock.acquire()  # a /think holds the game (waiting for the engine, deciding)
    try:
        waiters = [
            threading.Thread(target=lambda: served.append(app.game_state(gid)), daemon=True)
            for _ in range(PlayApp.GAME_WAITERS)
        ]
        for waiter in waiters:
            waiter.start()
        _wait_for(lambda: app._game_waiters.get(gid) == PlayApp.GAME_WAITERS)
        for request in (
            lambda: app.game_state(gid),
            lambda: app.move(gid, {"uci": "e2e4"}),
            lambda: app.think(gid, client="b"),
            lambda: app.pgn(gid),
            lambda: app.decisions(gid),
            lambda: app.undo(gid),
            lambda: app.resign(gid),
        ):
            err = refused_at_once(request)
            assert (err.status, err.limit) == (409, "game_busy")
            assert err.retry_after == 2  # the engine's initial hold-time estimate
            assert "this game is busy" in err.message
        assert served == []
    finally:
        session.lock.release()
    for waiter in waiters:
        waiter.join(10)
    assert len(served) == PlayApp.GAME_WAITERS  # the waiters were served in turn
    assert app._game_waiters == {}  # and their places given back
    app.move(gid, {"uci": "e2e4"})


def test_local_game_requests_all_wait_their_turn() -> None:
    app = make_app()  # no max_connections: as in 0.1.0, nobody is refused
    gid = game(app, "a")
    session = app._sessions[gid]
    served: list[object] = []
    session.lock.acquire()
    try:
        waiters = [
            threading.Thread(target=lambda: served.append(app.game_state(gid)), daemon=True)
            for _ in range(6)
        ]
        for waiter in waiters:
            waiter.start()
        time.sleep(0.3)
        assert served == []
    finally:
        session.lock.release()
    for waiter in waiters:
        waiter.join(10)
    assert len(served) == 6
    assert app._game_waiters == {}


def test_requests_on_a_busy_game_cannot_park_server_threads() -> None:
    # The verifier's attack: while /think waits for the engine it holds its game; GETs on that
    # game (from any number of client identities) must not pile up server threads behind it.
    app = make_app(PUBLIC_LIMITS)
    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    with serving(app, options, public_bind=True) as server:
        server.log_stream = None
        port = port_of(server)
        me = {"X-Forwarded-For": "198.51.100.1"}
        created = call(port, "POST", "/api/games", {"human_color": "black"}, headers=me)
        gid = str(created.json()["game_id"])
        results: list[Response] = []
        app._engine_lock.acquire()  # other visitors' decisions keep the engine busy
        try:
            think = threading.Thread(
                target=lambda: results.append(
                    call(port, "POST", f"/api/games/{gid}/think", {}, headers=me)
                ),
                daemon=True,
            )
            think.start()
            _wait_for(lambda: app._gate.waiting == 1)  # /think now holds the game's lock
            n = 20
            getters = [
                threading.Thread(
                    target=lambda i=i: results.append(
                        call(
                            port,
                            "GET",
                            f"/api/games/{gid}",
                            headers={"X-Forwarded-For": f"203.0.113.{i}"},
                        )
                    ),
                    daemon=True,
                )
                for i in range(n)
            ]
            for getter in getters:
                getter.start()
            _wait_for(lambda: len(results) == n - PlayApp.GAME_WAITERS)
            refusals = list(results)
            _wait_for(lambda: server.connections == 1 + PlayApp.GAME_WAITERS)
            time.sleep(0.2)
            assert server.connections == 1 + PlayApp.GAME_WAITERS  # the /think and 2 waiters
        finally:
            app._engine_lock.release()
        for getter in getters:
            getter.join(20)
        think.join(20)
    for resp in refusals:
        assert resp.status == 409
        assert resp.json()["limit"] == "game_busy"
        assert int(resp.headers["Retry-After"]) >= 1  # from the engine line's hold times
    assert sorted(r.status for r in results[len(refusals) :]) == [200] * 3


@pytest.mark.parametrize("framed", [False, True])
def test_connections_beyond_the_cap_are_refused_at_once(
    framed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)  # the idle three keep their places
    app = make_app(Limits(max_connections=3))
    log = io.StringIO()
    ancestors = ("https://huggingface.co",) if framed else ()
    options = ServerOptions(public=True, allowed_hosts=(HF,), frame_ancestors=ancestors)
    with serving(app, options, public_bind=True) as server:
        server.log_stream = log
        port = port_of(server)
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(3)]
        try:
            _wait_for(lambda: server.connections == 3)  # three threads wait for a request
            threads = _server_threads()
            with socket.create_connection(("127.0.0.1", port), timeout=10) as extra:
                data = b""
                while chunk := extra.recv(65536):
                    data += chunk
            assert _server_threads() <= threads  # answered without a new thread
            assert server.connections == 3
        finally:
            for sock in idle:
                sock.close()
        _wait_for(lambda: server.connections == 0)
        assert call(port, "GET", "/healthz").status == 200  # served again
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    assert lines[0] == "HTTP/1.0 503 Service Unavailable"
    headers = dict(line.split(": ", 1) for line in lines[1:])
    assert headers["Retry-After"] == str(PlayServer.FULL_RETRY_AFTER_S)
    if framed:  # this server's own framing headers, so the 503 shows inside the Space's frame
        assert headers["Content-Security-Policy"].endswith(
            "; frame-ancestors https://huggingface.co"
        )
        assert "X-Frame-Options" not in headers
    else:
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Type"] == "application/json"
    assert headers["Connection"] == "close"
    assert int(headers["Content-Length"]) == len(body)
    payload = json.loads(body)
    assert payload["limit"] == "max_connections"
    assert "busy: 3 connections are open" in payload["error"]
    refused_lines = [line for line in log.getvalue().splitlines() if " - - 503 -ms" in line]
    assert len(refused_lines) == 1
    assert LOG_LINE.fullmatch(refused_lines[0])


def _stall(port: int, count: int, forwarded: str) -> list[socket.socket]:
    """``count`` API requests whose body never comes: each stays in progress until closed."""
    stalled = []
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        head = [
            "POST /api/analyse HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Content-Type: application/json",
            "Content-Length: 10",
            f"X-Forwarded-For: {forwarded}",
        ]
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
        stalled.append(sock)
    return stalled


def test_one_client_has_a_bounded_number_of_api_requests_in_progress() -> None:
    app = make_app(Limits(max_connections=32))
    with serving(app, ServerOptions(trusted_proxies=1)) as server:
        port = port_of(server)
        me = "198.51.100.1"
        stalled = _stall(port, PlayServer.API_REQUESTS_PER_CLIENT, me)
        try:
            _wait_for(lambda: server.api_requests(me) == PlayServer.API_REQUESTS_PER_CLIENT)
            resp = analyse_over_http(port, me)
            assert resp.status == 429
            assert resp.headers["Retry-After"] == "1"
            assert resp.json()["limit"] == "requests_at_once"
            assert "too many requests at once" in str(resp.json()["error"])
            assert call(port, "GET", "/api/info", headers={"X-Forwarded-For": me}).status == 429
            # Static files and health checks are not API requests; other clients are served.
            assert call(port, "GET", "/healthz", headers={"X-Forwarded-For": me}).status == 200
            assert call(port, "GET", "/", headers={"X-Forwarded-For": me}).status == 200
            assert analyse_over_http(port, "198.51.100.2").status == 200
            assert server.api_requests(me) == PlayServer.API_REQUESTS_PER_CLIENT
        finally:
            for sock in stalled:
                sock.close()
        _wait_for(lambda: server.api_requests(me) == 0)  # every place was given back
        assert analyse_over_http(port, me).status == 200
        # (a place is given back just after the answer is sent, hence the wait)
        _wait_for(lambda: server._api_requests == {})


def test_a_slow_body_is_drained_for_a_bounded_time() -> None:
    app = make_app(Limits(max_connections=8))
    with serving(app) as server:
        port = port_of(server)
        head = [
            "POST /api/games HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Origin: https://evil.example",  # refused (403) before the body is read
            "Content-Type: application/json",
            "Content-Length: 60000",
        ]
        stop = threading.Event()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)

        def trickle() -> None:  # one byte every 0.2 s: never silent for the 1 s read timeout
            with contextlib.suppress(OSError):
                while not stop.wait(0.2):
                    sock.sendall(b" ")

        try:
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
            started = time.monotonic()
            threading.Thread(target=trickle, daemon=True).start()
            _wait_for(lambda: server.connections == 1)
            _wait_for(lambda: server.connections == 0)  # the thread is done: it gave up
            took = time.monotonic() - started
        finally:
            stop.set()
            sock.close()
    assert took < PlayHandler.DRAIN_S + 2.5, took  # (plus at most one 1 s read timeout)


def test_errors_name_the_limit_only_for_limit_refusals() -> None:
    app = make_app(Limits(decisions_per_minute=1, max_connections=8))
    with serving(app) as server:
        port = port_of(server)
        assert analyse_over_http(port).status == 200
        limited = analyse_over_http(port)
        assert (limited.status, limited.json()["limit"]) == (429, "decisions_per_minute")
        other = call(port, "GET", f"/api/games/{'A' * 32}")
        assert other.status == 404
        assert "limit" not in other.json()
        assert other.headers["Retry-After"] is None


def test_a_connection_whose_thread_cannot_start_frees_its_place(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(Limits(max_connections=2))
    with serving(app) as server:
        port = port_of(server)

        def no_thread(self: object, request: object, client_address: object) -> None:
            raise RuntimeError("can't start new thread")

        with monkeypatch.context() as patch:
            patch.setattr(socketserver.ThreadingMixIn, "process_request", no_thread)
            for _ in range(3):  # more than max_connections: each place came back
                with contextlib.suppress(OSError):
                    _raw_exchange(port, b"GET /healthz HTTP/1.0\r\n\r\n")
                _wait_for(lambda: server.connections == 0)
        assert call(port, "GET", "/healthz").status == 200
    assert "can't start new thread" in capsys.readouterr().err  # reported, not hidden


def test_local_server_bounds_no_threads() -> None:
    # Without max_connections (the local default) nothing is counted or refused, as in 0.1.0.
    app = make_app()
    with serving(app) as server:
        port = port_of(server)
        stalled = _stall(port, PlayServer.API_REQUESTS_PER_CLIENT + 4, "198.51.100.1")
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(4)]
        try:
            time.sleep(0.3)
            assert analyse_over_http(port).status == 200  # the same peer, 127.0.0.1
            assert call(port, "GET", "/api/info").status == 200
            assert (server.connections, server._api_requests) == (0, {})
        finally:
            for sock in stalled + idle:
                sock.close()


# The verifier's round-2 attacks, as guards: one client identity must not tie up more threads
# than its places (API_REQUESTS_PER_CLIENT, DRAINS_PER_CLIENT), whatever path or pace it uses.
ATTACKER = "198.51.100.7"


def _slow_head(
    port: int, path: str, forwarded: str = ATTACKER, *, length: int = 60000, **extra: str
) -> socket.socket:
    """The head of a JSON POST whose body is not sent (``extra``: more header lines)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = [
        f"POST {path} HTTP/1.1",
        f"Host: {HF}",
        "Content-Type: application/json",
        f"Content-Length: {length}",
        f"X-Forwarded-For: {forwarded}",
        *(f"{key}: {value}" for key, value in extra.items()),
    ]
    sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
    return sock


def _reply(sock: socket.socket, timeout: float = 5.0) -> tuple[int, dict[str, str]]:
    """Status and headers of the response on ``sock`` (TimeoutError if none comes in time)."""
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        assert chunk, f"closed without a reply: {data!r}"
        data += chunk
    lines = data.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:])


def _trickle(sock: socket.socket, stop: threading.Event, every: float = 0.1) -> None:
    """Send one body byte every ``every`` s until ``stop`` (never silent for a read timeout)."""

    def run() -> None:
        with contextlib.suppress(OSError):
            while not stop.wait(every):
                sock.sendall(b" ")

    threading.Thread(target=run, daemon=True).start()


def _bounded_server(app: PlayApp) -> contextlib.AbstractContextManager[PlayServer]:
    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    return serving(app, options, public_bind=True)


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/", 405),
        ("/healthz", 405),
        ("/static/css/app.css", 405),
        ("/API/games", 404),
        ("/api/games/", 404),  # ("//api/games" is /api/games to the stdlib: an API request)
        ("/api/nope", 404),
        (f"/api/games/{'A' * 32}/pgn", 405),
    ],
)
def test_posts_off_the_api_are_refused_before_their_body(path: str, status: int) -> None:
    # Round-2 attack (a): a POST to a page or /healthz read its body before routing, outside
    # every per-client count, and without a deadline. Now: refused at once, nothing read.
    app = make_app(Limits(max_connections=32))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        socks = [_slow_head(port, path) for _ in range(PlayServer.API_REQUESTS_PER_CLIENT + 12)]
        try:
            replies = [_reply(sock) for sock in socks]  # 5 s each at most (BODY_S is 10 s)
            _wait_for(lambda: server.connections == 0)
            assert server.api_requests(ATTACKER) == 0
        finally:
            for sock in socks:
                sock.close()
    assert {code for code, _ in replies} == {status}


def test_one_identity_cannot_fill_the_connection_cap() -> None:
    cap = 16
    app = make_app(Limits(max_connections=cap))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        socks = [_slow_head(port, "/healthz") for _ in range(cap)]
        try:
            # Two of them may be drained (in the attacker's drain places), the rest end at once.
            _wait_for(lambda: server.connections <= PlayServer.DRAINS_PER_CLIENT)
            victim = analyse_over_http(port, "203.0.113.9", host=HF)
        finally:
            for sock in socks:
                sock.close()
    assert victim.status == 200


def test_refused_bodies_are_drained_in_few_places_per_client() -> None:
    # Round-2 attack (b): refused POSTs were drained outside any count, so a stream of them
    # from one identity kept every connection slot busy. Now the identity has 8 API places and
    # 2 drain places; any other refusal of it is closed at once, without waiting for its body.
    app = make_app(Limits(max_connections=32))
    stop = threading.Event()
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        places = PlayServer.API_REQUESTS_PER_CLIENT
        socks = [_slow_head(port, "/api/analyse") for _ in range(places)]  # counted, stalled
        try:
            _wait_for(lambda: server.api_requests(ATTACKER) == places)
            for _ in range(PlayServer.DRAINS_PER_CLIENT):  # refused (429), drained: trickled
                socks.append(_slow_head(port, "/api/analyse"))
                _trickle(socks[-1], stop)
            _wait_for(lambda: server.draining(ATTACKER) == PlayServer.DRAINS_PER_CLIENT)
            flood = [_slow_head(port, "/api/analyse") for _ in range(20)]
            socks += flood
            for sock in flood:  # a drain would hold these for DRAIN_S: they must end first
                _trickle(sock, stop)
            deadline = time.monotonic() + PlayHandler.DRAIN_S / 2
            while server.connections > places + PlayServer.DRAINS_PER_CLIENT:
                assert time.monotonic() < deadline, server.connections
                time.sleep(0.01)
            assert server.draining(ATTACKER) == PlayServer.DRAINS_PER_CLIENT
            assert server.api_requests(ATTACKER) == places
            assert analyse_over_http(port, "203.0.113.9", host=HF).status == 200  # others served
        finally:
            stop.set()
            for sock in socks:
                sock.close()
        _wait_for(lambda: (server.connections, server._draining) == (0, {}))


@pytest.mark.parametrize(
    ("path", "extra", "status"),
    [
        ("/api/games", {"Origin": "https://evil.example"}, 403),  # counted: its API place
        ("/", {}, 405),  # not counted: one of its client's drain places
    ],
)
def test_a_refusal_waits_for_a_late_body_so_the_client_reads_it(
    path: str, extra: dict[str, str], status: int
) -> None:
    # Closing a socket with unread data resets the connection, and a reset can destroy the
    # reply before the client reads it; so a refused body is drained when the client has a
    # place for that. (A flood beyond its places is closed without waiting, see above.)
    app = make_app(Limits(max_connections=8))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        sock = _slow_head(port, path, length=2, **extra)
        try:
            time.sleep(0.4)  # the server refused already, and waits for the body
            sock.sendall(b"{}")
            time.sleep(0.4)  # (a reset, if any, has arrived before we read)
            code, _ = _reply(sock)
        finally:
            sock.close()
    assert code == status


def test_a_counted_body_must_arrive_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PlayHandler, "BODY_S", 0.5)
    app = make_app(Limits(max_connections=8))
    stop = threading.Event()
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        sock = _slow_head(port, "/api/games")
        _trickle(sock, stop)  # never silent for the 30 s socket timeout: only a deadline ends it
        try:
            started = time.monotonic()
            code, _ = _reply(sock)
            took = time.monotonic() - started
            _wait_for(lambda: server.api_requests(ATTACKER) == 0)  # its place was given back
        finally:
            stop.set()
            sock.close()
    assert code == 408
    assert took < 0.5 + 1.0, took


def test_local_bodies_have_no_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in 0.1.0, locally only the socket timeout between two receives limits a body.
    monkeypatch.setattr(PlayHandler, "BODY_S", 0.2)
    app = make_app()
    with serving(app) as server:
        port = port_of(server)
        body = json.dumps({"fen": chess.STARTING_FEN}).encode("utf-8")
        head = (
            f"POST /api/analyse HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(head.encode("ascii") + body[:5])
            time.sleep(0.6)  # three times BODY_S
            sock.sendall(body[5:])
            code, _ = _reply(sock)
    assert code == 200


def test_the_route_is_checked_before_the_body_only_when_threads_are_bounded() -> None:
    # Local mode keeps 0.1.0's order (the body's checks, then the route): a POST of plain text
    # to "/" is 415 there, and 405 when threads are bounded.
    request = (
        b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\n"
        b"Content-Length: 2\r\n\r\nhi"
    )
    statuses = []
    for limits in (None, Limits(max_connections=8)):
        with serving(make_app(limits)) as server:
            reply = _raw_exchange(port_of(server), request)
        statuses.append(int(reply.split(b" ", 2)[1]))
    assert statuses == [415, 405]


# ---------------------------------------------------------------- engine budget
@pytest.mark.parametrize("field", ["nodes", "depth", "movetime", "threads", "hash", "multipv"])
def test_no_request_can_raise_the_engine_search_budget(field: str) -> None:
    app = make_app(PUBLIC_LIMITS, nodes=5000)
    body = {"fen": chess.STARTING_FEN, field: 10**9}
    assert refused(lambda: app.analyse(dict(body), client="a")).status == 400
    assert refused(lambda: app.create_game({field: 10**9}, client="a")).status == 400
    engine = app.info()["engine"]
    assert isinstance(engine, dict)
    assert engine["nodes"] == 5000


def test_every_engine_call_keeps_the_node_limit() -> None:
    # A deadline (the only per-call change) can shorten a search, never lengthen it.
    engine = object.__new__(StockfishEngine)  # no process: only the limit logic is under test
    engine._limit = chess.engine.Limit(nodes=5000)
    assert engine._call_limit(None).nodes == 5000
    limited = engine._call_limit(3600.0)
    assert (limited.nodes, limited.time) == (5000, 3600.0)


# ---------------------------------------------------------------- /api/info
def test_info_exposes_the_limits_in_force() -> None:
    app = make_app(PUBLIC_LIMITS)
    assert app.info()["limits"] == {
        "public": True,
        "max_sessions_per_client": 3,
        "decisions_per_minute": 20,
        "max_queue": 6,
        "session_idle_minutes": 30,
        "max_connections": 64,
        "max_think_seconds": 20.0,
        "reads_per_minute": 60,
        "max_connections_per_peer": None,  # off unless asked for, even in public mode
        "waiting": 0,
    }
    direct = make_app(dataclasses.replace(PUBLIC_LIMITS, max_connections_per_peer=16))
    shown = direct.info()["limits"]
    assert isinstance(shown, dict)
    assert shown["max_connections_per_peer"] == 16
    direct.close()
    partial = make_app(Limits(max_queue=2)).info()["limits"]
    assert isinstance(partial, dict)
    assert (partial["public"], partial["max_queue"], partial["decisions_per_minute"]) == (
        False,
        2,
        None,
    )
    assert "limits" not in make_app().info()  # local: the 0.1.0 output


def test_resolve_limits_fills_public_defaults_only() -> None:
    assert resolve_limits(public=False) == Limits()
    assert not Limits().active
    assert resolve_limits(public=True) == PUBLIC_LIMITS
    assert PUBLIC_LIMITS.active
    mixed = resolve_limits(public=True, max_queue=0, decisions_per_minute=5)
    assert (mixed.max_queue, mixed.decisions_per_minute, mixed.max_sessions_per_client) == (
        0,
        5,
        3,
    )
    assert resolve_limits(public=False, max_queue=4) == Limits(max_queue=4)
    assert Limits(max_queue=4).active
    assert resolve_limits(public=True, max_connections=5).max_connections == 5
    assert resolve_limits(public=False, max_connections=5) == Limits(max_connections=5)
    assert Limits(max_connections=5).active
    assert resolve_limits(public=True).max_connections_per_peer is None  # never a default
    peer = resolve_limits(public=True, max_connections_per_peer=4)
    assert (peer.max_connections_per_peer, peer.max_connections) == (4, 64)
    assert Limits(max_connections_per_peer=4).active


# ---------------------------------------------------------------- request log
LOG_LINE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (GET|POST|PUT|-) (/[a-z/:.]*|\(other\)|-) \d{3} (\d+|-)ms"
)


def test_public_log_has_one_line_per_request_and_no_addresses_or_ids() -> None:
    app = make_app()
    log = io.StringIO()
    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    with serving(app, options, public_bind=True) as server:
        server.log_stream = log
        port = port_of(server)
        spoof = {"X-Forwarded-For": "203.0.113.77"}
        created = call(port, "POST", "/api/games", {"human_color": "black"}, headers=spoof)
        game_id = str(created.json()["game_id"])
        statuses = [
            created.status,
            call(port, "POST", f"/api/games/{game_id}/think", {}, headers=spoof).status,
            call(port, "GET", f"/api/games/{game_id}").status,
            call(port, "GET", "/static/js/main.js").status,
            call(port, "GET", "/secret-path-xyz?token=abc").status,
            call(port, "GET", "/healthz", host="evil.example").status,
            call(port, "PUT", "/api/games").status,
        ]
        raw = _raw_exchange(port, b"GARBAGE<script>\r\n\r\n")
        # Exactly one byte over the stdlib's limit, so no unread bytes reset the connection.
        long_line = _raw_exchange(port, b"GET /" + b"a" * (65537 - 5))
    assert statuses == [201, 200, 200, 200, 404, 421, 501]
    assert raw.startswith(b"HTTP/1.0 400")
    assert b" 414 " in long_line.split(b"\r\n", 1)[0]
    lines = log.getvalue().splitlines()
    assert len(lines) == 9  # one per request, the malformed ones included
    for line in lines:
        assert LOG_LINE.fullmatch(line), line
    text = log.getvalue()
    for secret in (game_id, "203.0.113.77", "127.0.0.1", "secret-path", "token", "<script>", HF):
        assert secret not in text, secret
    routes = [line.split(" ")[2] for line in lines]
    assert routes[:5] == [
        "/api/games",
        "/api/games/:id/think",
        "/api/games/:id",
        "/static/...",
        "(other)",
    ]
    assert lines[-1].endswith(" 414 -ms")  # refused before a request could be parsed


def test_run_writes_the_request_log_to_its_error_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(play_server, "is_loopback", lambda host: False)  # a "public" bind
    app = make_app(PUBLIC_LIMITS)
    err = io.StringIO()
    statuses: list[int] = []

    def on_listen(server: PlayServer) -> None:
        def probe() -> None:
            try:
                statuses.append(call(port_of(server), "GET", "/healthz").status)
            finally:
                server.shutdown()

        threading.Thread(target=probe, daemon=True).start()

    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    code = run(
        app,
        port=0,
        open_browser=False,
        out=io.StringIO(),
        err=err,
        options=options,
        on_listen=on_listen,
    )
    assert (code, statuses) == (0, [200])
    log = [line for line in err.getvalue().splitlines() if LOG_LINE.fullmatch(line)]
    assert len(log) == 1
    assert " GET /healthz 200 " in log[0]
    assert not LOG_LINE.search(capsys.readouterr().err)  # not on the process's stderr


def test_nothing_is_logged_once_run_has_returned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Round-5 finding: run() returns at once on Ctrl+C / SIGTERM, while request threads
    # (daemon threads) still write their log lines; one caught writing to stderr when the
    # interpreter shut down aborted the process ("Fatal Python error: _enter_buffered_busy",
    # exit code 0xC0000409). run() now stops the log before it returns: a request that ends
    # later is still answered, but writes nothing.
    monkeypatch.setattr(play_server, "is_loopback", lambda host: False)  # a "public" bind
    app = make_app(PUBLIC_LIMITS)
    err = io.StringIO()
    servers: list[PlayServer] = []
    late: list[socket.socket] = []
    client = "198.51.100.7"

    def on_listen(server: PlayServer) -> None:
        servers.append(server)

        def stop() -> None:
            try:
                late.append(_slow_head(port_of(server), "/api/analyse", client, length=2))
                _wait_for(lambda: server.api_requests(client) == 1)  # it waits for its body
            finally:
                server.shutdown()  # (whatever happened: run() must return)

        threading.Thread(target=stop, daemon=True).start()

    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    code = run(
        app,
        port=0,
        open_browser=False,
        out=io.StringIO(),
        err=err,
        options=options,
        on_listen=on_listen,
    )
    assert code == 0
    server, sock = servers[0], late[0]
    written = err.getvalue()
    assert "cca play: PUBLIC MODE" in written
    try:
        sock.sendall(b"{}")  # the body of a request that outlived run()
        status, _ = _reply(sock)
        _wait_for(lambda: server.api_requests(client) == 0)  # its thread is done
    finally:
        sock.close()
    assert status == 503  # answered (the engines are gone), not logged
    server.write_log("late")
    assert err.getvalue() == written
    assert not LOG_LINE.search(capsys.readouterr().err)


def test_a_log_line_that_waited_for_the_lock_is_dropped_once_logging_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # stop_logging() empties the log under its lock: a request thread that saw the stream
    # before, and waited for the lock meanwhile, must not write to it afterwards.
    app = make_app()
    server = make_server("127.0.0.1", 0, app, ServerOptions(public=True))

    class Gate:
        """The log's lock, telling when a thread is about to wait for it."""

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.reached = threading.Event()

        def __enter__(self) -> None:
            self.reached.set()
            self.lock.acquire()

        def __exit__(self, *exc: object) -> None:
            self.lock.release()

    gate = Gate()
    log = io.StringIO()
    try:
        server.log_stream = log
        monkeypatch.setattr(server, "_log_lock", gate)
        gate.lock.acquire()  # held (as by stop_logging)
        writer = threading.Thread(target=server.write_log, args=("late",), daemon=True)
        writer.start()
        assert gate.reached.wait(10)  # the writer has seen the stream, and waits for the lock
        server.log_stream = None  # what stop_logging does while it holds the lock
        gate.lock.release()
        writer.join(10)
        assert not writer.is_alive()
    finally:
        server.server_close()
        app.close()
    assert log.getvalue() == ""


@pytest.mark.parametrize("public", [True, False])
def test_error_reports_stop_with_the_log_in_public_mode_only(
    public: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise LookupError("late bug")

    app = make_app()
    server = make_server("127.0.0.1", 0, app, ServerOptions(public=public))
    request = socket.socket()
    try:
        server.stop_logging()  # as run() does before it returns
        try:
            fail()
        except LookupError:
            server.handle_error(request, ("203.0.113.5", 4242))
            server.report_error()
        err = capsys.readouterr().err
        assert err.count("LookupError: late bug") == (0 if public else 2)  # local: as always
    finally:
        request.close()
        server.server_close()
        app.close()


def test_a_public_server_reports_no_error_once_logging_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app()

    def boom() -> dict[str, object]:
        raise ZeroDivisionError("secret detail")

    with serving(app, ServerOptions(public=True)) as server:
        monkeypatch.setattr(app, "info", boom)
        port = port_of(server)
        assert call(port, "GET", "/api/info").status == 500
        assert "ZeroDivisionError" in capsys.readouterr().err  # reported while it runs
        server.stop_logging()  # as run() does before it returns
        assert call(port, "GET", "/api/info").status == 500  # still answered
    assert capsys.readouterr().err == ""  # but neither reported nor logged


# A process like `cca play --public`: run() is stopped under load, then the process exits.
SHUTDOWN_CHILD = """
import _thread, dataclasses, json, socket, sys, threading, time
import chess
import cca.play.server as ps
from cca.agent import AgentConfig
from cca.play.app import Engines, PlayApp, PlaySettings
from cca.play.limits import PUBLIC_LIMITS
from tests.conftest import FakeEngine, FakeHuman

class SlowEngine(FakeEngine):
    def evaluate(self, *args, **kwargs):
        time.sleep(0.005)
        return super().evaluate(*args, **kwargs)

ps.is_loopback = lambda host: False  # a "public" bind, on 127.0.0.1 only
limits = dataclasses.replace(PUBLIC_LIMITS, decisions_per_minute=100000)
app = PlayApp(
    lambda: Engines(SlowEngine(), FakeHuman(), "qre"), PlaySettings(AgentConfig(), limits=limits)
)
BODY = json.dumps({"fen": chess.STARTING_FEN}).encode()
LATE = "198.51.100.7"
go, answered = threading.Event(), threading.Event()

def head(client):
    return (
        "POST /api/analyse HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\nContent-Type: application/json"
        f"\\r\\nContent-Length: {len(BODY)}\\r\\nX-Forwarded-For: {client}\\r\\n\\r\\n"
    ).encode()

def busy(port, n):  # requests in flight when the server stops
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(head(f"10.0.{n}.1") + BODY)
                while sock.recv(65536):
                    pass
        except OSError:
            time.sleep(0.01)

def late(port):  # its body comes once run() has returned
    while True:  # (a full listen backlog refuses a connection at once: try again)
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(head(LATE))
            break
        except OSError:
            time.sleep(0.01)
    data = b""
    try:
        go.wait(60)
        sock.sendall(BODY)
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    answer.append(data.split(b"\\r\\n", 1)[0].decode("ascii", "replace"))
    answered.set()

def on_listen(server):
    port = server.server_address[1]
    threading.Thread(target=late, args=(port,), daemon=True).start()
    for n in range(16):
        threading.Thread(target=busy, args=(port, n), daemon=True).start()

    def stop():
        deadline = time.monotonic() + 60
        while server.api_requests(LATE) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.5)
        _thread.interrupt_main()

    threading.Thread(target=stop, daemon=True).start()

answer = []
code = ps.run(
    app, host="127.0.0.1", port=0, open_browser=False, on_listen=on_listen,
    options=ps.ServerOptions(public=True, trusted_proxies=1),
)
print("child: run() returned", file=sys.stderr, flush=True)
go.set()
answered.wait(30)
print(f"child: the late request got {answer}", file=sys.stderr, flush=True)
sys.exit(code)
"""


def test_a_public_server_stopped_under_load_exits_cleanly() -> None:
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(  # fixed argument list, no shell; binds 127.0.0.1 only
        [sys.executable, "-c", SHUTDOWN_CHILD],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert "Fatal Python error" not in done.stderr, done.stderr[-3000:]
    assert done.returncode == 0, done.stderr[-3000:]
    before, marker, after = done.stderr.partition("child: run() returned")
    assert marker, done.stderr[-3000:]
    assert "cca play: stopped" in before
    assert LOG_LINE.search(before)  # the load was served and logged
    assert "child: the late request got ['HTTP/1.0 503 " in after  # it outlived run()
    assert not LOG_LINE.search(after), after  # but nothing was logged once run() had returned


@pytest.mark.parametrize("public", [True, False])
def test_error_reports_name_the_client_only_in_local_mode(
    public: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise LookupError("real bug")

    app = make_app()
    server = make_server("127.0.0.1", 0, app, ServerOptions(public=public))
    request = socket.socket()
    try:
        try:
            fail()
        except LookupError:
            server.handle_error(request, ("203.0.113.5", 4242))
        err = capsys.readouterr().err
        assert "LookupError: real bug" in err
        assert ("203.0.113.5" in err) is not public  # local: the stdlib's report, unchanged
    finally:
        request.close()
        server.server_close()
        app.close()


def test_route_labels_never_carry_input() -> None:
    game_id = "A" * 32
    assert route_label(f"/api/games/{game_id}/pgn") == "/api/games/:id/pgn"
    assert route_label("/static/../../etc/passwd") == "/static/..."
    assert route_label("/\n1.2.3.4 injected") == "(other)"
    assert route_label("/api/info") == "/api/info"


def test_public_notice_states_the_limits_and_warns_without_a_trusted_proxy() -> None:
    app = make_app(PUBLIC_LIMITS)
    for trusted in (0, 1):
        options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=trusted)
        server = make_server("127.0.0.1", 0, app, options)
        try:
            text = public_notice(server)
        finally:
            server.server_close()
        assert text.startswith("cca play: PUBLIC MODE on 127.0.0.1:")
        assert HF in text
        assert "games per client 3" in text
        assert "CCA decisions per client and minute 20" in text
        assert "idle games dropped after 30 min" in text
        assert "requests waiting for the engine 6" in text
        assert (
            "games in total 16 (when all are in use, a game in which nothing was played for "
            "5 min, 15 min on the move of a player who has moved in it, goes to a newcomer; a "
            "player's move counts as play once CCA has played in the game, and a game started "
            "in place of the same client's older one keeps that one's time);"
        ) in text
        assert (
            "connections at once 64 (per client 8 API requests at once and 2 refused bodies "
            "drained; request heads within 10 s, and a head late by 0.5 s gives its place to "
            "a newcomer when all are taken; request bodies within 10 s; per game 2 waiting "
            "requests)."
        ) in text
        assert "Decisions: at most 20 s each, once they have the engine" in text
        assert "waiting clients take turns" in text
        assert "reads of a game's decisions or PGN per client and minute 60." in text
        assert "Framing allowed from: nobody" in text
        assert ("shares ONE quota" in text) is (trusted == 0)
        assert "xff=N" not in text
        assert "One request per connection (HTTP/1.0)." in text
        if trusted:
            assert (
                "Connections per address: no cap (the TCP peer is the proxy): the platform's "
                "edge is expected to buffer request heads and bodies; the deadlines above bound "
                "what it passes on slowly."
            ) in text
        else:
            assert "Connections per address: no cap (--max-connections-per-peer N sets" in text
    app.close()
    app = make_app(dataclasses.replace(PUBLIC_LIMITS, max_connections_per_peer=16))
    server = make_server("127.0.0.1", 0, app, ServerOptions(public=True, allowed_hosts=(HF,)))
    try:
        text = public_notice(server)
    finally:
        server.server_close()
        app.close()
    assert "Connections per address: 16 at once, whatever they are doing" in text
    assert "no cap" not in text


# ---------------------------------------------------------------- command line
@pytest.fixture
def never_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    def started(*args: object, **kwargs: object) -> int:
        raise AssertionError("cca play started serving")

    monkeypatch.setattr("cca.play.server.run", started)


@pytest.mark.usefixtures("never_serve")
@pytest.mark.parametrize("host", [None, "127.0.0.1", "127.8.0.1", "localhost", "::1", "[::1]"])
def test_public_refuses_a_loopback_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str | None
) -> None:
    argv = ["play", "--public", "--allowed-host", HF, "--no-browser"]
    argv += ["--stockfish", str(make_launcher(tmp_path))]
    if host is not None:
        argv += ["--host", host]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: --public needs a non-loopback --host")


@pytest.mark.usefixtures("never_serve")
def test_public_needs_an_allowed_host(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["play", "--public", "--host", WILDCARD, "--no-browser"]
    assert main([*argv, "--stockfish", str(make_launcher(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: --public needs --allowed-host NAME")


def test_run_refuses_public_mode_on_a_loopback_bind() -> None:
    built: list[bool] = []

    def factory() -> Engines:
        built.append(True)
        return Engines(FakeEngine(), FakeHuman(), "qre")

    def served(server: PlayServer) -> None:
        raise KeyboardInterrupt  # it started serving: stop at once (the test then fails)

    app = PlayApp(factory, PlaySettings(AgentConfig(), limits=PUBLIC_LIMITS))
    err = io.StringIO()
    options = ServerOptions(public=True, allowed_hosts=(HF,))
    code = run(
        app,
        port=0,
        open_browser=False,
        out=io.StringIO(),
        err=err,
        options=options,
        on_listen=served,
    )
    assert code == 1
    assert err.getvalue().startswith("error: --public needs a non-loopback --host")
    assert built == []


def _captured_run(monkeypatch: pytest.MonkeyPatch) -> list[tuple[PlayApp, dict[str, object]]]:
    seen: list[tuple[PlayApp, dict[str, object]]] = []

    def fake_run(app: PlayApp, **kwargs: object) -> int:
        seen.append((app, kwargs))
        app.close()
        return 0

    monkeypatch.setattr("cca.play.server.run", fake_run)
    return seen


def test_public_flags_reach_the_server_and_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _captured_run(monkeypatch)
    argv = ["play", "--public", "--host", WILDCARD, "--no-browser"]
    argv += ["--stockfish", str(make_launcher(tmp_path)), "--allowed-host", "Demo-Space.HF.space"]
    argv += ["--allowed-host", "demo-space.hf.space", "--trusted-proxies", "1"]
    argv += ["--frame-ancestor", "https://huggingface.co"]
    assert main(argv) == 0
    app, kwargs = seen[0]
    assert kwargs["options"] == ServerOptions(
        public=True,
        trusted_proxies=1,
        frame_ancestors=("https://huggingface.co",),
        allowed_hosts=("demo-space.hf.space",),  # normalised, duplicates dropped
    )
    assert app.settings.limits == PUBLIC_LIMITS
    overrides = ["--max-sessions-per-client", "1", "--decisions-per-minute", "5"]
    overrides += ["--max-queue", "0", "--session-idle-minutes", "10", "--max-connections", "7"]
    overrides += ["--max-think-seconds", "7.5", "--reads-per-minute", "9"]
    assert main([*argv, *overrides]) == 0
    assert seen[1][0].settings.limits == Limits(
        public=True,
        max_sessions_per_client=1,
        decisions_per_minute=5,
        max_queue=0,
        session_idle_minutes=10,
        max_connections=7,
        max_think_seconds=7.5,
        reads_per_minute=9,
    )
    for hops in (2, 8, 0):  # passed through as given (the last flag wins), never clamped
        assert main([*argv, "--trusted-proxies", str(hops)]) == 0
        assert seen[-1][1]["options"] == dataclasses.replace(
            ServerOptions(
                public=True,
                frame_ancestors=("https://huggingface.co",),
                allowed_hosts=("demo-space.hf.space",),
            ),
            trusted_proxies=hops,
        )


def test_the_hugging_face_space_command_line_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CMD of the Space image (after its host placeholder is filled in), as documented for
    # the deployment: these flag names are its contract with this CLI.
    seen = _captured_run(monkeypatch)
    space = ["play", "--public", "--allowed-host", HF, "--host", WILDCARD, "--port", "8765"]
    space += ["--no-browser", "--human", "qre", "--trusted-proxies", "1"]
    space += ["--frame-ancestor", "https://huggingface.co", "--max-sessions", "48"]
    assert main([*space, "--stockfish", str(make_launcher(tmp_path))]) == 0
    app, kwargs = seen[0]
    assert (kwargs["host"], kwargs["port"], kwargs["open_browser"]) == (WILDCARD, 8765, False)
    assert kwargs["options"] == ServerOptions(
        public=True,
        trusted_proxies=1,
        frame_ancestors=("https://huggingface.co",),
        allowed_hosts=(HF,),
    )
    assert (app.settings.max_sessions, app.settings.limits) == (48, PUBLIC_LIMITS)
    assert app.settings.requested_human == "qre"


def test_limit_flags_work_without_public_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _captured_run(monkeypatch)
    argv = ["play", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    assert main([*argv, "--max-queue", "3"]) == 0
    app, kwargs = seen[0]
    assert app.settings.limits == Limits(max_queue=3)
    assert kwargs["options"] == ServerOptions()


@pytest.mark.parametrize(
    "argv",
    [
        ["--trusted-proxies", "-1"],
        ["--trusted-proxies", "9"],
        ["--max-sessions-per-client", "0"],
        ["--decisions-per-minute", "0"],
        ["--decisions-per-minute", "601"],
        ["--max-queue", "-1"],
        ["--session-idle-minutes", "0"],
        ["--max-connections", "0"],
        ["--max-connections", "1025"],
        ["--max-think-seconds", "0.5"],
        ["--max-think-seconds", "3601"],
        ["--max-think-seconds", "nan"],
        ["--max-think-seconds", "soon"],
        ["--reads-per-minute", "0"],
        ["--reads-per-minute", "6001"],
        ["--max-connections-per-peer", "0"],
        ["--max-connections-per-peer", "1025"],
    ],
)
def test_public_numbers_are_range_checked(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["play", *argv])


def test_help_states_the_public_defaults() -> None:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help or "" for a in sub.choices["play"]._actions}
    for field in (
        "max_sessions_per_client",
        "decisions_per_minute",
        "max_queue",
        "max_connections",
        "reads_per_minute",
    ):
        assert helps[field].endswith(f"[public: {getattr(PUBLIC_LIMITS, field)}]"), field
    assert helps["max_think_seconds"].endswith(f"[public: {PUBLIC_LIMITS.max_think_seconds:g}]")
    assert f"request heads {PlayHandler.HEAD_S:g} s" in helps["max_connections"]
    # The bounds that come with --max-connections, as the help states them.
    assert f"per client ({PlayServer.API_REQUESTS_PER_CLIENT})" in helps["max_connections"]
    assert f"busy game ({PlayApp.GAME_WAITERS})" in helps["max_connections"]
    assert helps["session_idle_minutes"].endswith(f"[public: {PUBLIC_LIMITS.session_idle_minutes}]")
    assert PUBLIC_LIMITS.max_connections_per_peer is None
    assert helps["max_connections_per_peer"].endswith("[public: off]")
    assert "--trusted-proxies 0" in helps["max_connections_per_peer"]


def _serve_then(monkeypatch: pytest.MonkeyPatch, probe: Callable[[int], None]) -> list[object]:
    original = PlayServer.serve_forever
    errors: list[object] = []

    def serve(self: PlayServer, poll_interval: float = 0.5) -> None:
        def worker() -> None:
            try:
                probe(int(self.server_address[1]))
            except BaseException as exc:  # reported by the test, never lost in a thread
                errors.append(exc)
            finally:
                self.shutdown()

        threading.Thread(target=worker, daemon=True).start()
        original(self, poll_interval)

    monkeypatch.setattr(PlayServer, "serve_forever", serve)
    return errors


def test_cca_play_public_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bound to 127.0.0.1 but treated as a public bind (0.0.0.0 would face the network).
    monkeypatch.setattr(play_server, "is_loopback", lambda host: False)
    seen: dict[str, object] = {}

    def probe(port: int) -> None:
        deadline = time.monotonic() + 60
        while not call(port, "GET", "/healthz").json()["ready"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        public = {"Host": HF, "Origin": HF_ORIGIN, "X-Forwarded-For": "6.6.6.6, 203.0.113.5"}
        info = call(port, "GET", "/api/info", headers={"Host": HF}, host=None)
        seen["info"] = info.json()
        seen["framing"] = info.headers["Content-Security-Policy"]
        seen["xfo"] = info.headers["X-Frame-Options"]
        created = call(port, "POST", "/api/games", {"human_color": "black"}, headers=public)
        game_id = str(created.json()["game_id"])
        seen["game_id"] = game_id
        think = call(port, "POST", f"/api/games/{game_id}/think", {}, headers=public)
        seen["statuses"] = [created.status, think.status]

    errors = _serve_then(monkeypatch, probe)
    argv = ["play", "--public", "--host", "127.0.0.1", "--port", "0", "--no-browser"]
    argv += ["--stockfish", str(make_launcher(tmp_path)), "--human", "qre", "--nodes", "500"]
    argv += ["--allowed-host", HF, "--trusted-proxies", "1"]
    argv += ["--frame-ancestor", "https://huggingface.co"]
    assert main(argv) == 0
    assert errors == []
    assert seen["statuses"] == [201, 200]
    info = seen["info"]
    assert isinstance(info, dict)
    assert info["limits"] == {**PUBLIC_LIMITS.describe(), "waiting": 0}
    assert str(seen["framing"]).endswith("frame-ancestors https://huggingface.co")
    assert seen["xfo"] is None
    err = capsys.readouterr().err
    assert err.startswith("cca play: PUBLIC MODE on 127.0.0.1:")
    assert "WARNING" not in err  # the notice replaces the local exposure warning
    log = [line for line in err.splitlines() if LOG_LINE.fullmatch(line)]
    assert any(" POST /api/games/:id/think 200 " in line for line in log)
    for secret in (str(seen["game_id"]), "203.0.113.5", "6.6.6.6"):
        assert secret not in err


# ---------------------------------------------------------------- local mode is unchanged
def test_local_defaults_are_unchanged() -> None:
    args = build_parser().parse_args(["play"])
    new = {
        "public": False,
        "allowed_host": None,
        "trusted_proxies": 0,
        "frame_ancestor": None,
        "max_sessions_per_client": None,
        "decisions_per_minute": None,
        "max_queue": None,
        "session_idle_minutes": None,
        "max_connections": None,
        "max_connections_per_peer": None,
    }
    assert {k: getattr(args, k) for k in new} == new
    assert CSP == CSP_0_1_0
    assert dict(SECURITY_HEADERS)["X-Frame-Options"] == "DENY"
    assert ServerOptions() == ServerOptions(False, 0, (), ())
    assert PlaySettings(AgentConfig()).limits == Limits()


def test_local_server_ignores_forwarded_headers_and_never_limits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = make_app(max_sessions=2)
    with serving(app) as server:
        port = port_of(server)
        assert server.log_stream is None
        statuses = [analyse_over_http(port, f"198.51.100.{n}").status for n in range(25)]
        assert statuses == [200] * 25  # beyond any public default, and XFF changes nothing
        resp = call(port, "GET", "/api/info", headers={"X-Forwarded-For": "1.2.3.4"})
        assert "limits" not in resp.json()
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Content-Security-Policy"] == CSP_0_1_0
        assert resp.headers["Retry-After"] is None
        origin = {"Origin": HF_ORIGIN}
        assert call(port, "POST", "/api/games", {}, headers=origin).status == 403
        for host in (HF, "evil.example"):
            resp = call(port, "GET", "/healthz", headers={"Host": host}, host=None)
            assert resp.status == 421
            assert "use localhost, 127.0.0.1 or [::1]" in str(resp.json()["error"])
    assert capsys.readouterr().err == ""  # no request log


def test_local_sessions_keep_plain_lru_eviction() -> None:
    clock = FakeClock()
    app = make_app(max_sessions=2, now=clock)
    first, second = game(app, "a"), game(app, "a")
    clock.advance(10 * 24 * 3600)  # nothing expires locally
    app.game_state(first)
    third = game(app, "b")  # a client below any quota still evicts: owners do not matter
    err = refused(lambda: app.game_state(second))
    assert err.message == "no such game (evicted, or the server restarted)"
    assert alive(app, first, third) == [True, True]
    for _ in range(30):
        lab(app, "a")  # no rate limit


# ---------------------------------------------------------------- page
NODE = shutil.which("node")
API_PROBE = """
import {api, ApiError} from API_URL
const json = {"Content-Type": "application/json"}
const reply = (status, body, extra = {}) =>
  new Response(JSON.stringify(body), {status, headers: {...json, ...extra}})
const replies = [
  reply(429, {error: "slow down", limit: "decisions_per_minute"}, {"Retry-After": "7"}),
  reply(503, {error: "warming up"}),
  reply(503, {error: "busy"}, {"Retry-After": "x"}),
  reply(429, {error: "at once", limit: "requests_at_once"}, {"Retry-After": "1"}),
  reply(409, {error: "odd", limit: 5}),
  reply(200, {ok: 1}),
]
globalThis.fetch = async () => replies.shift()
const out = []
for (let i = 0; i < 5; i++) {
  try { await api.post("/api/analyse", {}) } catch (err) {
    out.push([err instanceof ApiError, err.status, err.retryAfter, err.message, err.limit])
  }
}
out.push(await api.get("/api/info"))
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_page_reads_retry_after_from_refusals() -> None:
    assert NODE is not None
    probe = API_PROBE.replace("API_URL", json.dumps((STATIC / "js" / "api.js").as_uri()))
    done = subprocess.run(  # fixed argument list, no shell
        [NODE, "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        [True, 429, 7, "slow down", "decisions_per_minute"],
        [True, 503, None, "warming up", None],
        [True, 503, None, "busy", None],
        [True, 429, 1, "at once", "requests_at_once"],
        [True, 409, None, "odd", None],  # only a string names a limit
        {"ok": 1},
    ]


def _node(probe: str) -> object:
    """Run a Node.js ES-module probe and return what it printed, as JSON."""
    assert NODE is not None
    done = subprocess.run(  # fixed argument list, no shell
        [NODE, "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_page_words_every_error_in_i18n() -> None:
    # Round-3 finding: the new-game dialog showed the server's English sentence in the
    # Vietnamese page. Every toast and the dialog now word an error through i18n.js (see the
    # behaviour below); main.js never shows a server message by itself.
    main_js = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    show = "function showError(err, retryIn = null) {\n  toast(errorText(err, retryIn), true)\n}"
    assert show in main_js
    assert "    showError,\n    noGame,\n" in main_js  # the view hooks flow.js calls
    assert '$("new-error").textContent = newGameErrorText(err)' in main_js
    assert "err.message" not in main_js
    status = 'else if (ui.retrying) text = t("status_retrying")'
    assert status in main_js
    assert main_js.index('else if (ui.thinking) text = t("status_thinking")') < main_js.index(
        status
    )
    strings = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    keys = ["err_slow_down", "err_busy", "err_at_once", "err_game_busy", "err_no_slot"]
    keys += ["err_reads", "err_connections"]
    keys += ["err_retrying", "status_retrying", "status_no_game", "status_no_game_retry"]
    keys += ["wait_s", "wait_min", "wait_moment", "ask_text", "ask_cca"]
    for key in keys:
        assert strings.count(f"    {key}: ") == 2, key  # English and Vietnamese
    assert "{seconds}" not in strings  # every wait is worded by waitText()


I18N_PROBE = """
globalThis.document = {documentElement: {}}
const {setLanguage, errorText, newGameErrorText, waitText} = await import(I18N_URL)
const refused = (status, retryAfter, limit, message = "the server's own words") =>
  Object.assign(new Error(message), {status, retryAfter, limit})
const cases = [
  refused(429, 7, "decisions_per_minute"),
  refused(429, 1, "requests_at_once"),
  refused(503, 12, "max_queue"),
  refused(503, 2, "max_connections"),
  refused(503, 2, "max_connections_per_peer"),
  refused(503, 1792, "max_sessions"),
  refused(409, 27, "game_busy"),
  refused(429, 30, "reads_per_minute"),
  refused(429, null, "a_new_limit"),
  refused(503, 9, null),
  refused(429, 4, "__proto__"),
  refused(503, null, null, "warming up"),
  refused(409, null, null, "not your turn"),
  refused(400, null, "constructor", "bad FEN"),
  refused(0, null, null, "Failed to fetch"),
]
const out = {}
for (const lang of ["en", "vi"]) {
  setLanguage(lang)
  out[lang] = {
    toast: cases.map((err) => errorText(err)),
    dialog: cases.map((err) => newGameErrorText(err)),
    retrying: errorText(refused(503, 12, "max_queue"), 12),
    waits: [0, 2.5, 1, 59, 119, 120, 121, 1792, 3600, null].map(waitText),
  }
}
console.log(JSON.stringify(out))
"""

BUSY_EN = "The CCA server is busy with other players' games. Try again in {}."
SLOW_EN = (
    "Slow down a little: this public demo allows a limited number of CCA moves per minute. "
    "Try again in {}."
)
BUSY_VI = "Máy chủ CCA đang bận với ván cờ của người chơi khác. Thử lại sau {}."
SLOW_VI = (
    "Chậm lại một chút: bản demo công khai này giới hạn số nước đi của CCA mỗi phút. "
    "Thử lại sau {}."
)
LIMIT_WORDS = {
    "en": [
        SLOW_EN.format("7 s"),
        "You already have requests waiting for CCA. Try again in 1 s, once they are done.",
        BUSY_EN.format("12 s"),
        BUSY_EN.format("2 s"),
        "Too many connections from your network to this public demo (other tabs or devices?). "
        "Try again in 2 s.",
        "No free game slot: all games this public demo can hold are in use. Try again in 30 min.",
        "This game is still busy with an earlier request (CCA is thinking, perhaps in another "
        "tab). Try again in 27 s.",
        "Slow down a little: this public demo limits how often a game's moves and CCA's "
        "reasons are downloaded. Try again in 30 s.",
        SLOW_EN.format("a moment"),  # a limit this page does not know: by its status
        BUSY_EN.format("9 s"),  # a 503 with a Retry-After but no limit (a proxy's, say)
        SLOW_EN.format("4 s"),  # only the page's own table names a sentence
    ],
    "vi": [
        SLOW_VI.format("7 giây"),
        "Bạn đang có yêu cầu chờ CCA xử lý. Thử lại sau 1 giây, khi các yêu cầu đó đã xong.",
        BUSY_VI.format("12 giây"),
        BUSY_VI.format("2 giây"),
        "Có quá nhiều kết nối từ mạng của bạn tới bản demo công khai này (các thẻ hoặc thiết "
        "bị khác?). Thử lại sau 2 giây.",
        "Hết chỗ chơi: mọi ván mà bản demo công khai này chứa được đều đang được dùng. "
        "Thử lại sau 30 phút.",
        "Ván này vẫn đang bận với một yêu cầu trước đó (CCA đang nghĩ, có thể ở một thẻ "
        "khác). Thử lại sau 27 giây.",
        "Chậm lại một chút: bản demo công khai này giới hạn số lần tải nước đi và lý do của "
        "CCA trong một ván. Thử lại sau 30 giây.",
        SLOW_VI.format("giây lát"),
        BUSY_VI.format("9 giây"),
        SLOW_VI.format("4 giây"),
    ],
}


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_page_words_every_refusal_of_the_limits_in_both_languages() -> None:
    # Round-3 findings: a 409 game_busy fell to "Error: <English>", a full table read as
    # "busy ... try again in 1792 s", and the dialog showed the server's English sentence.
    probe = I18N_PROBE.replace("I18N_URL", json.dumps((STATIC / "js" / "i18n.js").as_uri()))
    out = _node(probe)
    assert isinstance(out, dict)
    generic = {"en": "Error: {}", "vi": "Lỗi: {}"}
    offline = {
        "en": 'Cannot reach the CCA server. Is "cca play" still running?',
        "vi": 'Không kết nối được máy chủ CCA. "cca play" còn chạy không?',
    }
    for lang, words in LIMIT_WORDS.items():
        # Other errors keep the page's words from before public mode: the toast wraps the
        # server's message, and the new-game dialog shows it as it is (a bad FEN, say).
        others = ["warming up", "not your turn", "bad FEN"]
        assert out[lang]["toast"] == [
            *words,
            *(generic[lang].format(m) for m in others),
            offline[lang],
        ], lang
        assert out[lang]["dialog"] == [*words, *others, "Failed to fetch"], lang
    assert out["en"]["retrying"] == (
        "CCA cannot move yet: this public demo is busy or limits CCA moves per minute. "
        "The page asks again by itself in 12 s."
    )
    assert out["vi"]["retrying"] == (
        "CCA chưa đi được: bản demo công khai đang bận hoặc giới hạn số nước đi của CCA mỗi "
        "phút. Trang sẽ tự hỏi lại sau 12 giây."
    )
    # Whole seconds (at least 1, rounded up) below two minutes, else whole minutes rounded up.
    en_waits = ["1 s", "3 s", "1 s", "59 s", "119 s", "2 min", "3 min", "30 min", "60 min"]
    vi_waits = ["1 giây", "3 giây", "1 giây", "59 giây", "119 giây", "2 phút", "3 phút"]
    assert out["en"]["waits"] == [*en_waits, "a moment"]
    assert out["vi"]["waits"] == [*vi_waits, "30 phút", "60 phút", "giây lát"]


def test_page_offers_to_ask_for_cca_move_and_words_a_refused_first_game() -> None:
    # Round-3 findings: after the automatic retries gave up nothing let the player ask for
    # CCA's move again; a refused first game left an empty page once its toast was gone.
    main_js = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    assert 'const ask = $("ask-banner")\n  const asking = canAsk(ui)\n' in main_js
    assert "  ask.hidden = !asking\n" in main_js  # (announced as it appears: test_play_a11y)
    assert '$("ask-btn").addEventListener("click", () => flow.maybeThink())' in main_js
    assert "await flow.firstGame()" in main_js
    assert "flow.newGame({})" not in main_js  # (only through firstGame)
    no_game = main_js.split("function noGame(err, retryIn) {", 1)[1].split("\n}\n", 1)[0]
    assert "  ui.noGame = {err, retryIn}\n  renderNoGame()" in no_game
    words = main_js.split("function renderNoGame() {", 1)[1].split("\n}\n", 1)[0]
    assert "  if (ui.state || !ui.noGame) return\n" in words  # (renderStatus, once a game is)
    assert 'status.textContent = errorText(err) + " " + (retryIn == null' in words
    assert 't("status_no_game")' in words
    assert 't("status_no_game_retry", {wait: waitText(retryIn)})' in words
    assert "  render()\n  renderNoGame()\n}" in main_js  # a language change words it again
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ask = r'<div class="ask-banner" id="ask-banner" hidden>(.*?)</div>'
    banner = re.search(ask, html, re.DOTALL)
    assert banner is not None
    assert '<button type="button" class="btn btn-small" id="ask-btn" data-i18n="ask_cca">' in (
        banner.group(1)
    )
    assert html.index('id="status"') < html.index('id="ask-banner"') < html.index('id="moves"')
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert ".review-banner, .lab-banner, .ask-banner {" in css


FLOW_PROBE = """
import {createFlow, canMove, canAsk, retryDelay, RETRY_MAX_S, RETRY_WINDOW_S} from FLOW_URL

const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0)) }
const st = (toMove, tag, over = false) =>
  ({tag, to_move: toMove, game_over: over, human_color: "black", moves: [], history: [], cca: {}})
const refusal = (status, retryAfter, limit) =>
  Object.assign(new Error("refused " + status), {status, retryAfter, limit, state: null})
const answer = (next) => { if (next instanceof Error) throw next; return next }

// Fakes of the page. h.feed lists what the server answers in turn (a state, or an error to
// throw): think, move, games (POST /api/games) and reads (GET; else the page's own state).
// h.rest answers /think and /api/games once their lists are empty. The clock moves only when
// a wait (sleep) ends. Renders record the flags they saw (main.js's applyState renders, and
// its drawBoard updates the input).
function harness({think = [], move = [], games = [], reads = [], rest = null} = {}) {
  const ui = {state: null, gameId: null, gen: 0, busy: false, thinking: false, retrying: false,
              starting: false, labBusy: false, reveal: null, review: null, lab: null,
              decisions: new Map()}
  const h = {ui, rest, feed: {think, move, games, reads}, clock: 0,
             posts: {think: 0, move: 0, games: 0}, errors: [], noGame: [], sleeps: [],
             renders: [], inputs: [], waiting: []}
  h.flags = () => ({busy: ui.busy, thinking: ui.thinking, retrying: ui.retrying,
                    canMove: canMove(ui), canAsk: canAsk(ui)})
  const api = {
    async post(path) {
      if (path === "/api/games") {
        h.posts.games++
        const state = answer(h.feed.games.length ? h.feed.games.shift() : h.rest)
        return {game_id: "n" + h.posts.games, state}
      }
      if (path.endsWith("/move")) {
        h.posts.move++
        return {state: answer(h.feed.move.shift())}
      }
      if (!path.endsWith("/think")) throw new Error("unexpected " + path)
      h.posts.think++
      const state = answer(h.feed.think.length ? h.feed.think.shift() : h.rest)
      return {move: null, decision: null, reveal_in_ms: 0, state}
    },
    async get() { return h.feed.reads.length ? answer(h.feed.reads.shift()) : ui.state },
  }
  const view = {
    applyState(state) { ui.state = state; h.renders.push(h.flags()) },
    render() { h.renders.push(h.flags()) },
    updateInput() { h.inputs.push(h.flags()) },
    drawBoard() { h.inputs.push(h.flags()) },
    renderPlayers() {}, remember() {}, setOrientation() {}, announceCca() {},
    announceHuman() {}, announceOver() {},
    showError(err, retryIn) { h.errors.push([err.status, retryIn ?? null]) },
    noGame(err, retryIn) { h.noGame.push([err.status, retryIn ?? null]) },
  }
  const sleep = (ms) => {
    h.waiting.push(ui.retrying && !canMove(ui) && !canAsk(ui))
    return new Promise((resolve) => h.sleeps.push({ms, resolve}))
  }
  h.flow = createFlow({ui, api, view, sleep, now: () => h.clock})
  return h
}
// Ends the oldest wait (the clock moves on by its length); wakeAll ends waits until none is
// left (at most `max`) and returns how many ended.
const wake = async (h) => { const s = h.sleeps.shift(); h.clock += s.ms; s.resolve(); await tick() }
const wakeAll = async (h, max = 200) => {
  let n = 0
  while (h.sleeps.length && n < max) { await wake(h); n++ }
  return n
}
const out = {}

{ // A busy 503 is asked again after its Retry-After, and the game goes on.
  const h = harness({think: [refusal(503, 12, "max_queue"), st("human", "moved")]})
  h.flow.startSession("g1", st("cca", "start"))
  await tick()
  out.busy = {thinks: h.posts.think, errors: h.errors, sleeps: h.sleeps.map((s) => s.ms),
              waiting: h.waiting}
  await wake(h)
  out.busy.after = {thinks: h.posts.think, tag: h.ui.state.tag, ...h.flags(),
                    sleeps: h.sleeps.length}
}
{ // Every limit refusal is retried (429 slow down or at once, 409 game busy, 503 connections).
  const think = [refusal(429, 7, "decisions_per_minute"), refusal(409, 3, "game_busy"),
                 refusal(429, 1, "requests_at_once"), refusal(503, 2, "max_connections"),
                 st("human", "moved")]
  const h = harness({think})
  h.flow.startSession("g1", st("cca", "start"))
  await tick()
  const waits = []
  while (h.sleeps.length) { waits.push(h.sleeps[0].ms); await wake(h) }
  out.kinds = {waits, thinks: h.posts.think, tag: h.ui.state.tag}
}
{ // Short waits are followed for as long as the window lasts, however many (no cap by count).
  const refused = Array.from({length: 30}, () => refusal(429, 1, "requests_at_once"))
  const h = harness({think: [...refused, st("human", "moved")]})
  h.flow.startSession("g1", st("cca", "start"))
  await tick()
  const woken = await wakeAll(h)
  out.short = {thinks: h.posts.think, woken, clock: h.clock, tag: h.ui.state.tag}
}
{ // Other failures are not asked again by themselves (no limit, as an engine error; no
  // Retry-After; offline, even for the read that follows). The page then offers to ask
  // (canAsk); the last render already shows it, and asking goes on with the game.
  out.others = []
  const cases = [[refusal(503, null, null), []], [refusal(503, 5, null), []],
                 [refusal(429, null, "x"), []], [refusal(400, null, null), []],
                 [refusal(0, null, null), [refusal(0, null, null)]]]
  for (const [err, reads] of cases) {
    const h = harness({think: [err, st("human", "moved")], reads})
    h.flow.startSession("g1", st("cca", "start"))
    await tick()
    const row = {thinks: h.posts.think, sleeps: h.sleeps.length, errors: h.errors,
                 last: h.renders.at(-1)}
    h.flow.maybeThink()
    await tick()
    row.asked = {thinks: h.posts.think, tag: h.ui.state.tag, canAsk: canAsk(h.ui)}
    out.others.push(row)
  }
}
{ // A new context (a new game, a take-back) cancels the wait, even one that waits in turn.
  const h = harness({think: [refusal(503, 4, "max_queue"), refusal(503, 9, "max_queue")]})
  h.flow.startSession("g1", st("cca", "start"))
  await tick()
  h.flow.startSession("g2", st("cca", "other"))  // CCA moves first here too, and is refused
  await tick()
  await wake(h)  // g1's wait ends: nothing is sent (not for g1, nor early for g2)
  out.cancelled = {thinks: h.posts.think, sleeps: h.sleeps.map((s) => s.ms),
                   retrying: h.ui.retrying}
  h.flow.startSession("g3", st("human", "mine"))
  await wake(h)
  out.cancelled.after = {thinks: h.posts.think, retrying: h.ui.retrying}
}
{ // Refusals are asked again for RETRY_WINDOW_S at most (by time, not by count). Then the page
  // offers to ask (the round-3 finding: it gave up and nothing let the player ask again), and
  // asking starts a new window.
  const h = harness({rest: refusal(503, 60, "max_queue")})
  h.flow.startSession("g1", st("cca", "start"))
  await tick()
  const woken = await wakeAll(h)
  out.window = {thinks: h.posts.think, woken, clock: h.clock, last: h.errors.at(-1),
                flags: h.flags(), render: h.renders.at(-1)}
  h.feed.think.push(refusal(503, 60, "max_queue"), st("human", "moved"))
  h.flow.maybeThink()
  await tick()
  out.window.asked = {thinks: h.posts.think, sleeps: h.sleeps.map((s) => s.ms)}
  await wake(h)
  out.window.asked.after = {thinks: h.posts.think, tag: h.ui.state.tag, ...h.flags()}
}
{ // A refused move whose read is refused too: the page shows the move is no longer being
  // sent (round-3 finding: "Sending your move..." stayed, with the move entry disabled).
  out.move = []
  for (const err of [refusal(409, 3, "game_busy"), refusal(503, 2, "max_connections"),
                     refusal(0, null, null)]) {
    const h = harness({move: [err], reads: [err]})
    h.flow.startSession("g1", st("human", "start"))
    await tick()
    await h.flow.humanMove({from: "e7", to: "e5", san: "e5"})
    out.move.push({moves: h.posts.move, errors: h.errors, render: h.renders.at(-1),
                   input: h.inputs.at(-1), thinks: h.posts.think, sleeps: h.sleeps.length})
  }
}
{ // The first game (none to resume): a short Retry-After is waited for and the game asked
  // for again; a long one (a full table), or any other failure, is only said.
  out.first = {}
  {
    const h = harness({games: [refusal(503, 3, "max_queue"), st("human", "first")]})
    const done = h.flow.firstGame()
    await tick()
    out.first.short = {noGame: [...h.noGame], sleeps: h.sleeps.map((s) => s.ms),
                       starting: h.ui.starting}
    await wake(h)
    await done
    out.first.short.after = {games: h.posts.games, tag: h.ui.state.tag, noGame: h.noGame.length,
                             sleeps: h.sleeps.length}
  }
  const cases = {full: refusal(503, 1792, "max_sessions"), long: refusal(503, 61, "max_queue"),
                 longest_short: refusal(503, 60, "max_queue"), offline: refusal(0, null, null),
                 engine: refusal(500, null, null)}
  for (const [name, err] of Object.entries(cases)) {
    const h = harness({games: [err, st("human", "later")]})
    h.flow.firstGame()
    await tick()
    out.first[name] = {noGame: h.noGame, sleeps: h.sleeps.map((s) => s.ms), games: h.posts.games,
                       state: h.ui.state}
  }
  for (const name of ["started", "starting"]) {  // New game used meanwhile ends the tries
    const h = harness({games: [refusal(503, 3, "max_queue"), st("human", "late")]})
    h.flow.firstGame()
    await tick()
    if (name === "started") h.flow.startSession("mine", st("human", "mine"))
    else h.ui.starting = true
    await wake(h)
    out.first[name] = {games: h.posts.games, tag: h.ui.state && h.ui.state.tag,
                       sleeps: h.sleeps.length}
  }
  {
    const h = harness({rest: refusal(503, 60, "max_queue")})
    h.flow.firstGame()
    await tick()
    const woken = await wakeAll(h)
    out.first.window = {games: h.posts.games, woken, tries: h.noGame.length,
                        last: h.noGame.at(-1)}
  }
}
{
  const base = {state: st("cca", "x"), busy: false, thinking: false, retrying: false,
                starting: false, labBusy: false, review: null}
  const variants = {due: {}, busy: {busy: true}, thinking: {thinking: true},
                    retrying: {retrying: true}, starting: {starting: true},
                    human: {state: st("human", "x")}, over: {state: st("cca", "x", true)},
                    none: {state: null}, lab: {labBusy: true}, review: {review: 0}}
  out.canAsk = Object.fromEntries(
    Object.entries(variants).map(([name, v]) => [name, canAsk({...base, ...v})]))
}
out.delays = [
  retryDelay({limit: "max_queue", retryAfter: 12}, 0),
  retryDelay({limit: "max_queue", retryAfter: 0}, 0),
  retryDelay({limit: "max_queue", retryAfter: 500}, 0),
  retryDelay({limit: "max_queue", retryAfter: 2.5}, 0),
  retryDelay({limit: "max_queue", retryAfter: 60}, 840000),
  retryDelay({limit: "max_queue", retryAfter: 60}, 840001),
  retryDelay({limit: "max_queue", retryAfter: 5}, 895000),
  retryDelay({limit: "max_queue", retryAfter: 5}, 895001),
  retryDelay({limit: 7, retryAfter: 5}, 0),
  retryDelay({limit: "x", retryAfter: "5"}, 0),
  retryDelay({limit: "x", retryAfter: Infinity}, 0),
  retryDelay(null, 0),
]
out.constants = [RETRY_MAX_S, RETRY_WINDOW_S]
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_page_asks_again_for_a_move_the_limits_refused() -> None:
    # Round-2 finding: after a refused /think the page said "try again in N s" but nothing
    # ever asked again. Round-3 findings: the retries stopped after 20 (by count, so after
    # ~40 s of 2 s waits) and then nothing let the player ask; a refused move whose read was
    # refused too left "Sending your move..."; a refused first game left an empty page.
    probe = FLOW_PROBE.replace("FLOW_URL", json.dumps((STATIC / "js" / "flow.js").as_uri()))
    out = _node(probe)
    assert isinstance(out, dict)
    idle = {"busy": False, "thinking": False, "retrying": False}
    stuck = {**idle, "canMove": False, "canAsk": True}  # CCA's move due, nothing asks for it
    playing = {**idle, "canMove": True, "canAsk": False}
    assert out["busy"] == {
        "thinks": 1,
        "errors": [[503, 12]],  # shown as "asks again in 12 s"
        "sleeps": [12000],
        "waiting": [True],  # "waiting" status while asleep; nothing to click meanwhile
        "after": {"thinks": 2, "tag": "moved", **playing, "sleeps": 0},
    }
    assert out["kinds"] == {"waits": [7000, 3000, 1000, 2000], "thinks": 5, "tag": "moved"}
    assert out["short"] == {"thinks": 31, "woken": 30, "clock": 30000, "tag": "moved"}
    statuses = [503, 503, 429, 400, 0]
    assert len(out["others"]) == len(statuses)
    for row, status in zip(out["others"], statuses, strict=True):
        errors = [[status, None]] * (2 if status == 0 else 1)  # offline: its read fails too
        assert row == {
            "thinks": 1,
            "sleeps": 0,
            "errors": errors,
            "last": stuck,
            "asked": {"thinks": 2, "tag": "moved", "canAsk": False},
        }, status
    assert out["cancelled"] == {
        "thinks": 2,
        "sleeps": [9000],
        "retrying": True,  # g2 waits for its own Retry-After
        "after": {"thinks": 2, "retrying": False},
    }
    window_s = out["constants"][1]
    retries = window_s // 60  # one at 0 s, then one every 60 s up to the window
    after = {"thinks": 3 + retries, "tag": "moved", **playing}
    assert out["window"] == {
        "thinks": 1 + retries,
        "woken": retries,
        "clock": window_s * 1000,
        "last": [503, None],  # shown as it is: no automatic retry any more
        "flags": stuck,
        "render": stuck,
        "asked": {"thinks": 2 + retries, "sleeps": [60000], "after": after},
    }
    for row in out["move"]:
        status = row["errors"][0][0]
        assert row == {
            "moves": 1,
            "errors": [[status, None], [status, None]],  # the move, then its read
            "render": playing,
            "input": playing,
            "thinks": 0,
            "sleeps": 0,
        }, status
    first = out["first"]
    assert first["short"] == {
        "noGame": [[503, 3]],  # "no game yet: the page tries again in 3 s"
        "sleeps": [3000],
        "starting": False,
        "after": {"games": 2, "tag": "first", "noGame": 1, "sleeps": 0},
    }
    for name, status in (("full", 503), ("long", 503), ("offline", 0), ("engine", 500)):
        assert first[name] == {
            "noGame": [[status, None]],  # said, with New game as the way on
            "sleeps": [],
            "games": 1,
            "state": None,
        }, name
    assert first["longest_short"] == {
        "noGame": [[503, 60]],
        "sleeps": [60000],
        "games": 1,
        "state": None,
    }
    assert first["started"] == {"games": 1, "tag": "mine", "sleeps": 0}
    assert first["starting"] == {"games": 1, "tag": None, "sleeps": 0}
    assert first["window"] == {
        "games": 1 + window_s // 60,
        "woken": window_s // 60,
        "tries": 1 + window_s // 60,
        "last": [503, None],
    }
    assert out["canAsk"] == {
        "due": True,
        "busy": False,
        "thinking": False,
        "retrying": False,
        "starting": False,
        "human": False,
        "over": False,
        "none": False,
        "lab": True,  # a position-lab analysis does not hold CCA's move
        "review": True,
    }
    assert out["delays"] == [12, 1, 60, 3, 60, None, 5, None, None, None, None, None]
    assert out["constants"] == [60, 900]


# ---------------------------------------------------------------- small hosts (Render Free)
class TimedEngine(FakeEngine):
    """A FakeEngine whose every search takes ``cost`` seconds, or its time limit if shorter
    (as Stockfish stops at ``Limit(time=...)``); it records the limits it was given."""

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost
        self.limits: list[float | None] = []

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        self.limits.append(time_limit)
        time.sleep(self.cost if time_limit is None else min(self.cost, time_limit))
        return super().evaluate(board, perspective=perspective, moves=moves, multipv=multipv)


def _spy_choose(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float | None, object]]:
    """Record (seconds to the deadline, stop event) of every CAIMEAgent.choose call."""
    seen: list[tuple[float | None, object]] = []
    choose = CAIMEAgent.choose

    def spy(
        self: CAIMEAgent,
        board: chess.Board,
        clock: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        seen.append((None if deadline is None else deadline - time.monotonic(), stop))
        return choose(self, board, clock, deadline=deadline, stop=stop)

    monkeypatch.setattr(CAIMEAgent, "choose", spy)
    return seen


def _legal(fen: str, uci: object) -> bool:
    return isinstance(uci, str) and chess.Move.from_uci(uci) in chess.Board(fen).legal_moves


def test_max_think_seconds_bounds_every_decision() -> None:
    # On 0.1 CPU a 200k-node decision takes minutes; the cap bounds it, and the agent shortens
    # its search to meet it (engine calls get time slices, the look-ahead is cut short).
    cap = 1.0
    engine = TimedEngine(0.4)  # ten searches per decision: about 4 s without the cap
    app = make_app(Limits(max_think_seconds=cap), engine=engine)
    black = game(app, "a", human_color="black")  # CCA (white) to move
    decisions: list[Callable[[], dict[str, object]]] = [
        lambda: lab(app, "a"),
        lambda: app.think(black, client="a"),
    ]
    for decide in decisions:
        engine.limits.clear()
        started = time.monotonic()
        result = decide()
        took = time.monotonic() - started
        assert took < cap + 0.5, took
        decision = result["decision"]
        assert isinstance(decision, dict)
        move = decision["move"]
        assert isinstance(move, dict)
        assert _legal(chess.STARTING_FEN, move["uci"])
        assert engine.limits, "no engine search"
        assert all(limit is not None and 0 < limit <= cap + 1e-6 for limit in engine.limits)


def test_a_timed_game_keeps_the_earlier_of_its_clock_and_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_choose(monkeypatch)
    app = make_app(Limits(max_think_seconds=5.0))
    long = game(app, "a", human_color="black", time_control={"base_s": 3 * 3600, "inc_s": 0})
    app.think(long, client="a")
    short = game(app, "b", human_color="black", time_control={"base_s": 2.0, "inc_s": 0})
    app.think(short, client="b")
    untimed = game(app, "c", human_color="black")
    app.think(untimed, client="c")
    lab(app, "d")
    left = [s for s, _ in seen]
    assert all(s is not None for s in left), left
    long_s, short_s, untimed_s, lab_s = (float(s) for s in left if s is not None)
    assert 4.0 < long_s <= 5.0  # the cap: its clock would allow minutes
    assert short_s < 2.0  # its clock's own deadline comes first
    assert 4.0 < untimed_s <= 5.0
    assert 4.0 < lab_s <= 5.0
    assert info_limits(app)["max_think_seconds"] == 5.0


def info_limits(app: PlayApp) -> dict[str, object]:
    limits = app.info()["limits"]
    assert isinstance(limits, dict)
    return limits


def test_local_decisions_get_no_deadline_and_no_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local mode is unchanged: node-limited, reproducible searches, and no stop at shutdown.
    seen = _spy_choose(monkeypatch)
    engine = TimedEngine(0.0)
    app = make_app(engine=engine)
    lab(app, None)
    untimed = game(app, None, human_color="black")
    app.think(untimed)
    assert seen == [(None, None), (None, None)]
    assert engine.limits
    assert set(engine.limits) == {None}
    app.close()
    assert "limits" not in app.info()


def _waiting_is(gate: EngineGate, count: int) -> bool:
    return gate.waiting == count


def test_the_engine_line_serves_waiting_clients_in_turn() -> None:
    # Load-review finding (R4): the lock went to whichever waiter won it, so one identity at
    # its allowed rate took most of the engine and each greedy identity multiplied everyone
    # else's wait. Now each turn goes to the waiting client whose last turn is the oldest.
    lock = threading.Lock()
    gate = EngineGate(lock, 6)
    order: list[str] = []

    def take(client: str) -> threading.Thread:
        def run() -> None:
            gate.acquire(client)
            order.append(client)
            gate.release()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker

    workers: list[threading.Thread] = []
    lock.acquire()  # a decision runs
    try:
        for count, client in enumerate(("a", "a", "c", "c", "b"), start=1):  # 2 greedy, 1 new
            workers.append(take(client))
            _wait_for(functools.partial(_waiting_is, gate, count))
    finally:
        lock.release()
    for worker in workers:
        worker.join(10)
    assert order == ["a", "c", "b", "a", "c"]  # in arrival order "b" would come last
    assert gate.waiting == 0


def test_shutdown_refuses_the_engine_line_at_once_and_stops_the_running_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Load-review finding: on shutdown, requests queued for the engine ran one after another
    # (close() waited its 5 s) instead of being refused.
    seen = _spy_choose(monkeypatch)
    app = make_app(Limits(max_queue=6))
    lab(app, "x")
    stop = seen[-1][1]
    assert isinstance(stop, threading.Event)
    assert not stop.is_set()
    outcomes: list[object] = []

    def wait_in_line(client: str) -> threading.Thread:
        def run() -> None:
            try:
                outcomes.append(lab(app, client))
            except ApiError as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker

    app._engine_lock.acquire()  # a decision runs
    closer = threading.Thread(target=app.close, daemon=True)
    try:
        workers = [wait_in_line("a"), wait_in_line("b")]
        _wait_for(lambda: app._gate.waiting == 2)
        closer.start()
        _wait_for(lambda: len(outcomes) == 1)  # the one waiting for its turn: refused at once
        assert stop.is_set()  # and the running decision was asked to stop
    finally:
        app._engine_lock.release()
    for worker in workers:
        worker.join(10)
    closer.join(10)
    assert len(outcomes) == 2
    for outcome in outcomes:  # the one whose turn it was gave the engine back unused
        assert isinstance(outcome, ApiError), outcome
        assert outcome.status == 503
        assert "shutting down" in outcome.message
    assert refused_at_once(lambda: app._gate.acquire("c")).status == 503  # and every later one
    assert app._gate.waiting == 0  # the refused are no longer counted as waiting
    assert app._gate._waiting_by == {}


def test_reads_of_a_games_records_are_rate_limited_per_client() -> None:
    app = make_app(Limits(reads_per_minute=2))
    with serving(app, ServerOptions(trusted_proxies=1)) as server:
        port = port_of(server)
        me = {"X-Forwarded-For": "198.51.100.1"}
        gid = str(call(port, "POST", "/api/games", {}, headers=me).json()["game_id"])
        assert call(port, "GET", f"/api/games/{gid}/decisions", headers=me).status == 200
        assert call(port, "GET", f"/api/games/{gid}/pgn", headers=me).status == 200
        limited = call(port, "GET", f"/api/games/{gid}/decisions", headers=me)
        assert limited.status == 429
        assert limited.headers["Retry-After"] == "30"
        assert limited.json()["limit"] == "reads_per_minute"
        assert "reads of a game's decisions or PGN" in str(limited.json()["error"])
        assert call(port, "GET", f"/api/games/{gid}/pgn", headers=me).status == 429
        assert call(port, "GET", f"/api/games/{gid}", headers=me).status == 200  # not counted
        other = {"X-Forwarded-For": "198.51.100.2"}
        assert call(port, "GET", f"/api/games/{gid}/decisions", headers=other).status == 200


def test_decisions_are_served_from_their_serialisation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Load-review finding (M4): every read of /decisions serialised the whole game (702 kB for
    # 200 decisions), which slowed every other visitor ~95x. Each decision is now serialised
    # once, when made; a read only joins the bytes.
    app = make_app(Limits(decisions_per_minute=600))
    gid = game(app, "a", human_color="black")
    app.think(gid, client="a")
    stored = app.session(gid).decisions
    assert list(stored) == [0]
    assert all(isinstance(blob, bytes) for blob in stored.values())
    calls = 0
    dumps = play_serialize.dumps  # (the function play_server imported)

    def counted(value: object) -> bytes:
        nonlocal calls
        calls += 1
        return dumps(value)

    monkeypatch.setattr(play_server, "dumps", counted)
    with serving(app) as server:
        body = call(port_of(server), "GET", f"/api/games/{gid}/decisions").body
    assert calls == 0  # nothing serialised by the read
    decisions = json.loads(body)["decisions"]
    assert [item["ply"] for item in decisions] == [0]
    assert body == dumps(json.loads(body))  # the same bytes as serialising it now


HOPS_LINE = re.compile(LOG_LINE.pattern + r" xff=(\d+|-)")


def test_forwarded_hops_are_logged_as_numbers_only() -> None:
    assert forwarded_hops([]) == 0
    assert forwarded_hops(["6.6.6.6, 203.0.113.5", "10.0.0.1"]) == 3  # every line counts
    assert forwarded_hops(["6.6.6.6,"]) == 2  # as forwarded_client counts them
    app = make_app(Limits(max_connections=8))
    log = io.StringIO()
    options = ServerOptions(
        public=True, allowed_hosts=(HF,), trusted_proxies=1, log_forwarded_hops=True
    )
    with serving(app, options, public_bind=True) as server:
        server.log_stream = log
        port = port_of(server)
        call(port, "GET", "/api/info")
        call(port, "GET", "/api/info", headers={"X-Forwarded-For": "203.0.113.5"})
        three = {"X-Forwarded-For": "6.6.6.6, 203.0.113.5, 10.1.2.3"}
        call(port, "GET", "/healthz", headers=three)
        _raw_exchange(
            port,
            b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\nX-Forwarded-For: 6.6.6.6\r\n"
            b"X-Forwarded-For: 203.0.113.5, 10.1.2.3\r\n\r\n",
        )
        _raw_exchange(port, b"GARBAGE\r\n\r\n")  # no head parsed: no count
    lines = log.getvalue().splitlines()
    for line in lines:
        assert HOPS_LINE.fullmatch(line), line
    assert [line.rsplit(" ", 1)[1] for line in lines] == [
        "xff=0",
        "xff=1",
        "xff=3",
        "xff=3",
        "xff=-",
    ]
    for address in ("6.6.6.6", "203.0.113.5", "10.1.2.3", "127.0.0.1"):
        assert address not in log.getvalue(), address
    server = make_server("127.0.0.1", 0, make_app(PUBLIC_LIMITS), options)
    try:
        assert "xff=N is the number of X-Forwarded-For entries" in public_notice(server)
    finally:
        server.server_close()


def test_a_connection_refused_at_accept_is_logged_with_xff_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verifier mutant (refusal_line_drops_xff): with --log-forwarded-hops every line has an xff
    # field, also the 503 of a connection refused when it is accepted (no head read: xff=-).
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)
    app = make_app(Limits(max_connections=1))
    log = io.StringIO()
    options = ServerOptions(
        public=True, allowed_hosts=(HF,), trusted_proxies=1, log_forwarded_hops=True
    )
    with serving(app, options, public_bind=True) as server:
        server.log_stream = log
        port = port_of(server)
        holder = _slow_head(port, "/api/games", length=10)  # past its head: keeps the place
        try:
            _wait_for(lambda: server.connections == 1)
            data = _raw_exchange(port, f"GET /api/info HTTP/1.0\r\nHost: {HF}\r\n\r\n".encode())
        finally:
            holder.close()
    assert data.startswith(b"HTTP/1.0 503 ")
    refusals = [line for line in log.getvalue().splitlines() if " - - 503 -ms" in line]
    assert len(refusals) == 1
    assert HOPS_LINE.fullmatch(refusals[0])
    assert refusals[0].endswith(" xff=-")


def test_answers_are_written_with_the_handlers_timeout_once_the_head_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verifier mutant (head_done_keeps_poll_timeout) as a guard: with max_connections a
    # request head is read in steps of _GuardedReader.POLL_S (its deadline); once it is in,
    # the connection is back to PlayHandler.timeout. Left at POLL_S, the answer to a reader
    # slower than that (a large one, that the send buffer cannot hold) would be cut off.
    seen: list[tuple[str, float | None]] = []
    original = PlayHandler._send

    def spy(
        self: PlayHandler,
        status: int,
        body: bytes,
        ctype: str,
        *,
        cache: str = "no-store",
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        seen.append((self.path, self.connection.gettimeout()))
        original(self, status, body, ctype, cache=cache, extra=extra)

    monkeypatch.setattr(PlayHandler, "_send", spy)
    app = make_app(PUBLIC_LIMITS)
    paths = ["/", "/static/js/main.js", "/api/info", "/healthz"]
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        for path in paths:
            assert call(port, "GET", path).status == 200
        assert call(port, "POST", "/api/games", {}).status == 201
    assert PlayHandler.timeout > _GuardedReader.POLL_S
    assert seen == [(path, PlayHandler.timeout) for path in [*paths, "/api/games"]]


@pytest.mark.usefixtures("never_serve")
def test_log_forwarded_hops_needs_public(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["play", "--log-forwarded-hops", "--no-browser"]
    assert main([*argv, "--stockfish", str(make_launcher(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: --log-forwarded-hops needs --public")


def test_log_forwarded_hops_reaches_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _captured_run(monkeypatch)
    argv = ["play", "--public", "--host", WILDCARD, "--no-browser", "--allowed-host", HF]
    argv += ["--stockfish", str(make_launcher(tmp_path)), "--log-forwarded-hops"]
    argv += ["--max-think-seconds", "12.5"]
    assert main(argv) == 0
    app, kwargs = seen[0]
    options = kwargs["options"]
    assert isinstance(options, ServerOptions)
    assert options.log_forwarded_hops is True
    assert app.settings.limits.max_think_seconds == 12.5


def _closed_by_server(sock: socket.socket, wait: float = 3.0) -> bool:
    """Whether the server closed ``sock`` within ``wait`` seconds (without a reply)."""
    sock.settimeout(wait)
    try:
        return sock.recv(1) == b""
    except TimeoutError:
        return False
    except OSError:
        return True


def test_idle_connections_give_their_places_to_newcomers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Load-review finding (C4): 64 idle sockets, reopened when the server timed them out, held
    # every thread; a visitor was served 1 time in 139. Now a connection that has waited more
    # than HEAD_GRACE_S for its request head gives its place to a newcomer on a full server.
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 0.2)
    app = make_app(Limits(max_connections=4))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(4)]
        try:
            for round_ in range(2):  # the attacker refills the table between visitors
                _wait_for(lambda: server.connections == 4)
                time.sleep(0.3)  # older than the grace: they hold places, nothing more
                visitor = analyse_over_http(port, f"203.0.113.{round_}", host=HF)
                assert visitor.status == 200
                assert _closed_by_server(idle[round_])  # the oldest idle one gave its place
                idle.append(socket.create_connection(("127.0.0.1", port), timeout=10))
            _wait_for(lambda: server.connections == 4)
            assert not any(_closed_by_server(sock, 0.2) for sock in idle[2:])  # the others
        finally:
            for sock in idle:
                sock.close()


@pytest.mark.parametrize("bounded", [True, False])
def test_a_request_head_has_an_overall_deadline_only_when_threads_are_bounded(
    bounded: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Load-review finding (C5): 59 bytes sent slower than one per 30 s held a thread for ever
    # (the 30 s timeout was per receive). Locally the head keeps only that per-receive timeout.
    monkeypatch.setattr(PlayHandler, "HEAD_S", 0.3)
    app = make_app(Limits(max_connections=8) if bounded else None)
    with serving(app) as server:
        port = port_of(server)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"GET /healthz HTTP/1.0\r\n")
            time.sleep(0.9)  # three times HEAD_S, then the rest of the head
            with contextlib.suppress(OSError):
                sock.sendall(b"Host: 127.0.0.1\r\n\r\n")
            if bounded:
                assert _closed_by_server(sock)
            else:
                assert _reply(sock)[0] == 200


def test_a_trickled_head_is_cut_off_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PlayHandler, "HEAD_S", 0.8)
    app = make_app(Limits(max_connections=4))
    stop = threading.Event()
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        _trickle(sock, stop, every=0.1)  # a request line that never ends, never silent
        started = time.monotonic()
        try:
            _wait_for(lambda: server.connections == 1)
            _wait_for(lambda: server.connections == 0)
            took = time.monotonic() - started
        finally:
            stop.set()
            sock.close()
    assert took < 0.8 + _GuardedReader.POLL_S + 0.5, took


def test_a_refusal_at_the_cap_reaches_a_client_that_sends_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Load-review finding: the 503 of a full server was often lost to a reset (51 of 138),
    # because the socket was closed before the client's request arrived; the page then saw a
    # network error, not a refusal it could wait out.
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)
    app = make_app(Limits(max_connections=1))
    # A request larger than what the refusal reads at once: part of it is still unread (or
    # on its way) when the server is done with the connection. Closing it then would reset
    # it, and a reset destroys the 503 waiting in the client's buffer.
    request = b"GET /api/info HTTP/1.0\r\nHost: " + HF.encode() + b"\r\nX-Pad: "
    request += b"a" * 400_000 + b"\r\n\r\n"
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        holder = _slow_head(port, "/api/games", length=10)  # past its head: keeps the place
        try:
            _wait_for(lambda: server.connections == 1)
            with socket.create_connection(("127.0.0.1", port), timeout=10) as late:

                def send() -> None:
                    with contextlib.suppress(OSError):
                        late.sendall(request)

                sender = threading.Thread(target=send, daemon=True)
                sender.start()
                assert select.select([late], [], [], 5)[0]  # the 503 has arrived
                _wait_for(lambda: server._lingerer.held == 1)  # kept open, read and dropped
                sender.join(10)
                time.sleep(0.3)  # (a reset, if any, has arrived before we read)
                data = b""
                while chunk := late.recv(65536):
                    data += chunk
            _wait_for(lambda: server._lingerer.held == 0)  # closed once the client closed
        finally:
            holder.close()
    assert data.startswith(b"HTTP/1.0 503 ")
    assert json.loads(data.partition(b"\r\n\r\n")[2])["limit"] == "max_connections"


def test_a_request_whose_client_left_is_still_logged() -> None:
    # Load-review finding (L2): 10 analyses whose client reset the connection gave 1 log line,
    # though every decision was made. Each now has its line, marked "aborted".
    app = make_app(Limits(max_connections=8), engine=TimedEngine(0.03))
    log = io.StringIO()
    options = ServerOptions(public=True, allowed_hosts=(HF,), trusted_proxies=1)
    body = json.dumps({"fen": chess.STARTING_FEN}).encode("utf-8")
    head = (
        f"POST /api/analyse HTTP/1.0\r\nHost: {HF}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")

    def reset(sock: socket.socket) -> None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()  # a reset: nothing more can be read from it or delivered to it

    with serving(app, options, public_bind=True) as server:
        server.log_stream = log
        port = port_of(server)
        for _ in range(3):  # left during the decision: its answer (200) cannot be delivered
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(head + body)
            time.sleep(0.05)
            reset(sock)
        _wait_for(lambda: log.getvalue().count("\n") == 3)
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(head + body[:5])  # left while its body was awaited: no answer attempted
        time.sleep(0.2)
        reset(sock)
        _wait_for(lambda: log.getvalue().count("\n") == 4)
    lines = log.getvalue().splitlines()
    assert len(lines) == 4
    for line in lines[:3]:
        assert " POST /api/analyse 200 " in line, line
        assert line.endswith(" aborted"), line
    assert " POST /api/analyse - " in lines[3]
    assert lines[3].endswith(" aborted")


def test_the_think_cap_starts_once_the_decision_has_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round-3 finding: nothing pinned that waiting in the engine line does not eat into the
    # max_think_seconds cap (the deadline must be taken once the engine is the decision's).
    seen = _spy_choose(monkeypatch)
    cap = 5.0
    app = make_app(Limits(max_think_seconds=cap, max_queue=6))
    black = game(app, "a", human_color="black")  # CCA (white) to move
    decided: list[object] = []
    app._engine_lock.acquire()  # another decision runs
    try:
        workers = [
            threading.Thread(target=lambda: decided.append(app.think(black, client="a"))),
            threading.Thread(target=lambda: decided.append(lab(app, "b"))),
        ]
        for count, worker in enumerate(workers, start=1):
            worker.daemon = True
            worker.start()
            _wait_for(functools.partial(_waiting_is, app._gate, count))
        time.sleep(1.2)  # both wait in line
    finally:
        app._engine_lock.release()
    for worker in workers:
        worker.join(10)
    assert len(decided) == 2
    left = [s for s, _ in seen]
    assert len(left) == 2
    for seconds in left:  # the whole cap, not the cap less the 1.2 s in line
        assert seconds is not None
        assert cap - 0.5 < seconds <= cap, left


class _Flood:
    """Idle connections from one loopback address (never a byte sent), opened at ``rate`` per
    second and kept open until the server closes them, like the load review's attack C4."""

    def __init__(self, port: int, source: str, rate: float) -> None:
        self._port, self._source, self._gap = port, source, 1.0 / rate
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _Flood:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(10)

    def _run(self) -> None:
        socks: list[socket.socket] = []
        due = time.monotonic()
        try:
            while not self._stop.is_set():
                if time.monotonic() >= due:
                    due += self._gap
                    with contextlib.suppress(OSError):
                        sock = socket.create_connection(
                            ("127.0.0.1", self._port), timeout=2, source_address=(self._source, 0)
                        )
                        sock.setblocking(False)
                        socks.append(sock)
                kept = []
                for sock in socks:
                    try:
                        if sock.recv(4096) == b"":  # closed by the server
                            sock.close()
                            continue
                    except BlockingIOError:
                        pass
                    except OSError:
                        sock.close()
                        continue
                    kept.append(sock)
                socks = kept
                time.sleep(0.002)
        finally:
            for sock in socks:
                sock.close()


def _record_admissions(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, dict[str, int]]]:
    """(connections counted, connections by peer) after every admission of a bounded server."""
    seen: list[tuple[int, dict[str, int]]] = []
    admit = PlayServer._admit_connection

    def spy(self: PlayServer, request: object, peer: str) -> bool:
        admitted = admit(self, request, peer)
        by_peer: dict[str, int] = {}
        for conn in self._conns.values():
            by_peer[conn.peer] = by_peer.get(conn.peer, 0) + 1
        seen.append((self.connections, by_peer))
        return admitted

    monkeypatch.setattr(PlayServer, "_admit_connection", spy)
    return seen


def _other_loopback_addresses(count: int) -> list[str]:
    """``count`` loopback addresses other than 127.0.0.1 a client can send from (skip if not:
    Linux and Windows route all of 127.0.0.0/8 to loopback, macOS only 127.0.0.1)."""
    addresses = [f"127.0.0.{n}" for n in range(2, 2 + count)]
    for address in addresses:
        with socket.socket() as probe:
            try:
                probe.bind((address, 0))
            except OSError:
                pytest.skip(f"cannot send from {address} here")
    return addresses


def _healthz_from(port: int, source: str) -> int:
    """The status of GET /healthz sent from ``source`` (0: no reply in time)."""
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=5, source_address=(source, 0)
        ) as sock:
            sock.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return _reply(sock)[0]
    except OSError:
        return 0


def test_one_address_flooding_idle_connections_cannot_keep_others_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round-3 finding: newcomers could evict only connections older than HEAD_GRACE_S, and
    # evicted ones hold their thread until they notice; one client opening idle connections
    # faster than ~16-30 per second kept the count at its ceiling and turned away 60-70% of
    # other visitors. Now one TCP peer takes at most cap + peer_slack places, and a peer that
    # holds more than its fair share gives way to other peers' newcomers at once.
    (attacker,) = _other_loopback_addresses(1)
    admissions = _record_admissions(monkeypatch)
    cap = 32
    app = make_app(Limits(max_connections=cap))
    with serving(app) as server:
        port = port_of(server)
        assert (server.eviction_slack, server.peer_slack) == (8, 4)
        with _Flood(port, attacker, rate=200):
            _wait_for(lambda: server.connections >= cap)
            time.sleep(1.0)  # the flood has filled every place
            statuses = []
            for _ in range(30):
                statuses.append(_healthz_from(port, "127.0.0.1"))
                time.sleep(0.05)
    # (Before: about half were refused. A refusal or two can still come from a loaded machine
    # whose threads have not yet ended, as each answered visitor's thread counts until then.)
    assert statuses.count(200) >= len(statuses) - 2, statuses
    assert set(statuses) <= {200, 503}
    assert admissions
    for counted, by_peer in admissions:
        assert counted <= cap + server.eviction_slack
        assert by_peer.get(attacker, 0) <= cap + server.peer_slack


def test_a_few_addresses_sharing_a_flood_cannot_keep_others_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verifier probe P3: with the flood split over 2 or 3 addresses, none held half the places,
    # the flood rule never applied, and victims were refused again (59 of 80 served with 2
    # addresses at 100/s each, 38 of 80 with 3 at 70/s). A peer above its fair share gives way.
    attackers = _other_loopback_addresses(3)
    admissions = _record_admissions(monkeypatch)
    cap = 32
    app = make_app(Limits(max_connections=cap))
    with serving(app) as server:
        port = port_of(server)
        with contextlib.ExitStack() as stack:
            for source in attackers:
                stack.enter_context(_Flood(port, source, rate=70))
            _wait_for(lambda: server.connections >= cap)
            time.sleep(1.0)  # the floods have filled every place
            statuses = []
            for _ in range(30):
                statuses.append(_healthz_from(port, "127.0.0.1"))
                time.sleep(0.05)
    assert statuses.count(200) >= len(statuses) - 2, statuses
    assert set(statuses) <= {200, 503}
    for counted, by_peer in admissions:
        assert counted <= cap + server.eviction_slack
        assert max(by_peer.values()) <= cap + server.peer_slack


def test_a_peer_at_its_fair_share_leaves_the_rest_of_the_slack_to_the_others() -> None:
    # Verifier probe P3: flooders replacing their own stale connections filled the whole
    # eviction slack (threads of connections that gave their place and have yet to notice),
    # so other peers were refused even when a connection could give way to them. A peer
    # holding its fair share or more now takes at most peer_slack threads beyond the bound.
    app = make_app(Limits(max_connections=8))
    server = make_server("127.0.0.1", 0, app)
    try:
        assert (server.eviction_slack, server.peer_slack) == (2, 1)
        late = time.monotonic() - PlayServer.HEAD_GRACE_S - 1  # every head late: replaceable
        for peer, count in (("A", 2), ("B", 3), ("C", 3), ("D", 1)):  # 9 counted: cap + 1
            for _ in range(count):
                conn = play_server._Conn(since=late, head_deadline=late + 60, peer=peer)
                server._conns[object()] = conn
        server.connections = 9
        assert not server._admit_connection(object(), "A")  # 2 of 8, 4 peers: its fair share
        assert not server._admit_connection(object(), "B")
        assert server._admit_connection(object(), "D")  # below its share: the rest of the slack
        assert server.connections == 10
        assert not server._admit_connection(object(), "E")  # the whole slack is in use
    finally:
        server._conns.clear()
        server.connections = 0
        server.server_close()
        app.close()


def test_many_flooding_addresses_never_exceed_the_eviction_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The threads beyond max_connections (connections that gave their place and have not yet
    # noticed) stay within eviction_slack, however many peers flood the server.
    admissions = _record_admissions(monkeypatch)
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 0.05)  # every idle one soon replaceable
    cap = 8
    app = make_app(Limits(max_connections=cap))
    with serving(app) as server:
        port = port_of(server)
        floods = [_Flood(port, source, rate=150) for source in _other_loopback_addresses(5)]
        with contextlib.ExitStack() as stack:
            for flood in floods:
                stack.enter_context(flood)
            time.sleep(2.0)
    counted = [n for n, _ in admissions]
    assert max(counted) == cap + server.eviction_slack  # the ceiling was reached, and held
    for _, by_peer in admissions:
        assert max(by_peer.values()) <= cap + server.peer_slack


def test_a_peer_above_its_fair_share_gives_way_to_others_at_once_never_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The grace (HEAD_GRACE_S) protects a connection whose head is on its way, except from
    # other peers' newcomers when its own peer holds more than its fair share: a flood.
    (other,) = _other_loopback_addresses(1)
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)
    app = make_app(Limits(max_connections=2))
    with serving(app) as server:
        server.log_stream = None
        port = port_of(server)
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(2)]
        try:
            _wait_for(lambda: server.connections == 2)
            assert _healthz_from(port, "127.0.0.1") == 503  # its own: all within the grace
            assert not any(_closed_by_server(sock, 0.2) for sock in idle)
            assert _healthz_from(port, other) == 200  # another peer's newcomer: at once
            assert _closed_by_server(idle[0])  # the oldest gave its place
            assert not _closed_by_server(idle[1], 0.2)
        finally:
            for sock in idle:
                sock.close()


def test_a_head_that_has_arrived_is_never_cut_off_for_another_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A proxy sends each head with its connection; on a busy machine the thread may not have
    # read it yet. Such a connection is not idle, and no other peer's newcomer takes its place.
    (other,) = _other_loopback_addresses(1)
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)
    go = threading.Event()
    handle = PlayServer.process_request_thread

    def busy(
        self: PlayServer,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: object,
    ) -> None:
        go.wait(10)  # the machine is too busy to read the heads for now
        handle(self, request, client_address)

    monkeypatch.setattr(PlayServer, "process_request_thread", busy)
    app = make_app(Limits(max_connections=2))
    with serving(app) as server:
        server.log_stream = None
        port = port_of(server)
        socks = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(2)]
        try:
            for sock in socks:
                sock.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            _wait_for(lambda: server.connections == 2)
            time.sleep(PlayServer.FLOOD_GRACE_S + 0.1)
            assert _healthz_from(port, other) == 503  # refused: both heads have arrived
            go.set()
            assert [_reply(sock)[0] for sock in socks] == [200, 200]
        finally:
            go.set()
            for sock in socks:
                sock.close()


def test_the_flood_rule_takes_only_silent_heads_of_a_peer_above_its_fair_share() -> None:
    assert play_server._peer_key(("2001:db8::1", 8000, 0, 0)) == "2001:db8::/64"
    assert play_server._peer_key(("::ffff:192.0.2.1", 8000, 0, 0)) == "192.0.2.1"
    assert play_server._peer_key(("192.0.2.1", 8000)) == "192.0.2.1"
    app = make_app(Limits(max_connections=4))
    server = make_server("127.0.0.1", 0, app)
    pairs = [socket.socketpair() for _ in range(3)]
    try:
        now = time.monotonic()
        young = now - PlayServer.FLOOD_GRACE_S / 2  # (all within HEAD_GRACE_S)
        older = now - PlayServer.FLOOD_GRACE_S * 2
        a_young, a_older, b_older = (pair[0] for pair in pairs)
        conns: dict[object, play_server._Conn] = {
            a_young: play_server._Conn(since=young, head_deadline=now + 10, peer="A"),
            a_older: play_server._Conn(since=older, head_deadline=now + 10, peer="A"),
            b_older: play_server._Conn(since=older, head_deadline=now + 10, peer="B"),
        }
        server._conns.update(conns)
        half = Counter({"A": 2, "B": 1})  # of the 4 places, A holds 2 and B 1
        # 3 peers with the newcomer's: A holds more than its fair share (4/3), 2 more than C.
        assert server._stale_head(now, "C", half) is conns[a_older]
        assert server._stale_head(now, "B", half) is None  # 2 peers: A's half is fair
        assert server._stale_head(now, "B", Counter({"A": 3, "B": 1})) is conns[a_older]
        assert server._stale_head(now, "A", half) is None  # never for the flooder itself
        assert server._stale_head(now, "C", Counter({"A": 1, "B": 1})) is None  # no flooder
        pairs[1][1].sendall(b"GET / HTTP/1.0\r\n")  # a_older's head has arrived, unread
        assert _wait_until(lambda: not play_server._silent(a_older))
        assert server._stale_head(now, "C", half) is None  # (and a_young is too young)
        conns[a_older].since = now - PlayServer.HEAD_GRACE_S  # older than the grace itself
        assert server._stale_head(now, "C", half) is conns[a_older]  # as before this rule
    finally:
        for pair in pairs:
            for sock in pair:
                sock.close()
        server.server_close()
        app.close()


def test_the_flood_rule_takes_from_the_peer_holding_most_and_never_twice() -> None:
    # Verifier probe P3: two or three addresses sharing a flood each held less than half the
    # places, so the rule (then: half the places or more) never applied and victims were
    # refused again (38 of 80 served with 3 addresses). Now any peer above its fair share of
    # the places gives way, the one holding most first; and a connection that has already
    # given its place (its thread has yet to notice) is never chosen again (verifier mutant
    # flood_loop_reuses_evicted).
    app = make_app(Limits(max_connections=8))
    server = make_server("127.0.0.1", 0, app)
    pairs = [socket.socketpair() for _ in range(4)]
    try:
        now = time.monotonic()
        aged = now - PlayServer.FLOOD_GRACE_S * 2  # (within HEAD_GRACE_S)
        a1, a2, b1, closed = (pair[0] for pair in pairs)
        conns: dict[object, play_server._Conn] = {
            a1: play_server._Conn(since=aged - 0.01, head_deadline=now + 10, peer="A"),
            b1: play_server._Conn(since=aged - 0.005, head_deadline=now + 10, peer="B"),
            a2: play_server._Conn(since=aged, head_deadline=now + 10, peer="A"),
        }
        server._conns.update(conns)
        two = Counter({"A": 3, "B": 3, "V": 1})  # neither flooder holds half of 8
        assert server._stale_head(now, "V", two) is conns[a1]
        assert server._stale_head(now, "V", Counter({"A": 3, "B": 4, "V": 1})) is conns[b1]
        assert server._stale_head(now, "V", Counter({"A": 3, "B": 3, "V": 2})) is None
        conns[a1].head_deadline = now  # a1 gave its place: its reader has yet to notice
        later = now + 0.01
        assert server._stale_head(later, "V", two) is conns[b1]  # not a1 again
        conns[b1].head_deadline = later
        assert server._stale_head(later + 0.01, "V", two) is conns[a2]
        # A peer holding just its fair share keeps its places (8 / 4 peers: B's 2 here), even
        # when the peer above it has no connection to give (a2's head has arrived, unread).
        conns[b1].head_deadline = now + 10
        pairs[1][1].sendall(b"GET / HTTP/1.0\r\n")
        assert _wait_until(lambda: not play_server._silent(a2))
        assert server._stale_head(later, "V", Counter({"A": 4, "B": 2, "C": 2})) is None
        assert server._stale_head(later, "V", Counter({"A": 4, "B": 3, "C": 1})) is conns[b1]
        # A socket that cannot be asked (closed meanwhile) is never taken for silent.
        server._conns.clear()
        pairs[3][0].close()
        assert play_server._silent(closed) is False
        assert play_server._silent(object()) is False
        server._conns[closed] = play_server._Conn(since=aged, head_deadline=now + 10, peer="A")
        assert server._stale_head(now, "V", two) is None
    finally:
        for pair in pairs:
            for sock in pair:
                sock.close()
        server.server_close()
        app.close()


def _wait_until(predicate: Callable[[], bool], wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


# ---------------------------------------------------------------- connection deadlines and caps
class _Holders:
    """``count`` connections from one loopback address that never finish their request head:
    idle (never a byte, the load review's attack C4) or, with ``trickle``, one byte of a
    request line that never ends every ``trickle`` seconds (attack C5). With ``reopen`` each
    one the server closes is opened again at once, as C4 did."""

    def __init__(
        self, port: int, count: int, *, trickle: float | None = None, reopen: bool = False
    ) -> None:
        self._port, self._count, self._trickle, self._reopen = port, count, trickle, reopen
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._lock = threading.Lock()
        self._closed = 0

    @property
    def closed(self) -> int:
        """Connections the server has closed so far."""
        with self._lock:
            return self._closed

    def __enter__(self) -> _Holders:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(10)

    def _open(self) -> socket.socket | None:
        try:
            sock = socket.create_connection(("127.0.0.1", self._port), timeout=2)
            if self._trickle is not None:
                sock.sendall(b"GET /")  # a request line that never ends
        except OSError:
            return None
        sock.setblocking(False)
        return sock

    def _run(self) -> None:
        socks = [sock for sock in (self._open() for _ in range(self._count)) if sock]
        next_byte = time.monotonic()
        try:
            while not self._stop.is_set():
                kept = []
                for sock in socks:
                    try:
                        if sock.recv(4096) == b"":  # closed by the server
                            raise ConnectionResetError
                    except BlockingIOError:
                        pass
                    except OSError:
                        sock.close()
                        with self._lock:
                            self._closed += 1
                        continue
                    kept.append(sock)
                socks = kept
                while self._reopen and len(socks) < self._count and not self._stop.is_set():
                    fresh = self._open()
                    if fresh is None:
                        break
                    socks.append(fresh)
                if self._trickle is not None and time.monotonic() >= next_byte:
                    next_byte += self._trickle
                    for sock in socks:
                        with contextlib.suppress(OSError):
                            sock.send(b"a")
                time.sleep(0.005)
        finally:
            for sock in socks:
                sock.close()


@pytest.mark.parametrize("trickle", [None, 0.1], ids=["idle-C4", "trickled-C5"])
def test_heads_that_never_arrive_free_a_full_server_at_their_deadline(
    trickle: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Load-review findings C4 and C5: 64 connections that never finished their request head,
    # idle or sending a byte now and then, held every thread (the 30 s timeout was per
    # receive), and a second client was refused every time. Now a head must arrive within
    # HEAD_S in all. Here nothing else can free a place: no connection gives its place to a
    # newcomer (grace of 60 s; one TCP peer, so no flood rule), only the head deadline.
    monkeypatch.setattr(PlayHandler, "HEAD_S", 1.0)
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 60.0)
    cap = 4
    app = make_app(Limits(max_connections=cap))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        with _Holders(port, cap, trickle=trickle) as holders:
            _wait_for(lambda: server.connections == cap)
            started = time.monotonic()
            assert _healthz_from(port, "127.0.0.1") == 503  # every place held: refused
            statuses = [503]
            while statuses[-1] != 200 and time.monotonic() - started < 5:
                time.sleep(0.1)
                statuses.append(_healthz_from(port, "127.0.0.1"))
            served_after = time.monotonic() - started
            assert statuses[-1] == 200, statuses  # the second client is served
            assert _wait_until(lambda: holders.closed == cap)  # closed without an answer
    assert served_after < 1.0 + _GuardedReader.POLL_S + 1.0, served_after


@pytest.mark.parametrize("trickle", [None, 0.1], ids=["idle-C4", "trickled-C5"])
def test_heads_reopened_as_fast_as_they_are_closed_cannot_keep_a_second_client_out(
    trickle: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C4 as measured: every connection the server closes is opened again at once, so the head
    # deadline alone would leave a newcomer to race the attacker for each freed place (served
    # 1 time in 139). A connection waiting longer than HEAD_GRACE_S for its head gives its
    # place to the newcomer; the head deadline (10 s) plays no part within this test.
    monkeypatch.setattr(PlayServer, "HEAD_GRACE_S", 0.2)
    cap = 4
    app = make_app(Limits(max_connections=cap))
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        with _Holders(port, cap, trickle=trickle, reopen=True) as holders:
            _wait_for(lambda: server.connections >= cap)
            statuses = []
            for _ in range(10):
                time.sleep(0.25)  # the attacker's connections are older than the grace again
                statuses.append(_healthz_from(port, "127.0.0.1"))
            assert holders.closed >= 5  # each visitor took an attacker's place
    assert statuses.count(200) >= len(statuses) - 1, statuses


@pytest.mark.parametrize("version", ["HTTP/1.1", "HTTP/1.0"])
def test_one_request_per_connection_even_when_keep_alive_is_asked_for(version: str) -> None:
    # Load review C7, pinned: the server speaks HTTP/1.0, so a connection is closed once its
    # answer is sent, even when the client (a reverse proxy's pool, say) asks for keep-alive.
    # No connection idles between requests, so there is no keep-alive time to bound.
    app = make_app(Limits(max_connections=4))
    request = f"GET /healthz {version}\r\nHost: {HF}\r\nConnection: keep-alive\r\n\r\n"
    with _bounded_server(app) as server:
        server.log_stream = None
        port = port_of(server)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(request.encode("ascii"))  # then nothing more: an idle keep-alive
            sock.settimeout(1.0)
            started = time.monotonic()
            data = b""
            while chunk := sock.recv(65536):  # (TimeoutError: the server kept it open)
                data += chunk
            closed_after = time.monotonic() - started
        _wait_for(lambda: server.connections == 0)  # its thread has ended
    head = data.partition(b"\r\n\r\n")[0].decode("latin-1").lower()
    assert head.startswith("http/1.0 200 "), head
    assert "keep-alive" not in head
    assert closed_after < 1.0, closed_after


def _refusal(sock: socket.socket) -> tuple[int, object]:
    """Status and ``limit`` of the whole answer on ``sock`` (read until the server closes)."""
    sock.settimeout(5)
    data = b""
    with contextlib.suppress(ConnectionResetError):
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)["limit"]


@dataclasses.dataclass
class _Held:
    entered: threading.Semaphore  # released once per request held
    release: threading.Event  # set: the held requests are answered


@contextlib.contextmanager
def _slow_answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Held]:
    """``GET /static/slow.txt`` holds its thread past the head until ``release`` is set, as an
    answer read slowly would."""
    held = _Held(threading.Semaphore(0), threading.Event())
    original = play_server.static_file

    def static_file(path: str) -> tuple[bytes, str] | None:
        if path != "slow.txt":
            return original(path)
        held.entered.release()
        held.release.wait(10)
        return b"slow", "text/plain; charset=utf-8"

    monkeypatch.setattr(play_server, "static_file", static_file)
    try:
        yield held
    finally:
        held.release.set()


def _slow_get(port: int, source: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10, source_address=(source, 0))
    sock.sendall(b"GET /static/slow.txt HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    return sock


@pytest.mark.parametrize("per_peer", [2, None], ids=["capped", "no-cap"])
def test_one_address_holding_threads_past_its_heads_is_capped_per_peer(
    per_peer: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A connection gives its place to a newcomer only while its head is awaited. Past the
    # head (an answer read slowly, a body) one address could hold every thread of a server
    # that clients reach directly (the no-cap case below). With max_connections_per_peer it
    # holds at most that many, the next ones are refused at once, and others are served.
    (attacker,) = _other_loopback_addresses(1)
    cap = 4
    app = make_app(Limits(max_connections=cap, max_connections_per_peer=per_peer))
    options = ServerOptions(public=True, allowed_hosts=(HF,))  # trusted_proxies 0
    with serving(app, options, public_bind=True) as server, _slow_answers(monkeypatch) as slow:
        server.log_stream = None
        port = port_of(server)
        assert server.peer_cap == per_peer
        held = min(cap, per_peer or cap)
        socks = [_slow_get(port, attacker) for _ in range(held)]
        try:
            for _ in range(held):
                assert slow.entered.acquire(timeout=5)  # its thread is past the head, held
            more = _slow_get(port, attacker)
            socks.append(more)
            if per_peer is None:  # every place held past its head: nothing to give way
                assert _refusal(more) == (503, "max_connections")
                assert _healthz_from(port, "127.0.0.1") == 503
            else:  # refused at once, though places are free
                assert _refusal(more) == (503, "max_connections_per_peer")
                assert server.connections == per_peer
                assert _healthz_from(port, "127.0.0.1") == 200  # another address is served
        finally:
            slow.release.set()
            for sock in socks:
                sock.close()


def test_the_per_peer_cap_counts_every_connection_and_never_applies_behind_a_proxy() -> None:
    app = make_app(Limits(max_connections=8, max_connections_per_peer=2))
    server = make_server("127.0.0.1", 0, app)
    try:
        assert server.peer_cap == 2
        now = time.monotonic()
        server._conns[object()] = play_server._Conn(since=now, head_deadline=None, peer="A")
        assert not server._peer_full("A")
        # A connection that gave its place still runs its thread until it notices: counted.
        server._conns[object()] = play_server._Conn(since=now, head_deadline=now, peer="A")
        assert server._peer_full("A")
        assert not server._peer_full("B")
        assert b'"limit":"max_connections_per_peer"' in server._peer_reply.replace(b" ", b"")
        assert b"at most 2 at once" in server._peer_reply
    finally:
        server._conns.clear()
        server.server_close()
    loose = make_app(Limits(max_connections_per_peer=1))
    proxied = make_server("127.0.0.1", 0, app, ServerOptions(trusted_proxies=1))
    unbounded = make_server("127.0.0.1", 0, loose)
    try:
        assert proxied.peer_cap is None  # every connection has the proxy's address
        assert unbounded.peer_cap is None  # nothing counts connections without the bound
        assert not proxied._peer_full("")
    finally:
        proxied.server_close()
        unbounded.server_close()
        loose.close()
        app.close()


def test_the_per_peer_cap_is_not_applied_to_a_trusted_proxy_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Behind a proxy every connection is the proxy's: a per-peer cap would cap the whole site.
    cap = 3
    app = make_app(Limits(max_connections=cap, max_connections_per_peer=1))
    with _bounded_server(app) as server, _slow_answers(monkeypatch) as slow:
        server.log_stream = None
        port = port_of(server)
        assert server.peer_cap is None
        socks = [_slow_get(port, "127.0.0.1") for _ in range(cap)]
        try:
            for _ in range(cap):
                assert slow.entered.acquire(timeout=5)  # all three held: none refused
            assert server.connections == cap
        finally:
            slow.release.set()
            for sock in socks:
                sock.close()


def test_max_connections_per_peer_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _captured_run(monkeypatch)
    base = ["play", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    public = [*base, "--public", "--host", WILDCARD, "--allowed-host", HF]
    assert main([*public, "--max-connections-per-peer", "16"]) == 0
    limits = seen[-1][0].settings.limits
    assert limits == dataclasses.replace(PUBLIC_LIMITS, max_connections_per_peer=16)
    assert main([*base, "--max-connections", "8", "--max-connections-per-peer", "2"]) == 0
    assert seen[-1][0].settings.limits == Limits(max_connections=8, max_connections_per_peer=2)
    assert len(seen) == 2
    capsys.readouterr()
    # Behind a proxy every connection has the proxy's address: the whole site would be capped.
    refused = [*public, "--trusted-proxies", "1", "--max-connections-per-peer", "16"]
    assert main(refused) == 1
    assert capsys.readouterr().err.startswith(
        "error: --max-connections-per-peer needs --trusted-proxies 0"
    )
    # Without a bound on connections nothing counts them.
    assert main([*base, "--max-connections-per-peer", "2"]) == 1
    assert capsys.readouterr().err.startswith(
        "error: --max-connections-per-peer needs --max-connections (or --public"
    )
    assert len(seen) == 2  # neither was started
