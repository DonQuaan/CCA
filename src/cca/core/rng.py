"""Deterministic, platform-independent random streams.

Every stochastic choice in CCA (move sampling, think-time noise, initial conditions) flows
through :class:`DeterministicRng`, keyed by ``(seed, stream)``. The same seed therefore
replays the same game, which is what makes experiments auditable. Uniform draws come from
Mersenne Twister (bit-stable by CPython's guarantee); normals use ``log/cos/sin`` from libm,
which could in principle differ in the last bit across platforms — golden-value tests in CI
(Linux and Windows) check that they do not.

Only :meth:`random.Random.random` is used as the entropy source: CPython guarantees that it
reproduces the same sequence for the same seed across versions (unlike ``gauss``), so the
normal deviates are derived here with Box-Muller instead of ``random.gauss``.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def derive_seed(*parts: object) -> int:
    """Hash arbitrary parts into a 64-bit seed (stable across processes and platforms)."""
    text = "\x1f".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


class DeterministicRng:
    """A named, reproducible random stream."""

    __slots__ = ("_rng", "_spare")

    def __init__(self, *parts: object) -> None:
        self._rng = random.Random(derive_seed(*parts))  # noqa: S311 - research RNG, not crypto
        self._spare: float | None = None

    def uniform(self) -> float:
        """Uniform deviate in ``[0, 1)``."""
        return self._rng.random()

    def normal(self) -> float:
        """Standard normal deviate (Box-Muller on :meth:`uniform`)."""
        if self._spare is not None:
            value, self._spare = self._spare, None
            return value
        u1 = 1.0 - self._rng.random()  # (0, 1] so log() is finite
        u2 = self._rng.random()
        radius = math.sqrt(-2.0 * math.log(u1))
        self._spare = radius * math.sin(2.0 * math.pi * u2)
        return radius * math.cos(2.0 * math.pi * u2)

    def choice(self, weights: Mapping[str, float]) -> str:
        """Sample a key proportionally to its weight (keys sorted first, for determinism)."""
        items = sorted((k, w) for k, w in weights.items() if w > 0.0)
        if not items:
            raise ValueError("cannot sample from an empty / all-zero distribution")
        total = math.fsum(w for _, w in items)
        target = self.uniform() * total
        acc = 0.0
        for key, weight in items:
            acc += weight
            if target < acc:
                return key
        return items[-1][0]
