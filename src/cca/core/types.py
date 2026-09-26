"""Immutable value types shared by every CCA module.

Conventions used everywhere in CCA:

* **Expected score** ``q`` is in ``[0, 1]`` and is always expressed from the point of view of
  the CCA agent (win = 1, draw = 0.5, loss = 0), never from the side to move.
* Move identifiers are UCI strings (``"e2e4"``, ``"e7e8q"``) so that the math core never
  depends on a chess library.
* Probabilities are plain ``dict[str, float]`` that sum to 1 over their support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class WDL:
    """Win/draw/loss probabilities from one side's point of view."""

    win: float
    draw: float
    loss: float

    def __post_init__(self) -> None:
        total = self.win + self.draw + self.loss
        if min(self.win, self.draw, self.loss) < -1e-9 or abs(total - 1.0) > 1e-6:
            raise ValueError(f"invalid WDL {self.win}, {self.draw}, {self.loss}")

    @property
    def expected_score(self) -> float:
        """Expected game score ``W + D/2``."""
        return self.win + 0.5 * self.draw

    def flipped(self) -> WDL:
        """Same outcome seen from the other side."""
        return WDL(self.loss, self.draw, self.win)

    @classmethod
    def from_permille(cls, win: int, draw: int, loss: int) -> WDL:
        """Build from UCI ``info ... wdl W D L`` integers (per mille)."""
        total = win + draw + loss
        if total <= 0:
            raise ValueError("WDL permille must sum to a positive number")
        return cls(win / total, draw / total, loss / total)


@dataclass(frozen=True, slots=True)
class MoveEval:
    """Engine verdict for one root move, from the CCA agent's point of view."""

    uci: str
    q: float
    """Agent expected score after this move with best play by both sides."""
    depth: int = 0
    pv: tuple[str, ...] = ()
    """Principal variation starting with ``uci``."""


@dataclass(frozen=True, slots=True)
class Clock:
    """Remaining clock state in seconds (``None`` fields mean "unknown / untimed")."""

    my_time: float | None = None
    opp_time: float | None = None
    my_inc: float = 0.0
    opp_inc: float = 0.0
    moves_to_go: int | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """Everything the decision core knows about one of the agent's candidate moves."""

    uci: str
    q_opt: float
    """Agent expected score if the opponent answers perfectly (engine truth)."""
    q_human: float
    """Agent expected score if the opponent answers like a human of their rating."""
    prior: float
    """Human prior of the agent itself playing this move (Maia-2 at the agent's rating)."""
    opp_entropy: float
    """Entropy (nats) of the opponent's human reply distribution: decision complexity."""
    sharpness: float
    """Std-dev of the agent's score across human-weighted replies: how knife-edge it is."""
    attention: float = 1.0
    """Tunnel-vision attention weight in ``(0, 1]`` of the move's squares."""

    @property
    def trap_value(self) -> float:
        """Expected gain from the opponent's likely mistakes, ``q_human - q_opt``."""
        return self.q_human - self.q_opt


@dataclass(frozen=True, slots=True)
class PsychState:
    """Latent, *virtual* affective state of the agent (see docs/science.md for status).

    These are control variables inspired by findings on stress, reward-prediction error and
    attention; they are not physiological models.
    """

    stress: float = 0.2
    """Latent arousal ``C(t) >= 0`` (the owner spec calls it "virtual cortisol"; it is not a
    physiological model)."""
    drive: float = 0.0
    """Leaky integrator of evaluation surprises (reward-prediction errors); + = things went
    better than expected. Called "virtual dopamine" in the owner spec."""
    opp_stress: float = 0.2
    """Theory-of-mind estimate of the opponent's stress."""
    chaos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    """Normalised chaotic signals ``u_i in (-1, 1)`` from the strange attractor."""


@dataclass(frozen=True, slots=True)
class Knobs:
    """Decision parameters for one move, derived from :class:`PsychState`."""

    kl_weight: float
    """piKL temperature ``lambda_KL > 0``; large => imitate the human prior."""
    exploit: float
    """``omega in [0, 1]``: weight of the human-aware value vs the worst-case value."""
    entropy_bonus: float
    """``lambda_H >= 0``: bonus per nat of opponent decision entropy."""
    risk_budget: float
    """``epsilon >= 0``: max expected-score sacrifice vs the engine's best move."""
    tunnel: float
    """``alpha >= 0``: exponent on attention weights (0 = no tunnel vision)."""
    opp_temperature: float
    """``>= 1``: flattening of the opponent model when the opponent is under stress."""
    habit: float = 0.0
    """``>= 0``: stress-induced sharpening of the agent's own prior toward its habitual
    (most intuitive) move: the anchor is tempered with ``T = 1 / (1 + habit)``."""


@dataclass(frozen=True, slots=True)
class Decision:
    """Output of one call to the decision core."""

    move: str
    policy: Mapping[str, float]
    candidates: tuple[Candidate, ...]
    knobs: Knobs
    state: PsychState
    think_time: float
    """Seconds the agent "wants" to spend, from the human think-time model."""
    trace: Mapping[str, float] = field(default_factory=dict)
    """Diagnostics (surprise, RPE, pressure, ...) for logging and research."""
