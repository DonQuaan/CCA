"""CCA as a UCI engine (for lichess-bot, cutechess-cli, Arena, ...).

Only the subset of UCI that a GUI needs to *play* is implemented: ``uci``, ``isready``,
``setoption``, ``ucinewgame``, ``position``, ``go`` (clock fields, ``movetime``,
``infinite``, ``ponder``), ``ponderhit``, ``stop``, ``quit``. Search runs in a worker thread
so ``stop`` and ``isready`` are answered while thinking; ``stop`` also cuts the
computation short (the agent checks it between engine calls).

CCA-specific options::

    StockfishPath        string   path to the Stockfish binary (else auto-detect)
    Threads / Hash       spin     forwarded to Stockfish
    CCA_Nodes            spin     Stockfish node budget per evaluation
    UCI_Elo              spin     rating of the human the agent imitates (Maia anchor)
    UCI_Opponent         string   standard UCI: "<title> <rating> <computer|human> <name>"
    CCA_OpponentElo      spin     fallback opponent rating
    CCA_Persona          combo    shipped persona
    CCA_HumanModel       combo    maia2 | qre
    CCA_Maia2Type        combo    rapid | blitz
    CCA_Device           combo    gpu | cpu
    CCA_Seed             string   master seed; empty = secret random seed (online play)
    CCA_EmulateThinkTime check    actually wait the human-like think time
"""

from __future__ import annotations

import contextlib
import hashlib
import queue
import secrets
import sys
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, TextIO

import chess
import chess.engine

from cca import __version__
from cca.agent import AgentConfig, CAIMEAgent
from cca.config import list_personas, load_persona
from cca.core.types import Clock

if TYPE_CHECKING:
    from cca.core.types import Decision
    from cca.engines.base import HumanModel, SearchEngine

_GO_FIELDS = frozenset({"wtime", "btime", "winc", "binc", "movestogo", "movetime"})

_SPIN_LIMITS = {
    "Threads": (1, 1, 256),
    "Hash": (256, 16, 65536),
    "CCA_Nodes": (200_000, 1_000, 100_000_000),
    "UCI_Elo": (1900, 800, 2600),
    "CCA_OpponentElo": (1500, 400, 3000),
}


def parse_uci_opponent(value: str) -> int | None:
    """Rating from a ``UCI_Opponent`` value such as ``"GM 2800 human Gary Kasparov"``."""
    parts = value.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def parse_position(tokens: list[str]) -> chess.Board:
    """Build a board from the tokens after ``position``."""
    if not tokens:
        raise ValueError("empty position command")
    if tokens[0] == "startpos":
        board = chess.Board()
        rest = tokens[1:]
    elif tokens[0] == "fen":
        if "moves" in tokens:
            idx = tokens.index("moves")
            fen, rest = " ".join(tokens[1:idx]), tokens[idx:]
        else:
            fen, rest = " ".join(tokens[1:]), []
        board = chess.Board(fen)
        if not board.is_valid():  # Stockfish 19 terminates the process on an invalid position
            raise ValueError(f"invalid position ({board.status()!r}): {fen}")
    else:
        raise ValueError(f"bad position command: {tokens[0]}")
    if rest and rest[0] == "moves":
        for uci in rest[1:]:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError(f"illegal move in position command: {uci}")
            board.push(move)
    return board


def parse_go(tokens: list[str], turn: chess.Color) -> tuple[Clock, float | None, bool]:
    """Return ``(clock, movetime_seconds, infinite)`` from ``go`` tokens."""
    vals: dict[str, float] = {}
    infinite = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "infinite":
            infinite = True
        elif tok in _GO_FIELDS and i + 1 < len(tokens):
            with contextlib.suppress(ValueError):
                vals[tok] = float(tokens[i + 1])
            i += 1
        i += 1
    mine, theirs = ("w", "b") if turn == chess.WHITE else ("b", "w")
    ms = 1000.0

    def _get(key: str) -> float | None:
        return vals[key] / ms if key in vals else None

    clock = Clock(
        my_time=_get(f"{mine}time"),
        opp_time=_get(f"{theirs}time"),
        my_inc=_get(f"{mine}inc") or 0.0,
        opp_inc=_get(f"{theirs}inc") or 0.0,
        moves_to_go=int(vals["movestogo"]) if "movestogo" in vals else None,
    )
    return clock, _get("movetime"), infinite


