"""Ports (interfaces) between the decision core and the outside world.

The core never talks to Stockfish or a neural network directly; it talks to these two
protocols, so engines and human models can be swapped (Stockfish / Lc0, Maia-2 / QRE /
test fakes) without touching the math.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    import chess

    from cca.core.types import MoveEval


@runtime_checkable
class SearchEngine(Protocol):
    """A strong evaluator (the "machine" side of the human/machine gap)."""

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        """Evaluate root moves of ``board`` (all moves, or only ``moves``).

        Returns up to ``multipv`` :class:`MoveEval` whose ``q`` is the expected score of
        ``perspective`` after the move, best first. ``time_limit`` (seconds) additionally
        bounds this call in real-time play; ``None`` keeps the engine's own (reproducible)
        limit.
        """
        ...

    def new_game(self) -> None:
        """Forget position caches between games."""
        ...

    def close(self) -> None:
        """Release the engine process."""
        ...


@runtime_checkable
class HumanModel(Protocol):
    """A model of *which move a human would play* (the "human" side of the gap)."""

    def distribution(self, board: chess.Board, elo_self: int, elo_oppo: int) -> dict[str, float]:
        """Probability of each legal move for the side to move.

        ``elo_self`` is the rating of the human to move, ``elo_oppo`` the rating of the
        other side. Keys are UCI strings; values sum to 1 over legal moves.
        """
        ...

    def distributions(
        self, boards: Sequence[chess.Board], elo_self: int, elo_oppo: int
    ) -> list[dict[str, float]]:
        """Batched :meth:`distribution` (GPU models should override for throughput)."""
        ...
