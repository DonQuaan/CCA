"""Real Maia-2 adapter tests (needs the `maia2` extra, Python <= 3.12 and the 267 MB weights).

Run in the Maia-2 environment, e.g. ``.venv-maia2/Scripts/python -m pytest -m maia2``.
"""

from __future__ import annotations

import importlib.util
import math

import chess
import pytest

pytestmark = [
    pytest.mark.maia2,
    pytest.mark.skipif(importlib.util.find_spec("maia2") is None, reason="maia2 not installed"),
]


@pytest.fixture(scope="module")
def maia() -> object:
    from cca.engines.maia2_human import Maia2HumanModel

    return Maia2HumanModel(model_type="rapid", device="gpu", save_root="weights/maia2")


def _check(dist: dict[str, float], board: chess.Board) -> None:
    assert set(dist) == {m.uci() for m in board.legal_moves}
    assert math.fsum(dist.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(p > 0.0 for p in dist.values())  # full precision: no move rounded to zero


def test_white_and_black_to_move(maia: object) -> None:
    from cca.engines.maia2_human import Maia2HumanModel

    assert isinstance(maia, Maia2HumanModel)
    white = chess.Board()
    _check(maia.distribution(white, 1500, 1500), white)
    black = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 3 3")
    dist = maia.distribution(black, 1500, 1500)
    _check(dist, black)  # mirrored back correctly: every key is a legal Black move


def test_probabilities_are_not_rounded(maia: object) -> None:
    from cca.engines.maia2_human import Maia2HumanModel

    assert isinstance(maia, Maia2HumanModel)
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    dist = maia.distribution(board, 1500, 1500)
    # maia2.inference_each rounds to 4 decimals (so p < 5e-5 would become exactly 0.0);
    # the adapter must return the full-precision softmax.
    assert any(abs(p - round(p, 4)) > 1e-9 for p in dist.values())


def test_batch_matches_single_and_rating_matters(maia: object) -> None:
    from cca.engines.maia2_human import Maia2HumanModel

    assert isinstance(maia, Maia2HumanModel)
    a = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 3 3")
    batch = maia.distributions([a, b], 1500, 1500)
    for board, dist in zip((a, b), batch, strict=True):
        single = maia.distribution(board, 1500, 1500)
        assert max(abs(dist[k] - single[k]) for k in single) < 1e-4
    low = maia.distribution(a, 1100, 1100)
    high = maia.distribution(a, 1900, 1900)
    assert max(abs(low[k] - high[k]) for k in low) > 1e-3


def test_loading_does_not_write_to_stdout(capfd: pytest.CaptureFixture[str]) -> None:
    from cca.engines.maia2_human import Maia2HumanModel

    Maia2HumanModel(model_type="rapid", device="cpu", save_root="weights/maia2")
    out, _ = capfd.readouterr()
    assert out == ""  # a UCI stream must stay clean


def test_agent_with_maia2_over_fake_engine(maia: object) -> None:
    from cca.agent import AgentConfig, CAIMEAgent
    from cca.engines.maia2_human import Maia2HumanModel
    from tests.conftest import FakeEngine

    assert isinstance(maia, Maia2HumanModel)
    board = chess.Board()
    board.push_uci("e2e4")
    d = CAIMEAgent(FakeEngine(), maia, AgentConfig(elo_self=1900, elo_oppo=1500)).choose(board)
    assert chess.Move.from_uci(d.move) in board.legal_moves
    assert d.trace["human_logp"] < 0.0
