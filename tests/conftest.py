"""Shared fixtures: deterministic fake engine and fake human model (no binaries needed)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import chess
import pytest

from cca.core.rng import derive_seed
from cca.core.types import MoveEval

_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        _VALUES[p.piece_type] * (1 if p.color == color else -1) for p in board.piece_map().values()
    )


def _q_after(board: chess.Board, move: chess.Move, perspective: chess.Color) -> float:
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return 1.0 if after.turn != perspective else 0.0
    if after.is_game_over(claim_draw=False):
        return 0.5
    # one-ply greedy opponent reply: material after the opponent's best capture
    worst = material(after, perspective)
    for reply in after.legal_moves:
        if after.is_capture(reply):
            nxt = after.copy(stack=False)
            nxt.push(reply)
            worst = min(worst, material(nxt, perspective))
    jitter = (derive_seed(after.fen(), "jit") % 1000) / 1e5  # deterministic tie-breaking
    return 1.0 / (1.0 + math.exp(-0.5 * worst)) * 0.98 + jitter


class FakeEngine:
    """Deterministic 2-ply material engine implementing the SearchEngine protocol."""

    def __init__(self) -> None:
        self.calls = 0
        self.new_games = 0

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
    ) -> list[MoveEval]:
        self.calls += 1
        pool = list(moves) if moves else list(board.legal_moves)
        evals = []
        for m in pool:
            q = _q_after(board, m, perspective)
            after = board.copy(stack=False)
            after.push(m)
            replies = [r.uci() for r in after.legal_moves]
            pv = (m.uci(), replies[0]) if replies else (m.uci(),)
            evals.append(MoveEval(m.uci(), q, depth=2, pv=pv))
        evals.sort(key=lambda e: (-e.q, e.uci))
        return evals[:multipv]

    def new_game(self) -> None:
        self.new_games += 1

    def close(self) -> None:
        pass


class FakeHuman:
    """Humans like captures and checks; otherwise a smooth preference by move string."""

    def distribution(self, board: chess.Board, elo_self: int, elo_oppo: int) -> dict[str, float]:
        del elo_oppo
        scale = elo_self / 1500.0
        logits: dict[str, float] = {}
        for m in board.legal_moves:
            logit = 0.0
            if board.is_capture(m):
                logit += 2.0 * scale
            if board.gives_check(m):
                logit += 1.0
            logit += (derive_seed(m.uci(), "pref") % 100) / 100.0
            logits[m.uci()] = logit
        if not logits:
            return {}
        top = max(logits.values())
        ex = {k: math.exp(v - top) for k, v in logits.items()}
        total = sum(ex.values())
        return {k: v / total for k, v in ex.items()}

    def distributions(
        self, boards: Sequence[chess.Board], elo_self: int, elo_oppo: int
    ) -> list[dict[str, float]]:
        return [self.distribution(b, elo_self, elo_oppo) for b in boards]


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def fake_human() -> FakeHuman:
    return FakeHuman()
