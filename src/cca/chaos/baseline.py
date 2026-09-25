"""Control condition for the chaos driver: seeded AR(1) noise with matched autocorrelation.

The owner spec claims that deterministic chaos makes play *harder to predict* than noise. To
an observer who cannot see the internal state, a keyed pseudo-random process is already
unpredictable, so the claim is only meaningful as an A/B test (pre-registered prediction P5,
docs/science.md): Lorenz vs. a surrogate with the same short-range memory.

This driver reproduces the measured lag-1 autocorrelations of the Lorenz signals per ply
(x: 0.25, y: 0.14, z-maximum: 0.50) with independent Gaussian AR(1) processes, and exposes
the same interface as :class:`cca.chaos.LorenzOscillator`. It does *not* match the marginal
distributions or higher-order structure (an IAAFT surrogate would; roadmap).
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from cca.core.rng import DeterministicRng


@dataclass(frozen=True, slots=True)
class AR1Params:
    """Per-channel AR(1) coefficients and kick scaling."""

    phi: tuple[float, float, float] = (0.25, 0.14, 0.50)
    kick_scale: tuple[float, float, float] = (7.92, 9.01, 3.09)
    """Kicks are divided by the matching Lorenz channel std, so the same game events move
    both drivers by a comparable number of standard deviations."""
    max_kick: float = 5.0


class AR1Driver:
    """Three independent Gaussian AR(1) channels, advanced ply by ply."""

    __slots__ = ("_e", "_p", "_rng")

    def __init__(self, params: AR1Params | None = None, *seed_parts: object) -> None:
        self._p = params or AR1Params()
        if not all(0.0 <= f < 1.0 for f in self._p.phi):
            raise ValueError("phi must be in [0, 1)")
        self._rng = DeterministicRng("ar1-driver", *seed_parts)
        self._e = [self._rng.normal() for _ in range(3)]  # stationary start

    @classmethod
    def from_seed(cls, *seed_parts: object, params: AR1Params | None = None) -> AR1Driver:
        """Seeded constructor mirroring :meth:`LorenzOscillator.from_seed`."""
        return cls(params, *seed_parts)

    def advance_ply(self, kick: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        """Kick (scaled, capped), then one AR(1) step per channel."""
        p = self._p
        for i in range(3):
            k = kick[i] if math.isfinite(kick[i]) else 0.0
            k = max(-p.max_kick, min(p.max_kick, k)) / p.kick_scale[i]
            phi = p.phi[i]
            self._e[i] = phi * (self._e[i] + k) + math.sqrt(1.0 - phi * phi) * self._rng.normal()

    @property
    def state(self) -> tuple[float, float, float]:
        """Raw channel values."""
        return (self._e[0], self._e[1], self._e[2])

    def signals(self) -> tuple[float, float, float]:
        """``tanh`` of each standard-normal channel, in ``(-1, 1)``."""
        return (math.tanh(self._e[0]), math.tanh(self._e[1]), math.tanh(self._e[2]))

    def digest(self) -> str:
        """SHA-256 of the exact state (the RNG stream position is implied by the ply count)."""
        return hashlib.sha256(struct.pack("<3d", *self._e)).hexdigest()[:16]
