"""Adapter tests over real UCI: a fake engine always, real Stockfish when available."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent
from cca.engines.qre_human import QREHumanModel
from cca.engines.stockfish import (
    EngineNotFoundError,
    StockfishEngine,
    find_stockfish,
    info_to_q,
    sf19_expected_score,
    sf_material,
)
from tests.conftest import make_launcher as _launcher


@pytest.fixture
def fake_sf(tmp_path: Path) -> Iterator[StockfishEngine]:
    eng = StockfishEngine(_launcher(tmp_path), nodes=100)
    yield eng
    eng.close()


def test_multipv_and_perspective(fake_sf: StockfishEngine) -> None:
    board = chess.Board("4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1")  # e4xd5 wins the queen
    evals = fake_sf.evaluate(board, perspective=chess.WHITE, multipv=3)
    assert len(evals) == 3
    assert evals[0].uci == "e4d5"
    assert evals[0].q > evals[1].q
    black_view = fake_sf.evaluate(board, perspective=chess.BLACK, multipv=1)
    assert black_view[0].q == pytest.approx(1.0 - evals[0].q)
    assert fake_sf.name.startswith("FakeFish")


def test_root_moves_restriction(fake_sf: StockfishEngine) -> None:
    board = chess.Board()
    only = [chess.Move.from_uci("a2a3"), chess.Move.from_uci("h2h4")]
    evals = fake_sf.evaluate(board, perspective=chess.WHITE, moves=only, multipv=10)
    assert {e.uci for e in evals} == {"a2a3", "h2h4"}
    assert all(len(e.pv) >= 1 for e in evals)


def test_terminal_position_returns_empty(fake_sf: StockfishEngine) -> None:
    mate = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert fake_sf.evaluate(mate, perspective=chess.WHITE) == []


def test_score_fallback_without_wdl(tmp_path: Path) -> None:
    with StockfishEngine(_launcher(tmp_path, no_wdl=True), nodes=100) as eng:
        evals = eng.evaluate(
            chess.Board("4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1"), perspective=chess.WHITE, multipv=2
        )
    assert 0.5 < evals[0].q <= 1.0


def test_info_to_q_none_without_score() -> None:
    assert info_to_q({}, chess.WHITE, chess.Board()) is None


def test_limits_and_lookup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="limit"):
        StockfishEngine(_launcher(tmp_path), nodes=None)
    monkeypatch.delenv("CCA_STOCKFISH", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(EngineNotFoundError):
        find_stockfish(tmp_path / "missing.exe")
    monkeypatch.setenv("CCA_STOCKFISH", str(_launcher(tmp_path)))
    assert find_stockfish().exists()


def test_agent_over_uci_with_qre_human(fake_sf: StockfishEngine) -> None:
    human = QREHumanModel(fake_sf)
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    dist = human.distribution(board, 1500, 1500)
    assert set(dist) == {m.uci() for m in board.legal_moves}
    assert sum(dist.values()) == pytest.approx(1.0)
    assert human.precision(1900) > human.precision(1100)
    assert len(human.distributions([board, board], 1500, 1500)) == 2
    agent = CAIMEAgent(fake_sf, human, AgentConfig(engine_multipv=3, reply_top=3))
    d = agent.choose(board)
    assert chess.Move.from_uci(d.move) in board.legal_moves
    with pytest.raises(ValueError, match="positive"):
        QREHumanModel(fake_sf, mu0=0.0)


@pytest.mark.engine
@pytest.mark.skipif(
    not os.environ.get("CCA_STOCKFISH"), reason="set CCA_STOCKFISH to a real Stockfish"
)
def test_real_stockfish_smoke() -> None:
    with StockfishEngine(nodes=50_000) as sf:
        assert "Stockfish" in sf.name
        board = chess.Board()
        evals = sf.evaluate(board, perspective=chess.WHITE, multipv=4)
        assert len(evals) == 4
        assert all(0.3 < e.q < 0.7 for e in evals)  # opening is roughly balanced
        human = QREHumanModel(sf)
        d = CAIMEAgent(sf, human, AgentConfig(engine_multipv=4, reply_top=3)).choose(board)
        assert chess.Move.from_uci(d.move) in board.legal_moves


def test_sf19_win_rate_model_properties() -> None:
    start = chess.Board()
    assert sf_material(start) == 78
    assert sf19_expected_score(0, 78) == pytest.approx(0.5)
    # By construction of SF17+ normalisation, +100 displayed cp = 50 % WIN probability,
    # i.e. expected score > 0.5 + 0.5 * P(draw) ... strictly between 0.5 and 1.
    q100 = sf19_expected_score(100, 58)
    assert 0.7 < q100 < 0.8
    for material in (17, 40, 58, 78):
        prev = 0.0
        for cp in range(-800, 801, 50):
            q = sf19_expected_score(cp, material)
            assert 0.0 <= q <= 1.0
            assert q >= prev  # monotone in cp
            prev = q
        assert sf19_expected_score(-150, material) == pytest.approx(
            1 - sf19_expected_score(150, material)
        )
    assert sf19_expected_score(100_000, 58) == pytest.approx(1.0)


@pytest.mark.engine
@pytest.mark.skipif(
    not os.environ.get("CCA_STOCKFISH"), reason="set CCA_STOCKFISH to a real Stockfish"
)
def test_win_rate_model_matches_the_real_engine_wdl() -> None:
    """Oracle = Stockfish itself: our cp -> expected score must reproduce its reported WDL."""
    import random

    import chess.engine

    rng = random.Random(19)
    errors: list[float] = []
    with chess.engine.SimpleEngine.popen_uci(str(find_stockfish())) as eng:
        eng.configure({"UCI_ShowWDL": True, "Threads": 1, "Hash": 16})
        board = chess.Board()
        while len(errors) < 40 and not board.is_game_over():
            info = eng.analyse(board, chess.engine.Limit(nodes=20_000))
            pov = info["score"].pov(board.turn)
            cp = pov.score()
            if cp is not None and "wdl" in info:
                truth = info["wdl"].pov(board.turn).expectation()
                errors.append(sf19_expected_score(cp, sf_material(board)) - truth)
            best = info["pv"][0]
            move = rng.choice(list(board.legal_moves)) if board.ply() % 5 == 4 else best
            board.push(move)
    assert len(errors) >= 20
    # Integer cp costs up to ~0.006 per position; the per-mille WDL rounding 0.001.
    assert max(abs(e) for e in errors) < 0.012, errors
    assert abs(sum(errors) / len(errors)) < 0.002, errors  # no systematic bias
