"""End-to-end CLI tests over the fake UCI engine (real subprocesses, real python-chess I/O)."""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest

from cca.cli import main
from tests.conftest import make_launcher as _launcher

FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"


def test_cli_analyse_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engine = _launcher(tmp_path)
    code = main(
        [
            "analyse",
            FEN,
            "--stockfish",
            str(engine),
            "--human",
            "qre",
            "--nodes",
            "100",
            "--persona",
            "tal",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert chess.Move.from_uci(out["move"]) in chess.Board(FEN).legal_moves
    assert abs(sum(out["policy"].values()) - 1.0) < 1e-9
    assert {"knobs", "state", "candidates", "chaos_digest", "trace"} <= set(out)


def test_cli_match_writes_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engine = _launcher(tmp_path)
    out_dir = tmp_path / "run"
    code = main(
        [
            "match",
            "--stockfish",
            str(engine),
            "--human",
            "qre",
            "--nodes",
            "100",
            "--referee-nodes",
            "100",
            "--games",
            "2",
            "--max-plies",
            "6",
            "--out",
            str(out_dir),
            "--seed",
            "cli-test",
        ]
    )
    assert code == 0
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["games"] == 2
    manifest = summary["manifest"]
    assert manifest["engine"].startswith("FakeFish")
    assert manifest["config"]["seed"] == "cli-test"
    assert manifest["human_model"] == "QREHumanModel"
    assert (out_dir / "games.pgn").read_text(encoding="utf-8").count("[Event ") == 2
    assert "game 2/2" in capsys.readouterr().out


def test_cli_doctor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--stockfish", str(_launcher(tmp_path))]) == 0
    assert "engine ok: FakeFish" in capsys.readouterr().out
    # An explicit path that does not exist must fail, not silently use another binary.
    assert main(["doctor", "--stockfish", str(tmp_path / "missing.exe")]) == 1
