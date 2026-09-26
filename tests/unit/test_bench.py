"""Benchmark harness correctness (review findings TQ-04, TQ-05, F2, F3)."""

from __future__ import annotations

import chess

from cca.agent import AgentConfig, CAIMEAgent
from cca.bench.match import CCAPlayer, EnginePlayer, HumanModelPlayer, play_game, summarize
from cca.core.types import Clock
from tests.conftest import FakeEngine, FakeHuman


class Scripted:
    """Plays a fixed list of moves."""

    def __init__(self, name: str, moves: list[str]) -> None:
        self.name = name
        self.moves = moves
        self.i = 0

    def new_game(self, game_id: str) -> None:
        del game_id
        self.i = 0

    def play(self, board: chess.Board, clock: Clock) -> tuple[str, float, dict[str, float]]:
        del board, clock
        move = self.moves[self.i]
        self.i += 1
        return move, 1.0, {}


def test_time_forfeit_is_reachable_and_scored_for_the_opponent() -> None:
    slow = EnginePlayer(FakeEngine(), name="Slow", think=1.0)
    human = HumanModelPlayer(FakeHuman(), 1500, 1500)
    rec = play_game(slow, human, "f", base_time=0.5, increment=0.0, max_plies=10)
    assert rec.termination == "time forfeit"
    assert rec.result == "0-1"


def test_referee_loss_is_from_the_movers_perspective_and_attributed_correctly() -> None:
    white = Scripted("W", ["e2e4", "d1h5", "h5h4"])
    black = Scripted("B", ["e7e5", "d8h4"])  # 2...Qh4?? hangs the queen to 3.Qxh4
    rec = play_game(white, black, "r", base_time=None, max_plies=5, referee=FakeEngine())
    by_ply = {(p.mover, p.move): p.loss for p in rec.plies}
    assert by_ply[("B", "d8h4")] is not None
    assert by_ply[("B", "d8h4")] > 0.2
    assert by_ply[("W", "h5h4")] == 0.0
    summary = summarize([rec], "W")
    assert summary["score"] == 0.5  # ply cap -> draw
    opp = summary["opponent_errors"]
    own = summary["subject_errors"]
    assert isinstance(opp, dict)
    assert isinstance(own, dict)
    assert opp["blunder_rate"] == 0.5
    assert own["blunder_rate"] == 0.0


def test_engine_player_uses_the_engines_own_bestmove() -> None:
    class Limited(FakeEngine):
        def bestmove(self, board: chess.Board, time_limit: float | None = None) -> str:
            del time_limit
            return min(m.uci() for m in board.legal_moves)  # a deliberately weak choice

    player = EnginePlayer(Limited())
    move, _, _ = player.play(chess.Board(), Clock())
    assert move == "a2a3"


def test_summary_reports_human_likeness_and_predictability() -> None:
    cca = CCAPlayer(CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig()))
    opp = HumanModelPlayer(FakeHuman(), 1500, 1900)
    rec = play_game(
        cca, opp, "m", base_time=120.0, increment=1.0, max_plies=12, referee=FakeEngine()
    )
    s = summarize([rec], "CCA")
    agreement = s["subject_engine_agreement"]
    logp = s["subject_human_mean_logp"]
    ent = s["subject_mean_policy_entropy"]
    assert isinstance(agreement, float)
    assert 0.0 <= agreement <= 1.0
    assert isinstance(logp, float)
    assert logp <= 0.0
    assert isinstance(ent, float)
    assert ent >= 0.0
