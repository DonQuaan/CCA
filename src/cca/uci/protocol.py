"""CCA as a UCI engine (for lichess-bot, cutechess-cli, Arena, ...).

Only the subset of UCI that a GUI needs to *play* is implemented: ``uci``, ``isready``,
``setoption``, ``ucinewgame``, ``position``, ``go`` (clock fields, ``movetime``,
``infinite``, ``ponder``), ``ponderhit``, ``stop``, ``quit``. Search runs in a worker thread
so ``stop`` and ``isready`` are answered while thinking.

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

from cca import __version__
from cca.agent import AgentConfig, CAIMEAgent
from cca.config import list_personas, load_persona
from cca.core.types import Clock

if TYPE_CHECKING:
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


class UciServer:
    """A blocking UCI loop around :class:`CAIMEAgent` (engines are created lazily)."""

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
        self._games = 0
        # Unset seed -> a secret random one: opponents must not be able to replay the chaos.
        self._secret_seed = secrets.token_hex(16)

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
                self.send("readyok")
            elif cmd == "setoption":
                self._cmd_setoption(args)
            elif cmd == "ucinewgame":
                self._join()
                self._games += 1
                if self._agent is not None:
                    self._agent.new_game(f"uci-{self._games}")
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
        self.send("option name CCA_HumanModel type combo default maia2 var maia2 var qre")
        self.send("option name CCA_Maia2Type type combo default rapid var rapid var blitz")
        self.send("option name CCA_Device type combo default gpu var gpu var cpu")
        self.send("option name CCA_Seed type string default <secret>")
        self.send("option name CCA_EmulateThinkTime type check default false")
        if not self._opts["CCA_Seed"]:
            digest = hashlib.sha256(self._secret_seed.encode("utf-8")).hexdigest()[:16]
            self.send(f"info string cca secret seed commitment sha256:{digest}")
        self.send("uciok")

    def _cmd_setoption(self, args: list[str]) -> None:
        if "name" not in args:
            raise ValueError("setoption without name")
        i_name = args.index("name")
        i_val = args.index("value") if "value" in args else len(args)
        name = " ".join(args[i_name + 1 : i_val])
        value = " ".join(args[i_val + 1 :])
        if name in _SPIN_LIMITS:
            _, lo, hi = _SPIN_LIMITS[name]
            value = str(min(hi, max(lo, int(value))))
        elif name not in self._opts:
            self.send(f"info string ignoring unknown option {name}")
            return
        if value in {"<auto>", "<empty>"}:
            value = ""
        self._opts[name] = value
        if name in {
            "StockfishPath",
            "Threads",
            "Hash",
            "CCA_Nodes",
            "CCA_HumanModel",
            "CCA_Maia2Type",
            "CCA_Device",
        }:
            self._drop_engines()
        elif self._agent is not None:
            self._agent.config = self._config()

    def _cmd_go(self, args: list[str]) -> None:
        self._join()
        clock, movetime, infinite = parse_go(args, self._board.turn)
        ponder = "ponder" in args
        agent = self._ensure_agent()
        board = self._board.copy()
        emulate = self._opts["CCA_EmulateThinkTime"].lower() == "true"
        self._stop.clear()
        self._ponderhit.clear()

        def work() -> None:
            if ponder:
                # Never touch the agent on a *guessed* position: its affect/chaos state must
                # only see moves that were really played. Wait for ponderhit or stop.
                while not (self._ponderhit.is_set() or self._stop.is_set()):
                    self._ponderhit.wait(0.01)
                if not self._ponderhit.is_set():
                    legal = next(iter(board.legal_moves), None)
                    self.send(f"bestmove {legal.uci() if legal else '0000'}")
                    return
            start = time.monotonic()
            try:
                decision = agent.choose(board, clock)
            except (ValueError, RuntimeError) as exc:
                self.send(f"info string error: {exc}")
                legal = next(iter(board.legal_moves), None)
                self.send(f"bestmove {legal.uci() if legal else '0000'}")
                return
            k = decision.knobs
            s = decision.state
            self.send(
                "info string cca"
                f" q_opt={decision.trace.get('q_opt', 0):.3f}"
                f" q_human={decision.trace.get('q_human', 0):.3f}"
                f" trap={decision.trace.get('trap_value', 0):+.3f}"
                f" stress={s.stress:.2f} drive={s.drive:+.2f} opp_stress={s.opp_stress:.2f}"
                f" lam={k.kl_weight:.3f} omega={k.exploit:.2f} eps={k.risk_budget:.3f}"
                f" think={decision.think_time:.1f}s"
            )
            wait = decision.think_time if emulate else 0.0
            if movetime is not None:
                wait = min(wait, movetime)
            remaining = wait - (time.monotonic() - start)
            if infinite:
                self._stop.wait()
            elif remaining > 0:
                self._stop.wait(remaining)
            self.send(f"bestmove {decision.move}")

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ plumbing
    def _config(self) -> AgentConfig:
        opp = parse_uci_opponent(self._opts["UCI_Opponent"]) or int(self._opts["CCA_OpponentElo"])
        base = AgentConfig()
        return replace(
            base,
            persona=load_persona(self._opts["CCA_Persona"]),
            elo_self=int(self._opts["UCI_Elo"]),
            elo_oppo=opp,
            seed=self._opts["CCA_Seed"] or self._secret_seed,
        )

    def _ensure_agent(self) -> CAIMEAgent:
        if self._agent is None:
            from cca.engines.stockfish import StockfishEngine

            engine = StockfishEngine(
                self._opts["StockfishPath"] or None,
                threads=int(self._opts["Threads"]),
                hash_mb=int(self._opts["Hash"]),
                nodes=int(self._opts["CCA_Nodes"]),
            )
            self._engine = engine
            human = self._human_model(engine)
            self._agent = CAIMEAgent(engine, human, self._config(), game_id=f"uci-{self._games}")
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
            self._engine.close()
        self._engine = None
        self._agent = None

    def _shutdown(self) -> None:
        self._stop.set()
        self._join()
        self._drop_engines()


def main() -> None:
    """Entry point for ``cca uci``."""
    UciServer().serve()
