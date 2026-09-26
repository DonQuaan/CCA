"""Control condition for the chaos driver: seeded Gaussian AR(1) noise calibrated to Lorenz.

The owner spec claims that deterministic chaos makes play *harder to predict* than noise. To
an observer who cannot see the internal state, a keyed pseudo-random process is already
unpredictable, so the claim is only meaningful as an A/B test (pre-registered prediction P5,
docs/science.md): Lorenz vs. a stochastic surrogate with the same short-range statistics.

Calibration targets are the signals the agent actually consumes (``signals()``, after
``tanh``), measured on 20 000 plies of the default Lorenz driver:

* lag-1 autocorrelation per ply: 0.375 / 0.250 / 0.531 -> ``phi = (0.385, 0.26, 0.56)``
  (the ``tanh`` of a Gaussian AR(1) has a slightly lower lag-1 than ``phi``);
* cross-correlation of ``u0`` and ``u1``: 0.878 -> correlated innovations ``rho01 = 0.9``;
* mean absolute one-ply signal response to a kick of 4: 0.241 / 0.508 / 0.690 ->
  ``kick_scale = (9.89, 4.41, 2.98)`` (kick / scale is added to the Gaussian state *after* the
  AR step, so the immediate response is not damped by ``phi``).

It does *not* reproduce higher-order structure (lobe-switching bursts, the lag-4 tail); an
IAAFT / phase-randomised surrogate of the consumed signals would (roadmap). Tests compare the
two drivers' statistics so a change to either one cannot silently break the match.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from cca.core.rng import DeterministicRng


@dataclass(frozen=True, slots=True)
class AR1Params:
    """Per-channel AR(1) coefficients, innovation coupling and kick scaling."""

    phi: tuple[float, float, float] = (0.385, 0.26, 0.56)
    rho01: float = 0.9
    """Correlation of the innovations of channels 0 and 1."""
    kick_scale: tuple[float, float, float] = (9.89, 4.41, 2.98)
    max_kick: float = 5.0


class AR1Driver:
    """Three Gaussian AR(1) channels (0 and 1 coupled), advanced ply by ply."""

    __slots__ = ("_e", "_p", "_rng")

    def __init__(self, params: AR1Params | None = None, *seed_parts: object) -> None:
        self._p = params or AR1Params()
        if not all(0.0 <= f < 1.0 for f in self._p.phi):
            raise ValueError("phi must be in [0, 1)")
        if not -1.0 < self._p.rho01 < 1.0:
            raise ValueError("rho01 must be in (-1, 1)")
        self._rng = DeterministicRng("ar1-driver", *seed_parts)
        self._e = [self._rng.normal() for _ in range(3)]  # stationary start

    @classmethod
    def from_seed(cls, *seed_parts: object, params: AR1Params | None = None) -> AR1Driver:
        """Seeded constructor mirroring :meth:`LorenzOscillator.from_seed`."""
        return cls(params, *seed_parts)

    def advance_ply(self, kick: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        """One AR(1) step per channel, then add the (scaled, capped) kick."""
        p = self._p
        n0 = self._rng.normal()
        n1 = p.rho01 * n0 + math.sqrt(1.0 - p.rho01 * p.rho01) * self._rng.normal()
        innovations = (n0, n1, self._rng.normal())
        for i in range(3):
            phi = p.phi[i]
            k = kick[i] if math.isfinite(kick[i]) else 0.0
            k = max(-p.max_kick, min(p.max_kick, k)) / p.kick_scale[i]
            self._e[i] = phi * self._e[i] + math.sqrt(1.0 - phi * phi) * innovations[i] + k

    @property
    def state(self) -> tuple[float, float, float]:
        """Raw channel values."""
        return (self._e[0], self._e[1], self._e[2])

    def signals(self) -> tuple[float, float, float]:
        """``tanh`` of each (approximately standard-normal) channel, in ``(-1, 1)``."""
        return (math.tanh(self._e[0]), math.tanh(self._e[1]), math.tanh(self._e[2]))

    def digest(self) -> str:
        """SHA-256 of the exact state (the RNG stream position is implied by the ply count)."""
        return hashlib.sha256(struct.pack("<3d", *self._e)).hexdigest()[:16]
