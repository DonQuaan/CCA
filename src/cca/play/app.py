"""The ``cca play`` application, independent of HTTP: engines, sessions and the JSON API.

Engines are built once (in a background thread, so the server answers ``/healthz`` at once)
and shared: one Stockfish process and one human model serve every game, and a global lock
serialises every decision. Each game owns its :class:`~cca.agent.CAIMEAgent` (its latent
state, chaos and think-time memory). Sessions live in memory, capped with LRU eviction.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

import chess
import chess.engine

from cca import __version__
from cca.agent import CAIMEAgent
from cca.config import list_personas, load_persona
from cca.play.game import ApiError, GameSession, TimeControl, board_end
from cca.play.serialize import decision_to_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from cca.agent import AgentConfig
    from cca.engines.base import HumanModel, SearchEngine
    from cca.neuro.controller import Persona

# Same limits as the UCI options UCI_Elo and CCA_OpponentElo (cca.uci.protocol).
ELO_SELF_RANGE = (800, 2600)
ELO_OPPO_RANGE = (400, 3000)
BASE_RANGE = (1.0, 3 * 3600.0)
INC_RANGE = (0.0, 180.0)
MAX_FEN_LENGTH = 100
MAX_SEED_LENGTH = 64
TIME_CONTROLS = (None, (180, 2), (300, 3), (600, 5), (900, 10))
CLI_PERSONA = "cli"
_GAME_ID = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
# A chosen seed is public and goes into a PGN tag, which python-chess writes unescaped:
# no quotes or backslashes (or anything else that PGN or a file name would mangle).
_SEED = re.compile(rf"[A-Za-z0-9._:+-]{{1,{MAX_SEED_LENGTH}}}\Z")
_GAME_FIELDS = frozenset(
    {
        "human_color",
        "persona",
        "elo_self",
        "elo_oppo",
        "fen",
        "seed",
        "time_control",
        "emulate_think_time",
    }
)
_ANALYSE_FIELDS = frozenset({"fen", "persona", "elo_self", "elo_oppo", "seed"})


@dataclass(frozen=True, slots=True)
class Engines:
    """The shared engine and human model, and how the human model was chosen."""

    engine: SearchEngine
    human: HumanModel
    human_kind: str
    """``"maia2"`` or ``"qre"``."""
    fallback_reason: str | None = None
    """Why Maia-2 was requested but not used (``None`` if it was not requested or loaded)."""


@dataclass(frozen=True, slots=True)
class PlaySettings:
    """Everything fixed at start-up (from the command line)."""

    base_config: AgentConfig
    requested_human: str = "maia2"
    nodes: int = 200_000
    threads: int = 1
    hash_mb: int = 256
    max_sessions: int = 16


def persona_description(name: str) -> str:
    """The leading comment block of a shipped persona TOML, as one line."""
    text = resources.files("cca.personas").joinpath(f"{name}.toml").read_text(encoding="utf-8")
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            break
        lines.append(stripped.lstrip("#").strip())
    return " ".join(part for part in lines if part)


def check_base_config(cfg: AgentConfig) -> None:
    """Refuse start-up Elo values outside the ranges ``/api/info`` offers (``ValueError``).

    The command-line (or ``--config``) values are the defaults of every game, so they must
    obey the same limits as the values a client may send.
    """
    for key, value, (lo, hi) in (
        ("elo_self", cfg.elo_self, ELO_SELF_RANGE),
        ("elo_oppo", cfg.elo_oppo, ELO_OPPO_RANGE),
    ):
        if not lo <= value <= hi:
            flag = "--" + key.replace("_", "-")
            raise ValueError(f"{flag} {value} is outside the range cca play offers ({lo}-{hi})")


# ---------------------------------------------------------------- request field parsing
def _reject_unknown(body: dict[str, object], allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ApiError(400, f"unknown field(s): {', '.join(unknown)}")


def _int_field(body: dict[str, object], key: str, bounds: tuple[int, int], default: int) -> int:
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"{key} must be an integer")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return value


def _number(value: object, key: str, bounds: tuple[float, float]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, f"{key} must be a number")
    lo, hi = bounds
    number = float(value)
    if not lo <= number <= hi:  # also rejects NaN
        raise ApiError(400, f"{key} must be between {lo:g} and {hi:g}")
    return number


def parse_fen(value: object) -> chess.Board:
    """A valid, non-finished position (400 otherwise)."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "fen must be a non-empty string")
    if len(value) > MAX_FEN_LENGTH:
        raise ApiError(400, f"fen longer than {MAX_FEN_LENGTH} characters")
    try:
        board = chess.Board(value.strip())
    except ValueError as exc:
        raise ApiError(400, f"invalid FEN: {exc}") from None
    if not board.is_valid():
        raise ApiError(400, f"illegal position ({board.status()!r})")
    if board_end(board) is not None:
        raise ApiError(400, "this position is already finished")
    return board


