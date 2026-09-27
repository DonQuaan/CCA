"""One ``cca play`` game: board, server-authoritative clocks and CCA's decisions (in memory).

Clock rules (Fischer increment): the side to move runs from the moment its position appears.
CCA is charged the wall time since its clock started, or ``max(that, think_time)`` when the
human-like thinking delay is emulated; the client then shows CCA's move after the emulated
delay and the human's clock only starts at that moment (a human move sent before it is refused
with 409, so that answering early buys no time). A side whose clock reaches zero loses
on time, unless the other side cannot mate (then it is a draw). Clocks are checked lazily, on
every request, so nothing runs in the background.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import chess
import chess.pgn

from cca import __version__
from cca.core.types import Clock
from cca.play.serialize import decision_to_json

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from cca.agent import CAIMEAgent

COLOR_NAMES = {chess.WHITE: "white", chess.BLACK: "black"}
_UCI = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?\Z")
# The page shows CCA's move ``reveal_in_ms`` after the response arrived, so its answer always
# comes later than the reveal on the server's clock; a browser timer may fire a few ms early.
# A human move up to this early is accepted (and charged from the reveal), an earlier one is
# refused: answering a move before it is shown would be free time on the clock.
REVEAL_GRACE_S = 0.05


class ApiError(Exception):
    """A client-visible error with an HTTP status (reported as JSON)."""

    def __init__(self, status: int, message: str, state: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.state = state


@dataclass(frozen=True, slots=True)
class TimeControl:
    """Base time and Fischer increment, in seconds."""

    base_s: float
    inc_s: float

    @property
    def pgn(self) -> str:
        """PGN ``TimeControl`` tag value, e.g. ``300+3``."""
        return f"{self.base_s:g}+{self.inc_s:g}"


def board_end(board: chess.Board) -> tuple[str, str] | None:
    """``(result, reason)`` if the game ended on the board, else ``None``.

    Same rule as :func:`cca.agent.is_ended`: threefold repetition and the fifty-move rule end
    the game automatically, as an arbiter or an auto-adjudicating GUI would.
    """
    if board.is_checkmate():
        return ("0-1" if board.turn == chess.WHITE else "1-0"), "checkmate"
    if board.is_stalemate():
        return "1/2-1/2", "stalemate"
    if board.is_insufficient_material():
        return "1/2-1/2", "insufficient-material"
    if board.is_fifty_moves():
        return "1/2-1/2", "fifty-move"
    if board.is_repetition(3):
        return "1/2-1/2", "threefold"
    return None


def parse_move(board: chess.Board, uci: object) -> chess.Move:
    """A legal move of ``board`` from a UCI string, or :class:`ApiError` 400."""
    if not isinstance(uci, str) or not _UCI.match(uci):
        raise ApiError(400, "move must be a UCI string such as e2e4 or e7e8q")
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ApiError(400, f"illegal move {uci} in this position")
    return move


def seed_commitment(seed: str) -> str:
    """SHA-256 commitment to a game seed (published before the first move)."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class GameSession:
    """A game between one human and one :class:`~cca.agent.CAIMEAgent`."""

    def __init__(
        self,
        game_id: str,
        agent: CAIMEAgent,
        *,
        human_color: chess.Color,
        board: chess.Board,
        persona: str,
        human_model: str,
        seed_is_secret: bool,
        time_control: TimeControl | None,
        emulate_think_time: bool,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = game_id
        self.agent = agent
        self.lock = threading.Lock()
        self.human_color = human_color
        self.board = board
        self.start_fen = board.fen()
        self.persona = persona
        self.human_model = human_model
        self.seed_is_secret = seed_is_secret
        self.time_control = time_control
        self.emulate_think_time = emulate_think_time
        self.result: str | None = None
        self.reason: str | None = None
        self.history: list[dict[str, object]] = []
        self.decisions: dict[int, dict[str, object]] = {}
        self.started = dt.datetime.now(dt.UTC)
        self._now = now
        self._run_from = now()
        self._remaining: dict[chess.Color, float] | None = None
        self._running: chess.Color | None = None
        if time_control is not None:
            self._remaining = {chess.WHITE: time_control.base_s, chess.BLACK: time_control.base_s}
            self._running = board.turn

    # ------------------------------------------------------------------ properties
    @property
    def cca_color(self) -> chess.Color:
        """CCA plays the other colour."""
        return not self.human_color

    @property
    def over(self) -> bool:
        """Whether the game has a result."""
        return self.result is not None

    @property
    def seed(self) -> str:
        """The agent's seed (kept secret in :meth:`state` until the game ends)."""
        return self.agent.config.seed

    # ------------------------------------------------------------------ clocks
    def _left(self, color: chess.Color, now: float) -> float:
        if self._remaining is None:
            raise RuntimeError("untimed game has no clock")
        left = self._remaining[color]
        if self._running == color:
            left -= max(0.0, now - self._run_from)
        return left

    def refresh(self) -> None:
        """Flag the running side if its clock has run out."""
        if self.over or self._running is None:
            return
        if self._left(self._running, self._now()) <= 0.0:
            self._flag(self._running)

    def _flag(self, loser: chess.Color) -> None:
        if self._remaining is not None:
            self._remaining[loser] = 0.0
        self._running = None
        winner = not loser
        if self.board.has_insufficient_material(winner):
            self._finish("1/2-1/2", "time")
        else:
            self._finish("1-0" if winner == chess.WHITE else "0-1", "time")

    def _finish(self, result: str, reason: str) -> None:
        if self._running is not None and self._remaining is not None:
            self._remaining[self._running] = max(0.0, self._left(self._running, self._now()))
        self._running = None
        self.result, self.reason = result, reason

    def _charge(self, mover: chess.Color, spent: float, next_start: float) -> None:
        """Book ``spent`` seconds (plus increment) to ``mover``; the other clock starts later."""
        if self._remaining is not None and self.time_control is not None:
            self._remaining[mover] += self.time_control.inc_s - spent
            self._running = not mover
        self._run_from = next_start

    def agent_clock(self) -> Clock:
        """The clock as CCA sees it on its own turn (remaining time already net of waiting)."""
        if self._remaining is None or self.time_control is None:
            return Clock()
        now = self._now()
        inc = self.time_control.inc_s
        return Clock(
            my_time=max(0.0, self._left(self.cca_color, now)),
            opp_time=max(0.0, self._left(self.human_color, now)),
            my_inc=inc,
            opp_inc=inc,
        )

    # ------------------------------------------------------------------ moves
    def _check_board_end(self) -> None:
        end = board_end(self.board)
        if end is not None:
            self._finish(*end)

    def human_move(self, uci: object) -> None:
        """Play the human's move (400 if illegal or not their turn, 409 if the game is over).

        In a timed game, a move sent while CCA's move is still being revealed (the emulated
        thinking delay) is refused with 409 as well; see :data:`REVEAL_GRACE_S`.
        """
        self.refresh()
        if self.over:
            raise ApiError(409, "the game is over", self.state())
        if self.board.turn != self.human_color:
            raise ApiError(400, "it is not your turn")
        move = parse_move(self.board, uci)
        now = self._now()
        if self._remaining is not None and self._run_from - now > REVEAL_GRACE_S:
            raise ApiError(
                409, "CCA's move is still being revealed; answer it once shown", self.state()
            )
        self._charge(self.human_color, max(0.0, now - self._run_from), now)
        self.board.push(move)
        self._check_board_end()

    def cca_move(self, engine_lock: AbstractContextManager[object]) -> dict[str, object]:
        """Let CCA decide and play for the side to move (409 if it is the human's turn)."""
        self.refresh()
        if self.over:
            raise ApiError(409, "the game is over", self.state())
        if self.board.turn == self.human_color:
            raise ApiError(409, "it is your turn, not CCA's")
        clock_start = self._run_from
        with engine_lock:
            # Everything that depends on the time left is computed once the engine is ours:
            # waiting for another game's decision (or the position lab) must not eat into the
            # deadline, but it is still charged to CCA's clock, which ran meanwhile.
            self.refresh()
            # (self.result, not self.over: mypy keeps the narrowing of the check above.)
            if self.result is not None:  # CCA's flag fell while it waited for the engine
                return {"move": None, "decision": None, "compute_s": 0.0, "reveal_in_ms": 0}
            clock = self.agent_clock()
            deadline = (
                self.agent.deadline_for(self.board, clock) if clock.my_time is not None else None
            )
            # The engine process is shared with the other games and the lab: start each
            # decision from a cleared engine (ucinewgame) so that a replay of this game with
            # the same seed does not depend on what else the engine searched in between.
            self.agent.engine.new_game()
            known_game = self.agent.game_id
            started = time.monotonic()
            decision = self.agent.choose(self.board.copy(), clock, deadline=deadline)
            compute = time.monotonic() - started
            # The agent starts a new game (its latent state restarts) when the position does
            # not continue the one it knows, e.g. after a take-back of its own move.
            restarted = self.agent.game_id != known_game
        payload = decision_to_json(self.board, decision)
        now = self._now()
        spent = max(0.0, now - clock_start)
        charge = max(spent, decision.think_time) if self.emulate_think_time else spent
        if self._remaining is not None and charge >= self._remaining[self.cca_color]:
            self._flag(self.cca_color)  # CCA's flag fell before its move arrived
            return {"move": None, "decision": payload, "compute_s": compute, "reveal_in_ms": 0}
        move = chess.Move.from_uci(decision.move)
        ply = len(self.board.move_stack)
        san = self.board.san(move)
        move_number = self.board.fullmove_number  # as in the move list, for a FEN start too
        self._charge(self.cca_color, charge, clock_start + charge)
        self.board.push(move)
        state = decision.state
        self.history.append(
            {
                "ply": ply,
                "move_number": move_number,
                "uci": decision.move,
                "san": san,
                "q_opt": decision.trace.get("q_opt"),
                "q_human": decision.trace.get("q_human"),
                "trap_value": decision.trace.get("trap_value"),
                "stress": state.stress,
                "drive": state.drive,
                "opp_stress": state.opp_stress,
                "chaos": list(state.chaos),
                "think_time": decision.think_time,
                "compute_s": compute,
                "risk_budget": decision.knobs.risk_budget,
                "reflex": bool(decision.trace.get("reflex")),
                "restart": restarted,
            }
        )
        self.decisions[ply] = payload
        self._check_board_end()
        reveal = max(0.0, clock_start + charge - self._now())
        return {
            "move": {"uci": decision.move, "san": san},
            "decision": payload,
            "compute_s": compute,
            "reveal_in_ms": round(reveal * 1000),
        }

    def can_undo(self) -> bool:
        """Take-backs: untimed games only, not after resignation, and a human move must exist."""
        if self.time_control is not None or self.reason in {"resignation", "time"}:
            return False
        return self._last_human_ply() is not None

    def _last_human_ply(self) -> int | None:
        first = chess.Board(self.start_fen).turn
        for ply in range(len(self.board.move_stack) - 1, -1, -1):
            mover = first if ply % 2 == 0 else not first
            if mover == self.human_color:
                return ply
        return None

    def undo(self) -> None:
        """Take back the human's last move and CCA's reply to it (if any)."""
        if self.time_control is not None:
            raise ApiError(409, "take-backs are only available in untimed games")
        if self.reason in {"resignation", "time"}:
            raise ApiError(409, "the game is over")
        ply = self._last_human_ply()
        if ply is None:
            raise ApiError(400, "there is no move of yours to take back")
        while len(self.board.move_stack) > ply:
            self.board.pop()
        self.history = [h for h in self.history if _int(h["ply"]) < ply]
        self.decisions = {k: v for k, v in self.decisions.items() if k < ply}
        self.result = self.reason = None
        self._run_from = self._now()
        # Whether CCA's latent state (stress, drive, chaos) restarts is the agent's decision on
        # its next move: it does when one of its own moves was taken back, not when only a
        # game-ending move of the human was. That history point records it ("restart").

    def resign(self) -> None:
        """The human resigns."""
        self.refresh()
        if self.over:
            raise ApiError(409, "the game is over", self.state())
        self._finish("0-1" if self.human_color == chess.WHITE else "1-0", "resignation")

    # ------------------------------------------------------------------ views
    def _moves(self) -> list[dict[str, object]]:
        replay = chess.Board(self.start_fen)
        out: list[dict[str, object]] = []
        for move in self.board.move_stack:
            san = replay.san(move)
            replay.push(move)
            out.append({"uci": move.uci(), "san": san, "fen": replay.fen()})
        return out

    def _clocks(self) -> dict[str, object] | None:
        if self._remaining is None or self.time_control is None:
            return None
        now = self._now()
        running = self._running
        return {
            "white_ms": round(max(0.0, self._left(chess.WHITE, now)) * 1000),
            "black_ms": round(max(0.0, self._left(chess.BLACK, now)) * 1000),
            "running": COLOR_NAMES[running] if running is not None else None,
            "starts_in_ms": round(max(0.0, self._run_from - now) * 1000),
            "inc_ms": round(self.time_control.inc_s * 1000),
        }

    def _winner(self) -> str | None:
        if self.result == "1-0":
            return "human" if self.human_color == chess.WHITE else "cca"
        if self.result == "0-1":
            return "human" if self.human_color == chess.BLACK else "cca"
        return None

    def state(self) -> dict[str, object]:
        """Everything the client needs to draw the game (JSON-ready)."""
        board = self.board
        moves = self._moves()
        last = board.peek() if board.move_stack else None
        king = board.king(board.turn) if board.is_check() else None
        to_move = None
        if not self.over:
            to_move = "human" if board.turn == self.human_color else "cca"
        cfg = self.agent.config
        return {
            "id": self.id,
            "start_fen": self.start_fen,
            "fen": board.fen(),
            "turn": COLOR_NAMES[board.turn],
            "human_color": COLOR_NAMES[self.human_color],
            "to_move": to_move,
            "moves": moves,
            "last_move": None
            if last is None
            else {
                "uci": last.uci(),
                "san": moves[-1]["san"],
                "from": chess.square_name(last.from_square),
                "to": chess.square_name(last.to_square),
            },
            "check": king is not None,
            "check_square": chess.square_name(king) if king is not None else None,
            "game_over": self.over,
            "result": self.result or "*",
            "reason": self.reason,
            "winner": self._winner(),
            "cca": {
                "persona": self.persona,
                "elo": cfg.elo_self,
                "opponent_elo": cfg.elo_oppo,
                "human_model": self.human_model,
                "seed_commitment": seed_commitment(self.seed) if self.seed_is_secret else None,
                "seed": self.seed if (self.over or not self.seed_is_secret) else None,
            },
            "time_control": None
            if self.time_control is None
            else {"base_s": self.time_control.base_s, "inc_s": self.time_control.inc_s},
            "emulate_think_time": self.emulate_think_time,
            "clocks": self._clocks(),
            "can_undo": self.can_undo(),
            "history": self.history,
        }

    def pgn(self) -> str:
        """PGN with sensible headers; CCA's per-move diagnostics are added once the game ended."""
        game = chess.pgn.Game.from_board(self.board)
        cfg = self.agent.config
        cca = f"CCA {__version__} ({self.persona}, {cfg.elo_self})"
        human = "Human"
        white, black = (human, cca) if self.human_color == chess.WHITE else (cca, human)
        white_elo, black_elo = (
            (cfg.elo_oppo, cfg.elo_self)
            if self.human_color == chess.WHITE
            else (cfg.elo_self, cfg.elo_oppo)
        )
        termination = "unterminated"
        if self.reason == "time":
            termination = "time forfeit"
        elif self.over:
            termination = "normal"
        game.headers.update(
            {
                "Event": "CCA play (local)",
                "Site": "cca play",
                "Date": self.started.strftime("%Y.%m.%d"),
                "Round": "-",
                "White": white,
                "Black": black,
                "Result": self.result or "*",
                "WhiteElo": str(white_elo),
                "BlackElo": str(black_elo),
                "TimeControl": self.time_control.pgn if self.time_control else "-",
                "Termination": termination,
                "CCAPersona": self.persona,
                "CCAHumanModel": self.human_model,
            }
        )
        if self.seed_is_secret:
            game.headers["CCASeedCommitment"] = seed_commitment(self.seed)
        if self.over or not self.seed_is_secret:
            game.headers["CCASeed"] = self.seed
        if self.reason is not None:
            game.headers["CCAReason"] = self.reason
        if self.over:
            by_ply = {_int(h["ply"]): h for h in self.history}
            for ply, node in enumerate(game.mainline()):
                entry = by_ply.get(ply)
                if entry is not None:
                    node.comment = _pgn_comment(entry)
        return str(game) + "\n"


# (history key, PGN label, format) of the numbers in the PGN comment of each CCA move.
_PGN_FIELDS = (
    ("think_time", "think", "{:.1f}s"),
    ("q_opt", "q_opt", "{:.3f}"),
    ("trap_value", "trap", "{:+.3f}"),
    ("stress", "stress", "{:.2f}"),
)


def _pgn_comment(entry: Mapping[str, object]) -> str:
    """``cca think=... q_opt=... trap=... stress=...`` for one CCA move.

    A value the decision did not produce (a reflex move has no trap value) is left out rather
    than printed as ``nan``; a reflex move is marked as such.
    """
    parts = ["cca"]
    for key, label, fmt in _PGN_FIELDS:
        value = entry.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            parts.append(f"{label}={fmt.format(value)}")
    if entry.get("reflex") is True:
        parts.append("reflex")
    return " ".join(parts)


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError("expected an int")
