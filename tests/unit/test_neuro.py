import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cca.core.types import PsychState
from cca.neuro import (
    NeuroParams,
    Persona,
    attention_radius,
    battle_centroid,
    knobs_from_state,
    move_attention,
    relax,
    surprisal,
    time_pressure,
    update_on_opponent_move,
    update_opponent_model,
)


@given(st.floats(0, 5), st.floats(0, 2), st.floats(0.01, 3), st.floats(-5, 5), st.floats(0.1, 3))
def test_relax_matches_analytic_solution(
    v0: float, base: float, rate: float, inp: float, dt: float
) -> None:
    expected = base + inp / rate + (v0 - base - inp / rate) * math.exp(-rate * dt)
    assert relax(v0, base, rate, inp, dt) == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_relax_zero_rate_is_integration() -> None:
    assert relax(1.0, 0.0, 0.0, 0.5, 2.0) == 2.0


def test_relax_is_stable_for_huge_rates() -> None:
    assert relax(10.0, 0.2, 1e6, 0.0) == pytest.approx(0.2)  # explicit Euler would explode


def test_time_pressure_bounds_and_monotonicity() -> None:
    assert time_pressure(None, 0, 30, 3.0) == 0.0
    ps = [time_pressure(t, 0.0, 30, 3.0) for t in (3000, 300, 60, 10, 1, 0)]
    assert all(0.0 <= p <= 1.0 for p in ps)
    assert ps == sorted(ps)
    assert time_pressure(0.0, 0.0, 30, 3.0) == 1.0
    assert time_pressure(10, 2.0, 30, 3.0) < time_pressure(10, 0.0, 30, 3.0)  # increment helps


def test_surprisal_is_finite() -> None:
    assert surprisal({"a": 1.0}, "a") == 0.0
    assert math.isfinite(surprisal({"a": 1.0}, "zz"))


def test_stress_rises_on_shock_and_recovers() -> None:
    p = NeuroParams()
    s0 = PsychState(stress=p.c0)
    shocked = update_on_opponent_move(s0, p, surprise_excess=3.0, rpe=-0.3, pressure=0.0)
    assert shocked.stress > s0.stress
    assert shocked.drive < 0
    calm = shocked
    for _ in range(40):
        calm = update_on_opponent_move(calm, p, surprise_excess=0.0, rpe=0.0, pressure=0.0)
    assert calm.stress == pytest.approx(p.c0, abs=1e-3)


def test_stress_and_drive_are_capped() -> None:
    p = NeuroParams()
    s = PsychState()
    for _ in range(100):
        s = update_on_opponent_move(s, p, surprise_excess=50.0, rpe=5.0, pressure=1.0)
    assert s.stress <= p.stress_cap
    assert -p.drive_cap <= s.drive <= p.drive_cap
    s = update_on_opponent_move(
        PsychState(stress=0.0), p, surprise_excess=-50.0, rpe=0.0, pressure=0.0
    )
    assert s.stress >= 0.0


def test_opponent_model_rises_with_our_surprise() -> None:
    p = NeuroParams()
    s = update_opponent_model(PsychState(), p, our_surprise_excess=2.0, opp_pressure=0.5)
    assert s.opp_stress > PsychState().opp_stress


def test_attention_mask() -> None:
    c = battle_centroid(["e2e4", "e7e5"])
    assert c == pytest.approx((4.0, 3.875))  # weights 1 (e7,e5) and 0.6 (e2,e4)
    assert battle_centroid([]) is None
    assert move_attention("a1b1", None, 1.0) == 1.0
    wide = attention_radius(0.0)
    narrow = attention_radius(3.0)
    assert narrow < wide == 8.0
    near = move_attention("d4d5", c, narrow)
    far = move_attention("a1a2", c, narrow)
    assert near > far > 0.0


def _knobs(
    stress: float = 0.2, drive: float = 0.0, opp_stress: float = 0.2
) -> tuple[float, float, float, float, float, float]:
    state = PsychState(stress=stress, drive=drive, opp_stress=opp_stress)
    k = knobs_from_state(state, Persona(), elo_self=1900, elo_oppo=1500, current_score=0.5)
    return (k.kl_weight, k.exploit, k.entropy_bonus, k.risk_budget, k.tunnel, k.opp_temperature)


def test_knobs_monotone_in_state() -> None:
    lam_calm, *_ = _knobs(stress=0.2)
    lam_stress, *_ = _knobs(stress=2.0)
    assert lam_stress > lam_calm  # stress -> rely on intuition
    _, w_low, *_ = _knobs(drive=-0.5)
    _, w_high, *_ = _knobs(drive=0.5)
    assert w_high > w_low
    *_, tunnel_calm, _ = _knobs(stress=0.2)
    *_, tunnel_hi, _ = _knobs(stress=2.0)
    assert tunnel_calm == 0.0 < tunnel_hi
    calm_habit = knobs_from_state(
        PsychState(stress=0.2), Persona(), elo_self=1900, elo_oppo=1500, current_score=0.5
    ).habit
    hot_habit = knobs_from_state(
        PsychState(stress=2.0), Persona(), elo_self=1900, elo_oppo=1500, current_score=0.5
    ).habit
    assert calm_habit == 0.0 < hot_habit
    *_, t_calm = _knobs(opp_stress=0.2)
    *_, t_hi = _knobs(opp_stress=2.0)
    assert t_calm == 1.0 < t_hi


@given(
    st.tuples(st.floats(-1, 1), st.floats(-1, 1), st.floats(-1, 1)),
    st.floats(0, 3),
    st.floats(-1, 1),
    st.floats(0, 1),
)
def test_knobs_bounded(
    chaos: tuple[float, float, float], stress: float, drive: float, score: float
) -> None:
    persona = Persona()
    k = knobs_from_state(
        PsychState(stress=stress, drive=drive, chaos=chaos),
        persona,
        elo_self=2600,
        elo_oppo=800,
        current_score=score,
    )
    assert k.kl_weight > 0
    assert 0.0 <= k.exploit <= 1.0
    assert 0.0 <= k.risk_budget <= persona.eps_max
    assert k.entropy_bonus >= 0.0
    assert k.opp_temperature >= 1.0


def test_contempt_and_loss_domain_risk() -> None:
    p = Persona()
    weak = knobs_from_state(PsychState(), p, elo_self=2000, elo_oppo=1200, current_score=0.5)
    equal = knobs_from_state(PsychState(), p, elo_self=1500, elo_oppo=1500, current_score=0.5)
    assert weak.exploit > equal.exploit
    losing = knobs_from_state(PsychState(), p, elo_self=1500, elo_oppo=1500, current_score=0.1)
    assert losing.risk_budget > equal.risk_budget
