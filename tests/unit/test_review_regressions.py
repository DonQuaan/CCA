"""Regression tests for the verified findings of the 2026-09-25 adversarial review."""

from __future__ import annotations

import dataclasses
import math
import statistics
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent, is_ended
from cca.chaos import AR1Driver, LorenzOscillator
from cca.config import ConfigError, load_persona
from cca.core.rng import DeterministicRng
from cca.core.types import Clock, PsychState
from cca.engines.maia2_human import _mirror_uci
from cca.neuro import Persona
from cca.policy import reply_stats
from tests.conftest import FakeEngine, FakeHuman


class _Greedy(FakeHuman):
    """A human model that loves one terrible move (to prove the safety set is load-bearing)."""

    def __init__(self, bad: str) -> None:
        self.bad = bad

    def distribution(
        self, board: chess.Board, elo_self: int, elo_oppo: int, time_limit: float | None = None
    ) -> dict[str, float]:
        base = super().distribution(board, elo_self, elo_oppo)
        if self.bad in base:
            rest = 0.001 / max(1, len(base) - 1)
            return {k: (0.999 if k == self.bad else rest) for k in base}
        return base


class _Recording(FakeHuman):
    def __init__(self) -> None:
        self.seen: list[chess.Board] = []

    def distributions(
        self,
        boards: Sequence[chess.Board],
        elo_self: int,
        elo_oppo: int,
        time_limit: float | None = None,
    ) -> list[dict[str, float]]:
        self.seen.extend(b.copy() for b in boards)
        return super().distributions(boards, elo_self, elo_oppo)


# ---- safety TQ-01 ----
def test_selected_move_always_satisfies_the_risk_budget_under_sampling() -> None:
    fens = [
        chess.STARTING_FEN,
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
        "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1",
        "r2qkb1r/ppp2ppp/2n2n2/3pp3/3PP1b1/2N2N2/PPP2PPP/R1BQKB1R b KQkq - 0 5",
    ]
    for fen in fens:
        for seed in range(12):
            agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed=f"s{seed}"))
            d = agent.choose(chess.Board(fen))
            best = max(c.q_opt for c in d.candidates)
            chosen = next(c for c in d.candidates if c.uci == d.move)
            assert chosen.q_opt >= best - d.knobs.risk_budget - 1e-9, (fen, seed)


def test_a_loved_losing_move_is_never_played() -> None:
    board = chess.Board("4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1")  # exd5 wins the queen
    for seed in range(30):
        agent = CAIMEAgent(FakeEngine(), _Greedy("e1d1"), AgentConfig(seed=f"g{seed}"))
        assert agent.choose(board).move != "e1d1"


# ---- perspective TQ-02 ----
def test_black_to_move_perspective() -> None:
    board = chess.Board("4k3/8/8/4p3/3Q4/8/8/4K3 b - - 0 1")  # ...exd4 wins the queen
    persona = dataclasses.replace(Persona(), eps0=0.0, eps_max=0.0)
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=persona, sample=False))
    d = agent.choose(board)
    assert d.move == "e5d4"
    q = {c.uci: c.q_opt for c in d.candidates}
    # K+P vs K after the capture is good for Black (> 0.5); a quiet king move keeps K+P vs K+Q.
    assert q["e5d4"] > 0.55
    assert all(v < q["e5d4"] - 0.3 for u, v in q.items() if u != "e5d4")


# ---- draws AG-1  stack TQ-10 ----
def _shuffle_board() -> chess.Board:
    board = chess.Board()
    for uci in ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"]:
        board.push_uci(uci)
    return board  # Black to move: ...Nf6-g8 repeats the start position a third time


def test_repetition_child_is_treated_as_ended_and_not_modelled() -> None:
    board = _shuffle_board()
    child = board.copy()
    child.push_uci("f6g8")
    assert is_ended(child)
    human = _Recording()
    engine = FakeEngine()
    CAIMEAgent(engine, human, AgentConfig(engine_multipv=40, prior_top=40)).choose(board)
    assert all(not b.is_repetition(3) for b in human.seen)


def test_child_positions_keep_the_move_stack() -> None:
    board = _shuffle_board()
    engine = FakeEngine()
    CAIMEAgent(engine, FakeHuman(), AgentConfig()).choose(board)
    children = [b for b in engine.boards if len(b.move_stack) == len(board.move_stack) + 1]
    assert children  # reply analysis ran on positions that still know their history


# ---- knobs wired TQ-09 ----
def test_stress_knobs_change_the_decision() -> None:
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    board.push_uci("f1c4")
    board.push_uci("g8f6")
    policies = []
    for a_tunnel, a_habit in ((0.0, 0.0), (6.0, 6.0)):
        persona = dataclasses.replace(Persona(), a_tunnel=a_tunnel, a_habit=a_habit, tau=0.0)
        agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=persona))
        agent.state = dataclasses.replace(agent.state, stress=2.5)
        agent._history = [m.uci() for m in board.move_stack][:-1]
        policies.append(agent.choose(board).policy)
    assert policies[0] != policies[1]


# ---- seeds TQ-11 ----
def test_different_seeds_play_differently_and_plies_use_fresh_streams() -> None:
    games = set()
    for seed in range(5):
        agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed=f"d{seed}"))
        board = chess.Board()
        line = []
        for _ in range(8):
            mv = agent.choose(board).move
            board.push_uci(mv)
            line.append(mv)
            reply = min(board.legal_moves, key=lambda m: m.uci())
            board.push(reply)
        games.add(tuple(line))
    assert len(games) >= 2


