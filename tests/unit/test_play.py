"""``cca play``: game logic, JSON API, HTTP security guards, static files and vendored assets.

Everything runs against the deterministic fakes of ``tests/conftest.py`` (no Stockfish); HTTP
tests talk to a real ``PlayServer`` on 127.0.0.1 through ``http.client`` so raw headers
(``Host``, ``Origin``, ``Content-Type``, ``Content-Length``) can be forged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html as html_lib
import http.client
import io
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from email.message import Message
from functools import partial
from pathlib import Path
from typing import cast

import chess
import chess.engine
import chess.pgn
import pytest

import cca.play.game as play_game
import cca.play.serialize as play_serialize
import cca.play.server as play_server
from cca import __version__
from cca.agent import AgentConfig, CAIMEAgent
from cca.config import list_personas, load_persona
from cca.core.types import Candidate, Clock, Decision, Knobs, MoveEval, PsychState
from cca.play.app import ELO_OPPO_RANGE, ELO_SELF_RANGE, Engines, PlayApp, PlaySettings
from cca.play.game import ApiError, board_end, seed_commitment
from cca.play.serialize import decision_to_json, dumps, jsonable, loads_object
from cca.play.server import (
    CSP,
    MAX_BODY,
    PlayServer,
    exposure_warning,
    is_loopback,
    make_server,
    run,
    server_url,
    static_file,
)
from cca.uci import protocol
from tests.conftest import FakeEngine, FakeHuman

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(play_server.__file__).resolve().parent / "static"  # the package under test
VENDOR = STATIC / "vendor"
MATE_IN_ONE = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"  # Ra8#
STALEMATE_IN_ONE = "7k/5Q2/8/8/8/8/8/6K1 w - - 0 1"  # Qg6 stalemates
BARE_KINGS_IN_ONE = "k7/8/8/8/8/8/1r6/K7 w - - 0 1"  # Kxb2 leaves K v K
FIFTY_IN_ONE = "7k/8/8/8/8/8/R7/K7 w - - 99 80"  # any quiet move reaches 100 half-moves


# ---------------------------------------------------------------- helpers
class FakeClock:
    """Injected monotonic clock: time only moves when a test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ClosingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def make_app(
    *,
    max_sessions: int = 16,
    base: AgentConfig | None = None,
    now: Callable[[], float] = time.monotonic,
    engine: FakeEngine | None = None,
    reason: str | None = None,
) -> PlayApp:
    eng = engine or FakeEngine()
    human = FakeHuman()
    settings = PlaySettings(base or AgentConfig(), max_sessions=max_sessions)
    app = PlayApp(lambda: Engines(eng, human, "qre", reason), settings, now=now)
    app.warm_up()
    return app


def state_of(result: dict[str, object]) -> dict[str, object]:
    state = result["state"]
    assert isinstance(state, dict)
    return state


def new_game(app: PlayApp, **body: object) -> tuple[str, dict[str, object]]:
    created = app.create_game(dict(body))
    game_id = created["game_id"]
    assert isinstance(game_id, str)
    return game_id, state_of(created)


def api_status(call: Callable[[], object]) -> int:
    with pytest.raises(ApiError) as err:
        call()
    return err.value.status


@dataclasses.dataclass
class Response:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, object]:
        data = json.loads(self.body.decode("utf-8"), parse_constant=_no_constant)
        assert isinstance(data, dict)
        return data


def _no_constant(name: str) -> object:
    raise AssertionError(f"non-standard JSON constant {name}")


def call(
    port: int,
    method: str,
    path: str,
    body: object = None,
    *,
    headers: Mapping[str, str | None] | None = None,
    raw: bytes | None = None,
    host: str | None = "127.0.0.1",
) -> Response:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs: dict[str, str | None] = {}
        if host is not None:
            hdrs["Host"] = f"{host}:{port}"
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        hdrs.update(headers or {})
        for key, value in hdrs.items():
            if value is not None:
                conn.putheader(key, value)
        conn.endheaders()
        if data:
            conn.send(data)
        resp = conn.getresponse()
        return Response(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


@pytest.fixture
def served() -> Iterator[tuple[PlayServer, PlayApp]]:
    app = make_app()
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield server, app
    server.shutdown()
    server.server_close()
    thread.join(5)
    app.close()


def port_of(server: PlayServer) -> int:
    return int(server.server_address[1])


# ---------------------------------------------------------------- serialisation
def test_jsonable_turns_non_finite_floats_into_null() -> None:
    value = {"a": math.nan, "b": [math.inf, -math.inf, 1.5], "c": (1, "x", None, True)}
    assert jsonable(value) == {"a": None, "b": [None, None, 1.5], "c": [1, "x", None, True]}
    assert json.loads(dumps(value)) == jsonable(value)
    assert b"NaN" not in dumps({"x": math.nan})
    with pytest.raises(TypeError):
        jsonable(object())


def test_loads_object_is_strict() -> None:
    assert loads_object(b"") == {}
    assert loads_object(b'{"a": 1}') == {"a": 1}
    for bad in (b'{"a": NaN}', b'{"a": Infinity}'):
        with pytest.raises(ValueError, match="not valid JSON"):
            loads_object(bad)
    with pytest.raises(json.JSONDecodeError):
        loads_object(b"{")
    with pytest.raises(UnicodeDecodeError):
        loads_object(b"\xff\xfe")
    with pytest.raises(TypeError):
        loads_object(b"[1, 2]")


def test_decision_json_has_every_real_field(fake_engine: FakeEngine, fake_human: FakeHuman) -> None:
    board = chess.Board()
    board.push_uci("e2e4")
    decision = CAIMEAgent(fake_engine, fake_human, AgentConfig(seed="t")).choose(board)
    out = decision_to_json(board, decision)
    names = {f.name for f in dataclasses.fields(Candidate)}
    candidates = out["candidates"]
    assert isinstance(candidates, list)
    assert candidates
    for row, cand in zip(candidates, decision.candidates, strict=True):
        assert set(row) == names | {"trap_value", "san", "policy"}
        assert row["trap_value"] == pytest.approx(cand.q_human - cand.q_opt)
        assert row["san"] == board.san(chess.Move.from_uci(cand.uci))
        assert row["policy"] == decision.policy.get(cand.uci)
    knobs = out["knobs"]
    state = out["state"]
    assert isinstance(knobs, dict)
    assert isinstance(state, dict)
    assert set(knobs) == {f.name for f in dataclasses.fields(Knobs)}
    assert set(state) == {f.name for f in dataclasses.fields(PsychState)}
    assert out["trace"] == dict(decision.trace)
    assert out["think_time"] == decision.think_time
    policy = out["policy"]
    assert isinstance(policy, list)
    probs = [p["p"] for p in policy]
    assert probs == sorted(probs, reverse=True)
    assert sum(probs) == pytest.approx(1.0)
    assert {p["uci"] for p in policy} == set(decision.policy)
    assert out["move"] == {
        "uci": decision.move,
        "san": board.san(chess.Move.from_uci(decision.move)),
    }


# ---------------------------------------------------------------- start-up, health, info
def test_health_reports_warm_up_and_failure() -> None:
    app = PlayApp(lambda: Engines(FakeEngine(), FakeHuman(), "qre"), PlaySettings(AgentConfig()))
    assert app.health() == (200, {"status": "ok", "version": __version__, "ready": False})
    assert api_status(lambda: app.create_game({})) == 503
    app.warm_up()
    assert app.health()[1]["ready"] is True
    assert app.wait_ready(0.1)

    def broken() -> Engines:
        raise RuntimeError("no engine")

    bad = PlayApp(broken, PlaySettings(AgentConfig()))
    bad.warm_up()
    status, payload = bad.health()
    assert status == 503
    assert payload["status"] == "error"
    assert "RuntimeError: no engine" in str(payload["error"])
    assert bad.info()["error"] == payload["error"]
    assert api_status(lambda: bad.create_game({})) == 503
    assert not bad.wait_ready(0.1)


def test_info_lists_personas_defaults_ranges_and_fallback() -> None:
    app = make_app(reason="ImportError: no maia2")
    info = app.info()
    personas = info["personas"]
    assert isinstance(personas, list)
    assert [p["name"] for p in personas] == list_personas()
    assert all(p["description"] for p in personas)
    tal = next(p for p in personas if p["name"] == "tal")
    assert "NOT fitted" in tal["description"]
    assert info["human_model"] == {
        "requested": "maia2",
        "kind": "qre",
        "fallback_reason": "ImportError: no maia2",
    }
    ranges = info["ranges"]
    assert isinstance(ranges, dict)
    assert ranges["elo_self"] == [800, 2600]
    assert ranges["elo_oppo"] == [400, 3000]
    assert info["defaults"] == {
        "human_color": "white",
        "persona": "balanced",
        "elo_self": 1900,
        "elo_oppo": 1500,
        "time_control": None,
        "emulate_think_time": False,
    }
    engine = info["engine"]
    assert isinstance(engine, dict)
    assert engine["name"] == "FakeEngine"
    assert info["ready"] is True
    assert info["chaos_driver"] == "lorenz"  # the About panel names the driver in use
    assert make_app(base=AgentConfig(chaos_driver="ar1")).info()["chaos_driver"] == "ar1"


def test_about_panel_does_not_overstate_licences_or_the_chaos_source() -> None:
    # Maia-2's MIT licence covers its code only; its weights have no published licence
    # (THIRD_PARTY_NOTICES.md). The chaos driver is configurable (lorenz or ar1).
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    maia = next(line for line in html.splitlines() if "<strong>Maia-2</strong>" in line)
    assert "No licence is published for its model weights" in maia
    strings = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    for line in strings.splitlines():
        if line.strip().startswith(("g_latent:", "series_chaos_hint:")):
            assert "Lorenz" in line
            assert "default" in line or "mặc định" in line, line
    assert strings.count("chaos driver {chaos}") == 1
    assert strings.count("bộ tạo hỗn loạn {chaos}") == 1
    assert 'role="list"' not in html  # its children are buttons, not list items


def test_elo_ranges_match_the_uci_options() -> None:
    assert protocol._SPIN_LIMITS["UCI_Elo"][1:] == ELO_SELF_RANGE
    assert protocol._SPIN_LIMITS["CCA_OpponentElo"][1:] == ELO_OPPO_RANGE


def _elo_config(field: str, value: int) -> AgentConfig:
    return AgentConfig(elo_self=value) if field == "elo_self" else AgentConfig(elo_oppo=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("elo_self", 799),
        ("elo_self", 2601),
        ("elo_oppo", 399),
        ("elo_oppo", 3001),
        ("elo_oppo", -50),
    ],
)
def test_start_up_elo_must_lie_inside_the_offered_ranges(field: str, value: int) -> None:
    # The command-line values become every game's defaults, so they obey the API ranges too.
    flag = "--" + field.replace("_", "-")
    with pytest.raises(ValueError, match=f"^{flag} {value} is outside the range cca play offers"):
        PlayApp(
            lambda: Engines(FakeEngine(), FakeHuman(), "qre"),
            PlaySettings(_elo_config(field, value)),
        )
    limits = ELO_SELF_RANGE if field == "elo_self" else ELO_OPPO_RANGE
    for edge in limits:  # the limits themselves are fine
        app = make_app(base=_elo_config(field, edge))
        cca = new_game(app)[1]["cca"]
        assert isinstance(cca, dict)
        assert cca["elo" if field == "elo_self" else "opponent_elo"] == edge


def test_command_line_persona_is_offered_as_cli() -> None:
    custom = dataclasses.replace(load_persona("tal"), eps0=0.01)
    app = make_app(base=AgentConfig(persona=custom))
    info = app.info()
    personas = info["personas"]
    assert isinstance(personas, list)
    assert personas[0]["name"] == "cli"
    defaults = info["defaults"]
    assert isinstance(defaults, dict)
    assert defaults["persona"] == "cli"
    game_id, state = new_game(app, persona="cli")
    assert app.session(game_id).agent.config.persona == custom
    cca = state["cca"]
    assert isinstance(cca, dict)
    assert cca["persona"] == "cli"
    other_id, _ = new_game(app, persona="solid")
    assert app.session(other_id).agent.config.persona == load_persona("solid")


# ---------------------------------------------------------------- creating games
def test_default_game_is_white_untimed_balanced_with_secret_seed() -> None:
    app = make_app()
    game_id, state = new_game(app)
    assert state["human_color"] == "white"
    assert state["to_move"] == "human"
    assert state["time_control"] is None
    assert state["clocks"] is None
    assert state["moves"] == []
    assert state["history"] == []
    cca = state["cca"]
    assert isinstance(cca, dict)
    assert cca["persona"] == "balanced"
    assert cca["seed"] is None
    session = app.session(game_id)
    assert cca["seed_commitment"] == hashlib.sha256(session.seed.encode()).hexdigest()
    assert len(session.seed) == 32
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", game_id)


def test_chosen_seed_is_public_and_reproducible() -> None:
    app = make_app()
    moves = []
    for _ in range(2):
        game_id, state = new_game(app, seed="lab-1", human_color="black")
        cca = state["cca"]
        assert isinstance(cca, dict)
        assert cca["seed"] == "lab-1"
        assert cca["seed_commitment"] is None
        result = app.think(game_id)
        move = result["move"]
        assert isinstance(move, dict)
        moves.append(move["uci"])
    assert moves[0] == moves[1]


def test_chosen_seed_round_trips_through_the_pgn() -> None:
    app = make_app()
    seed = "Az09._:+-" + "x" * 55  # every allowed kind of character, at the 64-char limit
    game_id, _ = new_game(app, seed=seed)
    app.resign(game_id)
    game = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert game is not None
    assert game.headers["CCASeed"] == seed
    assert game.headers["Result"] == "0-1"