def _time_control(value: object) -> TimeControl | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError(400, "time_control must be null or {base_s, inc_s}")
    _reject_unknown(value, frozenset({"base_s", "inc_s"}))
    return TimeControl(
        base_s=_number(value.get("base_s"), "time_control.base_s", BASE_RANGE),
        inc_s=_number(value.get("inc_s", 0), "time_control.inc_s", INC_RANGE),
    )


def _seed(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SEED.match(value):
        raise ApiError(
            400,
            f"seed must be 1-{MAX_SEED_LENGTH} letters, digits or . _ : + - (no spaces)",
        )
    return value


class PlayApp:
    """Engines, sessions and API operations; the HTTP layer only routes and encodes."""

    def __init__(
        self,
        factory: Callable[[], Engines],
        settings: PlaySettings,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        check_base_config(settings.base_config)
        self._factory = factory
        self.settings = settings
        self._now = now
        self._engines: Engines | None = None
        self._error: str | None = None
        self._closed = False
        self._warmed = threading.Event()
        self._engine_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._sessions: OrderedDict[str, GameSession] = OrderedDict()
        base = settings.base_config.persona
        self._personas = {name: persona_description(name) for name in list_personas()}
        shipped = base.name in self._personas and load_persona(base.name) == base
        self._default_persona = base.name if shipped else CLI_PERSONA

    # ------------------------------------------------------------------ lifecycle
    def warm_up(self) -> None:
        """Build the engines (called once, in a background thread)."""
        try:
            engines = self._factory()
        except Exception as exc:  # report any start-up failure through /healthz and /api/info
            self._error = f"{type(exc).__name__}: {exc}"
        else:
            with self._state_lock:
                if self._closed:
                    engines.engine.close()
                else:
                    self._engines = engines
        finally:
            self._warmed.set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until warm-up finished; ``True`` if the engines are usable."""
        self._warmed.wait(timeout)
        return self._engines is not None and self._error is None

    def close(self) -> None:
        """Stop the engine process (waits briefly for a running decision)."""
        with self._state_lock:
            self._closed = True
            engines, self._engines = self._engines, None
            self._sessions.clear()
        if engines is not None:
            got = self._engine_lock.acquire(timeout=5.0)
            try:
                engines.engine.close()
            finally:
                if got:
                    self._engine_lock.release()

    @property
    def ready(self) -> bool:
        """Engines built and healthy."""
        return self._engines is not None and self._error is None

    def _require(self) -> Engines:
        if self._error is not None:
            raise ApiError(503, f"engine unavailable: {self._error}")
        engines = self._engines
        if engines is None:
            raise ApiError(503, "the engines are still warming up; try again in a moment")
        return engines

    def _run_engine(self, call: Callable[[], dict[str, object]]) -> dict[str, object]:
        """Run a decision; engine failures become 503 (and a dead engine marks us unhealthy)."""
        try:
            return call()
        except chess.engine.EngineTerminatedError as exc:
            self._error = f"the engine process stopped ({exc}); restart cca play"
            raise ApiError(503, self._error) from None
        except (chess.engine.EngineError, RuntimeError, OSError) as exc:
            raise ApiError(503, f"engine error: {type(exc).__name__}: {exc}") from None

    # ------------------------------------------------------------------ read-only endpoints
    def health(self) -> tuple[int, dict[str, object]]:
        """``/healthz``: 200 while starting or ready, 503 once the engine failed."""
        if self._error is not None:
            return 503, {
                "status": "error",
                "version": __version__,
                "ready": False,
                "error": self._error,
            }
        return 200, {"status": "ok", "version": __version__, "ready": self.ready}

    def info(self) -> dict[str, object]:
        """``/api/info``: versions, engine, human model, personas, defaults and ranges."""
        engines = self._engines
        cfg = self.settings.base_config
        personas: list[dict[str, str]] = [
            {"name": n, "description": d} for n, d in self._personas.items()
        ]
        if self._default_persona == CLI_PERSONA:
            personas.insert(
                0,
                {
                    "name": CLI_PERSONA,
                    "description": f"The persona given on the command line ({cfg.persona.name}).",
                },
            )
        engine_name = None
        if engines is not None:
            engine_name = str(getattr(engines.engine, "name", type(engines.engine).__name__))
        return {
            "version": __version__,
            "ready": self.ready,
            "error": self._error,
            "engine": {
                "name": engine_name,
                "nodes": self.settings.nodes,
                "threads": self.settings.threads,
                "hash_mb": self.settings.hash_mb,
            },
            "human_model": {
                "requested": self.settings.requested_human,
                "kind": engines.human_kind if engines is not None else None,
                "fallback_reason": engines.fallback_reason if engines is not None else None,
            },
            "personas": personas,
            "defaults": {
                "human_color": "white",
                "persona": self._default_persona,
                "elo_self": cfg.elo_self,
                "elo_oppo": cfg.elo_oppo,
                "time_control": None,
                "emulate_think_time": False,
            },
            "ranges": {
                "elo_self": list(ELO_SELF_RANGE),
                "elo_oppo": list(ELO_OPPO_RANGE),
                "base_s": list(BASE_RANGE),
                "inc_s": list(INC_RANGE),
                "fen_length": MAX_FEN_LENGTH,
                "seed_length": MAX_SEED_LENGTH,
            },
            "time_controls": [
                None if tc is None else {"base_s": tc[0], "inc_s": tc[1]} for tc in TIME_CONTROLS
            ],
            "max_sessions": self.settings.max_sessions,
            "sample": cfg.sample,
            "chaos_driver": cfg.chaos_driver,
        }

    # ------------------------------------------------------------------ configuration
    def _persona(self, value: object) -> tuple[str, Persona]:
        base = self.settings.base_config.persona
        if value is None or value == self._default_persona:
            return self._default_persona, base
        if not isinstance(value, str) or value not in self._personas:
            # Shipped names only: load_persona() also accepts file paths, clients may not.
            raise ApiError(400, f"persona must be one of {sorted(self._personas)}")
        return value, load_persona(value)

    def _config(self, body: dict[str, object], seed: str) -> tuple[str, AgentConfig]:
        base = self.settings.base_config
        name, persona = self._persona(body.get("persona"))
        cfg = dataclasses.replace(
            base,
            persona=persona,
            elo_self=_int_field(body, "elo_self", ELO_SELF_RANGE, base.elo_self),
            elo_oppo=_int_field(body, "elo_oppo", ELO_OPPO_RANGE, base.elo_oppo),
            seed=seed,
        )
        return name, cfg

    # ------------------------------------------------------------------ sessions
    def create_game(self, body: dict[str, object]) -> dict[str, object]:
        """``POST /api/games``."""
        _reject_unknown(body, _GAME_FIELDS)
        engines = self._require()
        color = body.get("human_color", "white")
        if not isinstance(color, str) or color not in {"white", "black", "random"}:
            raise ApiError(400, "human_color must be white, black or random")
        if color == "random":
            human_color = secrets.choice([chess.WHITE, chess.BLACK])
        else:
            human_color = chess.WHITE if color == "white" else chess.BLACK
        fen = body.get("fen")
        board = chess.Board() if fen is None or fen == "" else parse_fen(fen)
        chosen = _seed(body.get("seed"))
        seed = chosen if chosen is not None else secrets.token_hex(16)
        persona, cfg = self._config(body, seed)
        time_control = _time_control(body.get("time_control"))
        emulate = body.get("emulate_think_time", False)
        if not isinstance(emulate, bool):
            raise ApiError(400, "emulate_think_time must be true or false")
        with self._engine_lock:  # CAIMEAgent() resets the shared engine's game key
            agent = CAIMEAgent(engines.engine, engines.human, cfg, game_id="play")
        game_id = secrets.token_urlsafe(24)
        session = GameSession(
            game_id,
            agent,
            human_color=human_color,
            board=board,
            persona=persona,
            human_model=engines.human_kind,
            seed_is_secret=chosen is None,
            time_control=time_control,
            emulate_think_time=emulate,
            now=self._now,
        )
        with self._state_lock:
            while len(self._sessions) >= self.settings.max_sessions:
                self._sessions.popitem(last=False)  # least recently used
            self._sessions[game_id] = session
        return {"game_id": game_id, "state": session.state()}

    def session(self, game_id: str) -> GameSession:
        """Look up a game (404 if unknown or evicted) and mark it recently used."""
        if not _GAME_ID.match(game_id):
            raise ApiError(404, "no such game")
        with self._state_lock:
            session = self._sessions.get(game_id)
            if session is None:
                raise ApiError(404, "no such game (evicted, or the server restarted)")
            self._sessions.move_to_end(game_id)
        return session

    def session_count(self) -> int:
        """Number of live sessions."""
        with self._state_lock:
            return len(self._sessions)

    def game_state(self, game_id: str) -> dict[str, object]:
        """``GET /api/games/<id>``."""
        session = self.session(game_id)
        with session.lock:
            session.refresh()
            return session.state()

    def move(self, game_id: str, body: dict[str, object]) -> dict[str, object]:
        """``POST /api/games/<id>/move``."""
        _reject_unknown(body, frozenset({"uci"}))
        session = self.session(game_id)
        with session.lock:
            session.human_move(body.get("uci"))
            return {"state": session.state()}

    def think(self, game_id: str) -> dict[str, object]:
        """``POST /api/games/<id>/think``: CCA plays for the side to move."""
        self._require()
        session = self.session(game_id)
        with session.lock:
            result = self._run_engine(lambda: session.cca_move(self._engine_lock))
            result["state"] = session.state()
            return result

    def undo(self, game_id: str) -> dict[str, object]:
        """``POST /api/games/<id>/undo``."""
        session = self.session(game_id)
        with session.lock:
            session.undo()
            return {"state": session.state()}

    def resign(self, game_id: str) -> dict[str, object]:
        """``POST /api/games/<id>/resign``."""
        session = self.session(game_id)
        with session.lock:
            session.resign()
            return {"state": session.state()}

    def pgn(self, game_id: str) -> str:
        """``GET /api/games/<id>/pgn``."""
        session = self.session(game_id)
        with session.lock:
            session.refresh()
            return session.pgn()

    def decisions(self, game_id: str) -> dict[str, object]:
        """``GET /api/games/<id>/decisions``: every CCA decision of the game, by ply."""
        session = self.session(game_id)
        with session.lock:
            return {
                "decisions": [
                    {"ply": ply, "decision": d} for ply, d in sorted(session.decisions.items())
                ]
            }

    def analyse(self, body: dict[str, object]) -> dict[str, object]:
        """``POST /api/analyse``: one decision on a position, without a game (position lab)."""
        _reject_unknown(body, _ANALYSE_FIELDS)
        engines = self._require()
        board = parse_fen(body.get("fen"))
        seed = _seed(body.get("seed")) or self.settings.base_config.seed
        persona, cfg = self._config(body, seed)

        def run() -> dict[str, object]:
            with self._engine_lock:
                agent = CAIMEAgent(engines.engine, engines.human, cfg, game_id="analyse")
                started = time.monotonic()
                decision = agent.choose(board.copy())
                compute = time.monotonic() - started
            return {
                "fen": board.fen(),
                "turn": "white" if board.turn == chess.WHITE else "black",
                "persona": persona,
                "seed": seed,
                "decision": decision_to_json(board, decision),
                "compute_s": compute,
            }

        return self._run_engine(run)
