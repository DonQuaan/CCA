"""UCI server robustness (review findings UCI-1..7, SEC-1/8, TQ-06), all hermetic."""

from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path

import chess

from cca.agent import AgentConfig, CAIMEAgent
from cca.uci.protocol import UciServer, parse_go, parse_position, parse_uci_opponent
from tests.conftest import FakeEngine, FakeHuman


def _bestmoves(out: io.StringIO) -> list[str]:
    return [ln.split()[1] for ln in out.getvalue().splitlines() if ln.startswith("bestmove")]


def _wait_bestmoves(out: io.StringIO, n: int, timeout: float = 20.0) -> list[str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(_bestmoves(out)) < n:
        time.sleep(0.02)
    return _bestmoves(out)


def test_handshake_and_warm_up(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server.handle("uci")
    text = out.getvalue()
    for token in ("id name CCA", "uciok", "option name CCA_Seed type string default <secret>"):
        assert token in text
    assert "commitment" not in text  # committed per game, at its first move
    server.handle("isready")
    assert "readyok" in out.getvalue()
    assert server._agent is not None  # engines are built before the clock runs


def test_option_validation_keeps_last_good_value(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server.handle("isready")
    server.handle("setoption name CCA_Persona value Tal")  # case-insensitive match
    assert server._opts["CCA_Persona"] == "tal"
    server.handle("setoption name CCA_Persona value Nope")
    assert server._opts["CCA_Persona"] == "tal"
    assert "must be one of" in out.getvalue()
    server.handle("setoption name UCI_Elo value abc")
    assert "needs an integer" in out.getvalue()
    server.handle("setoption name UCI_Elo value 1700")  # later options still apply
    assert server._agent is not None
    assert server._agent.config.elo_self == 1700
    server.handle("setoption name UCI_Opponent value IM 2350 human Somebody")
    assert server._agent.config.elo_oppo == 2350
    server.handle("setoption name UCI_Elo value 99999")
    assert server._opts["UCI_Elo"] == "2600"  # clamped
    server.handle("setoption name Foo value 1")
    assert "ignoring unknown option Foo" in out.getvalue()


def test_echoed_secret_placeholder_keeps_the_seed_secret(
    uci: tuple[UciServer, io.StringIO],
) -> None:
    server, _ = uci
    server.handle("setoption name CCA_Seed value <secret>")
    assert server._opts["CCA_Seed"] == ""
    assert server._config().seed == server._secret_seed != "<secret>"


def test_every_go_answers_bestmove_even_if_the_engine_cannot_start(tmp_path: Path) -> None:
    out = io.StringIO()
    server = UciServer(stdin=io.StringIO(""), stdout=out)
    server.handle(f"setoption name StockfishPath value {tmp_path / 'missing.exe'}")
    server.handle("position startpos moves e2e4")
    server.handle("go wtime 1000 btime 1000")
    server._join()
    moves = _bestmoves(out)
    assert len(moves) == 1
    assert (
        chess.Move.from_uci(moves[0]) in parse_position(["startpos", "moves", "e2e4"]).legal_moves
    )
    assert "info string error" in out.getvalue()
    server._shutdown()


def test_no_engine_leak_when_the_human_model_fails(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci

    def broken(engine: object) -> object:
        raise RuntimeError("model load failed")

    server._human_model = broken  # type: ignore[method-assign,assignment]
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")
    server._join()
    assert len(_bestmoves(out)) == 1
    assert server._engine is None  # the half-built engine was closed, not kept


def test_dead_engine_is_restarted(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server.handle("isready")
    assert server._engine is not None
    server._engine.close()  # simulate a crash / OOM kill
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")
    server._join()
    assert len(_bestmoves(out)) == 1
    assert server._engine_dead.is_set()
    server.handle("go wtime 60000 btime 60000")
    server._join()
    assert "engine process died; restarted" in out.getvalue()
    assert len(_bestmoves(out)) == 2
    assert out.getvalue().count("info string cca q_opt") == 1  # the second go really searched


def test_go_infinite_waits_for_stop(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    server.handle("position startpos")
    server.handle("go infinite")
    time.sleep(0.3)
    assert _bestmoves(out) == []
    assert server._worker is not None
    assert server._worker.is_alive()
    server.handle("stop")
    assert len(_bestmoves(out)) == 1


def test_ponder_semantics(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    server.handle("position startpos")
    server.handle("go ponder wtime 60000 btime 60000")
    server.handle("ponderhit")
    assert len(_wait_bestmoves(out, 1)) == 1
    assert "info string cca q_opt" in out.getvalue()
    history = list(server._agent._history)
    server.handle("position startpos moves e2e4")
    server.handle("go ponder")
    server.handle("stop")  # opponent played something else: fallback answer, agent untouched
    assert len(_bestmoves(out)) == 2
    assert server._agent._history == history


def test_seed_commit_and_reveal(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), server._config())
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")
    server._join()
    commit = re.search(r"game 0 seed commitment sha256:([0-9a-f]{64})", out.getvalue())
    assert commit is not None
    server.handle("ucinewgame")
    reveal = re.search(r"game 0 seed reveal ([0-9a-f]+)", out.getvalue())
    assert reveal is not None
    assert hashlib.sha256(reveal.group(1).encode()).hexdigest() == commit.group(1)
    assert server._secret_seed != reveal.group(1)  # a fresh secret for the next game
    assert server._agent.config.seed == server._secret_seed
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")
    server._join()
    assert "game 1 seed commitment" in out.getvalue()


def test_time_trouble_answers_in_reflex_mode(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    server.handle("position startpos moves e2e4")
    start = time.monotonic()
    server.handle("go wtime 300 btime 300")
    server._join()
    assert time.monotonic() - start < 5.0
    assert len(_bestmoves(out)) == 1
    assert "reflex" in out.getvalue()


def test_parsers() -> None:
    clock, movetime, infinite = parse_go(
        ["wtime", "60000", "btime", "30000", "winc", "1000", "movestogo", "20"], chess.BLACK
    )
    assert (clock.my_time, clock.opp_time, clock.opp_inc, clock.moves_to_go) == (
        30.0,
        60.0,
        1.0,
        20,
    )
    assert movetime is None
    assert not infinite
    assert parse_uci_opponent("GM 2800 human Gary Kasparov") == 2800
    assert parse_uci_opponent("none") is None