def test_random_colour_uses_a_secret_coin(monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    monkeypatch.setattr("cca.play.app.secrets.choice", lambda seq: chess.BLACK)
    _, state = new_game(app, human_color="random")
    assert state["human_color"] == "black"
    assert state["to_move"] == "cca"


@pytest.mark.parametrize(
    "body",
    [
        {"colour": "white"},
        {"human_color": "green"},
        {"human_color": []},  # unhashable: must be a 400, not a TypeError (500)
        {"human_color": {}},
        {"human_color": None},
        {"persona": "../../secrets"},
        {"persona": "cli"},
        {"persona": 3},
        {"elo_self": 5000},
        {"elo_self": 799},
        {"elo_self": True},
        {"elo_self": 1900.5},
        {"elo_oppo": 3001},
        {"fen": "not a fen"},
        {"fen": "8/8/8/8/8/8/8/8 w - - 0 1"},
        {"fen": "4k3/8/8/8/8/8/8/P3K3 w - - 0 1"},  # pawn on the first rank, king can move
        {"fen": "4k3/4Q3/8/8/8/8/8/4K3 w - - 0 1"},  # the side not to move is in check
        {"fen": "4k3/8/8/8/8/8/8/4K2R w K - 0 1" + " " * 80},  # playable, but too long
        {"fen": " " * 101},
        {"fen": "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"},
        {"fen": 42},
        {"time_control": {"base_s": 0, "inc_s": 0}},
        {"time_control": {"base_s": "300"}},
        {"time_control": {"base_s": 300, "inc_s": 500}},
        {"time_control": {"base_s": 300, "delay": 2}},
        {"time_control": [300, 2]},
        {"emulate_think_time": "yes"},
        {"seed": "x" * 65},
        {"seed": "bad\nseed"},
        {"seed": 7},
        {"seed": 'x"] [Evil "1'},  # would break out of the PGN tag (python-chess does not escape)
        {"seed": "back\\slash"},
        {"seed": "two words"},
        {"seed": "hạt"},
    ],
)
def test_create_game_rejects_bad_fields(body: dict[str, object]) -> None:
    assert api_status(lambda: make_app().create_game(body)) == 400


def test_persona_paths_are_refused(tmp_path: Path) -> None:
    toml = tmp_path / "evil.toml"
    toml.write_text('name = "evil"\n', encoding="utf-8")
    app = make_app()
    assert api_status(lambda: app.create_game({"persona": str(toml)})) == 400
    assert api_status(lambda: app.analyse({"fen": chess.STARTING_FEN, "persona": str(toml)})) == 400


def test_fen_start_sets_up_the_position() -> None:
    app = make_app()
    fen = "4k3/1P6/8/8/8/8/6K1/8 w - - 0 1"
    game_id, state = new_game(app, fen=fen)
    assert state["start_fen"] == fen
    assert state["fen"] == fen
    app.move(game_id, {"uci": "b7b8q"})
    state = app.game_state(game_id)
    moves = state["moves"]
    assert isinstance(moves, list)
    assert moves[0]["san"] == "b8=Q+"
    assert state["check"] is True
    assert state["check_square"] == "e8"
    pgn = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert pgn is not None
    assert pgn.headers["FEN"] == fen
    assert pgn.headers["SetUp"] == "1"


# ---------------------------------------------------------------- playing
def test_move_think_flow_and_decisions() -> None:
    app = make_app()
    game_id, _ = new_game(app)
    state = state_of(app.move(game_id, {"uci": "e2e4"}))
    assert state["to_move"] == "cca"
    assert state["last_move"] == {"uci": "e2e4", "san": "e4", "from": "e2", "to": "e4"}
    result = app.think(game_id)
    move = result["move"]
    assert isinstance(move, dict)
    after = chess.Board()
    after.push_uci("e2e4")
    assert chess.Move.from_uci(str(move["uci"])) in after.legal_moves
    assert move["san"] == after.san(chess.Move.from_uci(str(move["uci"])))
    assert result["reveal_in_ms"] == 0
    decision = result["decision"]
    assert isinstance(decision, dict)
    assert decision["move"] == move
    state = state_of(result)
    assert state["to_move"] == "human"
    history = state["history"]
    assert isinstance(history, list)
    assert len(history) == 1
    entry = history[0]
    assert entry["ply"] == 1
    assert entry["move_number"] == 1  # 1. e4 and CCA's reply
    assert entry["san"] == move["san"]
    assert entry["restart"] is False
    for key in ("q_opt", "stress", "drive", "opp_stress", "chaos", "think_time", "compute_s"):
        assert key in entry
    assert len(entry["chaos"]) == 3
    decisions = app.decisions(game_id)["decisions"]
    assert isinstance(decisions, list)
    assert decisions == [{"ply": 1, "decision": decision}]


def test_move_rejections() -> None:
    app = make_app()
    game_id, _ = new_game(app)
    assert api_status(lambda: app.move(game_id, {"uci": "e2e5"})) == 400
    assert api_status(lambda: app.move(game_id, {"uci": "E2E4"})) == 400
    assert api_status(lambda: app.move(game_id, {"uci": "0000"})) == 400
    assert api_status(lambda: app.move(game_id, {"uci": ["e2e4"]})) == 400
    assert api_status(lambda: app.move(game_id, {"uci": "e2e4", "x": 1})) == 400
    assert api_status(lambda: app.think(game_id)) == 409  # the human's turn
    app.move(game_id, {"uci": "e2e4"})
    # CCA's turn: even a move that is legal for CCA's side is refused.
    assert api_status(lambda: app.move(game_id, {"uci": "e7e5"})) == 400
    moves = app.game_state(game_id)["moves"]
    assert isinstance(moves, list)
    assert [m["san"] for m in moves] == ["e4"]


@pytest.mark.parametrize(
    ("fen", "human_first", "numbers"),
    [
        ("4k3/8/8/8/8/8/4P3/4K3 b - - 0 40", False, [40, 41]),  # CCA (Black) starts: 40... Kd8
        ("4k3/8/8/8/8/8/4P3/4K3 w - - 0 12", True, [12, 13]),  # 12. e3 and CCA's reply
    ],
)
def test_history_numbers_moves_like_the_move_list(
    fen: str, human_first: bool, numbers: list[int]
) -> None:
    # Regression: the move number of the chart tooltips counted from 1 whatever the FEN said.
    app = make_app()
    game_id, _ = new_game(app, fen=fen, human_color="white")
    human = iter(["e2e3", "e3e4"])
    for _ in numbers:
        if human_first:
            app.move(game_id, {"uci": next(human)})
        app.think(game_id)
        if not human_first:
            app.move(game_id, {"uci": next(human)})
    history = app.game_state(game_id)["history"]
    assert isinstance(history, list)
    assert [h["move_number"] for h in history] == numbers


def test_human_with_black_waits_for_cca() -> None:
    app = make_app()
    game_id, state = new_game(app, human_color="black")
    assert state["to_move"] == "cca"
    assert api_status(lambda: app.move(game_id, {"uci": "e2e4"})) == 400  # legal, but CCA's
    state = state_of(app.think(game_id))
    moves = state["moves"]
    assert isinstance(moves, list)
    assert state["to_move"] == "human"
    assert len(moves) == 1


@pytest.mark.parametrize(
    ("fen", "uci", "result", "reason", "winner"),
    [
        (MATE_IN_ONE, "a1a8", "1-0", "checkmate", "human"),
        (STALEMATE_IN_ONE, "f7g6", "1/2-1/2", "stalemate", None),
        (BARE_KINGS_IN_ONE, "a1b2", "1/2-1/2", "insufficient-material", None),
        (FIFTY_IN_ONE, "a2a3", "1/2-1/2", "fifty-move", None),
    ],
)
def test_board_endings(fen: str, uci: str, result: str, reason: str, winner: str | None) -> None:
    app = make_app()
    game_id, _ = new_game(app, fen=fen)
    state = state_of(app.move(game_id, {"uci": uci}))
    assert (state["game_over"], state["result"], state["reason"], state["winner"]) == (
        True,
        result,
        reason,
        winner,
    )
    assert state["to_move"] is None
    cca = state["cca"]
    assert isinstance(cca, dict)
    assert cca["seed"] is not None  # revealed at the end
    assert api_status(lambda: app.think(game_id)) == 409
    assert api_status(lambda: app.move(game_id, {"uci": "a1a2"})) == 409


def test_threefold_repetition_ends_the_game() -> None:
    board = chess.Board()
    for _ in range(2):
        for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
            board.push_uci(uci)
    assert board_end(board) == ("1/2-1/2", "threefold")
    assert board_end(chess.Board()) is None


def test_undo_takes_back_the_pair_and_marks_the_restart() -> None:
    app = make_app()
    game_id, _ = new_game(app)
    assert api_status(lambda: app.undo(game_id)) == 400  # nothing to take back
    app.move(game_id, {"uci": "e2e4"})
    app.think(game_id)
    state = state_of(app.undo(game_id))
    assert state["moves"] == []
    assert state["history"] == []
    assert app.decisions(game_id)["decisions"] == []
    app.move(game_id, {"uci": "d2d4"})
    state = state_of(app.undo(game_id))  # CCA had not answered yet: one ply
    assert state["moves"] == []
    app.move(game_id, {"uci": "d2d4"})
    history = state_of(app.think(game_id))["history"]
    assert isinstance(history, list)
    assert history[-1]["restart"] is True


def test_taking_back_only_the_humans_mate_does_not_restart_cca() -> None:
    # Regression: every take-back marked a restart of CCA's latent state, even one that removed
    # only the human's mating move, which CCA never saw: its game simply goes on.
    app = make_app()
    game_id, _ = new_game(app, fen="k7/8/1K6/8/8/8/8/7R w - - 0 1")
    agent = app.session(game_id).agent
    app.move(game_id, {"uci": "h1h2"})
    app.think(game_id)  # Kb8, the only move
    known = agent.game_id
    assert state_of(app.move(game_id, {"uci": "h2h8"}))["reason"] == "checkmate"
    moves = state_of(app.undo(game_id))["moves"]
    assert isinstance(moves, list)
    assert [m["san"] for m in moves] == ["Rh2", "Kb8"]  # CCA's move stays
    app.move(game_id, {"uci": "h2h3"})
    history = state_of(app.think(game_id))["history"]
    assert isinstance(history, list)
    assert [h["restart"] for h in history] == [False, False]
    assert agent.game_id == known  # the agent's own view: the same game


def test_undo_refused_in_timed_games_and_after_resignation() -> None:
    app = make_app()
    timed, state = new_game(app, time_control={"base_s": 60, "inc_s": 1})
    assert state["can_undo"] is False
    app.move(timed, {"uci": "e2e4"})
    assert api_status(lambda: app.undo(timed)) == 409
    game_id, _ = new_game(app)
    app.move(game_id, {"uci": "e2e4"})
    app.think(game_id)
    assert app.game_state(game_id)["can_undo"] is True
    app.resign(game_id)
    assert app.game_state(game_id)["can_undo"] is False  # the page greys the button out
    assert api_status(lambda: app.undo(game_id)) == 409


def test_undo_is_allowed_after_a_board_ending() -> None:
    app = make_app()
    game_id, _ = new_game(app, fen=MATE_IN_ONE)
    app.move(game_id, {"uci": "a1a8"})
    state = state_of(app.undo(game_id))
    assert state["game_over"] is False
    assert state["moves"] == []


def test_resign_and_pgn() -> None:
    app = make_app()
    game_id, _ = new_game(app, elo_self=2100, elo_oppo=1200, persona="tal")
    app.move(game_id, {"uci": "e2e4"})
    app.think(game_id)
    ongoing = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert ongoing is not None
    assert ongoing.headers["Result"] == "*"
    assert ongoing.headers["Termination"] == "unterminated"
    assert "CCASeed" not in ongoing.headers
    assert all(not node.comment for node in ongoing.mainline())
    state = state_of(app.resign(game_id))
    assert (state["result"], state["reason"], state["winner"]) == ("0-1", "resignation", "cca")
    assert api_status(lambda: app.resign(game_id)) == 409
    game = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert game is not None
    headers = game.headers
    assert headers["White"] == "Human"
    assert headers["Black"] == f"CCA {__version__} (tal, 2100)"
    assert (headers["WhiteElo"], headers["BlackElo"]) == ("1200", "2100")
    assert headers["Result"] == "0-1"
    assert headers["Termination"] == "normal"
    assert headers["TimeControl"] == "-"
    assert headers["CCAPersona"] == "tal"
    seed = app.session(game_id).seed
    assert headers["CCASeed"] == seed
    assert headers["CCASeedCommitment"] == seed_commitment(seed)
    comments = [node.comment for node in game.mainline()]
    assert comments[0] == ""
    full = r"cca think=\d+\.\ds q_opt=\d\.\d{3} trap=[+-]\d\.\d{3} stress=\d\.\d{2}"
    assert re.fullmatch(full, comments[1]), comments[1]


def test_pgn_comments_leave_out_what_a_reflex_move_lacks() -> None:
    # Regression: a reflex move (CCA nearly out of time plays the engine's best move) has no
    # trap value, and its PGN comment read "trap=+nan".
    app = make_app()
    game_id, _ = new_game(
        app, human_color="black", time_control={"base_s": 1, "inc_s": 0}, emulate_think_time=False
    )
    decision = app.think(game_id)["decision"]
    assert isinstance(decision, dict)
    assert decision["trace"]["reflex"] == 1.0
    app.resign(game_id)
    game = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert game is not None
    comment = next(iter(game.mainline())).comment
    assert re.fullmatch(r"cca think=\d+\.\ds q_opt=\d\.\d{3} stress=\d\.\d{2} reflex", comment)
    assert "nan" not in app.pgn(game_id)


# ---------------------------------------------------------------- clocks
@pytest.mark.parametrize(
    ("think", "charged"),
    [
        (4.0, 4.0),  # CCA "wants" 4 s but spent 1 s: charged 4 s, shown 3 s later
        (0.5, 1.0),  # it already spent more than it wanted: charged the wall time, shown at once
    ],
)
def test_clocks_charge_emulated_think_time_and_pass_the_deadline(
    monkeypatch: pytest.MonkeyPatch, think: float, charged: float
) -> None:
    # The think time is pinned: the model's own draw depends on the (secret, random) seed.
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, state = new_game(app, time_control={"base_s": 60, "inc_s": 2}, emulate_think_time=True)
    assert state["clocks"] == {
        "white_ms": 60000,
        "black_ms": 60000,
        "running": "white",
        "starts_in_ms": 0,
        "inc_ms": 2000,
    }
    clock.advance(5)
    state = state_of(app.move(game_id, {"uci": "e2e4"}))
    clocks = state["clocks"]
    assert isinstance(clocks, dict)
    assert (clocks["white_ms"], clocks["running"]) == (57000, "black")
    session = app.session(game_id)
    clocks_seen: list[Clock | None] = []
    deadlines: list[float | None] = []
    original = session.agent.choose

    def spy(
        board: chess.Board,
        clk: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        clocks_seen.append(clk)
        deadlines.append(deadline)
        decision = original(board, clk, deadline=deadline, stop=stop)
        return dataclasses.replace(decision, think_time=think)

    monkeypatch.setattr(session.agent, "choose", spy)
    clock.advance(1)  # CCA's clock runs before /think arrives
    result = app.think(game_id)
    agent_clock = clocks_seen[0]
    assert agent_clock is not None
    assert agent_clock.my_time == pytest.approx(59.0)
    assert agent_clock.opp_time == pytest.approx(57.0)
    assert agent_clock.my_inc == 2.0
    assert isinstance(deadlines[0], float)  # agent.deadline_for(): CCA respects its clock
    decision = result["decision"]
    assert isinstance(decision, dict)
    assert decision["think_time"] == think
    reveal_ms = round((charged - 1.0) * 1000)
    assert result["reveal_in_ms"] == reveal_ms
    clocks = state_of(result)["clocks"]
    assert isinstance(clocks, dict)
    assert clocks["black_ms"] == round((60 - charged + 2) * 1000)
    assert clocks["running"] == "white"
    assert clocks["starts_in_ms"] == reveal_ms
    assert clocks["white_ms"] == 57000  # paused until CCA's move is shown
    clock.advance(reveal_ms / 1000 + 2)  # the move is shown, then the human thinks 2 s
    clocks = app.game_state(game_id)["clocks"]
    assert isinstance(clocks, dict)
    assert (clocks["white_ms"], clocks["starts_in_ms"]) == (55000, 0)


def _pin_think_time(
    monkeypatch: pytest.MonkeyPatch, session: play_game.GameSession, s: float
) -> None:
    original = session.agent.choose

    def pinned(
        board: chess.Board,
        clk: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        decision = original(board, clk, deadline=deadline, stop=stop)
        return dataclasses.replace(decision, think_time=s)

    monkeypatch.setattr(session.agent, "choose", pinned)


@pytest.mark.parametrize("timed", [True, False])
def test_a_move_before_ccas_move_is_revealed_gets_no_free_time(
    monkeypatch: pytest.MonkeyPatch, timed: bool
) -> None:
    # Regression: with the emulated delay, an API client could answer CCA's move while the
    # page would still be revealing it; it was charged no time and still got the increment.
    clock = FakeClock()
    app = make_app(now=clock)
    tc = {"base_s": 180, "inc_s": 2} if timed else None
    game_id, _ = new_game(app, human_color="black", time_control=tc, emulate_think_time=True)
    _pin_think_time(monkeypatch, app.session(game_id), 8.0)
    assert app.think(game_id)["reveal_in_ms"] == 8000
    clock.advance(0.5)
    if not timed:  # no clock, nothing to gain: accepted
        assert state_of(app.move(game_id, {"uci": "g8f6"}))["to_move"] == "cca"
        return
    with pytest.raises(ApiError) as refused:
        app.move(game_id, {"uci": "g8f6"})
    assert refused.value.status == 409
    assert refused.value.state is not None
    clocks = app.game_state(game_id)["clocks"]
    assert isinstance(clocks, dict)
    assert (clocks["black_ms"], clocks["running"], clocks["starts_in_ms"]) == (
        180000,
        "black",
        7500,
    )
    clock.advance(7.3)  # still 0.2 s early: refused too (the grace is a few browser-timer ms)
    assert api_status(lambda: app.move(game_id, {"uci": "g8f6"})) == 409
    # A browser timer may fire a few ms early: 20 ms early counts as answered at the reveal.
    clock.advance(0.18)
    clocks = state_of(app.move(game_id, {"uci": "g8f6"}))["clocks"]
    assert isinstance(clocks, dict)
    assert (clocks["black_ms"], clocks["running"]) == (182000, "white")  # 0 s spent, +2 s


def test_without_emulation_cca_pays_wall_time_only() -> None:
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 60, "inc_s": 0}, human_color="black")
    clock.advance(3)
    result = app.think(game_id)
    assert result["reveal_in_ms"] == 0
    clocks = state_of(result)["clocks"]
    assert isinstance(clocks, dict)
    assert clocks["white_ms"] == 57000
    assert clocks["starts_in_ms"] == 0


def test_flag_fall_loses_and_timeout_against_bare_king_draws() -> None:
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 10, "inc_s": 0})
    clock.advance(10.5)
    state = app.game_state(game_id)
    assert (state["result"], state["reason"], state["winner"]) == ("0-1", "time", "cca")
    clocks = state["clocks"]
    assert isinstance(clocks, dict)
    assert clocks["white_ms"] == 0
    assert clocks["running"] is None
    assert api_status(lambda: app.move(game_id, {"uci": "e2e4"})) == 409
    game = chess.pgn.read_game(io.StringIO(app.pgn(game_id)))
    assert game is not None
    assert game.headers["Termination"] == "time forfeit"
    lone, _ = new_game(app, fen="4k3/8/8/8/8/8/PPP5/4K3 w - - 0 1", time_control={"base_s": 5})
    clock.advance(6)
    state = app.game_state(lone)
    assert (state["result"], state["reason"], state["winner"]) == ("1/2-1/2", "time", None)


