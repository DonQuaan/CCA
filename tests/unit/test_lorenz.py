import math
import statistics

import pytest

from cca.chaos.lorenz import LorenzOscillator, LorenzParams


def test_same_seed_is_bit_identical() -> None:
    a = LorenzOscillator.from_seed("s", "g1")
    b = LorenzOscillator.from_seed("s", "g1")
    for i in range(50):
        kick = (0.1 * i, -0.2, 0.0)
        a.advance_ply(kick)
        b.advance_ply(kick)
    assert a.state == b.state
    assert a.digest() == b.digest()


def test_different_game_ids_diverge() -> None:
    a = LorenzOscillator.from_seed("s", "g1")
    b = LorenzOscillator.from_seed("s", "g2")
    assert a.state != b.state


def test_sensitive_dependence_on_tiny_kick() -> None:
    a = LorenzOscillator.from_seed("s", "g")
    b = LorenzOscillator.from_seed("s", "g")
    b.advance_ply((1e-3, 0.0, 0.0))  # one quantum
    a.advance_ply()
    for _ in range(40):
        a.advance_ply()
        b.advance_ply()
    ax, _, _ = a.state
    bx, _, _ = b.state
    assert abs(ax - bx) > 1.0  # chaos: a 1e-3 perturbation grows to O(1)


def test_kicks_below_quantum_are_ignored() -> None:
    a = LorenzOscillator.from_seed("s", "g")
    b = LorenzOscillator.from_seed("s", "g")
    a.advance_ply((4e-4, -4e-4, 1e-5))
    b.advance_ply()
    assert a.state == b.state


def test_non_finite_kick_is_ignored_and_large_kick_is_capped() -> None:
    a = LorenzOscillator.from_seed("s", "g")
    b = LorenzOscillator.from_seed("s", "g")
    a.advance_ply((math.nan, math.inf, 0.0))
    b.advance_ply()
    assert a.state == b.state
    c = LorenzOscillator(LorenzParams(steps_per_ply=1), (0.0, 0.0, 20.0))
    c.advance_ply((1e9, 0.0, 0.0))
    assert abs(c.state[0]) < 10.0


def test_state_stays_bounded_under_repeated_kicks() -> None:
    osc = LorenzOscillator.from_seed("bounded")
    for i in range(2000):
        osc.advance_ply((5.0 if i % 2 else -5.0, 5.0, -5.0))
        x, y, z = osc.state
        assert abs(x) < 60
        assert abs(y) < 80
        assert -20 < z < 110


def test_signals_are_in_open_interval() -> None:
    osc = LorenzOscillator.from_seed("sig")
    for _ in range(300):
        osc.advance_ply()
        assert all(-1.0 < u < 1.0 for u in osc.signals())


def test_invalid_params() -> None:
    with pytest.raises(ValueError, match="positive"):
        LorenzOscillator(LorenzParams(dt=0.0))


@pytest.mark.slow
def test_attractor_statistics_match_documented_constants() -> None:
    osc = LorenzOscillator(LorenzParams(steps_per_ply=1), (1.0, 1.0, 1.0))
    osc.integrate(5000)
    xs, zs = [], []
    for _ in range(200_000):
        osc.integrate(1)
        x, _, z = osc.state
        xs.append(x)
        zs.append(z)
    assert abs(statistics.fmean(zs) - 23.55) < 0.4
    assert abs(statistics.pstdev(zs) - 8.62) < 0.4
    assert abs(statistics.pstdev(xs) - 7.92) < 0.5


@pytest.mark.slow
def test_largest_lyapunov_exponent() -> None:
    p = LorenzParams(steps_per_ply=1)
    a = LorenzOscillator(p, (1.0, 1.0, 1.0))
    a.integrate(5000)
    d0 = 1e-9
    x, y, z = a.state
    b = LorenzOscillator(p, (x + d0, y, z))
    acc, t = 0.0, 0.0
    for _ in range(20_000):
        a.integrate(10)
        b.integrate(10)
        (ax, ay, az), (bx, by, bz) = a.state, b.state
        d = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2)
        acc += math.log(d / d0)
        t += 10 * p.dt
        b = LorenzOscillator(
            p, (ax + (bx - ax) * d0 / d, ay + (by - ay) * d0 / d, az + (bz - az) * d0 / d)
        )
    assert 0.8 < acc / t < 1.0  # literature: ~0.906


def _per_ply_signals(n: int) -> list[tuple[float, float, float]]:
    osc = LorenzOscillator.from_seed("acf")
    out = []
    for _ in range(n):
        osc.advance_ply()
        out.append(osc.signals())
    return out


def _lag1(v: list[float]) -> float:
    m = statistics.fmean(v)
    num = sum((v[i] - m) * (v[i + 1] - m) for i in range(len(v) - 1))
    return num / sum((x - m) ** 2 for x in v)


def test_signals_do_not_alternate_ply_to_ply() -> None:
    """Regression: raw z sampled every 40 steps has lag-1 acf -0.62 (predictable flip-flop)."""
    sig = _per_ply_signals(4000)
    for k in range(3):
        series = [s[k] for s in sig]
        assert _lag1(series) > 0.0, f"channel {k} alternates"
        mean = statistics.fmean(series)
        flips = sum(
            1 for i in range(1, len(series)) if (series[i] - mean) * (series[i - 1] - mean) < 0
        ) / (len(series) - 1)
        assert flips < 0.45, f"channel {k} flips sign too regularly ({flips:.2f})"


def test_raw_z_would_alternate() -> None:
    """Documents why z itself is not used as a signal."""
    osc = LorenzOscillator.from_seed("acf")
    zs = []
    for _ in range(4000):
        osc.advance_ply()
        zs.append(osc.state[2])
    assert _lag1(zs) < -0.4


@pytest.mark.slow
def test_lorenz_map_statistics() -> None:
    osc = LorenzOscillator(LorenzParams(steps_per_ply=1), (1.0, 1.0, 1.0))
    osc.integrate(5000)
    maxima = []
    last = osc.last_z_max
    for _ in range(400_000):
        osc.integrate(1)
        if osc.last_z_max != last:
            last = osc.last_z_max
            maxima.append(last)
    assert abs(statistics.fmean(maxima) - 38.04) < 0.2
    assert abs(statistics.pstdev(maxima) - 3.09) < 0.2
    assert min(maxima) > 29.0
    assert max(maxima) < 48.5
