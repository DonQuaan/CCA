"""Tests for the evidence-driven upgrades (research brief 2026-09-25, section 7)."""

import dataclasses
import statistics

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent
from cca.chaos import AR1Driver, AR1Params, LorenzOscillator, make_driver
from cca.core.types import Clock, PsychState
from cca.neuro import Persona, knobs_from_state
from tests.conftest import FakeEngine, FakeHuman


def _lag1(v: list[float]) -> float:
    m = statistics.fmean(v)
    return sum((v[i] - m) * (v[i + 1] - m) for i in range(len(v) - 1)) / sum(
        (x - m) ** 2 for x in v
    )


def test_ar1_driver_matches_lorenz_short_memory() -> None:
    drv = AR1Driver.from_seed("acf")
    series: list[list[float]] = [[], [], []]
    for _ in range(20_000):
        drv.advance_ply()
        for k, s in enumerate(drv.signals()):
            series[k].append(s)
            assert -1.0 < s < 1.0
    for k, target in enumerate(AR1Params().phi):
        assert _lag1(series[k]) == pytest.approx(target, abs=0.04)


def test_ar1_driver_reproducible_and_kicked() -> None:
    a, b = AR1Driver.from_seed("s", "g"), AR1Driver.from_seed("s", "g")
    for _ in range(10):
        a.advance_ply((1.0, -2.0, 0.5))
        b.advance_ply((1.0, -2.0, 0.5))
    assert a.state == b.state
    assert a.digest() == b.digest()
    c = AR1Driver.from_seed("s", "g")
    for _ in range(10):
        c.advance_ply((float("nan"), 0.0, 0.0))
    assert c.state != a.state
    with pytest.raises(ValueError, match="phi"):
        AR1Driver(AR1Params(phi=(1.0, 0.1, 0.1)))


def test_make_driver() -> None:
    assert isinstance(make_driver("lorenz", "x"), LorenzOscillator)
    assert isinstance(make_driver("ar1", "x"), AR1Driver)
    with pytest.raises(ValueError, match="unknown chaos driver"):
        make_driver("logistic", "x")


def test_agent_runs_with_control_driver() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(chaos_driver="ar1"))
    d = agent.choose(chess.Board())
    assert chess.Move.from_uci(d.move) in chess.Board().legal_moves


def _k(**kw: float) -> float:
    state = PsychState(drive=kw.get("drive", 0.0))
    return knobs_from_state(
        state,
        Persona(),
        elo_self=1500,
        elo_oppo=1500,
        current_score=0.5,
        pressure=kw.get("pressure", 0.0),
    ).risk_budget


def test_risk_signs_follow_field_data() -> None:
    assert _k(pressure=0.9) < _k(pressure=0.0)  # time pressure -> risk-averse
    assert _k(drive=-0.8) > _k(drive=0.0)  # after own mistakes -> more risk (tilt)


def test_kl_weight_is_clamped() -> None:
    p = Persona()
    hot = knobs_from_state(
        PsychState(stress=3.0, chaos=(0.0, 0.0, 0.999)),
        p,
        elo_self=1500,
        elo_oppo=1500,
        current_score=0.5,
    )
    cold = knobs_from_state(
        PsychState(stress=0.0, chaos=(0.0, 0.0, -0.999)),
        p,
        elo_self=1500,
        elo_oppo=1500,
        current_score=0.5,
    )
    assert hot.kl_weight <= p.lam0 * p.lam_range + 1e-12
    assert cold.kl_weight >= p.lam0 / p.lam_range - 1e-12


def test_safety_bank_caps_risk_until_opponent_gives_something() -> None:
    generous = dataclasses.replace(
        Persona(), eps0=0.05, eps_max=0.3, g_eps=0.0, d_eps=0.0, d_tilt=0.0, l_eps=0.0
    )
    banked = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=generous))
    board = chess.Board()
    d = banked.choose(board)
    assert d.knobs.risk_budget <= generous.eps0 + 1e-12  # empty bank: only the base allowance
    assert d.trace["risk_bank"] == 0.0
    unbanked = CAIMEAgent(
        FakeEngine(), FakeHuman(), AgentConfig(persona=generous, safety_bank=False)
    )
    assert "risk_bank" not in unbanked.choose(board).trace


def test_bank_accounting_gift_minus_risk() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(sample=False))
    board = chess.Board()
    d1 = agent.choose(board)
    bank_after_first = agent._bank
    assert bank_after_first <= 0.0  # only risk has been taken so far
    assert agent._expect is not None
    # Pretend our move's worst case was 0.3 lower than what the opponent now allows:
    # the difference is a gift and must be credited.
    agent._expect = dataclasses.replace(agent._expect, q_opt=agent._expect.q_opt - 0.3)
    board.push_uci(d1.move)
    board.push(next(iter(board.legal_moves)))
    agent.choose(board)
    assert agent._bank > bank_after_first + 0.2


def test_ood_clock_damps_exploitation() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    board = chess.Board()
    calm = agent.choose(board, Clock(my_time=300, opp_time=300)).knobs.exploit
    agent2 = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    d = agent2.choose(board, Clock(my_time=300, opp_time=12))
    assert d.trace["opp_clock_ood"] == 1.0
    assert d.knobs.exploit == pytest.approx(calm * 0.5, rel=1e-6)
