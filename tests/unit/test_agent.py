import dataclasses

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent, expected_score_to_cp
from cca.core.types import Clock
from cca.neuro import Persona
from tests.conftest import FakeEngine, FakeHuman


def _play(agent: CAIMEAgent, opponent: FakeHuman, plies: int, seed: str = "x") -> list[str]:
    from cca.core.rng import DeterministicRng

    board = chess.Board()
    moves: list[str] = []
    for i in range(plies):
        if board.is_game_over():
            break
        if board.turn == chess.WHITE:
            uci = agent.choose(
                board, Clock(my_time=120.0, opp_time=120.0, my_inc=1.0, opp_inc=1.0)
            ).move
        else:
            uci = DeterministicRng("opp", seed, i).choice(opponent.distribution(board, 1500, 1900))
        board.push_uci(uci)
        moves.append(uci)
    return moves


def test_agent_plays_legal_moves_and_is_reproducible(
    fake_engine: FakeEngine, fake_human: FakeHuman
) -> None:
    a = CAIMEAgent(fake_engine, fake_human, AgentConfig(seed="r"), game_id="g")
    b = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed="r"), game_id="g")
    ma = _play(a, fake_human, 24)
    mb = _play(b, fake_human, 24)
    assert ma == mb
    assert a.chaos_digest == b.chaos_digest
    c = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed="other"), game_id="g")
    assert _play(c, fake_human, 24) != ma  # a different seed must change the game


def test_decision_invariants(fake_engine: FakeEngine, fake_human: FakeHuman) -> None:
    agent = CAIMEAgent(fake_engine, fake_human, AgentConfig())
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    d = agent.choose(board, Clock(my_time=60.0, opp_time=60.0))
    assert chess.Move.from_uci(d.move) in board.legal_moves
    assert sum(d.policy.values()) == pytest.approx(1.0)
    assert d.move in d.policy
    assert d.policy[d.move] > 0.0
    best_q = max(c.q_opt for c in d.candidates)
    chosen = next(c for c in d.candidates if c.uci == d.move)
    assert chosen.q_opt >= best_q - d.knobs.risk_budget - 1e-9  # safety constraint honoured
    assert 0.0 < d.think_time <= 60.0 * 0.25
    assert len(d.candidates) >= 1


def test_zero_risk_budget_and_argmax_plays_engine_best(
    fake_engine: FakeEngine, fake_human: FakeHuman
) -> None:
    persona = dataclasses.replace(Persona(), eps0=0.0, eps_max=0.0)
    agent = CAIMEAgent(fake_engine, fake_human, AgentConfig(persona=persona, sample=False))
    board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
    d = agent.choose(board)
    best = max(d.candidates, key=lambda c: c.q_opt)
    assert d.move == best.uci or next(
        c for c in d.candidates if c.uci == d.move
    ).q_opt == pytest.approx(best.q_opt)


def test_opponent_surprise_updates_state(fake_engine: FakeEngine, fake_human: FakeHuman) -> None:
    agent = CAIMEAgent(fake_engine, fake_human, AgentConfig())
    board = chess.Board()
    d1 = agent.choose(board)
    board.push_uci(d1.move)
    # opponent plays its least likely move -> high surprise
    dist = fake_human.distribution(board, 1500, 1900)
    rare = min(dist, key=lambda k: (dist[k], k))
    board.push_uci(rare)
    d2 = agent.choose(board)
    assert d2.trace["opp_surprise_excess"] > 0.0
    assert d2.state.stress > AgentConfig().neuro.c0


def test_new_position_stream_resets(fake_engine: FakeEngine, fake_human: FakeHuman) -> None:
    agent = CAIMEAgent(fake_engine, fake_human, AgentConfig())
    agent.choose(chess.Board())
    games_before = fake_engine.new_games
    other = chess.Board("8/8/8/4k3/8/8/4P3/4K3 w - - 0 1")
    other.push_uci("e1d1")
    other.push_uci("e5d5")
    agent.choose(other)
    assert fake_engine.new_games == games_before + 1


def test_game_over_position_raises(fake_engine: FakeEngine, fake_human: FakeHuman) -> None:
    agent = CAIMEAgent(fake_engine, fake_human)
    mate = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    with pytest.raises(ValueError, match="game over"):
        agent.choose(mate)


def test_mate_in_one_is_found_when_budget_zero(
    fake_engine: FakeEngine, fake_human: FakeHuman
) -> None:
    persona = dataclasses.replace(Persona(), eps0=0.0, eps_max=0.0)
    agent = CAIMEAgent(fake_engine, fake_human, AgentConfig(persona=persona))
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    assert agent.choose(board).move == "a1a8"


def test_cp_helper() -> None:
    assert expected_score_to_cp(0.5) == pytest.approx(0.0)
    assert expected_score_to_cp(0.9) > 0 > expected_score_to_cp(0.1)


class _PeakedHuman(FakeHuman):
    """Pathological prior (like Maia-2's opening knight shuffle): 99% on one move."""

    def distribution(
        self, board: chess.Board, elo_self: int, elo_oppo: int, time_limit: float | None = None
    ) -> dict[str, float]:
        base = super().distribution(board, elo_self, elo_oppo)
        if not base:
            return base
        top = min(base)
        rest = 0.01 / max(1, len(base) - 1)
        return {k: (0.99 if k == top else rest) for k in base}


def test_opening_prior_is_tempered(fake_engine: FakeEngine) -> None:
    agent = CAIMEAgent(fake_engine, _PeakedHuman(), AgentConfig())
    d = agent.choose(chess.Board())
    assert d.trace["opening_tempered"] == 1.0
    assert max(c.prior for c in d.candidates) < 0.9  # 0.99 flattened by T = 3
    late = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    for _ in range(10):  # push the ply counter past the opening window
        late.push(next(iter(late.legal_moves)))
    d2 = CAIMEAgent(FakeEngine(), _PeakedHuman(), AgentConfig()).choose(late)
    assert "opening_tempered" not in d2.trace
