"""Head-to-head matches on a *virtual* clock, with a referee engine for error statistics.

Players never sleep: each move's think time (from the player's own model) is subtracted
from its virtual clock, so time-pressure dynamics are exercised at full speed.

Validity warning: a :class:`HumanModelPlayer` driven by Maia-2 is a *proxy* for humans.
Evaluating a Maia-exploiting agent against a Maia opponent is circular (it measures how well
the agent exploits that model, not humans). Real validation needs games against people
(e.g. a Lichess BOT account) or a held-out human model (e.g. Maia-1 via lc0).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import chess
import chess.pgn

from cca.core.rng import DeterministicRng
from cca.core.types import Clock
from cca.eval.metrics import error_profile, wilson_interval
from cca.timing.think_time import ThinkTimeModel

if TYPE_CHECKING:
    from cca.agent import CAIMEAgent
    from cca.engines.base import HumanModel, SearchEngine


class Player(Protocol):
    """Anything that can play a move on a virtual clock."""

    name: str

    def new_game(self, game_id: str) -> None:
        """Reset per-game state."""
        ...

    def play(self, board: chess.Board, clock: Clock) -> tuple[str, float, dict[str, float]]:
        """Return ``(uci, think_seconds, diagnostics)``."""
        ...


class CCAPlayer:
    """The C-AIME agent."""

    def __init__(self, agent: CAIMEAgent, name: str = "CCA") -> None:
        self.agent = agent
        self.name = name

    def new_game(self, game_id: str) -> None:
        """Reset the agent."""
        self.agent.new_game(game_id)

    def play(self, board: chess.Board, clock: Clock) -> tuple[str, float, dict[str, float]]:
        """Delegate to :meth:`CAIMEAgent.choose`."""
        d = self.agent.choose(board, clock)
        diag = dict(d.trace)
        diag.update(stress=d.state.stress, drive=d.state.drive, opp_stress=d.state.opp_stress)
        return d.move, d.think_time, diag


class HumanModelPlayer:
    """Samples moves from a human model at a fixed rating: a simulated human opponent."""

    def __init__(
        self, human: HumanModel, elo_self: int, elo_oppo: int, seed: str = "human", name: str = ""
    ) -> None:
        self.human = human
        self.elo_self = elo_self
        self.elo_oppo = elo_oppo
        self.seed = seed
        self.name = name or f"HumanModel-{elo_self}"
        self._timer = ThinkTimeModel(None, seed)
        self._game = "g"
        self._n = 0

    def new_game(self, game_id: str) -> None:
        """Reset timing memory and the sampling stream."""
        self._game = game_id
        self._timer = ThinkTimeModel(None, self.seed, game_id)
        self._n = 0

    def play(self, board: chess.Board, clock: Clock) -> tuple[str, float, dict[str, float]]:
        """Sample from the human distribution."""
        dist = self.human.distribution(board, self.elo_self, self.elo_oppo)
        self._n += 1
        move = DeterministicRng("human-move", self.seed, self._game, self._n).choice(dist)
        t = self._timer.sample(
            move_number=board.fullmove_number,
            remaining=clock.my_time,
            increment=clock.my_inc,
            top_prior=max(dist.values()),
            n_legal=len(dist),
        )
        return move, t, {"p_played": dist[move]}


class EnginePlayer:
    """Plays the engine's best move (configure strength limits on the engine itself)."""

    def __init__(self, engine: SearchEngine, name: str = "Engine", think: float = 1.0) -> None:
        self.engine = engine
        self.name = name
        self.think = think

    def new_game(self, game_id: str) -> None:
        """Clear engine caches."""
        del game_id
        self.engine.new_game()

    def play(self, board: chess.Board, clock: Clock) -> tuple[str, float, dict[str, float]]:
        """Best move."""
        del clock
        best = self.engine.evaluate(board, perspective=board.turn, multipv=1)
        return best[0].uci, self.think, {}


@dataclass
class PlyRecord:
    """One ply of a recorded game."""

    ply: int
    mover: str
    move: str
    think: float
    clock_after: float | None
    loss: float | None = None
    best: str | None = None
    diag: dict[str, float] = field(default_factory=dict)


@dataclass
class GameRecord:
    """A finished game."""

    game_id: str
    white: str
    black: str
    result: str
    termination: str
    plies: list[PlyRecord]


