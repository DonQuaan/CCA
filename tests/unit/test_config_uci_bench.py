import json
from pathlib import Path

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent
from cca.bench.match import CCAPlayer, HumanModelPlayer, play_game, summarize, to_pgn, write_results
from cca.cli import build_parser, main
from cca.config import ConfigError, list_personas, load_agent_config, load_persona
from cca.uci.protocol import parse_go, parse_position, parse_uci_opponent
from tests.conftest import FakeEngine, FakeHuman


def test_shipped_personas_load() -> None:
    names = list_personas()
    assert {"balanced", "tal", "dubov", "solid", "human"} <= set(names)
    for n in names:
        assert load_persona(n).name == n
    assert load_persona("tal").w0 > load_persona("solid").w0


def test_persona_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown persona"):
        load_persona("nope")
    bad = tmp_path / "bad.toml"
    bad.write_text('name = "x"\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_persona(bad)
    wrong = tmp_path / "wrong.toml"
    wrong.write_text('name = "x"\nw0 = "high"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="number"):
        load_persona(wrong)


def test_full_config(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.toml"
    cfg.write_text(
        '[agent]\nelo_self = 2100\nseed = "exp-1"\n'
        '[persona]\npreset = "tal"\nw0 = 0.9\n'
        "[lorenz]\nsteps_per_ply = 30\n",
        encoding="utf-8",
    )
    c = load_agent_config(cfg)
    assert (c.elo_self, c.seed, c.persona.name, c.persona.w0, c.lorenz.steps_per_ply) == (
        2100,
        "exp-1",
        "tal",
        0.9,
        30,
    )
    bad = tmp_path / "bad.toml"
    bad.write_text("[nonsense]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="section"):
        load_agent_config(bad)
    nested = tmp_path / "nested.toml"
    nested.write_text("[agent]\npersona = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="section"):
        load_agent_config(nested)


def test_uci_parsers() -> None:
    b = parse_position(["startpos", "moves", "e2e4", "e7e5"])
    assert b.fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3")
    fen = "8/8/8/4k3/8/8/4P3/4K3 w - - 0 1"
    assert parse_position(["fen", *fen.split()]).fen() == fen
    assert parse_position(["fen", *fen.split(), "moves", "e2e4"]).turn == chess.BLACK
    with pytest.raises(ValueError, match="illegal"):
        parse_position(["startpos", "moves", "e2e5"])
    with pytest.raises(ValueError, match="invalid position"):
        parse_position(["fen", "8/8/8/8/8/8/8/8", "w", "-", "-", "0", "1"])  # no kings
    with pytest.raises(ValueError, match="bad position"):
        parse_position(["nonsense"])
    clock, movetime, infinite = parse_go(
        ["wtime", "60000", "btime", "30000", "winc", "1000", "movestogo", "20"], chess.BLACK
    )
    assert (clock.my_time, clock.opp_time, clock.opp_inc, clock.moves_to_go) == (
        30.0,
        60.0,
        1.0,
        20,
    )
    assert movetime is None
    assert not infinite
    _, mt, inf = parse_go(["movetime", "500", "infinite"], chess.WHITE)
    assert mt == 0.5
    assert inf
    assert parse_uci_opponent("GM 2800 human Gary Kasparov") == 2800
    assert parse_uci_opponent("none") is None


def test_bench_game_and_outputs(tmp_path: Path) -> None:
    engine = FakeEngine()
    cca = CCAPlayer(CAIMEAgent(engine, FakeHuman(), AgentConfig()))
    opp = HumanModelPlayer(FakeHuman(), 1500, 1900, seed="o")
    rec = play_game(
        cca, opp, "t1", base_time=120.0, increment=1.0, max_plies=40, referee=FakeEngine()
    )
    assert rec.plies
    assert all(p.loss is not None for p in rec.plies)
    assert all((p.loss or 0.0) >= 0.0 for p in rec.plies)
    assert "CCA" in to_pgn(rec)
    summary = summarize([rec], "CCA")
    assert summary["games"] == 1
    path = write_results([rec], "CCA", tmp_path / "run")
    assert json.loads(path.read_text(encoding="utf-8"))["games"] == 1
    assert (tmp_path / "run" / "games.pgn").exists()


def test_cli_version_and_parser(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()
    args = build_parser().parse_args(
        ["analyse", "startpos-fen", "--human", "qre", "--persona", "tal"]
    )
    assert args.persona == "tal"
    assert args.human == "qre"
