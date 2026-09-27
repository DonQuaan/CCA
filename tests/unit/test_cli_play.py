"""``cca play`` command line: the contract other tools rely on, start-up and shutdown.

The end-to-end tests start the real command (fake UCI engine subprocess, real HTTP server on an
ephemeral port) and stop it from inside ``serve_forever`` once they have probed it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from cca import __version__, cli
from cca.cli import _play_human, build_parser, main
from cca.engines import maia2_human
from cca.engines.qre_human import QREHumanModel
from cca.play.server import PlayServer
from tests.conftest import FakeEngine, make_launcher

COMMON = {
    "stockfish": None,
    "threads": 1,
    "hash": 256,
    "nodes": 200_000,
    "human": "maia2",
    "maia2_type": "rapid",
    "device": "gpu",
    "persona": None,
    "config": None,
    "elo_self": 1900,
    "elo_oppo": 1500,
    "seed": "cca",
    "argmax": False,
}


def test_play_defaults_match_the_contract() -> None:
    args = build_parser().parse_args(["play"])
    assert args.command == "play"
    assert (args.host, args.port, args.no_browser, args.max_sessions) == (
        "127.0.0.1",
        8765,
        False,
        16,
    )
    assert {k: getattr(args, k) for k in COMMON} == COMMON


def test_play_accepts_every_common_engine_flag(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    args = build_parser().parse_args(
        [
            "play",
            "--host",
            "::1",
            "--port",
            "0",
            "--no-browser",
            "--max-sessions",
            "3",
            "--stockfish",
            "sf.exe",
            "--threads",
            "2",
            "--hash",
            "64",
            "--nodes",
            "5000",
            "--human",
            "qre",
            "--maia2-type",
            "blitz",
            "--device",
            "cpu",
            "--persona",
            "tal",
            "--config",
            str(config),
            "--elo-self",
            "2000",
            "--elo-oppo",
            "1200",
            "--seed",
            "s",
            "--argmax",
        ]
    )
    assert (args.host, args.port, args.no_browser, args.max_sessions) == ("::1", 0, True, 3)
    assert (args.threads, args.hash, args.nodes, args.human) == (2, 64, 5000, "qre")
    assert (args.maia2_type, args.device, args.persona, args.config) == (
        "blitz",
        "cpu",
        "tal",
        config,
    )
    assert (args.elo_self, args.elo_oppo, args.seed, args.argmax) == (2000, 1200, "s", True)


@pytest.mark.parametrize(
    "argv",
    [
        ["play", "--port", "-1"],
        ["play", "--port", "65536"],
        ["play", "--port", "http"],
        ["play", "--max-sessions", "0"],
        ["play", "--max-sessions", "1025"],
    ],
)
def test_play_rejects_out_of_range_numbers(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


# ---------------------------------------------------------------- human model choice
def _args(human: str = "maia2") -> argparse.Namespace:
    return argparse.Namespace(human=human, maia2_type="rapid", device="cpu")


def test_play_human_falls_back_to_qre_on_any_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(**kwargs: object) -> object:
        raise RuntimeError("CUDA error: no kernel image")

    monkeypatch.setattr(maia2_human, "Maia2HumanModel", broken)
    human, kind, reason = _play_human(_args(), FakeEngine())
    assert isinstance(human, QREHumanModel)
    assert kind == "qre"
    assert reason == "RuntimeError: CUDA error: no kernel image"
    assert "Maia-2 unavailable" in capsys.readouterr().err


def test_play_human_uses_maia2_when_it_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = QREHumanModel(FakeEngine())
    calls: list[dict[str, object]] = []

    def fake(**kwargs: object) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(maia2_human, "Maia2HumanModel", fake)
    assert _play_human(_args(), FakeEngine()) == (sentinel, "maia2", None)
    assert calls == [{"model_type": "rapid", "device": "cpu"}]


def test_play_human_skips_maia2_when_qre_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: object) -> object:
        raise AssertionError("Maia-2 must not be loaded")

    monkeypatch.setattr(maia2_human, "Maia2HumanModel", forbidden)
    human, kind, reason = _play_human(_args("qre"), FakeEngine())
    assert isinstance(human, QREHumanModel)
    assert (kind, reason) == ("qre", None)


# ---------------------------------------------------------------- end to end
def _get(port: int, path: str) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert isinstance(data, dict)
        return resp.status, data
    finally:
        conn.close()


def _post(port: int, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert isinstance(data, dict)
        return resp.status, data
    finally:
        conn.close()


def _probe_then_stop(
    monkeypatch: pytest.MonkeyPatch, probe: Callable[[int], None]
) -> list[BaseException]:
    """Run ``probe(port)`` against the live server, then stop it; errors are collected."""
    original = PlayServer.serve_forever
    errors: list[BaseException] = []

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


def _wait_ready(port: int) -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, health = _get(port, "/healthz")
        assert status in {200, 503}
        if health.get("ready") or health.get("status") == "error":
            return health
        time.sleep(0.05)
    raise AssertionError("engines never became ready")


def test_cca_play_serves_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    seen: dict[str, object] = {}

    def probe(port: int) -> None:
        status, health = _get(port, "/healthz")  # answers before the engines are ready
        assert status == 200
        assert health["status"] == "ok"
        assert health["version"] == __version__
        assert isinstance(health["ready"], bool)
        assert _wait_ready(port)["ready"] is True
        seen["info"] = _get(port, "/api/info")[1]
        status, created = _post(port, "/api/games", {"human_color": "black"})
        assert status == 201
        status, played = _post(port, f"/api/games/{created['game_id']}/think", {})
        assert status == 200
        seen["move"] = played["move"]

    errors = _probe_then_stop(monkeypatch, probe)
    code = main(
        [
            "play",
            "--port",
            "0",
            "--no-browser",
            "--stockfish",
            str(make_launcher(tmp_path)),
            "--human",
            "qre",
            "--nodes",
            "500",
            "--max-sessions",
            "4",
        ]
    )
    assert errors == []
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("CCA simulator: http://127.0.0.1:")
    assert out.rstrip().endswith("/")
    assert opened == []  # --no-browser
    info = seen["info"]
    assert isinstance(info, dict)
    engine = info["engine"]
    assert isinstance(engine, dict)
    assert str(engine["name"]).startswith("FakeFish")
    assert engine["nodes"] == 500
    assert info["max_sessions"] == 4
    assert info["human_model"] == {"requested": "qre", "kind": "qre", "fallback_reason": None}
    assert isinstance(seen["move"], dict)


def test_cca_play_opens_the_browser_and_reports_a_maia2_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    def broken(**kwargs: object) -> object:
        raise OSError("checkpoint download failed")

    monkeypatch.setattr(maia2_human, "Maia2HumanModel", broken)
    seen: dict[str, object] = {}

    def probe(port: int) -> None:
        _wait_ready(port)
        seen["info"] = _get(port, "/api/info")[1]

    errors = _probe_then_stop(monkeypatch, probe)
    code = main(["play", "--port", "0", "--stockfish", str(make_launcher(tmp_path))])
    assert errors == []
    assert code == 0
    url = capsys.readouterr().out.strip().removeprefix("CCA simulator: ")
    assert opened == [url]
    info = seen["info"]
    assert isinstance(info, dict)
    assert info["human_model"] == {
        "requested": "maia2",
        "kind": "qre",
        "fallback_reason": "OSError: checkpoint download failed",
    }


def test_cca_play_reports_a_failed_start_and_closes_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Engine(FakeEngine):
        closed = 0

        def close(self) -> None:
            Engine.closed += 1

    monkeypatch.setattr(cli, "_engine", lambda args, nodes=None: Engine())

    def fail(args: argparse.Namespace, engine: object) -> object:
        raise ValueError("bad human model")

    monkeypatch.setattr(cli, "_play_human", fail)
    seen: dict[str, object] = {}

    def probe(port: int) -> None:
        health = _wait_ready(port)
        seen["health"] = health
        seen["status"] = _get(port, "/healthz")[0]
        seen["info_error"] = _get(port, "/api/info")[1]["error"]

    errors = _probe_then_stop(monkeypatch, probe)
    code = main(
        ["play", "--port", "0", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    )
    assert errors == []
    assert code == 0
    assert seen["status"] == 503
    assert seen["info_error"] == "ValueError: bad human model"
    assert Engine.closed == 1  # never leak a Stockfish process


@pytest.fixture
def never_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fail-fast test fails at once, rather than serving forever, if the command gets as far
    as starting the server."""

    def started(*args: object, **kwargs: object) -> int:
        raise AssertionError("cca play started serving")

    monkeypatch.setattr("cca.play.server.run", started)


