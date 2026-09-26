"""Regression tests for the verified findings of the round-2 adversarial review (2026-09-26).

Each test is written so that reverting the corresponding fix makes it fail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import chess
import pytest

from cca.agent import AgentConfig, CAIMEAgent
from cca.bench.match import GameRecord, PlyRecord, summarize
from cca.cli import _sha256, code_provenance
from cca.core.types import Clock, MoveEval, PsychState
from cca.engines.maia2_human import CheckpointMismatchError, verify_checkpoint
from cca.engines.qre_human import QREHumanModel
from cca.engines.stockfish import EngineNotFoundError, find_stockfish
from cca.neuro import Persona, knobs_from_state
from cca.timing import ThinkTimeModel, ThinkTimeParams
from cca.uci.protocol import UciServer, _is_windows_pipe
from tests.conftest import FakeEngine, FakeHuman, make_launcher

_TRAP = "3qk3/8/8/8/3P4/8/4P3/4K3 w - - 0 1"  # e3 invites ...Qxd4?? exd4


class SlowEngine(FakeEngine):
    """Like FakeEngine, but each call costs ~0.15 s unless a shorter time limit is given."""

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        time.sleep(0.15 if time_limit is None else min(0.15, time_limit))
        return super().evaluate(board, perspective=perspective, moves=moves, multipv=multipv)


def _slow_agent(**cfg: object) -> CAIMEAgent:
    engine = SlowEngine()
    return CAIMEAgent(engine, QREHumanModel(engine), AgentConfig(**cfg))  # type: ignore[arg-type]


def _italian() -> chess.Board:
    board = chess.Board()
    for uci in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]:
        board.push_uci(uci)
    return board


# --- time control (R2-AG-01, UCI-6)
def test_resync_keeps_the_callers_deadline_and_stop() -> None:
    stop = threading.Event()
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    agent.choose(_italian(), stop=stop)  # history mismatch -> new_game() inside choose()
    assert agent._stop is stop


def test_qre_human_model_calls_respect_the_deadline() -> None:
    agent = _slow_agent()
    board = _italian()
    start = time.monotonic()
    d = agent.choose(board, deadline=time.monotonic() + 0.8)
    elapsed = time.monotonic() - start
    assert elapsed < 1.6  # unbounded: ~10 slow calls = 1.5 s + QRE full-width searches
    assert chess.Move.from_uci(d.move) in board.legal_moves


class _LimitSpyHuman(FakeHuman):
    """Records the time limit of every human-model call."""

    def __init__(self) -> None:
        self.limits: list[float | None] = []

    def distribution(
        self, board: chess.Board, elo_self: int, elo_oppo: int, time_limit: float | None = None
    ) -> dict[str, float]:
        self.limits.append(time_limit)
        return super().distribution(board, elo_self, elo_oppo)

    def distributions(
        self,
        boards: Sequence[chess.Board],
        elo_self: int,
        elo_oppo: int,
        time_limit: float | None = None,
    ) -> list[dict[str, float]]:
        self.limits.append(time_limit)
        return [FakeHuman.distribution(self, b, elo_self, elo_oppo) for b in boards]


def test_every_human_model_call_is_bounded_by_the_deadline() -> None:
    board = _italian()
    human = _LimitSpyHuman()
    CAIMEAgent(FakeEngine(), human, AgentConfig()).choose(board, deadline=time.monotonic() + 5.0)
    assert len(human.limits) >= 2  # own prior + opponent replies at least
    assert all(t is not None and 0.0 < t <= 5.0 for t in human.limits), human.limits
    untimed = _LimitSpyHuman()
    CAIMEAgent(FakeEngine(), untimed, AgentConfig()).choose(board)
    assert untimed.limits
    assert all(t is None for t in untimed.limits)  # node-limited research mode stays unbounded


def test_out_of_time_prior_falls_back_to_uniform() -> None:
    stop = threading.Event()
    stop.set()
    d = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig()).choose(chess.Board(), stop=stop)
    assert d.trace["prior_skipped"] == 1.0


def test_deadline_for_movetime_and_untimed() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    board = chess.Board()
    d = agent.deadline_for(board, Clock(), 0.5)
    assert d is not None
    assert 0.40 < d - time.monotonic() <= 0.45
    assert agent.deadline_for(board, Clock(), None) is None


def _uci_with(agent: CAIMEAgent) -> tuple[UciServer, io.StringIO]:
    out = io.StringIO()
    server = UciServer(stdin=io.StringIO(""), stdout=out)
    server._agent = agent
    return server, out


def _bestmove_at(out: io.StringIO, n: int, timeout: float = 10.0) -> float:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if sum(1 for ln in out.getvalue().splitlines() if ln.startswith("bestmove")) >= n:
            return time.monotonic()
        time.sleep(0.01)
    raise AssertionError("no bestmove")


def test_uci_movetime_is_honoured_with_a_slow_engine_and_qre() -> None:
    server, out = _uci_with(_slow_agent())
    server.handle("position startpos moves e2e4 e7e5 g1f3")
    start = time.monotonic()
    server.handle("go movetime 500")
    assert _bestmove_at(out, 1) - start < 1.2
    server._shutdown()


def test_uci_stop_is_honoured_promptly() -> None:
    server, out = _uci_with(_slow_agent())
    server.handle("position startpos moves e2e4 e7e5 g1f3")
    server.handle("go infinite")
    time.sleep(0.2)
    t_stop = time.monotonic()
    server.handle("stop")
    assert _bestmove_at(out, 1) - t_stop < 0.8
    server._shutdown()


def test_emulated_think_time_is_clamped_by_the_deadline() -> None:
    server, out = _uci_with(CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig()))
    server.handle("setoption name CCA_EmulateThinkTime value true")
    server.handle("position startpos moves e2e4")
    start = time.monotonic()
    server.handle("go movetime 300")
    assert _bestmove_at(out, 1) - start < 1.5
    server._shutdown()


# --- UCI robustness
def test_seed_reveal_follows_what_was_committed(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), server._config())
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")
    server._join()
    committed_seed = server._secret_seed
    server.handle("setoption name CCA_Seed value 42")  # changed before ucinewgame
    server.handle("ucinewgame")
    assert f"seed reveal {committed_seed}" in out.getvalue()
    server.handle("position startpos")
    server.handle("go wtime 60000 btime 60000")  # explicit seed: no commitment
    server._join()
    server.handle("setoption name CCA_Seed value <empty>")
    server.handle("ucinewgame")
    reveals = re.findall(r"seed reveal ([0-9a-f]+)", out.getvalue())
    assert reveals == [committed_seed]  # nothing revealed for a game that used seed 42


def test_invalid_stockfish_path_keeps_the_working_engine(
    uci: tuple[UciServer, io.StringIO], tmp_path: Path
) -> None:
    server, out = uci
    server.handle("isready")
    engine = server._engine
    assert engine is not None
    server.handle(f"setoption name StockfishPath value {tmp_path / 'nope.exe'}")
    assert "is not a file" in out.getvalue()
    assert server._engine is engine


def test_rejected_position_answers_the_null_move(uci: tuple[UciServer, io.StringIO]) -> None:
    server, out = uci
    server._agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig())
    server.handle("position startpos moves e2e4")
    server.handle("position startpos moves e2e5")  # illegal
    server.handle("go wtime 60000 btime 60000")
    server._join()
    assert out.getvalue().splitlines()[-1] == "bestmove 0000"


def test_maia_failure_falls_back_and_model_survives_stockfish_options(
    uci: tuple[UciServer, io.StringIO], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, out = uci
    import cca.engines.maia2_human as m2

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("CUDA error: out of memory")

    monkeypatch.setattr(m2, "Maia2HumanModel", boom)
    server.handle("setoption name CCA_HumanModel value maia2")
    server.handle("isready")
    assert "falling back to QRE" in out.getvalue()
    assert isinstance(server._agent.human, QREHumanModel)  # type: ignore[union-attr]
    sentinel = FakeHuman()
    server._maia = sentinel
    server.handle("setoption name CCA_Nodes value 2000")  # Stockfish-only: keep Maia
    assert server._maia is sentinel
    server.handle("setoption name CCA_Device value cpu")  # model option: rebuild Maia
    assert server._maia is None


def test_stdin_detection_is_safe_for_non_pipes() -> None:
    assert _is_windows_pipe(io.StringIO("")) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe reader")
def test_serve_over_a_real_windows_pipe(tmp_path: Path) -> None:
    """End to end through the polling pipe reader (the default path for GUIs on Windows)."""
    script = "from cca.uci.protocol import UciServer; UciServer().serve()"
    lines: list[str] = []
    with subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as p:
        stdin, stdout = p.stdin, p.stdout
        assert stdin is not None
        assert stdout is not None
        reader = threading.Thread(target=lambda: lines.extend(iter(stdout.readline, "")))
        reader.start()
        try:
            stdin.write(f"setoption name StockfishPath value {make_launcher(tmp_path)}\n")
            stdin.write("setoption name CCA_HumanModel value qre\nuci\nisready\n")
            stdin.flush()
            end = time.monotonic() + 30
            while time.monotonic() < end and "readyok\n" not in lines:
                time.sleep(0.05)
            stdin.write("quit\n")
            stdin.flush()
            p.wait(timeout=30)
        finally:
            if p.poll() is None:  # a broken reader must fail the test, not hang the suite
                p.kill()
                p.wait()
        reader.join(timeout=10)
    assert "uciok\n" in lines
    assert "readyok\n" in lines
    assert p.returncode == 0


# --- engine discovery (T2-05/06)
def test_missing_explicit_paths_never_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = make_launcher(tmp_path)  # a binary the old code would have silently used
    monkeypatch.setattr("cca.engines.stockfish.PROJECT_ENGINES", tmp_path / "noengines")
    monkeypatch.setattr("cca.engines.stockfish.shutil.which", lambda *a, **k: str(fallback))
    monkeypatch.delenv("CCA_STOCKFISH", raising=False)
    assert find_stockfish() == fallback  # control: the fallback is really discoverable
    with pytest.raises(EngineNotFoundError):
        find_stockfish(tmp_path / "missing.exe")
    monkeypatch.setenv("CCA_STOCKFISH", str(tmp_path / "missing.exe"))
    with pytest.raises(EngineNotFoundError):
        find_stockfish()
    from cca.cli import main

    monkeypatch.delenv("CCA_STOCKFISH")
    assert main(["doctor", "--stockfish", str(tmp_path / "missing.exe")]) == 1


def test_engines_in_the_working_directory_are_not_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rogue = tmp_path / "engines" / "stockfish-evil" / "stockfish"
    rogue.mkdir(parents=True)
    name = "stockfish-evil.exe" if sys.platform == "win32" else "stockfish-evil"
    (rogue / name).write_bytes(make_launcher(tmp_path).read_bytes())
    (rogue / name).chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cca.engines.stockfish.PROJECT_ENGINES", tmp_path / "noengines")
    monkeypatch.setattr("cca.engines.stockfish.shutil.which", lambda *a, **k: None)
    monkeypatch.delenv("CCA_STOCKFISH", raising=False)
    with pytest.raises(EngineNotFoundError):
        find_stockfish()


# --- exploitation wiring (TQ-08)
def test_exploitation_puts_policy_mass_on_a_human_trap() -> None:
    board = chess.Board(_TRAP)
    greedy = dataclasses.replace(Persona(), w0=0.9)
    blind = dataclasses.replace(Persona(), w0=1e-9, h0=0.0)
    d = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=greedy, sample=False)).choose(
        board
    )
    d0 = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=blind, sample=False)).choose(
        board
    )
    trap = next(c for c in d.candidates if c.uci == "e2e3")
    assert trap.trap_value > 0.05
    assert d.policy["e2e3"] > d0.policy["e2e3"] + 0.1


def test_opponent_stress_reaches_the_reply_model() -> None:
    board = chess.Board(_TRAP)
    q = []
    for s in (0.2, 3.0):
        agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(sample=False))
        agent.state = dataclasses.replace(agent.state, opp_stress=s)
        d = agent.choose(board)
        q.append(next(c.q_human for c in d.candidates if c.uci == "e2e3"))
    assert d.knobs.opp_temperature > 1.0
    assert q[0] != pytest.approx(q[1], abs=1e-3)


# --- perspective oracle (TQ-02)
def test_every_candidate_is_scored_for_the_side_to_move() -> None:
    oracle = FakeEngine()
    for fen in (
        chess.STARTING_FEN,
        "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1",
        "4k3/8/8/4p3/3Q4/8/8/4K3 b - - 0 1",
    ):
        board = chess.Board(fen)
        d = CAIMEAgent(
            FakeEngine(), FakeHuman(), AgentConfig(engine_multipv=1, prior_top=4)
        ).choose(board)
        for c in d.candidates:
            truth = oracle.evaluate(
                board, perspective=board.turn, moves=[chess.Move.from_uci(c.uci)]
            )[0].q
            assert c.q_opt <= truth + 1e-12, (fen, c.uci)
        assert d.trace["v_now"] == pytest.approx(
            oracle.evaluate(board, perspective=board.turn)[0].q
        )


# --- stress knobs separately (TQ-09)
@pytest.mark.parametrize(("a_tunnel", "a_habit"), [(6.0, 0.0), (0.0, 6.0)])
def test_each_stress_knob_changes_the_decision_on_its_own(a_tunnel: float, a_habit: float) -> None:
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    board.push_uci("f1c4")
    board.push_uci("g8f6")
    policies = []
    for t, h in ((0.0, 0.0), (a_tunnel, a_habit)):
        persona = dataclasses.replace(Persona(), a_tunnel=t, a_habit=h, tau=0.0)
        agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(persona=persona))
        agent.state = dataclasses.replace(agent.state, stress=2.5)
        agent._history = [m.uci() for m in board.move_stack][:-1]
        policies.append(agent.choose(board).policy)
    assert policies[0] != policies[1]


# --- per-ply sampling stream (TQ-11)
def test_each_sampling_call_uses_a_fresh_stream() -> None:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed="r"))
    assert {agent._select({"a": 0.5, "b": 0.5}) for _ in range(20)} == {"a", "b"}


# --- risk bank (M3, dead zone)
def test_negative_bank_forces_the_engine_move() -> None:
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed="neg"))
    agent._history = [m.uci() for m in board.move_stack]
    agent._bank = -1.0  # far more risk taken than ever given away
    d = agent.choose(board)
    assert d.knobs.risk_budget == 0.0
    assert next(c for c in d.candidates if c.uci == d.move).q_opt == max(
        c.q_opt for c in d.candidates
    )


def _gift_for(offset: float) -> float:
    agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(sample=False))
    board = chess.Board()
    board.push_uci(agent.choose(board).move)
    reply = next(iter(board.legal_moves))
    probe = board.copy()
    probe.push(reply)
    v_now = FakeEngine().evaluate(probe, perspective=probe.turn, multipv=1)[0].q
    assert agent._expect is not None
    agent._expect = dataclasses.replace(agent._expect, reply_scores={}, q_opt=v_now - offset)
    board.push(reply)
    return agent.choose(board).trace["gift"]


def test_unevaluated_gifts_only_count_beyond_the_dead_zone() -> None:
    dz = AgentConfig().bank_dead_zone
    assert _gift_for(dz / 2) == 0.0
    assert _gift_for(2 * dz) == pytest.approx(dz, abs=1e-9)


# --- opening surprise (AG-5 / R2-AG-03)
def test_opening_tempering_strictly_reduces_both_surprise_signals() -> None:
    board = chess.Board()
    board.push_uci("h2h4")
    cold = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(opening_temperature=1.0)).choose(board)
    warm = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig()).choose(board)
    assert abs(warm.trace["opp_surprise_excess"]) < abs(cold.trace["opp_surprise_excess"])
    assert warm.trace["our_surprise_excess"] != pytest.approx(cold.trace["our_surprise_excess"])


# --- think time (L2)
def test_think_time_noise_starts_stationary() -> None:
    p = ThinkTimeParams(phi=0.9, min_time=0.0, max_fraction=1.0, safety_margin=0.0)
    first = [
        math.log(
            ThinkTimeModel(p, "stationary", s).sample(
                move_number=22, remaining=None, prior_entropy=p.entropy_ref, n_legal=30
            )
        )
        for s in range(2000)
    ]
    assert statistics.pstdev(first) / p.sigma == pytest.approx(1.0, abs=0.1)


# --- bench attribution (TQ-05)
@pytest.mark.parametrize(
    ("result", "white", "black", "score"),
    [
        ("1-0", "S", "O", 1.0),
        ("0-1", "O", "S", 1.0),
        ("1-0", "O", "S", 0.0),
        ("0-1", "S", "O", 0.0),
    ],
)
def test_summary_scores_wins_for_the_right_player(
    result: str, white: str, black: str, score: float
) -> None:
    rec = GameRecord(
        "g", white, black, result, "checkmate", [PlyRecord(0, white, "e2e4", 1.0, None)]
    )
    assert summarize([rec], "S")["score"] == score


# --- provenance + checkpoint pin
def test_manifest_provenance_comes_from_the_package_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Path(__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607 - test helper
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    monkeypatch.chdir(tmp_path)  # not a git repo
    prov = code_provenance()
    if head:
        assert prov["git_commit"] == head
        assert isinstance(prov["git_dirty"], bool)
    f = tmp_path / "blob.bin"
    f.write_bytes(b"cca")
    assert _sha256(f) == hashlib.sha256(b"cca").hexdigest()
    assert _sha256(tmp_path / "missing") is None


def test_tampered_checkpoint_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "rapid_model.pt"
    bad.write_bytes(b"not the official Maia-2 checkpoint")
    with pytest.raises(CheckpointMismatchError, match="refusing to load"):
        verify_checkpoint(bad, "rapid")
    verify_checkpoint(tmp_path / "absent.pt", "rapid")  # absent: maia2 downloads + verifies


# --- gaps found by the grader's own mutations (EVO gate G23)
def test_risk_taken_is_debited_from_the_bank() -> None:
    board = chess.Board(_TRAP)
    debits = []
    for s in range(40):
        agent = CAIMEAgent(FakeEngine(), FakeHuman(), AgentConfig(seed=f"debit{s}"))
        d = agent.choose(board)
        best = max(c.q_opt for c in d.candidates)
        debit = best - next(c.q_opt for c in d.candidates if c.uci == d.move)
        assert agent._bank == pytest.approx(d.trace.get("gift", 0.0) - debit, abs=1e-12)
        debits.append(debit)
    assert max(debits) > 0.0  # some sampled moves really took risk


@pytest.mark.parametrize(("stress", "u2", "side"), [(40.0, 1.0, "hi"), (0.0, -1.0, "lo")])
def test_kl_weight_is_clamped(stress: float, u2: float, side: str) -> None:
    persona = dataclasses.replace(Persona(), g_lam=5.0)
    state = PsychState(stress=stress, chaos=(0.0, 0.0, u2))
    k = knobs_from_state(state, persona, elo_self=1500, elo_oppo=1500, current_score=0.5)
    lo, hi = persona.lam0 / persona.lam_range, persona.lam0 * persona.lam_range
    assert k.kl_weight == pytest.approx(hi if side == "hi" else lo)


def test_cli_uci_round_trips_non_ascii_paths_over_a_pipe(tmp_path: Path) -> None:
    """R001: GUIs send UTF-8; a cp1252 stream would mangle (or crash on) Vietnamese paths."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONIOENCODING", "PYTHONUTF8"}}
    folder = tmp_path / "CÔNG VIỆC" / "Tiếng Việt"
    folder.mkdir(parents=True)
    launcher = make_launcher(tmp_path)
    missing = folder / "sf.exe"
    cmd = [sys.executable, "-c", "import sys; from cca.cli import main; sys.exit(main(['uci']))"]
    lines = [
        f"setoption name StockfishPath value {missing}",
        f"setoption name StockfishPath value {launcher}",
        "setoption name CCA_HumanModel value qre",
        "uci",
        "isready",
        "position startpos moves e2e4",
        "go movetime 500",
        "quit",
    ]
    p = subprocess.run(
        cmd,
        input="".join(ln + "\n" for ln in lines).encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    out = p.stdout.decode("utf-8")  # the whole stream must be valid UTF-8
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert "CÔNG VIỆC" in out  # decoded and re-encoded without loss
    assert "readyok" in out
    assert sum(ln.startswith("bestmove") for ln in out.splitlines()) == 1
