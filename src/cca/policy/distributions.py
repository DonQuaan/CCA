"""Numerically careful helpers for discrete distributions over move strings."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def normalize(weights: Mapping[str, float]) -> dict[str, float]:
    """Rescale non-negative weights to a probability distribution."""
    if any(w < 0.0 or not math.isfinite(w) for w in weights.values()):
        raise ValueError("weights must be finite and non-negative")
    total = math.fsum(weights.values())
    if total <= 0.0:
        raise ValueError("weights sum to zero")
    return {k: w / total for k, w in weights.items()}


def restrict(
    dist: Mapping[str, float], support: Iterable[str], floor: float = 0.0
) -> dict[str, float]:
    """Restrict ``dist`` to ``support`` and renormalise.

    ``floor`` mixes in a uniform component, ``(1 - floor) * p + floor / n``, so that moves the
    human model ignores (typical for engine-only ideas) keep a small non-zero anchor mass;
    without it the KL term would forbid them outright.
    """
    keys = list(dict.fromkeys(support))
    if not keys:
        raise ValueError("empty support")
    if not 0.0 <= floor <= 1.0:
        raise ValueError("floor must be in [0, 1]")
    raw = {k: max(0.0, dist.get(k, 0.0)) for k in keys}
    total = math.fsum(raw.values())
    n = len(keys)
    if total <= 0.0:
        return dict.fromkeys(keys, 1.0 / n)
    return {k: (1.0 - floor) * raw[k] / total + floor / n for k in keys}


def entropy(dist: Mapping[str, float]) -> float:
    """Shannon entropy in nats."""
    return -math.fsum(p * math.log(p) for p in dist.values() if p > 0.0)


def kl_divergence(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """``KL(p || q)`` in nats; ``inf`` if ``p`` puts mass where ``q`` has none."""
    total = 0.0
    for k, pk in p.items():
        if pk <= 0.0:
            continue
        qk = q.get(k, 0.0)
        if qk <= 0.0:
            return math.inf
        total += pk * math.log(pk / qk)
    return total


def softmax(logits: Mapping[str, float], temperature: float = 1.0) -> dict[str, float]:
    """Softmax with max-subtraction; ``temperature -> 0`` tends to argmax."""
    if temperature <= 0.0:
        raise ValueError("temperature must be positive")
    if not logits:
        raise ValueError("empty logits")
    top = max(logits.values())
    ex = {k: math.exp((v - top) / temperature) for k, v in logits.items()}
    return normalize(ex)


def temper(dist: Mapping[str, float], temperature: float) -> dict[str, float]:
    """Return ``p^(1/T)`` renormalised: T > 1 flattens (more errors), T < 1 sharpens."""
    if temperature <= 0.0:
        raise ValueError("temperature must be positive")
    logits = {k: math.log(p) for k, p in dist.items() if p > 0.0}
    return softmax(logits, temperature)
