r"""KL-regularised policy (piKL) and logit quantal response.

piKL (Jacob et al., ICML 2022) chooses, at a decision point, the policy that maximises
expected utility while staying close to a human "anchor" policy:

.. math::

    \pi^* = \arg\max_\pi \; \mathbb{E}_{a\sim\pi}[U(a)] - \lambda\, KL(\pi \,\|\, \tau)

For a single decision this concave problem has the closed form

.. math::

    \pi^*(a) \propto \tau(a)\, \exp(U(a) / \lambda)

(Gibbs variational principle). ``lambda -> 0`` recovers the engine's argmax; ``lambda -> inf``
recovers pure imitation of the human anchor. The owner spec's objective with the complexity
bonus, ``U = Q + lambda_H * H(s')``, is exactly this with ``lambda = 1``.

The logit quantal response of McKelvey & Palfrey (1995) is the special case of a uniform
anchor: ``P(a) \propto \exp(\mu\, U(a))`` with *precision* ``mu = 1 / lambda``. Note the sign
convention: in QRE, larger ``mu`` means *more* rational; "noise" is ``1 / mu``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from cca.policy.distributions import entropy, kl_divergence, normalize

if TYPE_CHECKING:
    from collections.abc import Mapping

_ARGMAX_LAMBDA = 1e-12


def pikl_policy(
    anchor: Mapping[str, float],
    utility: Mapping[str, float],
    kl_weight: float,
) -> dict[str, float]:
    """Closed-form KL-regularised best response ``pi(a) ∝ anchor(a) * exp(U(a) / lambda)``.

    Args:
        anchor: Human prior ``tau`` over the candidate set (must cover every utility key with
            positive mass; use :func:`cca.policy.distributions.restrict` with a floor).
        utility: Utility ``U(a)`` per candidate (expected-score units).
        kl_weight: ``lambda > 0``. Values below ``1e-12`` are treated as the argmax limit.

    Returns:
        Probability distribution over the keys of ``utility``.
    """
    if not utility:
        raise ValueError("empty utility")
    if kl_weight < 0.0 or not math.isfinite(kl_weight):
        raise ValueError("kl_weight must be finite and >= 0")
    missing = [k for k in utility if anchor.get(k, 0.0) <= 0.0]
    if missing:
        raise ValueError(f"anchor has no mass on {missing}; apply a floor first")

    if kl_weight <= _ARGMAX_LAMBDA:
        best = max(utility.values())
        winners = {k: anchor[k] for k, u in utility.items() if u >= best - 1e-12}
        return _complete(normalize(winners), utility)

    logits = {k: math.log(anchor[k]) + u / kl_weight for k, u in utility.items()}
    top = max(logits.values())
    return normalize({k: math.exp(v - top) for k, v in logits.items()})


def pikl_objective(
    policy: Mapping[str, float],
    anchor: Mapping[str, float],
    utility: Mapping[str, float],
    kl_weight: float,
) -> float:
    """Value of the piKL objective ``E_pi[U] - lambda * KL(pi || anchor)`` (for tests/audits)."""
    expected = math.fsum(p * utility[k] for k, p in policy.items() if p > 0.0)
    return expected - kl_weight * kl_divergence(policy, anchor)


def logit_quantal_response(utility: Mapping[str, float], precision: float) -> dict[str, float]:
    """Logit QRE choice rule ``P(a) ∝ exp(precision * U(a))`` (McKelvey & Palfrey, 1995)."""
    if precision < 0.0 or not math.isfinite(precision):
        raise ValueError("precision must be finite and >= 0")
    uniform = dict.fromkeys(utility, 1.0 / len(utility)) if utility else {}
    if precision == 0.0:
        return uniform
    return pikl_policy(uniform, utility, 1.0 / precision)


def policy_entropy(policy: Mapping[str, float]) -> float:
    """Entropy of the final policy: the agent's own unpredictability, in nats."""
    return entropy(policy)


def _complete(dist: Mapping[str, float], keys: Mapping[str, float]) -> dict[str, float]:
    return {k: dist.get(k, 0.0) for k in keys}
