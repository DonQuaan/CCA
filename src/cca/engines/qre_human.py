r"""Engine-derived human model: logit quantal response over engine values.

This is the model-free fallback when Maia-2 is not installed, and a baseline in benchmarks.
It follows the idea of Regan & Haworth's intrinsic-rating model (move choice probability
falls with the move's value deficit, more sharply for stronger players) in its simplest
logit-QRE form:

.. math:: P_R(a) \propto \exp(\mu(R)\, q(a)), \qquad \mu(R) = \mu_0 \, 2^{(R - 1500)/\Delta}

The precision schedule ``mu(R)`` is an **unfitted placeholder** (documented as such); fit it
on human games before drawing conclusions from it. Unlike Maia it cannot represent
*systematic* human blind spots — only value-proportional noise — which is exactly why Maia
is the primary human model.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from cca.policy.pikl import logit_quantal_response

if TYPE_CHECKING:
    from collections.abc import Sequence

    import chess

    from cca.engines.base import SearchEngine


class QREHumanModel:
    """:class:`cca.engines.base.HumanModel` built from a :class:`SearchEngine`."""

    def __init__(
        self, engine: SearchEngine, mu0: float = 15.0, doubling_elo: float = 500.0
    ) -> None:
        if mu0 <= 0.0 or doubling_elo <= 0.0:
            raise ValueError("mu0 and doubling_elo must be positive")
        self._engine = engine
        self._mu0 = mu0
        self._doubling = doubling_elo

    def precision(self, elo: int) -> float:
        """Logit precision ``mu(R)`` for a player of rating ``elo``."""
        return self._mu0 * math.pow(2.0, (elo - 1500) / self._doubling)

    def distribution(
        self, board: chess.Board, elo_self: int, elo_oppo: int, time_limit: float | None = None
    ) -> dict[str, float]:
        """Logit response of the side to move over *all* legal moves."""
        del elo_oppo  # the simple QRE model ignores the opponent's rating
        n_legal = board.legal_moves.count()
        if n_legal == 0:
            return {}
        evals = self._engine.evaluate(
            board, perspective=board.turn, multipv=n_legal, time_limit=time_limit
        )
        values = {e.uci: e.q for e in evals}
        for move in board.legal_moves:  # engines may drop lines; never lose a legal move
            values.setdefault(move.uci(), min(values.values(), default=0.5))
        return logit_quantal_response(values, self.precision(elo_self))

    def distributions(
        self,
        boards: Sequence[chess.Board],
        elo_self: int,
        elo_oppo: int,
        time_limit: float | None = None,
    ) -> list[dict[str, float]]:
        """Sequential batch; ``time_limit`` is split evenly over the boards."""
        per = None if time_limit is None else time_limit / max(1, len(boards))
        return [self.distribution(b, elo_self, elo_oppo, per) for b in boards]