def test_cca_loses_on_time_when_its_decision_is_too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 2, "inc_s": 0}, human_color="black")
    session = app.session(game_id)
    original = session.agent.choose

    def slow(
        board: chess.Board,
        clk: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        clock.advance(3)
        return original(board, clk, deadline=deadline, stop=stop)

    monkeypatch.setattr(session.agent, "choose", slow)
    result = app.think(game_id)
    assert result["move"] is None
    state = state_of(result)
    assert (state["result"], state["reason"], state["winner"]) == ("0-1", "time", "human")
    assert state["moves"] == []


@pytest.mark.parametrize("action", ["move", "resign"])
def test_a_flagged_human_can_neither_move_nor_resign(action: str) -> None:
    # No state request in between: the move / resign request itself must notice the flag.
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 10, "inc_s": 0})
    clock.advance(10.5)

    def act() -> dict[str, object]:
        return app.move(game_id, {"uci": "e2e4"}) if action == "move" else app.resign(game_id)

    with pytest.raises(ApiError) as err:
        act()
    assert err.value.status == 409
    state = err.value.state
    assert state is not None
    assert (state["result"], state["reason"], state["winner"]) == ("0-1", "time", "cca")
    assert state["moves"] == []


class WaitingLock:
    """The engine lock while another game's decision runs: entering it takes ``wait`` seconds."""

    def __init__(self, clock: FakeClock, wait: float, events: list[str]) -> None:
        self.clock = clock
        self.wait = wait
        self.events = events

    def __enter__(self) -> WaitingLock:
        self.clock.advance(self.wait)
        self.events.append("lock")
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("unlock")


def spy_on_decision(
    monkeypatch: pytest.MonkeyPatch, session: play_game.GameSession, events: list[str]
) -> list[Clock | None]:
    """Record the order of deadline_for / engine.new_game / choose, and the clocks passed."""
    agent = session.agent
    seen: list[Clock | None] = []
    deadline_for, choose, new_game_ = agent.deadline_for, agent.choose, agent.engine.new_game

    def deadline_spy(board: chess.Board, clk: Clock, movetime: float | None = None) -> float | None:
        events.append("deadline")
        return deadline_for(board, clk, movetime)

    def choose_spy(
        board: chess.Board,
        clk: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        events.append("choose")
        seen.append(clk)
        return choose(board, clk, deadline=deadline, stop=stop)

    def new_game_spy() -> None:
        events.append("new_game")
        new_game_()

    monkeypatch.setattr(agent, "deadline_for", deadline_spy)
    monkeypatch.setattr(agent, "choose", choose_spy)
    monkeypatch.setattr(agent.engine, "new_game", new_game_spy)
    return seen


def test_cca_budgets_its_time_after_waiting_for_the_shared_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One Stockfish serves every game: a /think may first wait for another game's decision.
    # The deadline must be computed from the time left AFTER that wait (computed before it,
    # the wait used up the budget and CCA fell back to a reflex move), yet the wait is still
    # charged: CCA's clock was running.
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 60, "inc_s": 1}, human_color="black")
    session = app.session(game_id)
    events: list[str] = []
    seen = spy_on_decision(monkeypatch, session, events)
    clock.advance(1)
    result = session.cca_move(WaitingLock(clock, 20.0, events))
    assert events == ["lock", "deadline", "new_game", "choose", "unlock"]
    agent_clock = seen[0]
    assert agent_clock is not None
    assert agent_clock.my_time == pytest.approx(39.0)  # 60 - 1 - 20
    assert isinstance(result["move"], dict)
    clocks = session.state()["clocks"]
    assert isinstance(clocks, dict)
    assert clocks["white_ms"] == 40000  # 60 - 21 + the 1 s increment


def test_cca_flags_while_waiting_for_the_engine_without_deciding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    app = make_app(now=clock)
    game_id, _ = new_game(app, time_control={"base_s": 10, "inc_s": 0}, human_color="black")
    session = app.session(game_id)
    events: list[str] = []
    spy_on_decision(monkeypatch, session, events)
    result = session.cca_move(WaitingLock(clock, 12.0, events))
    assert events == ["lock", "unlock"]  # no engine time spent on a lost game
    assert result == {"move": None, "decision": None, "compute_s": 0.0, "reveal_in_ms": 0}
    state = session.state()
    assert (state["result"], state["reason"], state["winner"]) == ("0-1", "time", "human")


def test_every_decision_starts_from_a_cleared_engine() -> None:
    # The engine (and its hash) is shared by every game and the lab; ucinewgame before each
    # decision keeps a same-seed replay independent of what the engine searched in between.
    engine = FakeEngine()
    app = make_app(engine=engine)
    game_id, _ = new_game(app, human_color="black")
    before = engine.new_games
    app.think(game_id)
    reply = next(iter(chess.Board(str(app.game_state(game_id)["fen"])).legal_moves))
    app.move(game_id, {"uci": reply.uci()})
    app.think(game_id)
    assert engine.new_games - before == 2


class ExclusiveEngine(FakeEngine):
    """A fake engine that, like one UCI process, must never serve two commands at once."""

    def __init__(self) -> None:
        super().__init__()
        self.guard = threading.Lock()
        self.inside = 0
        self.entries = 0
        self.overlaps = 0
        self.app_lock: threading.Lock | None = None
        self.unlocked = 0  # calls made while the app's engine lock was free

    def _enter(self) -> None:
        with self.guard:
            self.inside += 1
            self.entries += 1
            if self.inside > 1:
                self.overlaps += 1
            if self.app_lock is not None and not self.app_lock.locked():
                self.unlocked += 1
        time.sleep(0.002)  # widen the window a concurrent caller would hit

    def _leave(self) -> None:
        with self.guard:
            self.inside -= 1

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        self._enter()
        try:
            return super().evaluate(
                board, perspective=perspective, moves=moves, multipv=multipv, time_limit=time_limit
            )
        finally:
            self._leave()

    def new_game(self) -> None:
        self._enter()
        try:
            super().new_game()
        finally:
            self._leave()


def test_every_engine_call_holds_the_global_engine_lock() -> None:
    # Single-threaded, so the lock is "locked" exactly when this very call path holds it.
    engine = ExclusiveEngine()
    app = make_app(engine=engine)
    engine.app_lock = app._engine_lock
    game_id, _ = new_game(app, human_color="black")  # CAIMEAgent() resets the engine
    app.think(game_id)
    reply = next(iter(chess.Board(str(app.game_state(game_id)["fen"])).legal_moves))
    app.move(game_id, {"uci": reply.uci()})
    app.think(game_id)
    app.analyse({"fen": chess.STARTING_FEN})
    assert engine.entries > 0
    assert engine.unlocked == 0


def test_concurrent_games_and_analyses_never_share_the_engine() -> None:
    engine = ExclusiveEngine()
    app = make_app(engine=engine)
    games = [new_game(app, human_color="black")[0] for _ in range(3)]
    workers: list[Callable[[], object]] = [partial(app.think, g) for g in games]
    workers += [partial(app.analyse, {"fen": chess.STARTING_FEN}) for _ in range(3)]
    workers += [partial(app.create_game, {}) for _ in range(4)]
    barrier = threading.Barrier(len(workers))
    errors: list[BaseException] = []

    def run_one(work: Callable[[], object]) -> None:
        barrier.wait()
        try:
            work()
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=run_one, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert engine.entries >= len(workers)
    assert engine.overlaps == 0


