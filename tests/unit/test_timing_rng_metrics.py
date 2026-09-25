import math
import statistics

import pytest

from cca.core.rng import DeterministicRng, derive_seed
from cca.core.types import WDL
from cca.eval import agreement_rate, error_profile, mean_log_likelihood, wilson_interval
from cca.timing import ThinkTimeModel, ThinkTimeParams


def test_rng_reproducible_and_stream_separated() -> None:
    a = [DeterministicRng("s", "x").uniform() for _ in range(3)]
    b = [DeterministicRng("s", "x").uniform() for _ in range(3)]
    assert a == b
    assert DeterministicRng("s", "x").uniform() != DeterministicRng("s", "y").uniform()
    assert derive_seed("a", 1) == derive_seed("a", 1) != derive_seed("a1")


def test_rng_normal_moments() -> None:
    r = DeterministicRng("normal")
    xs = [r.normal() for _ in range(40_000)]
    assert abs(statistics.fmean(xs)) < 0.03
    assert abs(statistics.pstdev(xs) - 1.0) < 0.03


def test_rng_choice_frequencies_and_errors() -> None:
    r = DeterministicRng("choice")
    counts = {"a": 0, "b": 0}
    for _ in range(20_000):
        counts[r.choice({"a": 0.25, "b": 0.75, "z": 0.0})] += 1
    assert abs(counts["b"] / 20_000 - 0.75) < 0.02
    with pytest.raises(ValueError, match="empty"):
        r.choice({"a": 0.0})


def test_wdl() -> None:
    w = WDL.from_permille(500, 300, 200)
    assert w.expected_score == pytest.approx(0.65)
    assert w.flipped().expected_score == pytest.approx(0.35)
    with pytest.raises(ValueError, match="invalid WDL"):
        WDL(0.5, 0.5, 0.5)
    with pytest.raises(ValueError, match="positive"):
        WDL.from_permille(0, 0, 0)


def test_think_time_noise_is_mean_preserving_and_autocorrelated() -> None:
    params = ThinkTimeParams(min_time=0.0, max_fraction=1.0, safety_margin=0.0)
    m = ThinkTimeModel(params, "seed")
    ts = [
        m.sample(move_number=22, remaining=None, prior_entropy=params.entropy_ref, n_legal=30)
        for _ in range(20_000)
    ]
    # E[exp(sigma e - sigma^2/2)] = 1 -> mean equals the deterministic budget (untimed: 10 s)
    assert statistics.fmean(ts) == pytest.approx(params.untimed_budget, rel=0.03)
    logs = [math.log(t) for t in ts]
    mu = statistics.fmean(logs)
    num = sum((logs[i] - mu) * (logs[i + 1] - mu) for i in range(len(logs) - 1))
    den = sum((x - mu) ** 2 for x in logs)
    assert num / den == pytest.approx(params.phi, abs=0.03)
    assert statistics.median(ts) < statistics.fmean(ts)  # right-skewed (log-normal)


def test_think_time_phase_is_inverted_u() -> None:
    m = ThinkTimeModel(None, "phase")
    assert m.phase(22) == pytest.approx(1.0)
    assert m.phase(2) < m.phase(22) > m.phase(70)
    assert m.phase(200) >= ThinkTimeParams().phase_floor


def test_think_time_respects_clock_and_forced_moves() -> None:
    m = ThinkTimeModel(None, "clock")
    for _ in range(500):
        t = m.sample(move_number=30, remaining=2.0, increment=0.0, n_legal=30)
        assert 0.0 <= t <= 2.0 * 0.25
    forced = ThinkTimeModel(None, "f").sample(move_number=22, remaining=600, n_legal=1)
    free = ThinkTimeModel(None, "f").sample(move_number=22, remaining=600, n_legal=30)
    assert forced == pytest.approx(free * ThinkTimeParams().forced_factor)
    with pytest.raises(ValueError, match="phi"):
        ThinkTimeModel(ThinkTimeParams(phi=1.0))


def test_error_profile_and_metrics() -> None:
    prof = error_profile([0.0, 0.06, 0.12, 0.3])
    assert prof.moves == 4
    assert (prof.inaccuracy_rate, prof.mistake_rate, prof.blunder_rate) == (0.75, 0.5, 0.25)
    assert error_profile([]).moves == 0
    with pytest.raises(ValueError, match="non-negative"):
        error_profile([-0.5])
    with pytest.raises(ValueError, match="increasing"):
        error_profile([0.1], thresholds=(0.2, 0.1, 0.3))
    assert agreement_rate(["a", "b"], ["a", "c"]) == 0.5
    assert agreement_rate([], []) == 0.0
    with pytest.raises(ValueError, match="length"):
        agreement_rate(["a"], [])
    assert mean_log_likelihood([1.0, 1.0]) == 0.0
    assert mean_log_likelihood([]) == 0.0
    lo, hi = wilson_interval(5, 100)
    assert lo < 0.05 < hi
    assert wilson_interval(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(5, 2)
