"""GUI / lichess-bot compatibility of the UCI server: the ``cca-uci`` launcher, the standard
options GUIs expect (Ponder, UCI_LimitStrength, Move Overhead), ``go`` limits CCA does not
apply, unknown options and the option help table. All hermetic (fake Stockfish)."""

from __future__ import annotations

import importlib
import io
import subprocess
import sys
import threading
import time
import tomllib
from importlib import metadata
from pathlib import Path

import chess
import chess.engine
import pytest

from cca.agent import AgentConfig, CAIMEAgent
from cca.core.types import Clock, Decision
from cca.uci import protocol
from cca.uci.protocol import (
    UCI_OPTION_HELP,
    UciServer,
    apply_move_overhead,
    console_main,
    unapplied_go_limits,
)
from tests.conftest import FakeEngine, FakeHuman, make_launcher

ROOT = Path(__file__).resolve().parents[2]
NOT_APPLIED = "info string cca does not apply go"


def _lines(out: io.StringIO) -> list[str]:
    return out.getvalue().splitlines()


def _advertised(lines: list[str]) -> list[str]:
    """Option names from a ``uci`` answer (names may contain spaces: ``Move Overhead``)."""
    return [
        ln.removeprefix("option name ").split(" type ")[0]
        for ln in lines
        if ln.startswith("option name ")
    ]


def _bestmoves(out: io.StringIO) -> list[str]:
    return [ln.split()[1] for ln in _lines(out) if ln.startswith("bestmove")]


def _server_with_agent() -> tuple[UciServer, io.StringIO, CAIMEAgent]:
    out = io.StringIO()
    server = UciServer(stdin=io.StringIO(""), stdout=out)
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), server._config())
    server._agent = agent
    return server, out, agent


def _launcher() -> Path:
    """The installed ``cca-uci`` launcher next to this interpreter (``uv sync`` creates it)."""
    path = Path(sys.executable).parent / ("cca-uci.exe" if sys.platform == "win32" else "cca-uci")
    if not path.is_file():
        if metadata.entry_points(group="console_scripts", name="cca-uci"):
            pytest.fail(f"the installed metadata declares cca-uci but {path} is missing")
        pytest.skip("cca-uci is not installed next to this interpreter (run `uv sync`)")
    return path


# ---------------------------------------------------------------- 1. cca-uci launcher
def test_pyproject_declares_the_cca_uci_launcher() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = cfg["project"]["scripts"]["cca-uci"]
    assert target == "cca.uci.protocol:console_main"
    module, attr = target.split(":")
    assert getattr(importlib.import_module(module), attr) is console_main


