"""Map the latent state (stress, drive, chaos) to the decision knobs of one move.

Each mapping is monotone and bounded, and every gain is a named persona parameter, so a
persona is a point in a small, documented parameter space rather than hidden code paths.

Mappings (``u = (u0, u1, u2)`` are the chaos signals in ``(-1, 1)``)::

    lambda_KL = lam0 * exp(g_lam * u2 + s_lam * (C - C0))      stress -> rely on intuition
                clamped to [lam0 / lam_range, lam0 * lam_range]
    omega     = sigmoid(logit(w0) + g_w * u0 + d_w * D + e_w * dElo/400)   contempt / mood
    lambda_H  = h0 * (1 + g_h * u1)_+
    epsilon   = min(eps_max, eps0 * exp(g_eps * u0) * (1 + d_eps * D_+ + d_tilt * D_-)
                          * (1 + l_eps * losing) * (1 - c_clock * p))
    alpha     = a_tunnel * max(0, C - tau)                  spatial tunnel vision (spec)
    habit     = a_habit * max(0, C - tau)                   stress -> habitual choice
    T_opp     = 1 + t_opp * max(0, C_opp - C0)

The two stress effects are deliberately separate knobs so each can be switched off and tested
on its own: ``alpha`` is the owner spec's spatial mask (Easterbrook-style narrowing), ``habit``
follows the stress-to-habit shift (Schwabe & Wolf 2009). Their direction is a *hypothesis*:
acute stress has also been reported to improve set-shifting (Gabrys et al. 2019).

Risk signs follow field data where it exists: less clock time -> more risk-averse moves
(``c_clock``), and more risk after one's own mistakes (``d_tilt`` on negative drive), both
reported for FIDE World Cup games by Carow & Witzig (2025, JEBO). ``p`` is the agent's own
bounded clock pressure.

``losing`` in ``[0, 1]`` makes the agent risk-seeking when behind (prospect-theory style
risk seeking in the loss domain); ``dElo = elo_self - elo_oppo`` implements contempt: the
stronger the agent relative to the opponent, the more it plays for the opponent's mistakes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cca.core.types import Knobs, PsychState


@dataclass(frozen=True, slots=True)
class Persona:
    """A playing personality: base knob values plus how state and chaos move them."""

    name: str = "balanced"
    lam0: float = 0.03
    lam_range: float = 4.0
    g_lam: float = 0.5
    s_lam: float = 0.3
    w0: float = 0.5
    g_w: float = 0.8
    d_w: float = 0.8
    e_w: float = 0.5
    h0: float = 0.004
    g_h: float = 0.5
    eps0: float = 0.04
    g_eps: float = 0.5
    d_eps: float = 0.5
    d_tilt: float = 0.5
    c_clock: float = 0.5
    l_eps: float = 1.0
    eps_max: float = 0.15
    a_tunnel: float = 1.0
    a_habit: float = 0.5
    tau: float = 0.8
    t_opp: float = 0.5


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(1.0 - 1e-9, max(1e-9, p))
    return math.log(p / (1.0 - p))


def knobs_from_state(
    state: PsychState,
    persona: Persona,
    *,
    elo_self: int,
    elo_oppo: int,
    current_score: float,
    baseline: float = 0.2,
    pressure: float = 0.0,
) -> Knobs:
    """Derive this move's knobs from the latent state (pure function).

    ``baseline`` is the resting stress ``C0`` of :class:`cca.neuro.NeuroParams`.
    """
    u0, u1, u2 = state.chaos
    lam = persona.lam0 * math.exp(persona.g_lam * u2 + persona.s_lam * (state.stress - baseline))
    lam = min(persona.lam0 * persona.lam_range, max(persona.lam0 / persona.lam_range, lam))
    omega = _sigmoid(
        _logit(persona.w0)
        + persona.g_w * u0
        + persona.d_w * state.drive
        + persona.e_w * (elo_self - elo_oppo) / 400.0
    )
    h = persona.h0 * max(0.0, 1.0 + persona.g_h * u1)
    losing = min(1.0, max(0.0, (0.5 - current_score) * 2.0))
    eps = persona.eps0 * math.exp(persona.g_eps * u0)
    eps *= 1.0 + persona.d_eps * max(0.0, state.drive) + persona.d_tilt * max(0.0, -state.drive)
    eps *= 1.0 + persona.l_eps * losing
    eps *= max(0.0, 1.0 - persona.c_clock * min(1.0, max(0.0, pressure)))
    return Knobs(
        kl_weight=lam,
        exploit=omega,
        entropy_bonus=h,
        risk_budget=min(persona.eps_max, eps),
        tunnel=persona.a_tunnel * max(0.0, state.stress - persona.tau),
        habit=persona.a_habit * max(0.0, state.stress - persona.tau),
        opp_temperature=1.0 + persona.t_opp * max(0.0, state.opp_stress - baseline),
    )