# ---------------------------------------------------------------- sessions and analysis
def test_game_ids_are_unguessable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A game id is the only key to a game: it must come from the secrets module (24 random
    # bytes, URL-safe base64), not from a counter or the seeded RNG.
    app = make_app(max_sessions=64)
    ids = [new_game(app)[0] for _ in range(20)]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{32}", game_id) for game_id in ids)
    asked: list[int] = []

    def token(nbytes: int) -> str:
        asked.append(nbytes)
        return "t" * 32

    monkeypatch.setattr(secrets, "token_urlsafe", token)  # the module cca.play.app uses
    assert new_game(app)[0] == "t" * 32
    assert asked == [24]


def test_sessions_are_evicted_least_recently_used_first() -> None:
    app = make_app(max_sessions=2)
    first, _ = new_game(app)
    second, _ = new_game(app)
    app.game_state(first)  # touch: now `second` is the least recently used
    third, _ = new_game(app)
    assert app.session_count() == 2
    assert api_status(lambda: app.game_state(second)) == 404
    app.game_state(first)
    app.game_state(third)
    assert api_status(lambda: app.game_state("short")) == 404
    assert api_status(lambda: app.game_state("A" * 32)) == 404


def test_analyse_is_a_reproducible_one_off_decision() -> None:
    app = make_app(base=AgentConfig(seed="lab"))
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    first = app.analyse({"fen": fen})
    second = app.analyse({"fen": fen})
    assert first["seed"] == "lab"
    assert first["turn"] == "white"
    decision = first["decision"]
    assert isinstance(decision, dict)
    move = decision["move"]
    assert isinstance(move, dict)
    assert chess.Move.from_uci(str(move["uci"])) in chess.Board(fen).legal_moves
    assert decision == second["decision"]
    assert app.analyse({"fen": fen, "seed": "other", "persona": "dubov"})["persona"] == "dubov"
    bad_bodies: list[dict[str, object]] = [
        {},
        {"fen": ""},
        {"fen": MATE_IN_ONE.replace(" w ", " x ")},
        {"fen": fen, "x": 1},
    ]
    for bad in bad_bodies:
        assert api_status(partial(app.analyse, bad)) == 400
    assert api_status(lambda: app.analyse({"fen": "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"})) == 400


@pytest.mark.engine
@pytest.mark.skipif(
    not os.environ.get("CCA_STOCKFISH"), reason="set CCA_STOCKFISH to a real Stockfish"
)
def test_a_seeded_game_replays_on_a_shared_real_engine() -> None:
    # Regression: other games and the position lab search on the same Stockfish between two
    # decisions of a game; what they left in its hash and history tables made a replay of a
    # seeded game diverge. Each decision now starts from a cleared engine (ucinewgame).
    from cca.engines.qre_human import QREHumanModel
    from cca.engines.stockfish import StockfishEngine

    engine = StockfishEngine(nodes=3000, threads=1, hash_mb=16)
    app = PlayApp(
        lambda: Engines(engine, QREHumanModel(engine), "qre"), PlaySettings(AgentConfig())
    )
    app.warm_up()
    lab_fen = "r1bq1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N1PN2/PP3PPP/R2QKB1R w KQ - 0 9"

    def play(disturbed: bool) -> list[object]:
        game_id, _ = new_game(app, seed="replay-1", human_color="black")
        decisions: list[object] = []
        for _ in range(3):
            if disturbed:  # another game and the lab use the engine in between
                other, _ = new_game(app, seed="someone-else", human_color="black")
                app.think(other)
                app.analyse({"fen": lab_fen, "seed": "lab"})
            result = app.think(game_id)
            decisions.append(result["decision"])
            board = chess.Board(str(state_of(result)["fen"]))
            reply = min(board.legal_moves, key=lambda m: m.uci())  # the same "human" each time
            app.move(game_id, {"uci": reply.uci()})
        return decisions

    try:
        assert play(disturbed=False) == play(disturbed=True)
    finally:
        app.close()


def test_engine_failures_become_503(monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    game_id, _ = new_game(app, human_color="black")
    session = app.session(game_id)

    def dead(*args: object, **kwargs: object) -> object:
        raise chess.engine.EngineTerminatedError("stockfish died")

    monkeypatch.setattr(session.agent, "choose", dead)
    assert api_status(lambda: app.think(game_id)) == 503
    assert app.health()[0] == 503

    app2 = make_app()
    game2, _ = new_game(app2, human_color="black")

    def flaky(*args: object, **kwargs: object) -> object:
        raise RuntimeError("engine returned no evaluation")

    monkeypatch.setattr(app2.session(game2).agent, "choose", flaky)
    assert api_status(lambda: app2.think(game2)) == 503
    assert app2.health()[0] == 200  # transient: still healthy


def test_close_stops_the_engine_even_if_warm_up_finishes_late() -> None:
    engine = ClosingEngine()
    app = make_app(engine=engine)
    app.close()
    assert engine.closed == 1
    late = ClosingEngine()
    pending = PlayApp(lambda: Engines(late, FakeHuman(), "qre"), PlaySettings(AgentConfig()))
    pending.close()
    pending.warm_up()
    assert late.closed == 1
    assert not pending.ready


# ---------------------------------------------------------------- HTTP: guards and headers
def _has_security_headers(resp: Response) -> None:
    assert resp.headers["Content-Security-Policy"] == CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_csp_is_strict() -> None:
    directives = dict(d.split(" ", 1) for d in CSP.split("; "))
    assert directives == {
        "default-src": "'self'",
        "script-src": "'self'",
        "connect-src": "'self'",
        "img-src": "'self' data:",
        "style-src": "'self'",
        "style-src-attr": "'unsafe-inline'",
        "object-src": "'none'",
        "base-uri": "'none'",
        "form-action": "'none'",
        "frame-ancestors": "'none'",
    }


def test_every_response_carries_the_security_headers(
    served: tuple[PlayServer, PlayApp],
) -> None:
    port = port_of(served[0])
    responses = [
        call(port, "GET", "/healthz"),
        call(port, "GET", "/"),
        call(port, "GET", "/static/js/main.js"),
        call(port, "GET", "/nope"),
        call(port, "POST", "/healthz", {}),
        call(port, "POST", "/api/games", raw=b"x", headers={"Content-Type": "text/plain"}),
        call(port, "GET", "/healthz", host="evil.example"),
        call(port, "PUT", "/api/games"),
    ]
    assert [r.status for r in responses] == [200, 200, 200, 404, 405, 415, 421, 501]
    for resp in responses:
        _has_security_headers(resp)
    assert responses[4].headers["Allow"] == "GET"
    assert responses[0].headers["Server"] == "cca-play"
    assert responses[0].json() == {"status": "ok", "version": __version__, "ready": True}
    for resp in responses[3:]:
        assert resp.headers["Content-Type"] == "application/json"
        assert b"<" not in resp.body  # errors are JSON, never HTML


def test_host_header_guard_blocks_dns_rebinding(served: tuple[PlayServer, PlayApp]) -> None:
    server, _ = served
    port = port_of(server)
    assert server.loopback  # the fixture binds 127.0.0.1
    # A loopback bind answers the contract's three names only (any port, or none).
    for host in ("127.0.0.1", "[::1]", "localhost", "LOCALHOST"):
        assert call(port, "GET", "/healthz", host=host).status == 200, host
    assert call(port, "GET", "/healthz", headers={"Host": "localhost"}, host=None).status == 200
    wildcard = ".".join(["0"] * 4)
    others = ("127.0.0.2", wildcard, "10.0.0.1", "[2001:db8::1]", "[::ffff:127.0.0.1]")
    for host in (*others, socket.gethostname()):
        resp = call(port, "GET", "/healthz", host=host)
        assert resp.status == 421, host
        assert "use localhost, 127.0.0.1 or [::1]" in str(resp.json()["error"])
    # A non-loopback bind (0.0.0.0 in a container, a LAN address) also answers IP literals and
    # this machine's name.
    server.loopback = False
    for host in (*others, "127.0.0.1", "localhost", socket.gethostname().upper()):
        assert call(port, "GET", "/healthz", host=host).status == 200, host
    server.loopback = True
    rebinding = (
        "evil.example",
        "127.0.0.1.evil.example",
        "localhost.evil",
        "::1",  # IPv6 without brackets is not a valid Host
        "[evil.example]",
        "localhost:80:80",
        "local host",
        "",
    )
    for loopback in (True, False):  # False: bound to 0.0.0.0, e.g. in a container
        server.loopback = loopback
        for host in rebinding:
            resp = call(port, "GET", "/api/info", headers={"Host": host}, host=None)
            assert resp.status == 421, host
            assert "rebinding" in str(resp.json()["error"])
        assert call(port, "GET", "/healthz", host=None).status == 421  # no Host at all


def test_host_names_are_matched_exactly() -> None:
    names = frozenset({"localhost", "mybox", "[::1]"})
    for host in ("localhost", "LocalHost:8765", "mybox", "MYBOX:1", "[::1]", "[::1]:80"):
        assert play_server.host_allowed(host, names, any_ip=False), host
    for host in ("1.2.3.4:80", "[fe80::1]:9"):
        assert not play_server.host_allowed(host, names, any_ip=False), host
        assert play_server.host_allowed(host, names, any_ip=True), host
    for host in ("mybox.evil", "evilmybox", "1.2.3.4.5", "[1.2.3]", "fe80::1", "localhost:"):
        assert not play_server.host_allowed(host, names, any_ip=True), host
    assert play_server.host_literal("127.0.0.2") == "127.0.0.2"
    assert play_server.host_literal("::1") == "[::1]"


def test_a_loopback_bind_also_answers_its_own_address() -> None:
    app = make_app()
    try:
        server = make_server("127.0.0.2", 0, app)  # --host 127.0.0.2
    except OSError:
        app.close()
        pytest.skip("127.0.0.2 is not a local address here (macOS without an alias)")
    try:
        assert server.loopback
        for host in ("127.0.0.2:8765", "127.0.0.1", "localhost:1", "[::1]"):
            assert server.host_ok(host), host
        for host in ("127.0.0.3", "10.0.0.1", socket.gethostname(), "evil.example"):
            assert not server.host_ok(host), host
    finally:
        server.server_close()
        app.close()


def test_posts_need_same_origin_and_json(served: tuple[PlayServer, PlayApp]) -> None:
    server, app = served
    port = port_of(server)
    same = f"http://127.0.0.1:{port}"
    assert call(port, "POST", "/api/games", {}, headers={"Origin": same}).status == 201
    for origin in ("http://evil.example", "null", f"http://localhost:{port}", "http://127.0.0.1"):
        assert call(port, "POST", "/api/games", {}, headers={"Origin": origin}).status == 403
    # The Origin must equal the page's own origin, not merely start like it.
    for host in ("127.0.0.1", "localhost"):
        for origin in (f"http://{host}:{port}.evil.example", f"http://{host}:{port}0"):
            resp = call(port, "POST", "/api/games", {}, headers={"Origin": origin}, host=host)
            assert resp.status == 403, origin
        own = {"Origin": f"http://{host}:{port}"}
        assert call(port, "POST", "/api/games", {}, headers=own, host=host).status == 201, host
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert call(port, "POST", "/api/games", {}, headers=cross).status == 403
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        resp = call(port, "POST", "/api/games", raw=b"{}", headers={"Content-Type": ctype})
        assert resp.status == 415
    ok = {"Content-Type": "application/json; charset=utf-8"}
    assert call(port, "POST", "/api/games", raw=b"{}", headers=ok).status == 201
    assert app.session_count() == 4


def test_no_other_socket_can_share_the_servers_port() -> None:
    # Measured on Windows 11: when both sockets set SO_REUSEADDR, a second one binds the same
    # port (and may receive the connections). The server keeps the option off on Windows; on
    # POSIX systems it cannot share a listening port anyway.
    app = make_app()
    server = make_server("127.0.0.1", 0, app)
    try:
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            shared = True
            try:
                other.bind(("127.0.0.1", port_of(server)))
            except OSError:  # EADDRINUSE on POSIX; WSAEADDRINUSE or WSAEACCES on Windows
                shared = False
            assert not shared, "another socket bound the server's port"
    finally:
        server.server_close()
        app.close()


def test_a_silent_client_cannot_hold_a_handler_thread_forever() -> None:
    # The socket timeout of every connection (the 408 test above shortens it to be quick).
    timeout = play_server.PlayHandler.timeout
    assert isinstance(timeout, (int, float))
    assert 0 < timeout <= 60


def test_request_body_limits(served: tuple[PlayServer, PlayApp]) -> None:
    port = port_of(served[0])
    big = b'{"fen": "' + b"x" * MAX_BODY + b'"}'
    assert call(port, "POST", "/api/analyse", raw=big).status == 413
    exact = b'{"seed": "' + b"x" * (MAX_BODY - 12) + b'"}'
    assert len(exact) == MAX_BODY
    assert call(port, "POST", "/api/games", raw=exact).status == 400  # parsed, then rejected
    # No body is sent with a missing or invalid length header (it could not be drained).
    assert call(port, "POST", "/api/games", raw=b"", headers={"Content-Length": None}).status == 411
    assert call(port, "POST", "/api/games", raw=b"", headers={"Content-Length": "x"}).status == 400
    assert call(port, "POST", "/api/games", raw=b"", headers={"Content-Length": "-1"}).status == 400
    chunked = {"Transfer-Encoding": "chunked"}
    assert call(port, "POST", "/api/games", raw=b"{}", headers=chunked).status == 411
    assert call(port, "POST", "/api/games", raw=b"{not json").status == 400
    assert call(port, "POST", "/api/games", raw=b"[1]").status == 400
    assert call(port, "POST", "/api/games", raw=b'{"seed": NaN}').status == 400
    deep = b'{"seed": ' + b"[" * 30000 + b"]" * 30000 + b"}"  # RecursionError in json.loads
    assert len(deep) < MAX_BODY
    resp = call(port, "POST", "/api/games", raw=deep)
    assert (resp.status, resp.json()) == (400, {"error": "invalid JSON: nested too deeply"})
    huge = {"Content-Length": str(10 * MAX_BODY)}
    assert call(port, "POST", "/api/games", raw=b"{}", headers=huge).status == 413


def test_a_body_shorter_than_announced_times_out_with_408(
    served: tuple[PlayServer, PlayApp],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(play_server.PlayHandler, "timeout", 0.3)  # the socket read timeout
    port = port_of(served[0])
    started = time.monotonic()
    resp = call(port, "POST", "/api/games", raw=b"{}", headers={"Content-Length": "100"})
    assert (resp.status, resp.json()) == (408, {"error": "timed out waiting for the request body"})
    assert time.monotonic() - started < 10
    assert capsys.readouterr().err == ""  # handled, not a 500 with a traceback
    assert served[1].session_count() == 0


def _raw_exchange(port: int, request: bytes) -> tuple[bytes, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def test_malformed_request_lines_get_json_errors(served: tuple[PlayServer, PlayApp]) -> None:
    port = port_of(served[0])
    head, body = _raw_exchange(port, b"GARBAGE<script>\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 400")
    assert b"Content-Security-Policy" in head
    assert b"application/json" in head
    assert "GARBAGE<script>" in json.loads(body)["error"]  # quoted as data...
    assert b"<" not in body  # ...and never as markup
    head, body = _raw_exchange(port, b"GET /api/info\r\n\r\n")  # HTTP/0.9: no headers at all
    assert head.startswith(b"HTTP/1.0 400")
    assert b"Content-Security-Policy" in head
    assert "HTTP/0.9" in json.loads(body)["error"]


def test_api_over_http_end_to_end(served: tuple[PlayServer, PlayApp]) -> None:
    port = port_of(served[0])
    created = call(port, "POST", "/api/games", {"human_color": "white"})
    assert created.status == 201
    game_id = str(created.json()["game_id"])
    base = f"/api/games/{game_id}"
    assert call(port, "GET", base).json()["to_move"] == "human"
    assert call(port, "POST", base + "/move", {"uci": "e2e4"}).status == 200
    bad = call(port, "POST", base + "/move", {"uci": "e7e5"})
    assert bad.status == 400
    assert "not your turn" in str(bad.json()["error"])
    think = call(port, "POST", base + "/think", {})
    assert think.status == 200
    assert set(think.json()) == {"move", "decision", "compute_s", "reveal_in_ms", "state"}
    assert call(port, "POST", base + "/think", {}).status == 409
    pgn = call(port, "GET", base + "/pgn")
    assert pgn.status == 200
    assert pgn.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert b"1. e4" in pgn.body
    assert call(port, "GET", base + "/decisions").json()["decisions"]
    assert call(port, "POST", base + "/undo", {}).status == 200
    resigned = call(port, "POST", base + "/resign", {}).json()["state"]
    assert isinstance(resigned, dict)
    assert resigned["reason"] == "resignation"
    assert call(port, "GET", base + "/move").status == 405
    assert call(port, "POST", base + "/pgn", {}).status == 405
    assert call(port, "GET", "/api/games/" + "x" * 8).status == 404
    assert call(port, "GET", "/api/games").status == 405
    analysed = call(port, "POST", "/api/analyse", {"fen": chess.STARTING_FEN})
    assert analysed.status == 200
    assert analysed.json()["turn"] == "white"
    info = call(port, "GET", "/api/info")
    assert info.status == 200
    assert info.json()["ready"] is True


def test_http_errors_carry_state_when_the_game_is_over(served: tuple[PlayServer, PlayApp]) -> None:
    port = port_of(served[0])
    game_id = str(call(port, "POST", "/api/games", {"fen": MATE_IN_ONE}).json()["game_id"])
    call(port, "POST", f"/api/games/{game_id}/move", {"uci": "a1a8"})
    resp = call(port, "POST", f"/api/games/{game_id}/resign", {})
    assert resp.status == 409
    state = resp.json()["state"]
    assert isinstance(state, dict)
    assert state["reason"] == "checkmate"


def test_non_finite_numbers_are_sent_as_null(
    served: tuple[PlayServer, PlayApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, app = served
    monkeypatch.setattr(app, "info", lambda: {"x": math.nan, "y": [math.inf]})
    resp = call(port_of(server), "GET", "/api/info")
    assert resp.status == 200
    assert resp.json() == {"x": None, "y": [None]}


def test_unexpected_errors_are_500_without_a_traceback(
    served: tuple[PlayServer, PlayApp],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server, app = served

    def boom() -> dict[str, object]:
        raise ZeroDivisionError("secret detail")

    monkeypatch.setattr(app, "info", boom)
    resp = call(port_of(server), "GET", "/api/info")
    assert resp.status == 500
    assert b"secret detail" not in resp.body
    assert "ZeroDivisionError" in capsys.readouterr().err


# ---------------------------------------------------------------- static files
@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/static/js/main.js", "text/javascript; charset=utf-8"),
        ("/static/css/app.css", "text/css; charset=utf-8"),
        ("/static/img/markers.svg", "image/svg+xml"),
        ("/static/vendor/MANIFEST.json", "application/json"),
        ("/static/vendor/cburnett-commons/LICENSE.txt", "text/plain; charset=utf-8"),
        ("/static/vendor/cm-chessboard/LICENSE", "text/plain; charset=utf-8"),
        ("/static/vendor/chess.js/dist/esm/chess.js", "text/javascript; charset=utf-8"),
    ],
)
def test_static_files_have_explicit_mime_types(
    served: tuple[PlayServer, PlayApp], path: str, ctype: str
) -> None:
    resp = call(port_of(served[0]), "GET", path)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == ctype
    assert resp.headers["Cache-Control"] == "no-cache"
    _has_security_headers(resp)


@pytest.mark.parametrize(
    "path",
    [
        "/static/js/missing.js",
        "/static/../server.py",
        "/static/../app.py",
        "/static/%2e%2e/server.py",
        "/static/..%2f..%2fserver.py",
        "/static/js/../../server.py",
        "/static/js/..%5c..%5cserver.py",
        "/static/js\\main.js",
        "/static/js/main.js%00.css",
        "/static/./index.html",
        "/static//index.html",
        "/static/js",
        "/static/vendor/chess.js/.gitignore",
        "/static/vendor/chess.js/dist/esm/chess.js.map",
        "/static/C:/Windows/win.ini",
        "/static/js/main.js:stream",
    ],
)
def test_static_paths_cannot_escape_or_reach_unlisted_types(
    served: tuple[PlayServer, PlayApp], path: str
) -> None:
    assert call(port_of(served[0]), "GET", path).status == 404


def test_static_lookup_stays_inside_the_static_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "static" / "js").mkdir(parents=True)
    (pkg / "static" / "js" / "ok.js").write_text("ok", encoding="utf-8")
    (pkg / "static" / "notes.py").write_text("x", encoding="utf-8")
    (pkg / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr("cca.play.server.resources.files", lambda name: pkg)
    assert static_file("js/ok.js") == (b"ok", "text/javascript; charset=utf-8")
    assert static_file("../secret.txt") is None
    assert static_file("js/../../secret.txt") is None
    assert static_file("notes.py") is None
    assert static_file("") is None


# ---------------------------------------------------------------- serving, binding, warnings
def test_run_prints_the_url_opens_the_browser_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = ClosingEngine()
    app = PlayApp(lambda: Engines(engine, FakeHuman(), "qre"), PlaySettings(AgentConfig()))
    opened: list[str] = []
    monkeypatch.setattr("cca.play.server.webbrowser.open", opened.append)
    out, err = io.StringIO(), io.StringIO()
    handlers: list[object] = []

    ready: list[bool] = []

    def on_listen(server: PlayServer) -> None:
        handlers.append(signal.getsignal(signal.SIGTERM))
        ready.append(app.wait_ready(10))  # engines warm up in the background
        threading.Thread(target=server.shutdown, daemon=True).start()

    before = signal.getsignal(signal.SIGTERM)
    assert run(app, port=0, out=out, err=err, on_listen=on_listen) == 0
    url = out.getvalue().strip().removeprefix("CCA simulator: ")
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", url)
    assert opened == [url]
    assert err.getvalue() == ""
    assert ready == [True]
    assert engine.closed == 1  # engines stopped with the server
    assert handlers == [play_server._raise_interrupt]  # docker stop -> KeyboardInterrupt
    assert signal.getsignal(signal.SIGTERM) == before


def test_run_warns_when_reachable_from_other_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(play_server, "is_loopback", lambda host: False)
    app = PlayApp(lambda: Engines(FakeEngine(), FakeHuman(), "qre"), PlaySettings(AgentConfig()))
    err = io.StringIO()

    def stop(server: PlayServer) -> None:
        raise KeyboardInterrupt

    assert run(app, port=0, open_browser=False, out=io.StringIO(), err=err, on_listen=stop) == 0
    text = err.getvalue()
    assert text.startswith(exposure_warning("127.0.0.1"))
    assert "no authentication" in text
    assert "(DNS-rebinding guard)" in text  # says which Host names are still answered
    assert "cca play: stopped" in text


@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1"])  # port in use; not our address
def test_run_reports_an_address_it_cannot_bind(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    built: list[bool] = []

    def factory() -> Engines:
        built.append(True)
        return Engines(FakeEngine(), FakeHuman(), "qre")

    app = PlayApp(factory, PlaySettings(AgentConfig()))
    opened: list[str] = []
    monkeypatch.setattr("cca.play.server.webbrowser.open", opened.append)
    out, err = io.StringIO(), io.StringIO()
    with socket.socket() as busy:  # 192.0.2.1 is TEST-NET-1 (RFC 5737): no DNS, never local
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = int(busy.getsockname()[1])
        assert run(app, host=host, port=port, out=out, err=err) == 1
    assert err.getvalue().startswith(f"error: cannot listen on {host}:{port}: ")
    assert "Traceback" not in err.getvalue()
    assert out.getvalue() == ""  # no URL: nothing listens
    assert opened == []
    assert built == []  # the engines were never started


def test_an_idle_connection_does_not_hold_up_the_shutdown() -> None:
    # Handler threads are daemons: a browser's idle keep-alive socket must not make Ctrl+C or
    # `docker stop` wait for the 30 s request timeout.
    app = make_app()
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port_of(server)), timeout=10):
            time.sleep(0.3)  # a handler thread now waits for a request line that never comes
            started = time.monotonic()
            server.shutdown()
            server.server_close()
            assert time.monotonic() - started < 5
    finally:
        server.shutdown()
        thread.join(5)
        app.close()


def test_server_ignores_clients_that_disconnect(
    served: tuple[PlayServer, PlayApp], capsys: pytest.CaptureFixture[str]
) -> None:
    server, _ = served

    def fail(exc: Exception) -> None:
        raise exc

    request = socket.socket()
    try:
        try:
            fail(ConnectionResetError("gone"))
        except ConnectionResetError:
            server.handle_error(request, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            fail(LookupError("real bug"))
        except LookupError:
            server.handle_error(request, ("127.0.0.1", 1))
        assert "LookupError: real bug" in capsys.readouterr().err
    finally:
        request.close()


def test_defensive_helpers() -> None:
    board = chess.Board()
    assert play_serialize.san(board, "e2e4") == "e4"
    assert play_serialize.san(board, "e2e5") == "e2e5"  # illegal: shown as UCI
    assert play_serialize.san(board, "zz") == "zz"
    app = make_app()
    game_id, _ = new_game(app)
    session = app.session(game_id)
    with pytest.raises(RuntimeError, match="untimed"):
        session._left(chess.WHITE, 0.0)
    with pytest.raises(TypeError):
        play_game._int("1")
    odd = {"think_time": math.nan, "q_opt": None, "trap_value": math.inf, "stress": 0.2}
    assert play_game._pgn_comment(odd) == "cca stress=0.20"  # no "nan" or "inf" in a PGN


def test_sigterm_handler_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        play_server._raise_interrupt(signal.SIGTERM, None)


def test_loopback_detection_and_urls() -> None:
    for host in ("127.0.0.1", "127.8.0.1", "::1", "[::1]", "localhost", "LOCALHOST"):
        assert is_loopback(host)
    wildcard = ".".join(["0"] * 4)
    for host in (wildcard, "::", "192.168.1.2", "example.com", ""):
        assert not is_loopback(host)

    class Fake:
        def __init__(self, host: str, family: socket.AddressFamily) -> None:
            self.server_address = (host, 8765)
            self.address_family = family

    def url(host: str, family: socket.AddressFamily) -> str:
        return server_url(cast("PlayServer", Fake(host, family)))

    assert url(wildcard, socket.AF_INET) == "http://127.0.0.1:8765/"
    assert url("10.1.2.3", socket.AF_INET) == "http://10.1.2.3:8765/"
    assert url("::", socket.AF_INET6) == "http://[::1]:8765/"
    assert url("::1", socket.AF_INET6) == "http://[::1]:8765/"


def test_ipv6_loopback_server() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    app = make_app()
    try:
        server = make_server("::1", 0, app)
    except OSError:
        pytest.skip("IPv6 loopback not available")
    try:
        assert server.loopback
        assert server_url(server).startswith("http://[::1]:")
    finally:
        server.server_close()
        app.close()


# ---------------------------------------------------------------- vendored files (supply chain)
MANIFEST = json.loads((VENDOR / "MANIFEST.json").read_text(encoding="utf-8"))
# Only this helper file may sit next to the vendored files without a manifest entry.
UNLISTED_OK = {"MANIFEST.json", "chess.js/.gitignore"}


def _vendored_on_disk() -> set[str]:
    return {p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*") if p.is_file()}


@pytest.mark.parametrize("entry", MANIFEST["files"], ids=lambda e: e["path"])
def test_vendored_file_matches_its_pinned_hash(entry: dict[str, object]) -> None:
    data = (VENDOR / str(entry["path"])).read_bytes()
    assert len(data) == entry["bytes"]
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert entry["license"] in {"MIT", "BSD-2-Clause", "BSD-3-Clause"}
    assert entry["source_url"] or entry.get("derived_from") or entry.get("note")


def test_manifest_lists_exactly_the_vendored_files() -> None:
    listed = {str(f["path"]) for f in MANIFEST["files"]}
    assert _vendored_on_disk() - UNLISTED_OK == listed
    for pkg in MANIFEST["packages"]:
        assert pkg["license_file"] in listed  # each licence ships next to its package
    forbidden = re.compile(r"(^|/)(\.claude|pieces)(/|$)|markers/markers\.svg$|\.map$")
    assert not [p for p in _vendored_on_disk() if forbidden.search(p)]
    derived = next(f for f in MANIFEST["files"] if f["path"].endswith("cburnett.svg"))
    assert len(derived["derived_from"]) == 12


def test_vendored_javascript_has_no_network_or_dynamic_code() -> None:
    risky = re.compile(
        r"fetch\(|WebSocket|EventSource|sendBeacon|\beval\(|new Function|import\(|"
        r"document\.cookie|localStorage|sessionStorage|indexedDB|importScripts|postMessage"
    )
    urls = re.compile(r"https?://[^\s\"'`)]+")
    allowed_urls = {  # all in comments or XML namespace strings (audited by hand)
        "https://peggyjs.org/",
        "https://github.com/jhlywa/chess.js/issues/230",
        "http://www.chessclub.com/help/PGN-spec",
        "https://shaack.com",
        "https://github.com/shaack/cm-chessboard",
        "https://github.com/shaack/cm-chessboard/issues/47",
        "https://medium.com/@karenmarkosyan/how-to-manage-promises-into-dynamic-queue-with-vanilla-javascript-9d0d1f8d4df5",
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/",
        "https://…",
    }
    xhr_files = []
    for path in (p for p in VENDOR.rglob("*.js") if p.is_file()):
        text = path.read_text(encoding="utf-8")
        assert not risky.search(text), path
        assert set(urls.findall(text)) <= allowed_urls, path
        if "XMLHttpRequest" in text:
            xhr_files.append(path.relative_to(VENDOR).as_posix())
    # The single network use: cm-chessboard fetching its SVG sprites from our own origin.
    assert xhr_files == ["cm-chessboard/src/view/ChessboardView.js"]


def test_own_frontend_stays_on_our_origin_and_avoids_markup_injection() -> None:
    for path in (STATIC / "js").glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for token in ("innerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write"):
            assert token not in text, (path, token)
        found = set(re.findall(r"https?://[^\s\"'`)]+", text))
        assert found <= {"http://www.w3.org/2000/svg"}, path
    fetches = [p.name for p in (STATIC / "js").glob("*.js") if "fetch(" in p.read_text("utf-8")]
    assert fetches == ["api.js"]
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # no inline scripts
    assert not re.search(r"\son[a-z]+\s*=", html)  # no inline event handlers
    assert not re.search(r"<style|\sstyle=", html)
    refs = re.findall(r'(?:src|href)="([^"]*)"', html)
    assert refs
    assert all(r.startswith(("/static/", "#")) for r in refs), refs


def test_i18n_dictionaries_cover_every_key() -> None:
    text = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    en_block = text.split("  en: {", 1)[1].split("\n  },\n  vi: {", 1)[0]
    vi_block = text.split("  vi: {", 1)[1].split("\n  }\n}", 1)[0]
    key = re.compile(r"^\s{4}([a-z0-9_]+):", re.MULTILINE)
    en, vi = set(key.findall(en_block)), set(key.findall(vi_block))
    assert en == vi
    assert len(en) > 100
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-title|-aria|-placeholder)?="([a-z0-9_]+)"', html))
    for path in (STATIC / "js").glob("*.js"):
        used |= set(re.findall(r'\bt\("([a-z0-9_]+)"\s*[,)]', path.read_text(encoding="utf-8")))
    assert used - en == set()


def _i18n_strings() -> dict[str, dict[str, str]]:
    """The ``STRINGS`` dictionary of i18n.js, parsed from its one-string-per-line layout."""
    text = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    blocks = {
        "en": text.split("  en: {", 1)[1].split("\n  },\n  vi: {", 1)[0],
        "vi": text.split("  vi: {", 1)[1].split("\n  }\n}", 1)[0],
    }
    entry = re.compile(r'^\s{4}([a-z0-9_]+): ("(?:[^"\\]|\\.)*"),?$', re.MULTILINE)
    key = re.compile(r"^\s{4}([a-z0-9_]+):", re.MULTILINE)
    out: dict[str, dict[str, str]] = {}
    for lang, block in blocks.items():
        out[lang] = {k: json.loads(v) for k, v in entry.findall(block)}
        assert set(out[lang]) == set(key.findall(block))  # every entry parsed
    return out


def test_latent_signals_are_labelled_honestly_in_both_languages() -> None:
    # ADR-0005: stress, drive, opponent stress and chaos are virtual control signals inspired
    # by, not models of, physiology. The page must say so wherever it shows or explains them.
    strings = _i18n_strings()
    required = {
        "en": {
            "honest_label": "virtual, latent control signals inspired by, not models of,"
            " physiology",
            "g_latent": "Virtual, latent control signals inspired by, not models of, physiology",
            "series_stress_hint": "virtual, inspired by stress research; not a physiological model",
        },
        "vi": {
            "honest_label": "tín hiệu điều khiển ảo, tiềm ẩn, lấy cảm hứng từ sinh lý học chứ"
            " không phải mô hình sinh lý",
            "g_latent": "tín hiệu điều khiển ảo, tiềm ẩn, lấy cảm hứng từ sinh lý học chứ không"
            " mô hình hóa sinh lý",
            "series_stress_hint": "không phải mô hình sinh lý",
        },
    }
    for lang, wanted in required.items():
        for key, phrase in wanted.items():
            assert phrase in strings[lang][key], (lang, key)
        assert "ADR-0005" in strings[lang]["honest_label"]
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for key in ("honest_label", "g_latent"):
        assert html.count(f'data-i18n="{key}"') == 1, key
    charts = (STATIC / "js" / "charts.js").read_text(encoding="utf-8")
    assert 'hint: "series_stress_hint"' in charts  # the stress chart's tooltip


def test_page_fallback_texts_are_the_english_strings() -> None:
    # The text inside each data-i18n element is what shows before i18n.js runs; it must be the
    # English string itself (it had drifted for the honest label and two glossary entries).
    en = _i18n_strings()["en"]
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    pairs = re.findall(r'<([a-z0-9]+)\b[^>]*\sdata-i18n="([a-z0-9_]+)"[^>]*>([^<]*)</\1>', html)
    assert len(pairs) > 60
    for _, key, text in pairs:
        assert html_lib.unescape(text) == en[key], key


# The About panel's credits are REQUIRED by the vendored packages' licences (MIT, BSD-2-Clause,
# BSD-3-Clause): name, licence, copyright holder and a link to the licence text.
CREDITS = {  # package -> (copyright holder as credited, version shown or None)
    "cm-chessboard": ("Stefan Haack", "8.14.2"),
    "chess.js": ("Jeff Hlywa", "1.4.0"),
    "cburnett-commons": ("Colin M.L. Burnett", None),
}


def test_about_panel_credits_every_vendored_package() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    credits = html.split('<ul class="credits">', 1)[1].split("</ul>", 1)[0]
    items = re.findall(r"<li>(.*?)</li>", credits, re.DOTALL)
    packages = MANIFEST["packages"]
    assert {p["name"] for p in packages} == set(CREDITS)
    for package in packages:
        holder, version = CREDITS[package["name"]]
        href = f'href="/static/vendor/{package["license_file"]}"'
        matching = [li for li in items if href in li]
        assert len(matching) == 1, package["name"]
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", matching[0]))
        assert package["license"] in text, package["name"]
        assert holder in text, package["name"]
        assert holder in package["copyright"], package["name"]
        if version is not None:
            assert version == package["version"], package["name"]
            assert version in text, package["name"]
        served = static_file(f"vendor/{package['license_file']}")
        assert served is not None
        assert served[1] == "text/plain; charset=utf-8"


NODE = shutil.which("node")


def _node(probe: str, **modules: Path) -> object:
    """Run an ES-module probe under Node.js (``NAME_URL`` placeholders -> module file URLs)."""
    assert NODE is not None
    for name in sorted(modules, key=len, reverse=True):  # CHESSBOARD_URL before BOARD_URL
        probe = probe.replace(name, json.dumps(modules[name].as_uri()))
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


CLOCK_PROBE = """
import {remainingMs} from CLOCK_URL
const s = {game_over: false,
           clocks: {white_ms: 900000, black_ms: 890000, running: "white", starts_in_ms: 12000}}
console.log(JSON.stringify([
  remainingMs(s, "white", 1000, 1000),   // just received: the human's clock is still paused
  remainingMs(s, "white", 1000, 13000),  // CCA's move shown after the 12 s delay: it starts
  remainingMs(s, "white", 1000, 15000),  // 2 s later
  remainingMs(s, "black", 1000, 15000),  // the other clock does not run
  remainingMs({...s, game_over: true}, "white", 1000, 15000),
  remainingMs(s, "white", 1000, 1e9),    // never below zero
]))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_clock_display_counts_from_the_response_not_from_the_reveal() -> None:
    # With the human-like delay, CCA's reply is shown reveal_in_ms after it arrived; the
    # human's clock starts starts_in_ms after the ARRIVAL. Anchoring at display time froze the
    # displayed clock for the whole delay (measured: ~12 s behind the server).
    out = _node(CLOCK_PROBE, CLOCK_URL=STATIC / "js" / "clock.js")
    assert out == [900000, 900000, 898000, 890000, 900000, 0]
    # That the arrival time is taken before the delay is checked by the flow test below.


# Drives static/js/flow.js (the page's request ordering) with a fake API whose responses the
# probe settles one by one, in the orders that used to freeze the page.
FLOW_PROBE = """
import {createFlow, canMove} from FLOW_URL

const deferred = () => {
  let resolve, reject
  const promise = new Promise((a, b) => { resolve = a; reject = b })
  return {promise, resolve, reject}
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
const st = (tag, toMove, extra = {}) =>
  ({tag, to_move: toMove, game_over: false, human_color: "white", moves: [], history: [], ...extra})
const fail = (message) => Object.assign(new Error(message), {status: 400, state: null})

function harness() {
  const requests = []
  const log = []
  const clock = {t: 0}
  const ui = {state: null, gameId: null, gen: 0, busy: false, thinking: false, starting: false,
              labBusy: false, reveal: null, review: null, lab: null, decisions: new Map()}
  const call = (method) => (path, body) => {
    const d = deferred()
    requests.push({method, path, body, d})
    return d.promise
  }
  const sent = (method, path) =>
    requests.filter((r) => r.method === method && r.path === path).map((r) => r.body)
  const take = (method, path) => {
    const i = requests.findIndex((r) => r.method === method && r.path === path)
    if (i < 0) throw new Error("no pending " + method + " " + path)
    return requests.splice(i, 1)[0].d
  }
  const said = []  // the aria-live announcements
  const shown = {fen: "F0"}  // the position the board shows
  const view = {
    applyState(state, opts) {
      ui.state = state
      const at = opts && opts.receivedAt !== undefined ? "@" + opts.receivedAt : ""
      log.push("apply:" + state.tag + at)
    },
    drawBoard() { log.push("draw") },
    render() {}, updateInput() {}, renderPlayers() {},
    showError(err) { log.push("error:" + err.message) },
    displayedFen() { return shown.fen },
    remember() {}, setOrientation() {},
    announceHuman(move) { said.push("you:" + move.san) },
    announceCca(res) { said.push("cca:" + (res.move ? res.move.san : "-")) },
    announceOver(state) { said.push("over:" + state.tag) }
  }
  const sleeps = []
  const sleep = () => { const d = deferred(); sleeps.push(d); return d.promise }
  const api = {get: call("GET"), post: call("POST")}
  const flow = createFlow({ui, api, view, sleep, now: () => clock.t})
  const pending = () => requests.map((r) => r.method + " " + r.path)
  return {ui, flow, take, log, sleeps, clock, pending, sent, said, shown}
}
const out = {}

{ // The move sent is the one played, with the promotion piece the human chose.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "human"))
  h.flow.humanMove({from: "a7", to: "a8", promotion: "n", san: "a8=N"})
  const first = h.sent("POST", "/api/games/g1/move")
  h.take("POST", "/api/games/g1/move").resolve({state: st("g1:a8", "human")})
  await tick()
  h.flow.humanMove({from: "e2", to: "e4", san: "e4"})
  out.move_bodies = [...first, ...h.sent("POST", "/api/games/g1/move")]
}

{ // The verifier's reproduction: a refused new game while CCA thinks.
  const h = harness()
  h.flow.startSession("g1", st("g1:e4", "cca", {moves: [{}]}))
  const think = h.take("POST", "/api/games/g1/think")
  const ng = h.flow.newGame({seed: "a b"}).catch((err) => h.log.push("rejected:" + err.message))
  h.take("POST", "/api/games").reject(fail("bad seed"))
  await ng
  const during = {thinking: h.ui.thinking, starting: h.ui.starting, canMove: canMove(h.ui)}
  think.resolve({move: {uci: "e7e5", san: "e5"}, decision: {d: 1}, reveal_in_ms: 0,
                 state: st("g1:e5", "human", {moves: [{}, {}]})})
  await tick()
  out.refused_new_game_during_think = {during, thinking: h.ui.thinking, canMove: canMove(h.ui),
                                       decision: h.ui.decisions.has(1), log: h.log}
}
{ // ... or while CCA's emulated think time runs; the clock anchor stays the arrival time.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "cca"))
  h.clock.t = 1000
  h.take("POST", "/api/games/g1/think").resolve({move: {uci: "e2e4", san: "e4"}, decision: {},
    reveal_in_ms: 3000, state: st("g1:e4", "human", {moves: [{}]})})
  await tick()
  const ng = h.flow.newGame({}).catch(() => {})
  h.take("POST", "/api/games").reject(fail("bad fen"))
  await ng
  h.clock.t = 4000
  h.sleeps[0].resolve()
  await tick()
  out.refused_new_game_during_reveal = {thinking: h.ui.thinking, reveal: h.ui.reveal, log: h.log}
}
{ // A new game that starts drops the old game's late reply.
  const h = harness()
  h.flow.startSession("g1", st("g1:e4", "cca", {moves: [{}]}))
  const think = h.take("POST", "/api/games/g1/think")
  const ng = h.flow.newGame({})
  h.take("POST", "/api/games").resolve({game_id: "g2", state: st("g2:start", "human")})
  await ng
  think.resolve({move: {uci: "e7e5", san: "e5"}, decision: {}, reveal_in_ms: 0,
                 state: st("g1:e5", "human")})
  await tick()
  out.new_game_drops_old_think = {game: h.ui.gameId, state: h.ui.state.tag,
    thinking: h.ui.thinking, canMove: canMove(h.ui), decisions: h.ui.decisions.size}
}
{ // A refused new game while the human's move is in flight.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "human"))
  h.flow.humanMove({from: "e2", to: "e4", san: "e4"})
  const move = h.take("POST", "/api/games/g1/move")
  const ng = h.flow.newGame({}).catch(() => {})
  h.take("POST", "/api/games").reject(fail("bad fen"))
  await ng
  const during = {busy: h.ui.busy, canMove: canMove(h.ui)}
  move.resolve({state: st("g1:e4", "cca", {moves: [{}]})})
  await tick()
  out.refused_new_game_during_move = {during, busy: h.ui.busy, state: h.ui.state.tag,
                                      thinking: h.ui.thinking, pending: h.pending()}
}
{ // A new game that starts while the human's move is in flight.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "human"))
  h.flow.humanMove({from: "e2", to: "e4", san: "e4"})
  const move = h.take("POST", "/api/games/g1/move")
  const ng = h.flow.newGame({})
  h.take("POST", "/api/games").resolve({game_id: "g2", state: st("g2:start", "human")})
  await ng
  move.resolve({state: st("g1:e4", "cca", {moves: [{}]})})
  await tick()
  out.new_game_drops_old_move = {busy: h.ui.busy, state: h.ui.state.tag,
                                 canMove: canMove(h.ui), pending: h.pending()}
}
{ // A refused take-back keeps the context: a pending refresh still applies.
  const h = harness()
  h.flow.startSession("g1", st("g1:a", "human", {moves: [{}, {}]}))
  h.flow.refresh()
  const refresh = h.take("GET", "/api/games/g1")
  const undo = h.flow.undo()
  h.take("POST", "/api/games/g1/undo").reject(fail("the game is over"))
  await undo
  refresh.resolve(st("g1:b", "human", {moves: [{}, {}]}))
  await tick()
  out.refused_undo = {state: h.ui.state.tag, busy: h.ui.busy}
}
{ // A done take-back drops the refresh sent before it.
  const h = harness()
  h.flow.startSession("g1", st("g1:a", "human", {moves: [{}, {}]}))
  h.flow.refresh()
  const refresh = h.take("GET", "/api/games/g1")
  const undo = h.flow.undo()
  h.take("POST", "/api/games/g1/undo").resolve({state: st("g1:undone", "human")})
  await undo
  refresh.resolve(st("g1:stale", "human", {moves: [{}, {}]}))
  await tick()
  out.undo_drops_stale_refresh = {state: h.ui.state.tag, busy: h.ui.busy}
}
{ // A move the server never took (no state in the error): the board is redrawn from the
  // refreshed state, which puts back the piece the board had moved locally.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "human"))
  h.flow.humanMove({from: "e2", to: "e4", san: "e4"})
  const offline = Object.assign(new Error("offline"), {status: 0, state: null})
  h.take("POST", "/api/games/g1/move").reject(offline)
  await tick()
  h.take("GET", "/api/games/g1").resolve(st("g1:again", "human"))
  await tick()
  out.lost_move_redraws = {busy: h.ui.busy, log: h.log, pending: h.pending()}
}
{ // A new game started during CCA's emulated think time: the old reply is dropped after it.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "cca"))
  h.take("POST", "/api/games/g1/think").resolve({move: {uci: "e2e4", san: "e4"}, decision: {d: 1},
    reveal_in_ms: 3000, state: st("g1:e4", "human", {moves: [{}]})})
  await tick()
  const ng = h.flow.newGame({})
  h.take("POST", "/api/games").resolve({game_id: "g2", state: st("g2:start", "human")})
  await ng
  h.sleeps[0].resolve()
  await tick()
  out.new_game_during_reveal = {state: h.ui.state.tag, decisions: h.ui.decisions.size,
    thinking: h.ui.thinking, reveal: h.ui.reveal, canMove: canMove(h.ui), said: h.said}
}
{ // A move that ends the game is announced as the end, and CCA is not asked to move.
  const h = harness()
  h.flow.startSession("g1", st("g1:start", "human"))
  h.flow.humanMove({from: "a1", to: "a8", san: "Ra8#"})
  h.take("POST", "/api/games/g1/move").resolve({state: st("g1:mate", null, {game_over: true})})
  await tick()
  out.game_over_announced = {said: h.said, pending: h.pending()}
}
// The position lab.
const labState = (tag) => st(tag, "human", {cca: {persona: "tal", elo: 2100, opponent_elo: 1200}})
const analysis = {decision: {d: 1}, turn: "white"}
{ // Its answer is kept for the position still shown ...
  const h = harness()
  h.flow.startSession("g1", labState("g1:a"))
  h.shown.fen = "F1"
  const lab = h.flow.lab()
  const during = {labBusy: h.ui.labBusy, canMove: canMove(h.ui)}
  const body = h.sent("POST", "/api/analyse")
  h.take("POST", "/api/analyse").resolve(analysis)
  await lab
  out.lab_kept = {during, body, lab: h.ui.lab, labBusy: h.ui.labBusy, canMove: canMove(h.ui)}
}
{ // ... not once the board shows another position (navigation) ...
  const h = harness()
  h.flow.startSession("g1", labState("g1:a"))
  const lab = h.flow.lab()
  h.shown.fen = "F2"
  h.take("POST", "/api/analyse").resolve(analysis)
  await lab
  out.lab_other_position = {lab: h.ui.lab, labBusy: h.ui.labBusy}
}
{ // ... nor in another game context (a new game from the same position) ...
  const h = harness()
  h.flow.startSession("g1", labState("g1:a"))
  const lab = h.flow.lab()
  const ng = h.flow.newGame({})
  h.take("POST", "/api/games").resolve({game_id: "g2", state: labState("g2:start")})
  await ng
  h.take("POST", "/api/analyse").resolve(analysis)
  await lab
  out.lab_new_game = {lab: h.ui.lab, labBusy: h.ui.labBusy, game: h.ui.gameId}
}
{ // ... and a refused analysis is reported.
  const h = harness()
  h.flow.startSession("g1", labState("g1:a"))
  const lab = h.flow.lab()
  h.take("POST", "/api/analyse").reject(fail("engine busy"))
  await lab
  out.lab_refused = {lab: h.ui.lab, labBusy: h.ui.labBusy, log: h.log}
}
const base = {state: st("s", "human"), review: null, busy: false, thinking: false,
              starting: false, labBusy: false}
out.can_move = [base, {...base, labBusy: true}, {...base, starting: true}, {...base, busy: true},
  {...base, thinking: true}, {...base, review: 3}, {...base, state: st("s", "cca")},
  {...base, state: {...st("s", "human"), game_over: true}}].map(canMove)
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_page_flow_never_stays_stuck_after_a_refused_request() -> None:
    # Regression: "New game" / "Load FEN" refused (bad seed or FEN) while CCA was thinking left
    # the page at "CCA is thinking..." until a reload, because the game context was switched
    # before the server accepted the new game and CCA's reply was then dropped as stale.
    out = _node(FLOW_PROBE, FLOW_URL=STATIC / "js" / "flow.js")
    assert isinstance(out, dict)
    assert out["refused_new_game_during_think"] == {
        "during": {"thinking": True, "starting": False, "canMove": False},
        "thinking": False,
        "canMove": True,
        "decision": True,
        "log": ["apply:g1:e4", "rejected:bad seed", "apply:g1:e5@0"],
    }
    assert out["refused_new_game_during_reveal"] == {
        "thinking": False,
        "reveal": None,
        "log": ["apply:g1:start", "apply:g1:e4@1000"],  # anchored at arrival, not at 4000
    }
    assert out["new_game_drops_old_think"] == {
        "game": "g2",
        "state": "g2:start",
        "thinking": False,
        "canMove": True,
        "decisions": 0,
    }
    assert out["refused_new_game_during_move"] == {
        "during": {"busy": True, "canMove": False},
        "busy": False,
        "state": "g1:e4",
        "thinking": True,
        "pending": ["POST /api/games/g1/think"],
    }
    assert out["new_game_drops_old_move"] == {
        "busy": False,
        "state": "g2:start",
        "canMove": True,
        "pending": [],
    }
    assert out["refused_undo"] == {"state": "g1:b", "busy": False}
    assert out["undo_drops_stale_refresh"] == {"state": "g1:undone", "busy": False}
    assert out["lost_move_redraws"] == {
        "busy": False,
        "log": ["apply:g1:start", "error:offline", "apply:g1:again", "draw"],
        "pending": [],
    }
    assert out["can_move"] == [True, False, False, False, False, False, False, False]
    assert out["move_bodies"] == [{"uci": "a7a8n"}, {"uci": "e2e4"}]
    assert out["new_game_during_reveal"] == {
        "state": "g2:start",
        "decisions": 0,
        "thinking": False,
        "reveal": None,
        "canMove": True,
        "said": [],  # CCA's old move is never announced
    }
    assert out["game_over_announced"] == {"said": ["you:Ra8#", "over:g1:mate"], "pending": []}
    assert out["lab_kept"] == {
        "during": {"labBusy": True, "canMove": False},
        "body": [{"fen": "F1", "persona": "tal", "elo_self": 2100, "elo_oppo": 1200}],
        "lab": {"fen": "F1", "decision": {"d": 1}, "side": "white"},
        "labBusy": False,
        "canMove": True,
    }
    assert out["lab_other_position"] == {"lab": None, "labBusy": False}
    assert out["lab_new_game"] == {"lab": None, "labBusy": False, "game": "g2"}
    assert out["lab_refused"] == {
        "lab": None,
        "labBusy": False,
        "log": ["apply:g1:a", "error:engine busy", "draw"],
    }


# Drives static/js/insight.js: the fair-game switch, the board arrows and the "why" sentence.
INSIGHT_PROBE = """
import {whySentence, decisionArrows, insightVisible, insightBadge, boardArrows, showInsightParts,
  trapMarked} from INSIGHT_URL

const cand = (uci, san, q_opt, extra = {}) => ({uci, san, q_opt, q_human: q_opt, trap_value: 0,
  prior: 0.1, opp_entropy: 1, sharpness: 0, attention: 0, policy: null, ...extra})
const pol = (uci, san, p) => ({uci, san, p})
const decision = (uci, san, candidates, policy, trace = {}) =>
  ({move: {uci, san}, candidates, policy, knobs: {risk_budget: 0.041}, trace, state: {}})
const arrows = (d) => decisionArrows(d).map((a) => [a.type.class, a.from, a.to])

const sampled = decision("g8f6", "Nf6",
  [cand("g8f6", "Nf6", 0.52), cand("b8c6", "Nc6", 0.55)],
  [pol("b8c6", "Nc6", 0.73), pol("g8f6", "Nf6", 0.27)])
const trap = decision("f8b4", "Bb4+",
  [cand("f8b4", "Bb4+", 0.488, {trap_value: 0.08}), cand("g8f6", "Nf6", 0.5, {trap_value: 0.01})],
  [pol("f8b4", "Bb4+", 0.6), pol("g8f6", "Nf6", 0.4)])
const human = decision("e7e5", "e5",
  [cand("e7e5", "e5", 0.51, {prior: 0.45}), cand("c7c5", "c5", 0.52, {prior: 0.2})],
  [pol("e7e5", "e5", 0.7), pol("c7c5", "c5", 0.3)])
const entropy = decision("d7d6", "d6",
  [cand("d7d6", "d6", 0.5, {prior: 0.1, opp_entropy: 2.5}),
   cand("c7c5", "c5", 0.52, {prior: 0.2, opp_entropy: 1.2})],
  [pol("d7d6", "d6", 0.7), pol("c7c5", "c5", 0.3)])
const best = decision("e2e4", "e4", [cand("e2e4", "e4", 0.54), cand("d2d4", "d4", 0.53)],
  [pol("e2e4", "e4", 0.9), pol("d2d4", "d4", 0.1)])
const bestTrap = decision("e2e4", "e4",
  [cand("e2e4", "e4", 0.54, {trap_value: 0.05}), cand("d2d4", "d4", 0.53)],
  [pol("e2e4", "e4", 0.9), pol("d2d4", "d4", 0.1)])
const sampledBest = decision("e2e4", "e4", [cand("e2e4", "e4", 0.54), cand("d2d4", "d4", 0.53)],
  [pol("d2d4", "d4", 0.6), pol("e2e4", "e4", 0.4)])
const reflex = decision("e2e4", "e4", [cand("e2e4", "e4", 0.54)], [pol("e2e4", "e4", 1)],
  {reflex: 1, q_opt: 0.54})
const promo = decision("a7a8q", "a8=Q+",
  [cand("a7a8q", "a8=Q+", 0.9), cand("a7a8n", "a8=N", 0.6)],
  [pol("a7a8q", "a8=Q+", 0.55), pol("a7a8n", "a8=N", 0.3), pol("e1e2", "Ke2", 0.12),
   pol("e1d1", "Kd1", 0.021), pol("h2h3", "h3", 0.019)])

const out = {why: {}, arrows: {}}
for (const [name, d] of Object.entries({sampled, trap, human, entropy, best, bestTrap,
                                        sampledBest, reflex})) out.why[name] = whySentence(d)
out.arrows.sampled = arrows(sampled)
out.arrows.promo = arrows(promo)
const decisions = new Map([[0, sampled]])
const lab = {decision: trap}
const view = (insight, over, withLab, ply, state = {game_over: over}) => {
  const ui = {insight, state, lab: withLab ? lab : null, decisions}
  return [insightVisible(ui), boardArrows(ui, ply).map((a) => a.from + a.to)]
}
out.fair = {
  fair: view(false, false, false, 0),
  fair_lab: view(false, false, true, 0),  // a lab answer that arrived after switching off
  fair_no_state: view(false, false, false, 0, null),
  fair_over: view(false, true, false, 0),
  live: view(true, false, false, 0),
  live_start: view(true, false, false, -1),
  live_no_decision: view(true, false, false, 1),
  live_lab: view(true, false, true, 0)
}
const badge = (insight, over, withLab, review = null) =>
  insightBadge({insight, state: {game_over: over}, lab: withLab ? lab : null, review, decisions})
out.badges = [badge(false, false, false), badge(false, false, true), badge(false, true, false),
  badge(true, true, false), badge(true, false, true, 2), badge(true, false, false, 2),
  badge(true, false, false, -1), badge(true, false, false)]
const parts = (insight, over) => {
  const p = {note: {hidden: null}, body: {hidden: null}, legend: {hidden: null}}
  const visible = showInsightParts(p, {insight, state: {game_over: over}})
  return [visible, p.note.hidden, p.body.hidden, p.legend.hidden]
}
out.parts = {fair: parts(false, false), fair_over: parts(false, true), live: parts(true, false)}
const marked = (insight, over, trap) => trapMarked({insight, state: {game_over: over}}, trap)
out.trap_marks = [marked(true, false, 0.08), marked(false, false, 0.08), marked(false, true, 0.08),
  marked(true, false, 0.03), marked(true, false, 0.029), marked(true, false, null),
  marked(true, false, undefined)]
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_insight_panel_logic_fair_game_arrows_and_why() -> None:
    out = _node(INSIGHT_PROBE, INSIGHT_URL=STATIC / "js" / "insight.js")
    assert isinstance(out, dict)
    # Fair game: nothing of CCA's thinking (arrows included) until the game ends.
    assert out["fair"] == {
        "fair": [False, []],
        "fair_lab": [False, []],
        "fair_no_state": [False, []],
        "fair_over": [True, ["b8c6", "g8f6"]],
        "live": [True, ["b8c6", "g8f6"]],
        "live_start": [True, []],
        "live_no_decision": [True, []],
        "live_lab": [True, ["g8f6", "f8b4"]],  # the lab's answer, not the game's decision
    }
    assert out["badges"] == [
        "badge_hidden",
        "badge_hidden",  # a lab answer that arrived after switching off stays hidden
        "badge_revealed",
        "badge_live",
        "badge_lab",
        "badge_review",
        "badge_live",  # reviewing the start position: no decision to show there
        "badge_live",
    ]
    strings = _i18n_strings()
    assert all(key in strings["en"] and key in strings["vi"] for key in out["badges"])
    # [visible, note hidden, body hidden, legend hidden]: a fair game shows only the note.
    assert out["parts"] == {
        "fair": [False, False, True, True],
        "fair_over": [True, True, False, False],
        "live": [True, True, False, False],
    }
    # The move list's trap marks: from +0.03, and never in a fair game before its end.
    assert out["trap_marks"] == [True, False, True, True, False, False, False]
    # Engine's best first (dashed), then the top policy moves by probability band; a move
    # already drawn (the other promotion pieces) and moves under 2% are not.
    assert out["arrows"] == {
        "sampled": [["arrow-engine", "b8", "c6"], ["arrow-p2", "g8", "f6"]],
        "promo": [["arrow-p1", "a7", "a8"], ["arrow-p3", "e1", "e2"], ["arrow-p4", "e1", "d1"]],
    }
    assert out["why"] == {
        "sampled": "CCA sampled Nf6 (27.0% of its policy; top was Nc6 at 73.0%) instead of the"
        " engine's best Nc6; the 0.030 cost is within its risk budget 0.041.",
        "trap": "CCA chose Bb4+ over the engine's best Nf6: its trap value +0.080 is worth the"
        " 0.012 expected-score sacrifice, within the risk budget 0.041.",
        "human": "CCA chose e5 over the engine's best c5: it is the more human-like move (prior"
        " 45.0% vs 20.0%) and costs only 0.010 expected score, within the risk budget 0.041.",
        "entropy": "CCA chose d6 over the engine's best c5: it leaves you more plausible replies"
        " (2.50 vs 1.20 nats) for a 0.020 expected-score cost, within the risk budget 0.041.",
        "best": "CCA played e4, also the engine's best move (expected score 0.540).",
        "bestTrap": "CCA played e4, the engine's best move, which also sets you problems: trap"
        " value +0.050.",
        "sampledBest": "CCA sampled e4 (40.0% of its policy; top was d4 at 60.0%): it samples"
        " from its policy, so less likely moves are sometimes played.",
        "reflex": "Almost out of time: CCA played the engine's best move e4 without its full"
        " analysis (reflex mode).",
    }


# Drives static/js/board.js with the vendored chess.js and a fake cm-chessboard: keyboard
# entry (SAN and UCI) and the promotion dialog.
BOARD_PROBE = """
import {BoardView, kingInCheck} from BOARD_URL
import {Chess} from CHESS_URL
import {INPUT_EVENT_TYPE} from CHESSBOARD_URL
import {PROMOTION_DIALOG_RESULT_TYPE as RESULT} from PROMOTION_URL

const START = new Chess().fen()
const WHITE_PROMO = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
const BLACK_PROMO = "4k3/8/8/8/8/8/p7/4K3 b - - 0 1"
const CAPTURE_PROMO = "1n2k3/P7/8/8/8/8/8/4K3 w - - 0 1"  // a7-a8 and a7xb8
const CASTLING = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
const EN_PASSANT = "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"
const fmt = (m) => (m ? m.from + m.to + (m.promotion || "") + " " + m.san : null)

function view(fen) {  // a BoardView without its (DOM) constructor
  const v = Object.create(BoardView.prototype)
  v.chess = new Chess(fen)
  v.animate = false
  v.played = []
  v.log = []
  v.onMove = (m) => v.played.push(fmt(m))
  v.board = {
    removeLegalMovesMarkers() {},
    addLegalMovesMarkers(moves) { v.log.push("dots:" + moves.length) },
    setPosition(position) { v.log.push("set:" + position) },
    removeArrows() {},
    showPromotionDialog(square, color, callback) {
      v.log.push("dialog:" + square + ":" + color)
      v.dialog = callback
    }
  }
  return v
}
const validate = (v, from, to, done = Promise.resolve()) => v._input({
  type: INPUT_EVENT_TYPE.validateMoveInput, squareFrom: from, squareTo: to,
  chessboard: {state: {moveInputProcess: done}}
})
const out = {}

const parse = (fen, text) => {
  const v = view(fen)
  const r = v.parse(text)
  const piece = (need) => "piece? " + need.san + " " + need.uci
  const shown = r === null ? null : r.move ? fmt(r.move) : piece(r.needsPiece)
  return [shown, v.chess.fen() === fen]  // parsing never changes the position
}
out.parse = {
  uci: parse(START, "e2e4"),
  uci_spaced_caps: parse(START, "  E2E4 "),
  uci_knight: parse(START, "g1f3"),
  san: parse(START, "Nf3"),
  san_annotated: parse(START, "Nf3!?"),
  illegal_uci: parse(START, "e2e5"),
  wrong_side: parse(START, "e7e5"),
  illegal_san: parse(START, "Ke2"),
  empty: parse(START, ""),
  junk: parse(START, "hello"),
  piece_on_a_plain_move: parse(START, "e2e4q"),
  castle_short: parse(CASTLING, "O-O"),
  castle_long_uci: parse(CASTLING, "e1c1"),
  en_passant: parse(EN_PASSANT, "exd6"),
  promo_uci: parse(WHITE_PROMO, "a7a8n"),
  promo_uci_rook: parse(WHITE_PROMO, "a7a8r"),
  promo_san: parse(WHITE_PROMO, "a8=B"),
  black_promo_uci: parse(BLACK_PROMO, "a2a1n"),
  // A promotion typed without its piece is never guessed (chess.js alone picks a knight).
  bare_uci: parse(CAPTURE_PROMO, "a7a8"),
  bare_long: parse(CAPTURE_PROMO, "a7-a8"),
  bare_san: parse(CAPTURE_PROMO, "a8"),
  bare_capture: parse(CAPTURE_PROMO, "axb8"),
  bare_capture_check: parse(CAPTURE_PROMO, "axb8+"),
  bare_long_capture: parse(CAPTURE_PROMO, "a7xb8"),
  bare_dash_capture: parse(CAPTURE_PROMO, "a7-b8"),
  bare_black_uci: parse(BLACK_PROMO, "a2a1"),
  bare_black_san: parse(BLACK_PROMO, "a1"),
  capture_named: parse(CAPTURE_PROMO, "axb8=R"),
  capture_named_lower: parse(CAPTURE_PROMO, "axb8=r"),
  named_no_equals_lower: parse(CAPTURE_PROMO, "a8n"),
  long_named: parse(CAPTURE_PROMO, "a7-b8=B"),
  uci_named_caps: parse(CAPTURE_PROMO, "A7B8Q"),
  black_named: parse(BLACK_PROMO, "a1=N"),
  king_promotion: parse(CAPTURE_PROMO, "a8=K")
}
{ // The move-entry field: a legal move is played; otherwise nothing is, and a message says why.
  const enter = (fen, text) => {
    const v = view(fen)
    return {problem: v.enter(text), played: v.played}
  }
  out.enter = {
    legal: enter(CAPTURE_PROMO, "axb8=Q"),
    bare: enter(CAPTURE_PROMO, "axb8"),
    illegal: enter(START, "  e2e5 ")
  }
}

{ // Promotion by the board: the dialog's piece is the one played.
  const v = view(WHITE_PROMO)
  const accepted = validate(v, "a7", "a8")
  const before = [...v.played]
  v.dialog({type: RESULT.pieceSelected, piece: "wn"})
  out.promote_white = {accepted, before, played: v.played, log: v.log, fen: v.chess.fen()}
}
{
  const v = view(BLACK_PROMO)
  validate(v, "a2", "a1")
  v.dialog({type: RESULT.pieceSelected, piece: "br"})
  out.promote_black = {played: v.played, log: v.log}
}
{ // Canceled: nothing is played and the pawn goes back.
  const v = view(WHITE_PROMO)
  validate(v, "a7", "a8")
  v.dialog({type: RESULT.canceled})
  out.promote_cancel = {played: v.played, log: v.log, fen: v.chess.fen()}
}
{ // A plain move is reported once cm-chessboard's own move input has finished.
  const v = view(START)
  let finish
  const done = new Promise((resolve) => { finish = resolve })
  const accepted = validate(v, "e2", "e4", done)
  const before = [...v.played]
  finish()
  await done
  await null
  out.plain = {accepted, before, played: v.played}
}
{
  const v = view(START)
  out.illegal = {accepted: validate(v, "e2", "e5"), played: v.played}
  const started = (square) =>
    v._input({type: INPUT_EVENT_TYPE.moveInputStarted, squareFrom: square})
  out.started = [started("e2"), started("e4"), v.log]
}
out.check = [kingInCheck(new Chess("4k3/8/8/8/8/8/4Q3/4K3 b - - 0 1")), kingInCheck(new Chess())]
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_board_keyboard_entry_and_promotion_choice() -> None:
    js, vendor = STATIC / "js", VENDOR / "cm-chessboard" / "src"
    out = _node(
        BOARD_PROBE,
        BOARD_URL=js / "board.js",
        CHESS_URL=VENDOR / "chess.js" / "dist" / "esm" / "chess.js",
        CHESSBOARD_URL=vendor / "Chessboard.js",
        PROMOTION_URL=vendor / "extensions" / "promotion-dialog" / "PromotionDialog.js",
    )
    assert isinstance(out, dict)
    queen_capture = "piece? axb8=Q+ a7b8q"
    assert out["parse"] == {
        "uci": ["e2e4 e4", True],
        "uci_spaced_caps": ["e2e4 e4", True],
        "uci_knight": ["g1f3 Nf3", True],
        "san": ["g1f3 Nf3", True],
        "san_annotated": ["g1f3 Nf3", True],
        "illegal_uci": [None, True],
        "wrong_side": [None, True],
        "illegal_san": [None, True],
        "empty": [None, True],
        "junk": [None, True],
        "piece_on_a_plain_move": [None, True],  # "e2e4q" is no UCI move (the server says 400)
        "castle_short": ["e1g1 O-O", True],
        "castle_long_uci": ["e1c1 O-O-O", True],
        "en_passant": ["e5d6 exd6", True],
        "promo_uci": ["a7a8n a8=N", True],
        "promo_uci_rook": ["a7a8r a8=R+", True],
        "promo_san": ["a7a8b a8=B", True],
        "black_promo_uci": ["a2a1n a1=N", True],
        "bare_uci": ["piece? a8=Q a7a8q", True],
        "bare_long": ["piece? a8=Q a7a8q", True],
        "bare_san": ["piece? a8=Q a7a8q", True],
        "bare_capture": [queen_capture, True],
        "bare_capture_check": [queen_capture, True],
        "bare_long_capture": [queen_capture, True],
        "bare_dash_capture": [queen_capture, True],
        "bare_black_uci": ["piece? a1=Q+ a2a1q", True],
        "bare_black_san": ["piece? a1=Q+ a2a1q", True],
        "capture_named": ["a7b8r axb8=R+", True],
        "capture_named_lower": ["a7b8r axb8=R+", True],
        "named_no_equals_lower": ["a7a8n a8=N", True],
        "long_named": ["a7b8b axb8=B", True],
        "uci_named_caps": ["a7b8q axb8=Q+", True],
        "black_named": ["a2a1n a1=N", True],
        "king_promotion": [None, True],
    }
    assert out["enter"] == {
        "legal": {"problem": None, "played": ["a7b8q axb8=Q+"]},
        "bare": {
            "problem": {"key": "err_promo_piece", "params": {"san": "axb8=Q+", "uci": "a7b8q"}},
            "played": [],
        },
        "illegal": {"problem": {"key": "err_illegal", "params": {"move": "e2e5"}}, "played": []},
    }
    strings = _i18n_strings()
    for lang in ("en", "vi"):
        for key in ("err_promo_piece", "err_illegal"):
            text = strings[lang][key]
            params = out["enter"]["bare" if key == "err_promo_piece" else "illegal"]["problem"]
            assert {m.group(1) for m in re.finditer(r"\{(\w+)\}", text)} == set(params["params"])
    after_knight = "N3k3/8/8/8/8/8/8/4K3 b - - 0 1"
    assert out["promote_white"] == {
        "accepted": True,
        "before": [],
        "played": ["a7a8n a8=N"],
        "log": ["dialog:a8:w", "set:" + after_knight],
        "fen": after_knight,
    }
    assert out["promote_black"]["played"] == ["a2a1r a1=R+"]
    assert out["promote_black"]["log"][0] == "dialog:a1:b"
    white_promo = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
    assert out["promote_cancel"] == {
        "played": [],
        "log": ["dialog:a8:w", "set:" + white_promo],
        "fen": white_promo,
    }
    assert out["plain"] == {"accepted": True, "before": [], "played": ["e2e4 e4"]}
    assert out["illegal"] == {"accepted": False, "played": []}
    assert out["started"] == [True, False, ["dots:2", "dots:0"]]
    assert out["check"] == ["e8", None]


# Drives static/js/api.js with a fake fetch().
API_PROBE = """
import {api, ApiError} from API_URL

const seen = []
let reply = null
globalThis.fetch = async (path, init) => {
  seen.push({path, init: JSON.parse(JSON.stringify(init))})
  if (reply instanceof Error) throw reply
  return reply
}
const json = (status, body) =>
  new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})
const text = (status, body) =>
  new Response(body, {status, headers: {"Content-Type": "text/plain; charset=utf-8"}})
const failure = async (call) => {
  try {
    await call()
    return "resolved"
  } catch (err) {
    const {status, message, state} = err
    return {api: err instanceof ApiError, status, message, state}
  }
}
const out = {}
reply = json(200, {game_id: "g"})
out.post = await api.post("/api/games", {human_color: "white"})
reply = json(200, {})
await api.post("/api/games/g/think")
reply = json(200, {fen: "x"})
out.get = await api.get("/api/games/g")
reply = text(200, "[Event]")
out.pgn = await api.get("/api/games/g/pgn")
out.requests = [...seen]
reply = json(400, {error: "illegal move: e2e5", state: {fen: "s"}})
out.refused = await failure(() => api.post("/api/games/g/move", {uci: "e2e5"}))
reply = text(502, "bad gateway")
out.text_error = await failure(() => api.get("/api/games/g/pgn"))
reply = new TypeError("Failed to fetch")
out.offline = await failure(() => api.get("/api/info"))
console.log(JSON.stringify(out))
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_api_client_sends_json_and_maps_errors() -> None:
    # Without "Content-Type: application/json" every POST gets 415 and the page cannot play.
    out = _node(API_PROBE, API_URL=STATIC / "js" / "api.js")
    assert isinstance(out, dict)
    post = {"credentials": "same-origin", "cache": "no-store", "method": "POST"}
    get = {"credentials": "same-origin", "cache": "no-store", "method": "GET"}
    json_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    assert out["requests"] == [
        {
            "path": "/api/games",
            "init": {**post, "headers": json_headers, "body": '{"human_color":"white"}'},
        },
        {"path": "/api/games/g/think", "init": {**post, "headers": json_headers, "body": "{}"}},
        {"path": "/api/games/g", "init": {**get, "headers": {"Accept": "application/json"}}},
        {"path": "/api/games/g/pgn", "init": {**get, "headers": {"Accept": "application/json"}}},
    ]
    assert (out["post"], out["get"], out["pgn"]) == ({"game_id": "g"}, {"fen": "x"}, "[Event]")
    assert out["refused"] == {
        "api": True,
        "status": 400,
        "message": "illegal move: e2e5",
        "state": {"fen": "s"},
    }
    assert out["text_error"] == {
        "api": True,
        "status": 502,
        "message": "bad gateway",
        "state": None,
    }
    assert out["offline"] == {"api": True, "status": 0, "message": "Failed to fetch", "state": None}


def test_page_uses_the_tested_fair_game_logic() -> None:
    # main.js (DOM wiring, not run by the tests) must not re-implement what the Node tests above
    # check: the fair-game switch, the move-entry field and the position lab.
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    assert "arrows: boardArrows(ui, ply)" in main
    assert (
        'const visible = showInsightParts({note: $("insight-hidden"), body: $("insight-body"),'
        ' legend: $("arrow-legend")}, ui)' in main
    )
    assert not re.search(r'\$\("(insight-hidden|insight-body|arrow-legend)"\)\.hidden', main)
    assert "const key = insightBadge(ui)" in main
    assert 'if (trapMarked(ui, traps.get(ply))) b.classList.add("cca-trap")' in main
    assert "insightVisible" not in main
    assert "const problem = board.enter(input.value)" in main
    # The field gets its focus back only once it is enabled again: focus() on a disabled field
    # does nothing (the focus stayed on the page after every keyboard move, seen in Chromium).
    enable = main.index('$("kbd-move").disabled = !entry')
    refocus = "if (entry && ui.keyboard && document.activeElement === document.body)"
    refocus += ' $("kbd-move").focus()'
    assert main.count('$("kbd-move").focus()') == 1
    assert main.index(refocus) > enable
    assert "board.parse(" not in main
    assert "board.play(" not in main
    assert '$("lab-btn").addEventListener("click", flow.lab)' in main
    assert "/api/analyse" not in main  # the lab's request and its ordering live in flow.js
    assert "    displayedFen,\n" in main  # the view hook flow.lab() reads the position from
    names = r"insightVisible|insightBadge|boardArrows|decisionArrows|showInsightParts|trapMarked"
    assert not re.search(rf"function ({names}|runLab|lab)\b", main)


def test_the_piece_sprite_points_to_its_licence_text() -> None:
    folder = VENDOR / "cburnett-commons"
    sprite = (folder / "cburnett.svg").read_text(encoding="utf-8")
    header = sprite[: sprite.index("<svg")]
    assert "LICENSE.txt, next to this file" in header
    assert "THIRD_PARTY_NOTICES" not in header
    licence = (folder / "LICENSE.txt").read_text(encoding="utf-8")
    assert "3-clause BSD License" in licence
    assert "3. Neither the name of" in licence  # the full text, all three clauses


def test_git_does_not_ignore_any_static_file() -> None:
    git = shutil.which("git")
    tree = ROOT / "src" / "cca" / "play" / "static"
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = [str(p.relative_to(ROOT)) for p in tree.rglob("*") if p.is_file()]
    proc = subprocess.run(  # fixed argument list, no shell
        [git, "-C", str(ROOT), "check-ignore", "--no-index", *files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.stdout.strip() == "", proc.stdout  # exit code 1 = nothing ignored


def test_simulator_files_use_lf_line_endings() -> None:
    # .gitattributes says eol=lf: a CRLF working copy (cli.py, app.py once) turns every line of
    # a diff into a change for other tools; for vendored files it would also break the hashes
    # of the MANIFEST in a fresh checkout.
    text_suffixes = {".py", ".js", ".mjs", ".css", ".html", ".svg", ".json", ".txt", ""}
    package = STATIC.parent  # the cca.play under test
    files = [
        package.parent / "cli.py",
        Path(__file__),
        Path(__file__).with_name("test_cli_play.py"),
    ]
    files += [
        p
        for p in package.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in text_suffixes
    ]
    assert len(files) > 30
    assert [str(p) for p in files if b"\r" in p.read_bytes()] == []