def test_console_main_is_cca_uci(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_cli_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 7

    monkeypatch.setattr(protocol, "_cli_main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["cca-uci", "--Threads=4"])  # lichess-bot engine_options
    assert console_main() == 7  # the launcher exits with cca.cli.main's code
    assert seen == [["uci"]]  # always exactly `cca uci`, whatever the command line holds


def test_console_main_serves_uci_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("uci\nquit\n"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["cca-uci", "--unknown=1"])
    assert console_main() == 0
    lines = _lines(out)
    assert lines[0].startswith("id name CCA ")
    assert lines[-1] == "uciok"


def test_real_cca_uci_launcher_handshake(tmp_path: Path) -> None:
    """The generated ``cca-uci`` executable itself, over real pipes, with no argument."""
    commands = (
        f"setoption name StockfishPath value {make_launcher(tmp_path)}\n"
        "setoption name CCA_HumanModel value qre\n"
        "uci\nisready\nquit\n"
    )
    proc = subprocess.run(
        [str(_launcher())],
        input=commands,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
        cwd=tmp_path,
    )
    lines = proc.stdout.splitlines()
    assert proc.returncode == 0, proc.stderr
    assert lines[0].startswith("id name CCA ")
    assert "uciok" in lines
    assert "readyok" in lines
    assert lines.index("uciok") < lines.index("readyok")
    assert "option name Ponder type check default false" in lines
    assert "error" not in proc.stdout  # the fake Stockfish really started


def test_python_chess_drives_cca_uci_like_lichess_bot(tmp_path: Path) -> None:
    """python-chess (lichess-bot's engine layer) against the real launcher."""
    fake = make_launcher(tmp_path)
    with chess.engine.SimpleEngine.popen_uci([str(_launcher())], cwd=tmp_path) as engine:
        assert {"Ponder", "UCI_LimitStrength", "Move Overhead"} <= set(engine.options)
        engine.configure(
            {
                "StockfishPath": str(fake),
                "CCA_HumanModel": "qre",
                "CCA_Nodes": 1000,
                "Move Overhead": 100,
                "Threads": 1,
                "UCI_Elo": 1700,
                "UCI_LimitStrength": True,
            }
        )
        # python-chess refuses to configure managed options (Ponder) and options the engine
        # does not advertise (SyzygyPath) itself, before anything reaches CCA.
        with pytest.raises(chess.engine.EngineError, match="automatically managed"):
            engine.configure({"Ponder": False})
        with pytest.raises(chess.engine.EngineError, match="does not support option"):
            engine.configure({"SyzygyPath": "/tb"})
        board = chess.Board()
        result = engine.play(board, chess.engine.Limit(time=1.0), ponder=True)
        assert result.move is not None
        assert result.move in board.legal_moves


# ---------------------------------------------------------------- 2./3./4. advertised options
def test_standard_gui_options_are_advertised(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server.handle("uci")
    lines = _lines(out)
    assert "option name Ponder type check default false" in lines
    assert "option name UCI_LimitStrength type check default true" in lines
    assert "option name Move Overhead type spin default 10 min 0 max 5000" in lines
    assert lines[-1] == "uciok"


def test_ponder_option_is_accepted_and_validated(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server.handle("setoption name Ponder value true")
    assert server._opts["Ponder"] == "true"
    server.handle("setoption name Ponder value maybe")
    assert server._opts["Ponder"] == "true"
    assert "must be true or false" in out.getvalue()
    assert "unknown option" not in out.getvalue()


def test_limit_strength_false_ignores_uci_elo() -> None:
    server, _, agent = _server_with_agent()
    assert server._config().elo_self == AgentConfig().elo_self == 1900  # default unchanged
    server.handle("setoption name UCI_Elo value 1700")
    assert agent.config.elo_self == 1700
    server.handle("setoption name UCI_LimitStrength value false")
    assert agent.config.elo_self == 2600  # the top of the advertised UCI_Elo range
    server.handle("setoption name UCI_Elo value 1200")  # ignored while strength is unlimited
    assert agent.config.elo_self == 2600
    server.handle("setoption name UCI_LimitStrength value True")
    assert agent.config.elo_self == 1200
    assert "2600, the top of the UCI_Elo range" in UCI_OPTION_HELP["UCI_LimitStrength"]
    server._shutdown()


# ---------------------------------------------------------------- 4. Move Overhead
def test_apply_move_overhead_never_goes_below_the_floor() -> None:
    floor = protocol._MIN_BUDGET
    assert apply_move_overhead(10.0, 0.0, 0.0) == 10.0
    assert apply_move_overhead(10.0, 0.0, 0.5) == 9.5
    assert apply_move_overhead(1.0, 0.0, 5.0) == floor  # cut to the floor, not below
    assert apply_move_overhead(0.004, 0.0, 1.0) == 0.004  # already inside the floor: kept
    assert apply_move_overhead(-1.0, 0.0, 1.0) == -1.0  # never extended
    # The floor is small: a large overhead is honoured almost fully.
    assert apply_move_overhead(0.95, 0.0, 0.6) == pytest.approx(0.35)
    assert 0.0 < floor <= 0.05


@pytest.mark.parametrize("name", ["Move Overhead", "Threads", "Ponder", "UCI_LimitStrength"])
@pytest.mark.parametrize("placeholder", ["<empty>", "<auto>", "<secret>"])
def test_placeholders_are_rejected_for_non_string_options(name: str, placeholder: str) -> None:
    out = io.StringIO()
    server = UciServer(stdin=io.StringIO(""), stdout=out)
    before = dict(server._opts)
    server.handle(f"setoption name {name} value {placeholder}")
    assert server._opts == before  # the old, valid value is kept
    assert "info string error" in out.getvalue()


def _spied_go(monkeypatch: pytest.MonkeyPatch, overhead_ms: int, go: str) -> dict[str, float]:
    """Run one ``go`` and return the deadlines the server derived and handed on."""
    server, out, agent = _server_with_agent()
    seen: dict[str, float] = {}
    real_deadline_for, real_choose = agent.deadline_for, agent.choose

    def deadline_for(
        board: chess.Board, clock: Clock, movetime: float | None = None
    ) -> float | None:
        derived = real_deadline_for(board, clock, movetime)
        assert derived is not None
        seen["derived"] = derived
        return derived

    def choose(
        board: chess.Board,
        clock: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        assert deadline is not None
        seen["passed"], seen["choose_at"] = deadline, time.monotonic()
        return real_choose(board, clock, deadline=deadline, stop=stop)

    def hold(think_end: float, deadline: float | None, infinite: bool) -> None:
        """Record the emulated think-time clamp instead of waiting it out."""
        del think_end, infinite
        assert deadline is not None
        seen["held"] = deadline

    monkeypatch.setattr(agent, "deadline_for", deadline_for)
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(server, "_hold", hold)
    server.handle(f"setoption name Move Overhead value {overhead_ms}")
    server.handle("setoption name CCA_EmulateThinkTime value true")
    server.handle("position startpos moves e2e4")
    seen["go_at"] = time.monotonic()
    server.handle(f"go {go}")
    server._join()
    assert len(_bestmoves(out)) == 1
    server._shutdown()
    return seen


@pytest.mark.parametrize("go", ["movetime 3000", "wtime 60000 btime 60000 winc 1000 binc 1000"])
def test_move_overhead_shrinks_every_deadline(monkeypatch: pytest.MonkeyPatch, go: str) -> None:
    for overhead_ms in (0, 400):
        seen = _spied_go(monkeypatch, overhead_ms, go)
        shrink = seen["derived"] - seen["passed"]  # overhead + the (tiny) set-up time
        # -1e-9: a coarse monotonic tick (Windows, Python 3.11) can make set-up time 0 and the
        # float difference land a hair below the overhead.
        assert overhead_ms / 1000 - 1e-9 <= shrink < overhead_ms / 1000 + 0.1
        assert seen["held"] == seen["passed"]  # the emulated think-time clamp shrinks too


def test_move_overhead_larger_than_the_budget_stops_at_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spied_go(monkeypatch, 5000, "movetime 1000")
    floor = protocol._MIN_BUDGET
    assert seen["go_at"] + floor <= seen["passed"] <= seen["choose_at"] + floor


def test_move_overhead_is_clamped_to_its_range(uci: tuple[UciServer, io.StringIO]) -> None:
    server, _ = uci
    server.handle("setoption name Move Overhead value 99999")
    assert server._opts["Move Overhead"] == "5000"
    server.handle("setoption name Move Overhead value -3")
    assert server._opts["Move Overhead"] == "0"


# ---------------------------------------------------------------- 5. go limits not applied
def test_unapplied_go_limits_parser() -> None:
    assert unapplied_go_limits(["wtime", "1000", "btime", "1000", "movetime", "50"]) == []
    tokens = ["searchmoves", "e2e4", "d2d4", "depth", "3", "mate", "2", "nodes", "9"]
    assert unapplied_go_limits(tokens) == ["depth", "nodes", "mate", "searchmoves"]


@pytest.mark.parametrize(
    ("go", "named"),
    [
        ("depth 5", "depth"),
        ("nodes 1000", "nodes"),
        ("mate 3", "mate"),
        ("searchmoves e2e4 d2d4", "searchmoves"),
        ("depth 3 nodes 100 mate 2 searchmoves g1f3 wtime 60000 btime 60000", "depth nodes mate"),
    ],
)
def test_unapplied_go_limits_are_reported_once_and_search_runs(go: str, named: str) -> None:
    server, out, _ = _server_with_agent()
    server.handle("position startpos")
    server.handle(f"go {go}")
    server._join()
    notes = [ln for ln in _lines(out) if ln.startswith(NOT_APPLIED)]
    assert len(notes) == 1
    assert named in notes[0]
    assert "CCA_Nodes" in notes[0]
    moves = _bestmoves(out)
    assert len(moves) == 1
    assert chess.Move.from_uci(moves[0]) in chess.Board().legal_moves
    assert "info string cca q_opt" in out.getvalue()  # a normal search, not a fallback
    server._shutdown()


def test_clock_only_go_has_no_unapplied_note() -> None:
    server, out, _ = _server_with_agent()
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000 movestogo 20")
    server._join()
    assert NOT_APPLIED not in out.getvalue()
    assert len(_bestmoves(out)) == 1
    server._shutdown()


# ---------------------------------------------------------------- 6. unknown options
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SyzygyPath", "/tables/syzygy"),
        ("UCI_ShowWDL", "true"),
        ("UCI_AnalyseMode", "true"),
        ("UCI_Chess960", "false"),
    ],
)
def test_common_unknown_options_are_answered_and_ignored(
    uci: tuple[UciServer, io.StringIO], name: str, value: str
) -> None:
    server, out = uci
    server.handle("isready")
    engine, opts = server._engine, dict(server._opts)
    assert engine is not None
    before = len(_lines(out))
    server.handle(f"setoption name {name} value {value}")
    assert _lines(out)[before:] == [f"info string ignoring unknown option {name}"]
    assert server._opts == opts
    assert server._engine is engine  # nothing was rebuilt


# ---------------------------------------------------------------- 7. option help
def test_option_help_covers_exactly_the_advertised_options(
    uci: tuple[UciServer, io.StringIO],
) -> None:
    server, out = uci
    server.handle("uci")
    names = _advertised(_lines(out))
    assert len(names) == len(set(names))
    assert set(names) == set(UCI_OPTION_HELP)
    for name, text in UCI_OPTION_HELP.items():
        assert text.strip(), name
        assert "\n" not in text, name
    assert not hasattr(UCI_OPTION_HELP, "__setitem__")  # read-only for the doc generator
