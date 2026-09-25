import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cca.core.types import Candidate, Knobs
from cca.policy import (
    entropy,
    kl_divergence,
    logit_quantal_response,
    normalize,
    pikl_objective,
    pikl_policy,
    reply_stats,
    restrict,
    safe_set,
    softmax,
    temper,
    utility,
)

MOVES = ["a", "b", "c", "d", "e"]


def _dist(ws: list[float]) -> dict[str, float]:
    return normalize(dict(zip(MOVES[: len(ws)], ws, strict=True)))


weights = st.lists(st.floats(0.01, 10.0), min_size=2, max_size=5)
utils = st.lists(st.floats(0.0, 1.0), min_size=5, max_size=5)


@settings(max_examples=200, deadline=None)
@given(
    weights, utils, st.floats(0.001, 5.0), st.lists(st.floats(0.01, 10.0), min_size=5, max_size=5)
)
def test_pikl_closed_form_is_the_maximiser(
    w: list[float], u: list[float], lam: float, other: list[float]
) -> None:
    anchor = _dist(w)
    util = {k: u[i] for i, k in enumerate(anchor)}
    pi = pikl_policy(anchor, util, lam)
    rival = _dist(other[: len(anchor)])
    assert pikl_objective(pi, anchor, util, lam) >= pikl_objective(rival, anchor, util, lam) - 1e-9
    assert pikl_objective(pi, anchor, util, lam) >= pikl_objective(anchor, anchor, util, lam) - 1e-9


def test_pikl_limits() -> None:
    anchor = {"a": 0.7, "b": 0.2, "c": 0.1}
    util = {"a": 0.4, "b": 0.6, "c": 0.5}
    assert pikl_policy(anchor, util, 0.0) == {"a": 0.0, "b": 1.0, "c": 0.0}
    near_anchor = pikl_policy(anchor, util, 1e6)
    assert all(abs(near_anchor[k] - anchor[k]) < 1e-5 for k in anchor)


def test_pikl_argmax_ties_split_by_anchor() -> None:
    pi = pikl_policy({"a": 0.75, "b": 0.25}, {"a": 0.5, "b": 0.5}, 0.0)
    assert pi == pytest.approx({"a": 0.75, "b": 0.25})


def test_pikl_rejects_uncovered_anchor_and_bad_lambda() -> None:
    with pytest.raises(ValueError, match="floor"):
        pikl_policy({"a": 1.0}, {"a": 0.5, "b": 0.5}, 0.1)
    with pytest.raises(ValueError, match="kl_weight"):
        pikl_policy({"a": 1.0}, {"a": 0.5}, -1.0)
    with pytest.raises(ValueError, match="empty"):
        pikl_policy({}, {}, 0.1)


def test_pikl_is_numerically_stable_for_tiny_lambda() -> None:
    pi = pikl_policy({"a": 0.5, "b": 0.5}, {"a": 1.0, "b": 0.0}, 1e-9)
    assert pi["a"] == pytest.approx(1.0)
    assert all(math.isfinite(v) for v in pi.values())


def test_qre_precision_limits() -> None:
    util = {"a": 0.1, "b": 0.9}
    assert logit_quantal_response(util, 0.0) == {"a": 0.5, "b": 0.5}
    sharp = logit_quantal_response(util, 1000.0)
    assert sharp["b"] > 0.999
    assert logit_quantal_response(util, 5.0)["b"] > logit_quantal_response(util, 1.0)["b"]


def test_distributions_helpers() -> None:
    d = {"a": 0.5, "b": 0.5}
    assert entropy(d) == pytest.approx(math.log(2))
    assert kl_divergence(d, d) == 0.0
    assert kl_divergence(d, {"a": 1.0}) == math.inf
    assert softmax({"a": 0.0, "b": 0.0}) == {"a": 0.5, "b": 0.5}
    assert temper({"a": 0.9, "b": 0.1}, 1e6)["a"] == pytest.approx(0.5, abs=1e-4)
    r = restrict({"a": 1.0}, ["a", "b"], floor=0.1)
    assert r == pytest.approx({"a": 0.95, "b": 0.05})
    assert restrict({}, ["a", "b"]) == {"a": 0.5, "b": 0.5}
    with pytest.raises(ValueError, match="non-negative"):
        normalize({"a": -1.0})
    with pytest.raises(ValueError, match="empty support"):
        restrict({"a": 1.0}, [], floor=0.1)
    with pytest.raises(ValueError, match="temperature"):
        softmax({"a": 1.0}, 0.0)


def test_reply_stats_conservative_for_missing_mass() -> None:
    probs = {"x": 0.5, "y": 0.3, "z": 0.2}
    scores = {"x": 0.9, "y": 0.2}  # z not evaluated
    s = reply_stats(probs, scores, q_opt=0.2)
    assert s.q_human == pytest.approx(0.5 * 0.9 + 0.3 * 0.2 + 0.2 * 0.2)
    assert s.coverage == pytest.approx(0.8)
    assert s.sharpness > 0.0
    assert s.entropy == pytest.approx(entropy(probs))


def test_reply_stats_terminal_and_temperature() -> None:
    assert reply_stats({}, {}, 0.7).q_human == 0.7
    probs = {"good": 0.9, "blunder": 0.1}
    scores = {"good": 0.5, "blunder": 1.0}
    calm = reply_stats(probs, scores, 0.5, opp_temperature=1.0)
    stressed = reply_stats(probs, scores, 0.5, opp_temperature=3.0)
    assert stressed.q_human > calm.q_human  # stressed opponents blunder more often


def _cand(uci: str, q_opt: float, q_h: float, h: float = 1.0) -> Candidate:
    return Candidate(uci, q_opt, q_h, prior=0.5, opp_entropy=h, sharpness=0.0)


def test_safe_set_keeps_best_and_respects_budget() -> None:
    cs = [_cand("a", 0.60, 0.60), _cand("b", 0.55, 0.80), _cand("c", 0.40, 0.95)]
    assert [c.uci for c in safe_set(cs, 0.0)] == ["a"]
    assert [c.uci for c in safe_set(cs, 0.06)] == ["a", "b"]
    assert safe_set([], 0.1) == []
    with pytest.raises(ValueError, match="risk_budget"):
        safe_set(cs, -0.1)


def test_utility_mixes_engine_and_human_values() -> None:
    k = Knobs(
        kl_weight=0.1,
        exploit=0.25,
        entropy_bonus=0.01,
        risk_budget=0.1,
        tunnel=0.0,
        opp_temperature=1.0,
    )
    c = _cand("a", 0.4, 0.8, h=2.0)
    assert utility(c, k) == pytest.approx(0.75 * 0.4 + 0.25 * 0.8 + 0.01 * 2.0)
    assert c.trap_value == pytest.approx(0.4)