# ---- clock M1  deadline ----
def test_movestogo_is_respected_by_clock_pressure() -> None:
    board = chess.Board()
    board.push_uci("e2e4")  # the agent (Black) appraises this move
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    d = agent.choose(board, Clock(my_time=20.0, opp_time=20.0, moves_to_go=1))
    assert d.trace["time_pressure"] < 0.01  # 20 s for one move is no pressure at all


def test_stop_cuts_the_lookahead_but_still_returns_a_sound_move() -> None:
    stop = threading.Event()
    stop.set()
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    d = agent.choose(chess.Board(), stop=stop)
    assert d.trace["lookahead_cut"] == 0.0
    best = max(c.q_opt for c in d.candidates)
    assert (
        next(c for c in d.candidates if c.uci == d.move).q_opt >= best - d.knobs.risk_budget - 1e-9
    )


def test_past_deadline_gives_reflex_engine_move() -> None:
    engine = FakeEngine()
    agent = CAIMEAgent(engine, FakeHuman(), AgentConfig())
    board = chess.Board("4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1")
    d = agent.choose(board, deadline=time.monotonic())
    assert d.trace["reflex"] == 1.0
    assert d.move == "e4d5"


# ---- opening AG-5 AG-6 ----
def test_endgame_fen_with_move_number_one_is_not_an_opening() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    d = agent.choose(chess.Board("8/5k2/8/8/8/8/1R6/4K3 w - - 0 1"))
    assert "opening_tempered" not in d.trace


def test_opening_surprise_uses_the_tempered_prior() -> None:
    board = chess.Board()
    board.push_uci("h2h4")  # an unusual first move
    cold = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(opening_temperature=1.0)).choose(board)
    warm = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig()).choose(board)
    assert abs(warm.trace["opp_surprise_excess"]) <= abs(cold.trace["opp_surprise_excess"])


# ---- reply_stats M2 L1 ----
def test_opponent_stress_never_lowers_q_human_when_the_tail_is_no_better() -> None:
    probs = {"best": 0.6, "blunder": 0.3, **{f"t{i}": 0.1 / 25 for i in range(25)}}
    scores = {"best": 0.5, "blunder": 0.9}
    values = [reply_stats(probs, scores, 0.5, t).q_human for t in (1.0, 1.25, 1.5, 2.0, 3.0)]
    assert values == sorted(values)


def test_truncated_reply_distribution_stays_conservative() -> None:
    # Model returned only 70 % of the mass: the missing 30 % must count as the opponent's best.
    s = reply_stats({"a": 0.5, "b": 0.2}, {"a": 1.0, "b": 1.0}, q_opt=0.0, opp_temperature=2.0)
    assert s.q_human == pytest.approx(0.7)


# ---- AR 1 control M4 ----
def _lag1(v: list[float]) -> float:
    m = statistics.fmean(v)
    return sum((v[i] - m) * (v[i + 1] - m) for i in range(len(v) - 1)) / sum(
        (x - m) ** 2 for x in v
    )


def _corr(a: list[float], b: list[float]) -> float:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return num / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))


@pytest.mark.slow
def test_ar1_control_matches_the_consumed_lorenz_signals() -> None:
    lor, ar = LorenzOscillator.from_seed("m4"), AR1Driver.from_seed("m4")
    ls: list[tuple[float, float, float]] = []
    as_: list[tuple[float, float, float]] = []
    for _ in range(12_000):
        lor.advance_ply()
        ar.advance_ply()
        ls.append(lor.signals())
        as_.append(ar.signals())
    for k in range(3):
        assert _lag1([s[k] for s in as_]) == pytest.approx(_lag1([s[k] for s in ls]), abs=0.06)
    assert _corr([s[0] for s in as_], [s[1] for s in as_]) == pytest.approx(
        _corr([s[0] for s in ls], [s[1] for s in ls]), abs=0.08
    )


# ---- golden values F7 ----
def test_golden_values_replay_bit_identically() -> None:
    """Pinned on Windows/CPython 3.13; CI re-checks them on Linux and Windows, 3.11-3.13."""
    o = LorenzOscillator.from_seed("golden", "g1")
    a = AR1Driver.from_seed("golden", "g1")
    for i in range(200):
        k = ((i % 7) * 0.37 - 1.0, (i % 5) * 0.21, 0.0)
        o.advance_ply(k)
        a.advance_ply(k)
    assert o.digest() == "ded89f2bffc57677"
    assert a.digest() == "a2febbc502cc20d9"
    r = DeterministicRng("golden")
    assert [r.uniform().hex() for _ in range(3)] == [
        "0x1.52065193ef7ecp-2",
        "0x1.fab7a639be818p-3",
        "0x1.a90f41358a761p-1",
    ]
    assert r.normal().hex() == "0x1.002e65e7031c1p-2"


# ---- misc ----
def test_maia2_mirroring_is_an_involution() -> None:
    assert _mirror_uci("e2e4") == "e7e5"
    assert _mirror_uci("a7a8q") == "a2a1q"
    assert _mirror_uci("e1g1") == "e8g8"
    for uci in ("b1c3", "h7h8n", "d2d4"):
        assert _mirror_uci(_mirror_uci(uci)) == uci


def test_config_rejects_non_finite_and_non_integer_values(tmp_path: Path) -> None:
    nan = tmp_path / "nan.toml"
    nan.write_text('name = "x"\nw0 = nan\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="finite"):
        load_persona(nan)
    from cca.config import load_agent_config

    flt = tmp_path / "flt.toml"
    flt.write_text("[agent]\nelo_self = 1900.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="integer"):
        load_agent_config(flt)


def test_state_default_is_calm() -> None:
    assert PsychState().stress == pytest.approx(0.2)
