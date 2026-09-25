"""Benchmark metrics (pure functions over recorded games).

Definitions (all in expected-score units from the mover's point of view):

* **move loss** ``l = q(best) - q(played) >= 0`` measured by a referee engine.
* **error classes**: inaccuracy ``l >= 0.05``, mistake ``l >= 0.10``, blunder ``l >= 0.20``.
  These thresholds are CCA defaults (configurable), not taken from a publication.
* **blunder-inducing rate**: share of the *opponent's* moves that are blunders; compared
  across agents facing the same opponent, it measures how well an agent creates problems.
* **engine agreement**: share of the agent's moves equal to the referee's best move (a
  proxy for how "engine-like", i.e. predictable to anti-cheat style analysis, play is).
* **human likelihood**: mean log-probability of the agent's moves under a human model at
  the agent's nominal rating (higher = more human-like).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_THRESHOLDS = (0.05, 0.10, 0.20)


@dataclass(frozen=True, slots=True)
class ErrorProfile:
    """Error statistics of one player over a set of moves."""

    moves: int
    mean_loss: float
    inaccuracy_rate: float
    mistake_rate: float
    blunder_rate: float


def error_profile(
    losses: Sequence[float], thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS
) -> ErrorProfile:
    """Summarise move losses into rates of inaccuracies / mistakes / blunders."""
    n = len(losses)
    if n == 0:
        return ErrorProfile(0, 0.0, 0.0, 0.0, 0.0)
    if any(loss < -1e-9 or not math.isfinite(loss) for loss in losses):
        raise ValueError("losses must be finite and non-negative")
    t1, t2, t3 = thresholds
    if not 0.0 < t1 <= t2 <= t3:
        raise ValueError("thresholds must be increasing and positive")
    return ErrorProfile(
        moves=n,
        mean_loss=math.fsum(losses) / n,
        inaccuracy_rate=sum(1 for x in losses if x >= t1) / n,
        mistake_rate=sum(1 for x in losses if x >= t2) / n,
        blunder_rate=sum(1 for x in losses if x >= t3) / n,
    )


def agreement_rate(played: Sequence[str], reference: Sequence[str]) -> float:
    """Share of positions where ``played[i] == reference[i]``."""
    if len(played) != len(reference):
        raise ValueError("sequences differ in length")
    if not played:
        return 0.0
    return sum(1 for a, b in zip(played, reference, strict=True) if a == b) / len(played)


def mean_log_likelihood(probs_of_played: Sequence[float], floor: float = 1e-6) -> float:
    """Mean ``ln p`` of the played moves under a model (floored for unseen moves)."""
    if not probs_of_played:
        return 0.0
    return math.fsum(math.log(max(floor, p)) for p in probs_of_played) / len(probs_of_played)


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial rate (report rates with uncertainty)."""
    if trials <= 0:
        return (0.0, 1.0)
    if not 0 <= successes <= trials:
        raise ValueError("successes must be in [0, trials]")
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))