def play_game(
    white: Player,
    black: Player,
    game_id: str,
    *,
    base_time: float | None = 300.0,
    increment: float = 2.0,
    max_plies: int = 300,
    referee: SearchEngine | None = None,
) -> GameRecord:
    """Play one game on a virtual clock; optionally annotate move losses with a referee."""
    board = chess.Board()
    players = {chess.WHITE: white, chess.BLACK: black}
    clocks: dict[chess.Color, float | None] = {chess.WHITE: base_time, chess.BLACK: base_time}
    for p in (white, black):
        p.new_game(game_id)
    plies: list[PlyRecord] = []
    termination = "normal"
    while not board.is_game_over(claim_draw=True) and len(plies) < max_plies:
        color = board.turn
        clock = Clock(
            my_time=clocks[color],
            opp_time=clocks[not color],
            my_inc=increment if base_time is not None else 0.0,
            opp_inc=increment if base_time is not None else 0.0,
        )
        uci, think, diag = players[color].play(board, clock)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise RuntimeError(f"{players[color].name} played illegal move {uci}")
        remaining = clocks[color]
        if remaining is not None:
            remaining = remaining - think
            if remaining < 0.0:
                termination = "time forfeit"
                plies.append(
                    PlyRecord(len(plies), players[color].name, uci, think, remaining, diag=diag)
                )
                result = "0-1" if color == chess.WHITE else "1-0"
                return GameRecord(game_id, white.name, black.name, result, termination, plies)
            remaining += increment
            clocks[color] = remaining
        rec = PlyRecord(len(plies), players[color].name, uci, think, remaining, diag=diag)
        if referee is not None:
            best = referee.evaluate(board, perspective=color, multipv=1)
            played = referee.evaluate(board, perspective=color, moves=[move], multipv=1)
            if best and played:
                rec.best = best[0].uci
                rec.loss = max(0.0, best[0].q - played[0].q) if rec.best != uci else 0.0
        plies.append(rec)
        board.push(move)
    if len(plies) >= max_plies and not board.is_game_over(claim_draw=True):
        return GameRecord(game_id, white.name, black.name, "1/2-1/2", "ply cap", plies)
    outcome = board.outcome(claim_draw=True)
    result = outcome.result() if outcome else "1/2-1/2"
    termination = outcome.termination.name.lower() if outcome else termination
    return GameRecord(game_id, white.name, black.name, result, termination, plies)


def to_pgn(record: GameRecord) -> str:
    """Render a record as PGN with per-move diagnostics in comments."""
    game = chess.pgn.Game()
    game.headers.update(
        {
            "Event": "CCA match",
            "White": record.white,
            "Black": record.black,
            "Result": record.result,
            "Termination": record.termination,
            "Round": record.game_id,
        }
    )
    node: chess.pgn.GameNode = game
    for ply in record.plies:
        node = node.add_variation(chess.Move.from_uci(ply.move))
        bits = [f"t={ply.think:.1f}s"]
        if ply.loss is not None:
            bits.append(f"loss={ply.loss:.3f}")
        bits.extend(
            f"{key}={ply.diag[key]:.3f}"
            for key in ("stress", "trap_value", "q_human")
            if key in ply.diag
        )
        node.comment = " ".join(bits)
    return str(game)


def summarize(records: list[GameRecord], subject: str) -> dict[str, object]:
    """Score and error profiles of ``subject`` and of its opponents."""
    score = 0.0
    own: list[float] = []
    opp: list[float] = []
    for r in records:
        if r.result == "1/2-1/2":
            score += 0.5
        elif (r.result == "1-0" and r.white == subject) or (
            r.result == "0-1" and r.black == subject
        ):
            score += 1.0
        for p in r.plies:
            if p.loss is None:
                continue
            (own if p.mover == subject else opp).append(p.loss)
    own_p, opp_p = error_profile(own), error_profile(opp)
    blunders = round(opp_p.blunder_rate * opp_p.moves)
    return {
        "games": len(records),
        "score": score,
        "subject_errors": asdict(own_p),
        "opponent_errors": asdict(opp_p),
        "opponent_blunder_rate_ci95": wilson_interval(blunders, opp_p.moves),
    }


def write_results(
    records: list[GameRecord],
    subject: str,
    out_dir: Path,
    manifest: dict[str, object] | None = None,
) -> Path:
    """Write ``games.pgn``, ``plies.jsonl`` and ``summary.json`` into ``out_dir``.

    ``manifest`` (versions, commit, engine ids, config, seed) is stored in the summary so that
    a result is never separated from what is needed to reproduce it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "games.pgn").write_text(
        "\n\n".join(to_pgn(r) for r in records) + "\n", encoding="utf-8"
    )
    with (out_dir / "plies.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            for p in r.plies:
                fh.write(json.dumps({"game": r.game_id, **asdict(p)}, ensure_ascii=True) + "\n")
    summary = out_dir / "summary.json"
    payload = {"manifest": manifest or {}, **summarize(records, subject)}
    summary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8"
    )
    return summary
