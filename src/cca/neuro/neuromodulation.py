r"""Module 1 — Virtual neuromodulation: latent stress / drive dynamics and tunnel vision.

Scientific status (see docs/science.md): "cortisol" and "dopamine" here are *labels* for two
latent control variables, inspired by (not models of) the literature on stress narrowing
attention and on reward-prediction errors. Each mechanism makes a falsifiable behavioural
prediction that the benchmark suite measures; none is claimed as physiology.

Dynamics (one step = one ply of the agent)
------------------------------------------
Stress follows the linear relaxation ODE of the owner spec,

.. math:: \dot C = I(t) - \gamma (C - C_0),

but is integrated *exactly* for a piecewise-constant input over the step ``Δ``:

.. math:: C_{n+1} = C_0 + (C_n - C_0)e^{-\gamma Δ} + \frac{I_n}{\gamma}(1 - e^{-\gamma Δ}),

which is unconditionally stable (explicit Euler is not for large ``γΔ``). The spec's clock
term ``α / ΔT_remain`` diverges as the clock runs out; it is replaced by the bounded
pressure ``p = exp(-budget / T_ref)`` where ``budget`` is the time available per remaining
move.

Input: ``I = κ_s (S - H) + κ_v max(0, -δ) + κ_t p`` where ``S = -ln P(a_oppo)`` is the
Shannon surprisal of the opponent's move under the human model, ``H`` the entropy of that
model (the *expected* surprisal, so ``S - H`` measures unexpectedness rather than mere
branching), and ``δ`` the reward-prediction error of the evaluation.

Drive is a leaky integrator of the reward-prediction error ``δ = V_now - V_predicted``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from cca.core.types import PsychState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class NeuroParams:
    """Parameters of the latent affect dynamics (defaults are hand-set, not fitted)."""

    c0: float = 0.2
    gamma_stress: float = 0.2
    """Recovery rate per ply (half-life ln2/0.2 ~ 3.5 plies)."""
    k_surprise: float = 0.15
    k_value_shock: float = 2.0
    k_time: float = 0.6
    time_ref: float = 3.0
    """Seconds-per-move budget at which time pressure reaches ``1/e``."""
    gamma_drive: float = 0.25
    k_drive: float = 3.0
    drive_cap: float = 1.0
    stress_cap: float = 3.0


def relax(value: float, baseline: float, rate: float, inp: float, dt: float = 1.0) -> float:
    """Exact step of ``dv/dt = inp - rate (v - baseline)`` with constant input."""
    if rate <= 0.0:
        return value + inp * dt
    decay = math.exp(-rate * dt)
    return baseline + (value - baseline) * decay + (inp / rate) * (1.0 - decay)


def time_pressure(
    remaining: float | None,
    increment: float,
    moves_left: float,
    time_ref: float,
) -> float:
    """Bounded time pressure in ``[0, 1)`` from the per-move time budget (0 if untimed)."""
    if remaining is None:
        return 0.0
    budget = max(0.0, remaining + increment * moves_left) / max(1.0, moves_left)
    return math.exp(-budget / max(1e-9, time_ref))


def surprisal(dist: Mapping[str, float], move: str, floor: float = 1e-6) -> float:
    """Shannon surprisal ``-ln P(move)`` in nats, floored to stay finite."""
    return -math.log(max(floor, dist.get(move, 0.0)))


def update_on_opponent_move(
    state: PsychState,
    params: NeuroParams,
    *,
    surprise_excess: float,
    rpe: float,
    pressure: float,
) -> PsychState:
    """Update the agent's own stress and drive after observing the opponent's move."""
    inp = (
        params.k_surprise * surprise_excess
        + params.k_value_shock * max(0.0, -rpe)
        + params.k_time * pressure
    )
    stress = relax(state.stress, params.c0, params.gamma_stress, inp)
    drive = state.drive * math.exp(-params.gamma_drive) + params.k_drive * rpe
    return replace(
        state,
        stress=min(params.stress_cap, max(0.0, stress)),
        drive=max(-params.drive_cap, min(params.drive_cap, drive)),
    )


def update_opponent_model(
    state: PsychState,
    params: NeuroParams,
    *,
    our_surprise_excess: float,
    opp_pressure: float,
) -> PsychState:
    """Theory of mind: estimate the stress our own move induced in the opponent."""
    inp = params.k_surprise * our_surprise_excess + params.k_time * opp_pressure
    opp = relax(state.opp_stress, params.c0, params.gamma_stress, inp)
    return replace(state, opp_stress=min(params.stress_cap, max(0.0, opp)))


# --------------------------------------------------------------------------- attention
def _square_xy(square: str) -> tuple[int, int]:
    return (ord(square[0]) - ord("a"), int(square[1]) - 1)


def battle_centroid(recent_moves: Sequence[str], decay: float = 0.6) -> tuple[float, float] | None:
    """Recency-weighted centroid of the from/to squares of the last moves (most recent last)."""
    if not recent_moves:
        return None
    sx = sy = sw = 0.0
    weight = 1.0
    for uci in reversed(recent_moves):
        for sq in (uci[0:2], uci[2:4]):
            fx, fy = _square_xy(sq)
            sx += weight * fx
            sy += weight * fy
            sw += weight
        weight *= decay
    return (sx / sw, sy / sw)


def attention_radius(
    stress: float, threshold: float = 0.8, sigma_max: float = 8.0, k: float = 1.5
) -> float:
    """Scan radius ``σ(C)``: full board below the threshold, shrinking as stress rises."""
    return sigma_max / (1.0 + k * max(0.0, stress - threshold))


def move_attention(uci: str, centroid: tuple[float, float] | None, sigma: float) -> float:
    """Gaussian attention weight of a move: the larger of its from/to square weights."""
    if centroid is None:
        return 1.0
    cx, cy = centroid
    best = 0.0
    for sq in (uci[0:2], uci[2:4]):
        fx, fy = _square_xy(sq)
        d2 = (fx - cx) ** 2 + (fy - cy) ** 2
        best = max(best, math.exp(-d2 / (2.0 * sigma * sigma)))
    return max(best, 1e-6)