_COMBOS: dict[str, tuple[str, ...]] = {
    "CCA_HumanModel": ("maia2", "qre"),
    "CCA_Maia2Type": ("rapid", "blitz"),
    "CCA_Device": ("gpu", "cpu"),
}
_ENGINE_OPTIONS = frozenset(
    {
        "StockfishPath",
        "Threads",
        "Hash",
        "CCA_Nodes",
        "CCA_HumanModel",
        "CCA_Maia2Type",
        "CCA_Device",
    }
)
# GUIs echo advertised defaults back verbatim; these placeholders mean "unset".
_PLACEHOLDERS = frozenset({"<auto>", "<empty>", "<secret>"})


class UciServer:
    """A UCI loop around :class:`CAIMEAgent` (engines are built on ``isready`` or first ``go``).

    Guarantees: every ``go`` produces exactly one ``bestmove`` (a legal fallback move if
    anything fails); ``stop`` bounds the computation; a crashed Stockfish is restarted on the
    next ``go``; invalid option values are rejected without changing the previous value.
    """

    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._lock = threading.Lock()
        self._opts: dict[str, str] = {
            "StockfishPath": "",
            "CCA_Persona": "balanced",
            "CCA_HumanModel": "maia2",
            "CCA_Maia2Type": "rapid",
            "CCA_Device": "gpu",
            "CCA_Seed": "",
            "CCA_EmulateThinkTime": "false",
            "UCI_Opponent": "",
        }
        self._opts.update({k: str(v[0]) for k, v in _SPIN_LIMITS.items()})
        self._agent: CAIMEAgent | None = None
        self._engine: SearchEngine | None = None
        self._board = chess.Board()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._ponderhit = threading.Event()
        self._engine_dead = threading.Event()
        self._games = 0
        # Unset CCA_Seed -> a fresh secret seed per game, committed (SHA-256) before the first
        # move and revealed when the game ends, so opponents cannot replay the temperament
        # during the game but anyone can audit it afterwards.
        self._secret_seed = secrets.token_hex(16)
        self._committed = False

    # ------------------------------------------------------------------ io
    def send(self, line: str) -> None:
        """Write one protocol line (thread-safe, flushed)."""
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def serve(self) -> None:
        """Read commands until ``quit`` or EOF."""
        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            for raw in self._in:
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        while True:
            raw = lines.get()
            if raw is None or not self.handle(raw.strip()):
                break
        self._shutdown()

    def handle(self, line: str) -> bool:
        """Process one command; return ``False`` on ``quit``."""
        if not line:
            return True
        cmd, *args = line.split()
        try:
            if cmd == "uci":
                self._cmd_uci()
            elif cmd == "isready":
                self._warm_up()
                self.send("readyok")
            elif cmd == "setoption":
                self._cmd_setoption(args)
            elif cmd == "ucinewgame":
                self._join()
                self._new_game()
            elif cmd == "position":
                self._join()
                self._board = parse_position(args)
            elif cmd == "go":
                self._cmd_go(args)
            elif cmd == "ponderhit":
                self._ponderhit.set()  # the predicted move was played: think for real now
            elif cmd == "stop":
                self._stop.set()
                self._join()
            elif cmd == "quit":
                return False
        except (ValueError, RuntimeError, OSError) as exc:
            self.send(f"info string error: {exc}")
        return True

    # ------------------------------------------------------------------ commands
    def _cmd_uci(self) -> None:
        self.send(f"id name CCA {__version__}")
        self.send("id author Nguyen Vu Dong Quan (DonQuaan)")
        self.send("option name StockfishPath type string default <auto>")
        for name, (default, lo, hi) in _SPIN_LIMITS.items():
            self.send(f"option name {name} type spin default {default} min {lo} max {hi}")
        self.send("option name UCI_Opponent type string default <empty>")
        personas = " ".join(f"var {p}" for p in list_personas())
        self.send(f"option name CCA_Persona type combo default balanced {personas}")
        for name, choices in _COMBOS.items():
            vars_ = " ".join(f"var {c}" for c in choices)
            self.send(f"option name {name} type combo default {self._opts[name]} {vars_}")
        self.send("option name CCA_Seed type string default <secret>")
        self.send("option name CCA_EmulateThinkTime type check default false")
        self.send("uciok")

    def _validated(self, name: str, value: str) -> str:
        """Canonical option value, or ``ValueError`` (the old value is then kept)."""
        if value in _PLACEHOLDERS:
            return ""
        if name in _SPIN_LIMITS:
            _, lo, hi = _SPIN_LIMITS[name]
            try:
                return str(min(hi, max(lo, int(value))))
            except ValueError:
                raise ValueError(f"option {name} needs an integer, got {value!r}") from None
        choices: tuple[str, ...] | None = _COMBOS.get(name)
        if name == "CCA_Persona":
            choices = tuple(list_personas())
        if choices is not None:
            match = next((c for c in choices if c.lower() == value.lower()), None)
            if match is None:
                raise ValueError(f"option {name} must be one of {list(choices)}, got {value!r}")
            return match
        if name == "CCA_EmulateThinkTime":
            if value.lower() not in {"true", "false"}:
                raise ValueError(f"option {name} must be true or false, got {value!r}")
            return value.lower()
        return value

    def _cmd_setoption(self, args: list[str]) -> None:
        if "name" not in args:
            raise ValueError("setoption without name")
        i_name = args.index("name")
        i_val = args.index("value") if "value" in args else len(args)
        name = " ".join(args[i_name + 1 : i_val])
        if name not in self._opts:
            self.send(f"info string ignoring unknown option {name}")
            return
        value = self._validated(name, " ".join(args[i_val + 1 :]))
        old = self._opts[name]
        self._opts[name] = value
        if name in _ENGINE_OPTIONS:
            self._drop_engines()
        elif self._agent is not None:
            try:
                self._agent.config = self._config()
            except (ValueError, RuntimeError):
                self._opts[name] = old
                raise

    def _cmd_go(self, args: list[str]) -> None:
        self._join()
        if self._engine_dead.is_set():
            self._drop_engines()
            self._engine_dead.clear()
            self.send("info string cca engine process died; restarted")
        clock, movetime, infinite = parse_go(args, self._board.turn)
        ponder = "ponder" in args
        board = self._board.copy()
        self._stop.clear()
        self._ponderhit.clear()
        self._worker = threading.Thread(
            target=self._search, args=(board, clock, movetime, infinite, ponder), daemon=True
        )
        self._worker.start()

    def _search(
        self, board: chess.Board, clock: Clock, movetime: float | None, infinite: bool, ponder: bool
    ) -> None:
        """Worker thread: exactly one ``bestmove`` on every path."""
        move: str | None = None
        try:
            if ponder:
                # Never touch the agent on a *guessed* position: its affect/chaos state must
                # only see moves that were really played. Wait for ponderhit or stop.
                while not (self._ponderhit.is_set() or self._stop.is_set()):
                    self._ponderhit.wait(0.01)
                if not self._ponderhit.is_set():
                    return  # the finally clause answers with a fallback move
            start = time.monotonic()
            agent = self._ensure_agent()
            if not self._committed:
                self._commit()
            deadline = None if infinite else agent.deadline_for(board, clock, movetime)
            decision = agent.choose(board, clock, deadline=deadline, stop=self._stop)
            move = decision.move
            self._report(decision)
            wait = decision.think_time if self._opts["CCA_EmulateThinkTime"] == "true" else 0.0
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            if infinite:
                self._stop.wait()
            elif wait - (time.monotonic() - start) > 0:
                self._stop.wait(wait - (time.monotonic() - start))
        except chess.engine.EngineError as exc:
            self._engine_dead.set()
            self.send(f"info string error: engine failed ({exc}); it will be restarted")
        except Exception as exc:
            self.send(f"info string error: {type(exc).__name__}: {exc}")
        finally:
            if move is None:
                legal = next(iter(board.legal_moves), None)
                move = legal.uci() if legal else "0000"
            self.send(f"bestmove {move}")

    def _report(self, decision: Decision) -> None:
        k, s, t = decision.knobs, decision.state, decision.trace
        self.send(
            "info string cca"
            f" q_opt={t.get('q_opt', 0):.3f} q_human={t.get('q_human', 0):.3f}"
            f" trap={t.get('trap_value', 0):+.3f}"
            f" stress={s.stress:.2f} drive={s.drive:+.2f} opp_stress={s.opp_stress:.2f}"
            f" lam={k.kl_weight:.3f} omega={k.exploit:.2f} eps={k.risk_budget:.3f}"
            f" think={decision.think_time:.1f}s" + (" reflex" if t.get("reflex") else "")
        )

    # ------------------------------------------------------------------ seeds
    def _commit(self) -> None:
        self._committed = True
        if not self._opts["CCA_Seed"]:
            digest = hashlib.sha256(self._secret_seed.encode("utf-8")).hexdigest()
            self.send(f"info string cca game {self._games} seed commitment sha256:{digest}")

    def _reveal(self) -> None:
        if self._committed and not self._opts["CCA_Seed"]:
            self.send(f"info string cca game {self._games} seed reveal {self._secret_seed}")

    def _new_game(self) -> None:
        self._reveal()
        self._games += 1
        self._secret_seed = secrets.token_hex(16)
        self._committed = False
        if self._agent is not None:
            self._agent.config = self._config()
            self._agent.new_game(f"uci-{self._games}")

    # ------------------------------------------------------------------ plumbing
    def _config(self) -> AgentConfig:
        opp = parse_uci_opponent(self._opts["UCI_Opponent"]) or int(self._opts["CCA_OpponentElo"])
        return replace(
            AgentConfig(),
            persona=load_persona(self._opts["CCA_Persona"]),
            elo_self=int(self._opts["UCI_Elo"]),
            elo_oppo=opp,
            seed=self._opts["CCA_Seed"] or self._secret_seed,
        )

    def _warm_up(self) -> None:
        """Build engines before the clock runs (``isready`` precedes a game)."""
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            self._ensure_agent()
        except Exception as exc:
            self.send(f"info string error: {type(exc).__name__}: {exc}")

    def _ensure_agent(self) -> CAIMEAgent:
        if self._agent is None:
            from cca.engines.stockfish import StockfishEngine

            engine = StockfishEngine(
                self._opts["StockfishPath"] or None,
                threads=int(self._opts["Threads"]),
                hash_mb=int(self._opts["Hash"]),
                nodes=int(self._opts["CCA_Nodes"]),
            )
            try:
                human = self._human_model(engine)
                agent = CAIMEAgent(engine, human, self._config(), game_id=f"uci-{self._games}")
            except BaseException:
                engine.close()  # never leak a Stockfish process
                raise
            self._engine, self._agent = engine, agent
        return self._agent

    def _human_model(self, engine: SearchEngine) -> HumanModel:
        if self._opts["CCA_HumanModel"] == "maia2":
            try:
                from cca.engines.maia2_human import Maia2HumanModel

                return Maia2HumanModel(
                    model_type=self._opts["CCA_Maia2Type"], device=self._opts["CCA_Device"]
                )
            except ImportError as exc:
                self.send(f"info string maia2 unavailable ({exc}); falling back to QRE human model")
        from cca.engines.qre_human import QREHumanModel

        return QREHumanModel(engine)

    def _join(self) -> None:
        if self._worker is not None:
            self._stop.set()
            self._worker.join()
            self._worker = None
            self._stop.clear()

    def _drop_engines(self) -> None:
        self._join()
        if self._engine is not None:
            with contextlib.suppress(Exception):
                self._engine.close()
        self._engine = None
        self._agent = None

    def _shutdown(self) -> None:
        self._stop.set()
        self._join()
        self._reveal()
        self._drop_engines()


def main() -> None:
    """Entry point for ``cca uci``."""
    UciServer().serve()
