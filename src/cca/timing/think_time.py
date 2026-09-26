"""Human-like think-time model.

``t_n = budget_n * phase(m) * complexity_n * forced_n * exp(σ e_n - σ²/2)``

* ``budget``: clock time available per remaining move (``(T + inc * L) / L``), where the
  expected number of moves left ``L`` is a simple decreasing heuristic of the move number.
* ``phase``: inverted-U over the move number (fast opening, slowest middlegame, faster
  endgame), a Gaussian bump on top of a floor.
* ``complexity``: ``exp(k_c * (H - H_ref))`` with ``H`` the entropy of the agent's own human
  prior (a decision with many plausible options takes longer).
* ``forced``: near-forced moves (one legal move, or a dominant intuitive move) are fast.
* ``e_n``: Gaussian AR(1) in log-space, ``e_n = φ e_{n-1} + sqrt(1-φ²) ξ_n``, so the marginal
  is log-normal (right-skewed, heavy upper tail) and consecutive think times are
  correlated; ``exp(σ e - σ²/2)`` has mean exactly 1, so noise does not bias time usage.

Default parameters are hand-set placeholders to be fitted on human game clocks (roadmap);
see docs/science.md for the literature each term is inspired by.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cca.core.rng import DeterministicRng


@dataclass(frozen=True, slots=True)
class ThinkTimeParams:
    """Parameters of :class:`ThinkTimeModel` (hand-set defaults, not fitted)."""

    moves_left_a: float = 45.0
    moves_left_b: float = 0.6
    moves_left_min: float = 12.0
    phase_floor: float = 0.35
    phase_peak_move: float = 22.0
    phase_width: float = 12.0
    k_complexity: float = 0.35
    entropy_ref: float = 1.5
    forced_factor: float = 0.25
    dominant_prior: float = 0.85
    sigma: float = 0.5
    phi: float = 0.3
    min_time: float = 0.2
    max_fraction: float = 0.25
    """Never spend more than this fraction of the remaining clock on one move."""
    safety_margin: float = 0.5
    untimed_budget: float = 10.0


class ThinkTimeModel:
    """Stateful generator of think times for one game (AR(1) memory)."""

    __slots__ = ("_e", "_p", "_rng")

    def __init__(self, params: ThinkTimeParams | None = None, *seed_parts: object) -> None:
        self._p = params or ThinkTimeParams()
        if not 0.0 <= self._p.phi < 1.0:
            raise ValueError("phi must be in [0, 1)")
        self._rng = DeterministicRng("think-time", *seed_parts)
        self._e = self._rng.normal()  # stationary start: E[noise] = 1 from the first move

    def moves_left(self, move_number: int) -> float:
        """Heuristic expected number of moves still to play."""
        p = self._p
        return max(p.moves_left_min, p.moves_left_a - p.moves_left_b * move_number)

    def horizon(self, move_number: int, moves_to_go: int | None = None) -> float:
        """Moves the clock must last for: UCI ``movestogo`` when given, else the heuristic."""
        return float(moves_to_go) if moves_to_go else self.moves_left(move_number)

    def phase(self, move_number: int) -> float:
        """Inverted-U phase factor in ``[phase_floor, 1]``."""
        p = self._p
        bump = math.exp(-((move_number - p.phase_peak_move) ** 2) / (2.0 * p.phase_width**2))
        return p.phase_floor + (1.0 - p.phase_floor) * bump

    def sample(
        self,
        *,
        move_number: int,
        remaining: float | None,
        increment: float = 0.0,
        moves_to_go: int | None = None,
        prior_entropy: float = 1.5,
        top_prior: float = 0.0,
        n_legal: int = 20,
    ) -> float:
        """Draw the think time (seconds) for the next move and advance the AR(1) state."""
        p = self._p
        xi = self._rng.normal()
        self._e = p.phi * self._e + math.sqrt(1.0 - p.phi * p.phi) * xi
        noise = math.exp(p.sigma * self._e - 0.5 * p.sigma * p.sigma)

        left = self.horizon(move_number, moves_to_go)
        if remaining is None:
            budget = p.untimed_budget
        else:
            budget = max(0.0, remaining + increment * left) / max(1.0, left)
        complexity = math.exp(p.k_complexity * (prior_entropy - p.entropy_ref))
        forced = p.forced_factor if (n_legal <= 1 or top_prior >= p.dominant_prior) else 1.0
        t = budget * self.phase(move_number) * complexity * forced * noise

        if remaining is not None:
            ceiling = max(0.0, min(p.max_fraction * remaining, remaining - p.safety_margin))
            return max(0.0, min(max(p.min_time, t), ceiling))
        return max(p.min_time, t)
