"""Chaos drivers: the Lorenz-63 strange attractor and its AR(1) control condition."""

from __future__ import annotations

from typing import Protocol

from cca.chaos.baseline import AR1Driver, AR1Params
from cca.chaos.lorenz import LorenzOscillator, LorenzParams


class ChaosDriver(Protocol):
    """What the agent needs from a temperament driver."""

    def advance_ply(self, kick: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        """Absorb game-event kicks and advance one ply."""
        ...

    def signals(self) -> tuple[float, float, float]:
        """Three signals in ``(-1, 1)``."""
        ...

    def digest(self) -> str:
        """Exact state fingerprint."""
        ...


DRIVERS = ("lorenz", "ar1")


def make_driver(kind: str, *seed_parts: object, lorenz: LorenzParams | None = None) -> ChaosDriver:
    """Build the driver named ``kind`` (``"lorenz"`` or its control ``"ar1"``)."""
    if kind == "lorenz":
        return LorenzOscillator.from_seed(*seed_parts, params=lorenz)
    if kind == "ar1":
        return AR1Driver.from_seed(*seed_parts)
    raise ValueError(f"unknown chaos driver {kind!r}; choose one of {DRIVERS}")


__all__ = [
    "DRIVERS",
    "AR1Driver",
    "AR1Params",
    "ChaosDriver",
    "LorenzOscillator",
    "LorenzParams",
    "make_driver",
]
