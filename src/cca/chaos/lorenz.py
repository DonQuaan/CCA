"""Deterministic chaos driver: a forced Lorenz-63 oscillator integrated with classical RK4.

Why a strange attractor instead of random noise
-----------------------------------------------
The attractor gives *structured* unpredictability: the lobe variable ``x`` stays on one lobe
for an irregular number of turns and then switches (a persistent "mood" with abrupt regime
changes). Unlike i.i.d. noise, the signal is autocorrelated over a few plies, bounded, and
exactly replayable from its seed.

Numerical facts (measured with this integrator, dt = 0.01, after burn-in; see
``tests/unit/test_lorenz.py``):

* mean/std: x ~ 0.0 / 7.92, y ~ 0.0 / 9.01, z ~ 23.55 / 8.62
* largest Lyapunov exponent ~ 0.90 (literature value ~ 0.906)
* ``z`` itself oscillates quasi-periodically inside a lobe (period ~ 75 steps). Sampled once
  per ply at 40 steps it has lag-1 autocorrelation -0.62 and changes sign on ~80 % of plies:
  a near-alternating, *predictable* pattern. The third signal therefore uses the **last local
  maximum of z** (the Lorenz map of successive maxima, Lorenz 1963, which is chaotic):
  mean 38.04, std 3.09, lag-1 autocorrelation +0.50 per ply, decorrelated after ~4 plies.
* ``x`` sampled per ply: lag-1 +0.25; ``y``: lag-1 +0.14.
* Burn-in of 3000 steps is needed before an ensemble matches the invariant measure (an
  independent Kolmogorov-Smirnov check found 1000 steps insufficient).

Reproducibility
---------------
Integration uses only ``+ - *`` and comparisons on Python floats (IEEE-754 binary64, correctly
rounded, no FMA contraction in CPython; no ``**``, which calls libm ``pow``), in a fixed order,
so trajectories are bit-identical across platforms. External inputs (surprise kicks computed
from neural networks) are quantised before injection so that last-ulp differences between
GPU/CPU inference cannot fork the trajectory except exactly at a quantisation boundary.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from cca.core.rng import DeterministicRng

# Attractor statistics for the default parameters (measured, see module docstring).
_X_STD = 7.92
_Y_STD = 9.01
_ZMAX_MEAN = 38.04
_ZMAX_STD = 3.09


@dataclass(frozen=True, slots=True)
class LorenzParams:
    """Parameters of the forced Lorenz-63 system and its integrator."""

    sigma: float = 10.0
    rho: float = 28.0
    beta: float = 8.0 / 3.0
    dt: float = 0.01
    steps_per_ply: int = 40
    burn_in: int = 3000
    kick_quantum: float = 1e-3
    """Resolution to which external kicks are rounded before entering the ODE."""
    max_kick: float = 5.0
    """Absolute cap on any single kick (keeps the state inside the trapping region)."""


def _quantise(value: float, quantum: float, cap: float) -> float:
    if not math.isfinite(value):
        return 0.0
    clipped = max(-cap, min(cap, value))
    return round(clipped / quantum) * quantum


class LorenzOscillator:
    """Forced Lorenz-63 system ``dX/dt = F(X)``, advanced ply by ply."""

    __slots__ = ("_p", "_x", "_y", "_z", "_z_max", "_z_prev", "_z_prev2")

    def __init__(
        self,
        params: LorenzParams | None = None,
        state: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> None:
        self._p = params or LorenzParams()
        if self._p.dt <= 0.0 or self._p.steps_per_ply <= 0:
            raise ValueError("dt and steps_per_ply must be positive")
        self._x, self._y, self._z = state
        self._z_prev = self._z_prev2 = self._z
        self._z_max = _ZMAX_MEAN  # neutral until the first maximum is seen

    @classmethod
    def from_seed(cls, *seed_parts: object, params: LorenzParams | None = None) -> LorenzOscillator:
        """Start from a seed-derived point and burn in until it sits on the attractor."""
        p = params or LorenzParams()
        rng = DeterministicRng("lorenz-init", *seed_parts)
        start = (
            rng.uniform() * 20.0 - 10.0,
            rng.uniform() * 20.0 - 10.0,
            rng.uniform() * 20.0 + 10.0,
        )
        osc = cls(p, start)
        osc.integrate(p.burn_in)
        return osc

    # ------------------------------------------------------------------ dynamics
    def _deriv(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        p = self._p
        return (p.sigma * (y - x), x * (p.rho - z) - y, x * y - p.beta * z)

    def integrate(self, n_steps: int) -> None:
        """Advance ``n_steps`` classical RK4 steps (fixed operation order).

        Also tracks the most recent local maximum of ``z`` (Lorenz map).
        """
        dt = self._p.dt
        half = 0.5 * dt
        sixth = dt / 6.0
        x, y, z = self._x, self._y, self._z
        z1, z2, zmax = self._z_prev, self._z_prev2, self._z_max
        for _ in range(n_steps):
            k1x, k1y, k1z = self._deriv(x, y, z)
            k2x, k2y, k2z = self._deriv(x + half * k1x, y + half * k1y, z + half * k1z)
            k3x, k3y, k3z = self._deriv(x + half * k2x, y + half * k2y, z + half * k2z)
            k4x, k4y, k4z = self._deriv(x + dt * k3x, y + dt * k3y, z + dt * k3z)
            x = x + sixth * (k1x + 2.0 * k2x + 2.0 * k3x + k4x)
            y = y + sixth * (k1y + 2.0 * k2y + 2.0 * k3y + k4y)
            z = z + sixth * (k1z + 2.0 * k2z + 2.0 * k3z + k4z)
            if z1 > z2 and z1 >= z:  # the previous step was a local maximum of z
                zmax = z1
            z2, z1 = z1, z
        self._x, self._y, self._z = x, y, z
        self._z_prev, self._z_prev2, self._z_max = z1, z2, zmax

    def advance_ply(self, kick: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        """Apply a (quantised, capped) impulse to the state, then integrate one ply.

        An impulse is a discrete forcing term: game events (a shocking move, a sudden eval
        swing) push the trajectory, which the attractor's dynamics then fold back onto the
        attractor. The Lorenz system has a global trapping region, so bounded kicks keep the
        state bounded.
        """
        q, cap = self._p.kick_quantum, self._p.max_kick
        self._x += _quantise(kick[0], q, cap)
        self._y += _quantise(kick[1], q, cap)
        self._z += _quantise(kick[2], q, cap)
        self.integrate(self._p.steps_per_ply)

    # ------------------------------------------------------------------ outputs
    @property
    def state(self) -> tuple[float, float, float]:
        """Raw state ``(x, y, z)``."""
        return (self._x, self._y, self._z)

    @property
    def last_z_max(self) -> float:
        """Most recent local maximum of ``z`` (one step of the Lorenz map)."""
        return self._z_max

    def signals(self) -> tuple[float, float, float]:
        """Normalised signals in ``(-1, 1)``.

        * ``u[0] = tanh(x / σx)``: lobe = persistent mood regime -> exploitation / risk.
        * ``u[1] = tanh(y / σy)``: faster component -> appetite for complexity.
        * ``u[2] = tanh((z_max - μ) / σ)``: Lorenz-map maximum -> KL anchoring / exploration.
        """
        return (
            math.tanh(self._x / _X_STD),
            math.tanh(self._y / _Y_STD),
            math.tanh((self._z_max - _ZMAX_MEAN) / _ZMAX_STD),
        )

    def digest(self) -> str:
        """SHA-256 of the exact binary64 state (incl. map memory), for reproducibility audits."""
        packed = struct.pack(
            "<6d", self._x, self._y, self._z, self._z_prev, self._z_prev2, self._z_max
        )
        return hashlib.sha256(packed).hexdigest()[:16]