@pytest.mark.usefixtures("never_serve")
def test_cca_play_fails_fast_without_stockfish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.exe"
    assert main(["play", "--port", "0", "--no-browser", "--stockfish", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


@pytest.mark.usefixtures("never_serve")
@pytest.mark.parametrize("kind", ["missing", "directory", "toml-syntax", "not-utf8"])
def test_cca_play_fails_fast_on_a_bad_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    # Regression: a missing --config file ended in a FileNotFoundError traceback.
    config = tmp_path / "agent.toml"
    if kind == "directory":
        config.mkdir()
    elif kind == "toml-syntax":
        config.write_text("seed = [", encoding="utf-8")
    elif kind == "not-utf8":
        config.write_bytes(b'seed = "\xff"')
    argv = ["play", "--port", "0", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    assert main([*argv, "--config", str(config)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


@pytest.mark.usefixtures("never_serve")
@pytest.mark.parametrize("flags", [["--elo-self", "3000"], ["--elo-oppo", "-50"]])
def test_cca_play_fails_fast_on_an_elo_outside_the_offered_ranges(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    argv = ["play", "--port", "0", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    assert main([*argv, *flags]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # no URL printed: nothing was started
    assert captured.err.startswith(f"error: {flags[0]} {flags[1]} is outside the range")


def test_cca_play_reports_a_busy_port_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A second `cca play` on the default port is the usual way to hit this.
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = int(busy.getsockname()[1])
        argv = ["play", "--port", str(port), "--no-browser"]
        assert main([*argv, "--stockfish", str(make_launcher(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"error: cannot listen on 127.0.0.1:{port}: ")
    assert "Traceback" not in captured.err


@pytest.mark.usefixtures("never_serve")
def test_cca_play_fails_fast_on_an_unknown_persona(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["play", "--no-browser", "--stockfish", str(make_launcher(tmp_path))]
    assert main([*argv, "--persona", "nobody"]) == 1
    assert "unknown persona" in capsys.readouterr().err
