"""The ``cca play`` application, independent of HTTP: engines, sessions and the JSON API.

Engines are built once (in a background thread, so the server answers ``/healthz`` at once)
and shared: one Stockfish process and one human model serve every game, and a global lock
serialises every decision. Each game owns its :class:`~cca.agent.CAIMEAgent` (its latent
state, chaos and think-time memory). Sessions live in memory, capped with LRU eviction.

With :class:`~cca.play.limits.Limits` on (a public deployment) the session table also follows
the per-client rules below (:meth:`PlayApp._make_room`), idle games expire, decisions are
rate-limited per client and capped in wall time (``max_think_seconds``), reads of a game's
decisions and PGN are rate-limited per client, and at most ``max_queue`` requests wait for the
engine lock (served round-robin by client). With ``max_connections`` set, at most
:attr:`PlayApp.GAME_WAITERS` requests wait for a game that is busy (a ``/think`` holds its game
while it waits for the engine and decides); one more gets 409 at once, so no client can park
server threads behind a game. No client can raise the engine's search budget: the API has no
field that reaches it, and every engine call keeps the configured ``nodes`` limit (a deadline
can only shorten a search). Shutting down with limits on refuses the requests waiting for the
engine at once and asks a running decision to stop early.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import re
import secrets
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING

import chess
import chess.engine

from cca import __version__
from cca.agent import CAIMEAgent
from cca.config import list_personas, load_persona
from cca.play.game import ApiError, GameSession, TimeControl, board_end
from cca.play.limits import EngineGate, Limits, RateLimiter
from cca.play.serialize import decision_to_json

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
    limits: Limits = field(default_factory=Limits)
    """Per-client and queue limits (all off by default; on with ``--public``)."""


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

    GAME_WAITERS = 2
    """With ``max_connections``: requests that may wait for a busy game (more: 409 at once)."""
    RECLAIM_UNPLAYED_S = 300.0
    """With ``max_sessions_per_client``: on a full table, a game in which nothing was played
    for this long (no CCA decision, no human move further than the game had been once CCA has
    decided in it; reading it does not count, nor does starting it in place of an older game
    of the same client) may be taken for a newcomer's game, whoever owns it (see
    :meth:`_make_room`)."""
    RECLAIM_THINKING_S = 900.0
    """The same while it is the human's move in a game where the human has moved already:
    someone thinking about a move, the page open (15 minutes, the whole base time of the
    page's longest time control). A game nobody has moved in keeps
    :attr:`RECLAIM_UNPLAYED_S`: the page starts one for every visitor, most of whom leave."""
    TAKEN_REMEMBERED = 256
    """Games taken for a newcomer whose reason is remembered, so that their 404 can say why."""

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
        # Per game, under _state_lock: who created it, when it was last used (any request) and
        # when it was last played (created, a CCA decision made, or a human move further than
        # the game had been once CCA has decided in it), on the _now clock.
        self._owners: dict[str, str] = {}
        self._used: dict[str, float] = {}
        self._played: dict[str, float] = {}
        # Games in which CCA has made a decision: only there does a human move count as play
        # (a first move costs no engine time, see _make_room).
        self._decided: set[str] = set()
        self._game_waiters: dict[str, int] = {}  # requests waiting for a busy game, by game id
        # /think requests in progress, by game id: such a game is never taken for someone
        # else's new game (its decision would be made, and charged, for a game then gone).
        self._thinking: dict[str, int] = {}
        # Why recent games were taken for a newcomer (with max_sessions_per_client), for 404s.
        self._taken: OrderedDict[str, str] = OrderedDict()
        # Games started in place of an older game of the same client that kept that game's
        # time without play (see _make_room), until played: their 404 says so.
        self._carried: set[str] = set()
        limits = settings.limits
        self._gate = EngineGate(self._engine_lock, limits.max_queue, now=now)
        self._rate = (
            RateLimiter(limits.decisions_per_minute, now=now)
            if limits.decisions_per_minute is not None
            else None
        )
        self._reads = (
            RateLimiter(
                limits.reads_per_minute,
                now=now,
                limit="reads_per_minute",
                what="reads of a game's decisions or PGN",
            )
            if limits.reads_per_minute is not None
            else None
        )
        # Set by close() with limits on: a running decision stops early (local: never passed).
        self._stop = threading.Event()
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
        """Stop the engine process (waits briefly for a running decision).

        With limits on, the requests waiting for the engine are refused at once (503) and a
        running decision is asked to stop early, so shutting down waits for one decision's
        current engine call at most, not for the whole line.
        """
        if self.settings.limits.active:
            self._stop.set()
            self._gate.close()
        with self._state_lock:
            self._closed = True
            engines, self._engines = self._engines, None
            self._sessions.clear()
            self._owners.clear()
            self._used.clear()
            self._played.clear()
            self._decided.clear()
            self._taken.clear()
            self._carried.clear()
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
        """``/api/info``: versions, engine, human model, personas, defaults and ranges.

        With limits on (``--public``) it also has ``limits``: the values in force (``None`` =
        no limit) and ``waiting``, the requests waiting for the engine right now, so a page
        can explain a 429 or 503. Without limits that key is absent (unchanged output).
        """
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
        info: dict[str, object] = {
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
        limits = self.settings.limits
        if limits.active:
            info["limits"] = {**limits.describe(), "waiting": self._gate.waiting}
        return info

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
    def create_game(self, body: dict[str, object], client: str | None = None) -> dict[str, object]:
        """``POST /api/games`` (``client``: the requester, for the per-client limits)."""
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
        owner = client or ""
        with self._state_lock:  # a full table refuses before any engine time is spent
            now = self._now()
            self._expire(now)
            self._room_plan(owner, now)
        with self._gate.slot(owner):  # CAIMEAgent() resets the shared engine's game key
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
            now = self._now()
            self._expire(now)
            kept_until = self._make_room(owner, now)
            self._sessions[game_id] = session
            self._owners[game_id] = owner
            self._used[game_id] = now
            self._played[game_id] = now
            if kept_until is not None:  # it replaces the owner's own game: no new time
                start = kept_until - self._grace(game_id)
                if start < now:
                    self._played[game_id] = start
                    self._carried.add(game_id)
        return {"game_id": game_id, "state": session.state()}

    def _drop(self, game_id: str, taken: str | None = None) -> None:
        """Forget a game (``taken``: why it went to a newcomer, remembered for its 404)."""
        del self._sessions[game_id]
        del self._owners[game_id]
        del self._used[game_id]
        del self._played[game_id]
        self._decided.discard(game_id)
        self._carried.discard(game_id)
        if taken is not None:
            self._taken[game_id] = taken
            while len(self._taken) > self.TAKEN_REMEMBERED:
                self._taken.popitem(last=False)

    def _expire(self, now: float) -> None:
        """Drop the games idle for ``session_idle_minutes`` or longer (no-op without it)."""
        minutes = self.settings.limits.session_idle_minutes
        if minutes is None:
            return
        idle_s = minutes * 60.0
        for game_id in [g for g, used in self._used.items() if now - used >= idle_s - 1e-9]:
            self._drop(game_id)

    def _make_room(self, owner: str, now: float) -> float | None:
        """Free a slot for a new game of ``owner`` (called under ``_state_lock``).

        Without a per-client limit (the local default) the least recently used game goes, as
        always. With ``max_sessions_per_client`` = *q*:

        1. an owner who already has *q* games loses its own least recently used one(s);
        2. while the table is full (``max_sessions``), the game dropped is, in this order: the
           owner's own least recently used game; then, of the other games, never one with a
           ``/think`` in progress: the least recently used *finished* game of anyone; the least
           recently used game of a client that holds *q* games (its whole share); the game of
           anyone in which nothing was played (no CCA decision, no human move further than the
           game had been once CCA has decided in it) for :attr:`RECLAIM_THINKING_S` seconds if
           it is the human's move and the human has moved in it, :attr:`RECLAIM_UNPLAYED_S`
           seconds otherwise, the longest unplayed first. Reading a game (its state, PGN or
           decisions) does not keep it, nor does a move played again after a take-back. So a
           game in play is taken for someone else's only if its player, below the share, has
           not moved for :attr:`RECLAIM_THINKING_S` seconds, has left CCA's move unasked for,
           or not made a first move, for :attr:`RECLAIM_UNPLAYED_S` seconds; its 404 then
           says so;
        3. a new game counts as played when it is created, unless it replaces a game of its
           own owner (rule 1, or the first choice of rule 2): then it may be taken when that
           game could have been, or when a new game could be if that is sooner (at once if that
           game could be already). A human move counts as play only in a game in which CCA has
           decided (the page asks for CCA's reply at once, and that decision counts): until
           then a game's time runs from its creation (or is the time it kept), whatever its
           player does. So restarting games, and making a first move in each, never keeps a
           slot; only play that costs engine time does;
        4. if no game qualifies, the new game is refused: 503 with ``Retry-After`` = the time
           until the first game can be reclaimed (or expires, if sooner; 60 s without either
           rule; at most an hour).

        Returns when the owner's replaced game could have been taken for a newcomer (``None``:
        it replaces none of its own, or no per-client limit). It replaces one at most: rule 1
        leaves the table below ``max_sessions``, and rule 2 then drops nothing.

        Known limit of this policy: it cannot tell a visitor from an attacker who uses many
        client identities. A newcomer gets a fresh game, so ``max_sessions`` + 1 identities
        that take each game as soon as it may be reclaimed (each from an identity whose own
        game has just gone: one new game per slot every :attr:`RECLAIM_UNPLAYED_S` seconds,
        no engine time) keep most newcomers out. Fewer identities, ``max_sessions`` / (*q* - 1)
        below their share, can keep newcomers out only by playing their games: a CCA decision
        per game every :attr:`RECLAIM_UNPLAYED_S` + :attr:`RECLAIM_THINKING_S` seconds or more
        often (engine time). The defence is a table larger than an attacker can fill: more
        games than the identities it can use without engine time, or *q* - 1 times as many
        with it (a game takes a few KiB).

        :meth:`create_game` asks :meth:`_room_plan` once before it builds the game's agent, so
        a refusal costs no engine time, and again here, when the game is stored.
        """
        quota = self.settings.limits.max_sessions_per_client
        kept_until: float | None = None
        for game_id, taken in self._room_plan(owner, now):
            if quota is not None and self._owners[game_id] == owner:
                kept_until = self._played[game_id] + self._grace(game_id)
            self._drop(game_id, taken)
        return kept_until

    def _room_plan(self, owner: str, now: float) -> list[tuple[str, str | None]]:
        """The games :meth:`_make_room` drops (and why, if someone else's), or its 503.

        Called under ``_state_lock``; drops none.
        """
        quota = self.settings.limits.max_sessions_per_client
        doomed: list[tuple[str, str | None]] = []
        if quota is not None:
            own = [g for g in self._sessions if self._owners[g] == owner]
            doomed += [(g, None) for g in own[: max(0, len(own) - quota + 1)]]
        while len(self._sessions) - len(doomed) >= self.settings.max_sessions:
            victim = self._victim(owner, quota, {g for g, _ in doomed}, now)
            if victim is None:
                wait = self._slot_free_in(now)
                raise ApiError(
                    503,
                    f"all {self.settings.max_sessions} game slots are in use; "
                    f"try again in {wait} s",
                    retry_after=wait,
                    limit="max_sessions",
                )
            doomed.append(victim)
        return doomed

    def _victim(
        self, owner: str, quota: int | None, doomed: set[str], now: float
    ) -> tuple[str, str | None] | None:
        """The next game to drop for a new one of ``owner``, and why if it is someone else's.

        See :meth:`_make_room`. Games already ``doomed`` (to be dropped for the same new game)
        are left out.
        """
        left = [g for g in self._sessions if g not in doomed]  # least recently used first
        if quota is None:
            return left[0], None
        for game_id in left:
            if self._owners[game_id] == owner:
                return game_id, None
        held = Counter(self._owners[g] for g in left)
        others = [g for g in left if g not in self._thinking]
        for game_id in others:
            if self._sessions[game_id].over:
                return game_id, "it had ended"
        for game_id in others:
            if held[self._owners[game_id]] >= quota:
                return game_id, f"its player had {quota} games, the most one player may keep"
        unplayed = [g for g in others if now - self._played[g] >= self._grace(g) - 1e-9]
        if not unplayed:
            return None
        game_id = min(unplayed, key=self._played.__getitem__)
        minutes = round(self._grace(game_id) / 60)
        carried = game_id in self._carried
        also = ", nor in the older game of its player's it replaced," if carried else ""
        # (A move of the player's before CCA's first decision is no play: say what was missing.)
        first_only = self._sessions[game_id].human_moved and game_id not in self._decided
        what = "CCA had not played" if first_only else "nothing had been played"
        return game_id, f"{what} in it{also} for {minutes} minutes"

    def _grace(self, game_id: str) -> float:
        """How long a game may go unplayed before a newcomer may take it (see _make_room).

        (A finished game gets the grace it would have if it were not over: that matters only
        to a new game that replaces it, see :meth:`_make_room`; the finished game itself goes
        before any unplayed one anyway.)
        """
        session = self._sessions[game_id]
        thinking = session.board.turn == session.human_color and session.human_moved
        return self.RECLAIM_THINKING_S if thinking else self.RECLAIM_UNPLAYED_S

    def _slot_free_in(self, now: float) -> int:
        """Whole seconds until the first game can be reclaimed or expires.

        Whichever comes sooner; 60 without either rule; at most an hour.
        """
        limits = self.settings.limits
        if not self._used:
            return 60
        frees: list[float] = []
        if limits.session_idle_minutes is not None:
            frees.append(min(self._used.values()) + limits.session_idle_minutes * 60.0)
        if limits.max_sessions_per_client is not None:
            frees += [
                played + self._grace(g)
                for g, played in self._played.items()
                if g not in self._thinking
            ]
        if not frees:
            return 60
        return min(3600, max(1, math.ceil(min(frees) - now - 1e-9)))

    def session(self, game_id: str) -> GameSession:
        """Look up a game (404 if unknown, evicted or expired) and mark it recently used."""
        return self._lookup(game_id)

    def _lookup(self, game_id: str, *, thinking: bool = False) -> GameSession:
        """:meth:`session`; ``thinking``: count a ``/think`` in progress (see ``_thinking``)."""
        if not _GAME_ID.match(game_id):
            raise ApiError(404, "no such game")
        with self._state_lock:
            now = self._now()
            self._expire(now)
            session = self._sessions.get(game_id)
            if session is None:
                raise ApiError(404, self._gone(game_id))
            self._sessions.move_to_end(game_id)
            self._used[game_id] = now
            if thinking:
                self._thinking[game_id] = self._thinking.get(game_id, 0) + 1
        return session

    def _played_now(self, game_id: str, *, decision: bool = False) -> None:
        """The game is in play (see :meth:`_make_room`), if it is still here (under the lock).

        ``decision``: CCA has decided in it; otherwise a human move, which counts only in a
        game in which CCA has decided (a first move costs no engine time).
        """
        if game_id not in self._played:
            return
        if decision:
            self._decided.add(game_id)
        elif game_id not in self._decided:
            return
        self._played[game_id] = self._now()
        self._carried.discard(game_id)

    def _gone(self, game_id: str) -> str:
        taken = self._taken.get(game_id)
        if taken is not None:
            return (
                "no such game any more: every game slot was in use and this game went to a "
                f"newcomer, as {taken}"
            )
        minutes = self.settings.limits.session_idle_minutes
        if minutes is None:
            return "no such game (evicted, or the server restarted)"
        return (
            f"no such game (unused for {minutes} minutes, replaced by a newer game, "
            "or the server restarted)"
        )

    def session_count(self) -> int:
        """Number of live sessions."""
        with self._state_lock:
            self._expire(self._now())
            return len(self._sessions)

    @contextlib.contextmanager
    def _game(self, session: GameSession) -> Iterator[None]:
        """Hold ``session``'s lock; with ``max_connections``, wait behind few others only.

        A game's lock is held long only by a ``/think`` (waiting for the engine, then deciding;
        the engine line bounds both). Locally every request waits its turn, as always. With
        ``max_connections`` at most :attr:`GAME_WAITERS` requests wait for a busy game and one
        more is refused at once (409, ``Retry-After`` from the engine's recent hold times), so
        requests on a busy game cannot pile up server threads.
        """
        lock = session.lock
        if self.settings.limits.max_connections is None:
            lock.acquire()  # local: every request waits its turn
        elif not lock.acquire(blocking=False):
            self._wait_for_game(session)
        try:
            yield
        finally:
            lock.release()

    def _wait_for_game(self, session: GameSession) -> None:
        """Take a busy game's lock as one of its few waiters (409 at once when they are many)."""
        with self._state_lock:
            waiting = self._game_waiters.get(session.id, 0)
            if waiting >= self.GAME_WAITERS:
                wait = self._gate.estimate()
                raise ApiError(
                    409,
                    "this game is busy with another request (CCA is thinking); "
                    f"try again in {wait} s",
                    retry_after=wait,
                    limit="game_busy",
                )
            self._game_waiters[session.id] = waiting + 1
        try:
            session.lock.acquire()
        finally:
            with self._state_lock:
                left = self._game_waiters.pop(session.id) - 1
                if left:
                    self._game_waiters[session.id] = left

    def _decision(
        self, client: str | None, work: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        """Run ``work`` as one CCA decision of ``client`` under the per-client rate limit.

        429 when the client's budget is spent. A refusal of the engine line (503 busy, or 429
        too many requests of this client at once: the only 429 ``work`` can raise) or an engine
        failure (503) means no decision was made: the client gets its token back. Any other
        refusal (409: not CCA's turn, game busy) is the client's doing and is charged.
        """
        rate = self._rate
        if rate is None:
            return work()
        key = client or ""
        rate.take(key)
        try:
            return work()
        except ApiError as exc:
            if exc.status in {429, 503}:
                rate.give_back(key)
            raise

    def game_state(self, game_id: str) -> dict[str, object]:
        """``GET /api/games/<id>``."""
        session = self.session(game_id)
        with self._game(session):
            session.refresh()
            return session.state()

    def move(self, game_id: str, body: dict[str, object]) -> dict[str, object]:
        """``POST /api/games/<id>/move``."""
        _reject_unknown(body, frozenset({"uci"}))
        session = self.session(game_id)
        with self._game(session):
            further = session.human_move(body.get("uci"))
            state = session.state()
        if further:  # (not a move played again after a take-back; see _played_now)
            with self._state_lock:
                self._played_now(game_id)
        return {"state": state}

    def _stop_event(self) -> threading.Event | None:
        """What a decision is stopped by at shutdown (limits on only; local: nothing)."""
        return self._stop if self.settings.limits.active else None

    def _think_deadline(self) -> float | None:
        """The ``max_think_seconds`` deadline of a decision that has just taken the engine."""
        cap = self.settings.limits.max_think_seconds
        return None if cap is None else time.monotonic() + cap

    def think(self, game_id: str, client: str | None = None) -> dict[str, object]:
        """``POST /api/games/<id>/think``: CCA plays for the side to move."""
        self._require()
        session = self._lookup(game_id, thinking=True)  # (counted until it ends, see below)
        cap = self.settings.limits.max_think_seconds
        stop = self._stop_event()

        def play() -> dict[str, object]:
            with self._game(session):
                slot = self._gate.slot(client or "")
                result = self._run_engine(
                    lambda: session.cca_move(slot, max_think_s=cap, stop=stop)
                )
                result["state"] = session.state()
                return result

        decided = False
        try:
            result = self._decision(client, play)
            decided = True
        finally:
            with self._state_lock:
                left = self._thinking.pop(game_id, 1) - 1
                if left > 0:
                    self._thinking[game_id] = left
                if decided:
                    self._played_now(game_id, decision=True)
        return result

    def undo(self, game_id: str) -> dict[str, object]:
        """``POST /api/games/<id>/undo``."""
        session = self.session(game_id)
        with self._game(session):
            session.undo()
            return {"state": session.state()}

    def resign(self, game_id: str) -> dict[str, object]:
        """``POST /api/games/<id>/resign``."""
        session = self.session(game_id)
        with self._game(session):
            session.resign()
            return {"state": session.state()}

    def _read(self, client: str | None) -> None:
        """Count a read whose size grows with the game (no limit without ``reads_per_minute``).

        429 when the client's budget is spent.
        """
        if self._reads is not None:
            self._reads.take(client or "")

    def pgn(self, game_id: str, client: str | None = None) -> str:
        """``GET /api/games/<id>/pgn``."""
        self._read(client)
        session = self.session(game_id)
        with self._game(session):
            session.refresh()
            return session.pgn()

    def decisions(self, game_id: str, client: str | None = None) -> bytes:
        """``GET /api/games/<id>/decisions``: every CCA decision of the game, by ply (JSON).

        The response is joined from the decisions serialised when they were made (the same
        bytes as serialising them all now), so its cost does not grow with serialising work.
        """
        self._read(client)
        session = self.session(game_id)
        with self._game(session):
            return session.decisions_body()

    def analyse(self, body: dict[str, object], client: str | None = None) -> dict[str, object]:
        """``POST /api/analyse``: one decision on a position, without a game (position lab)."""
        _reject_unknown(body, _ANALYSE_FIELDS)
        engines = self._require()
        board = parse_fen(body.get("fen"))
        seed = _seed(body.get("seed")) or self.settings.base_config.seed
        persona, cfg = self._config(body, seed)

        stop = self._stop_event()

        def run() -> dict[str, object]:
            with self._gate.slot(client or ""):
                agent = CAIMEAgent(engines.engine, engines.human, cfg, game_id="analyse")
                started = time.monotonic()
                decision = agent.choose(board.copy(), deadline=self._think_deadline(), stop=stop)
                compute = time.monotonic() - started
            return {
                "fen": board.fen(),
                "turn": "white" if board.turn == chess.WHITE else "black",
                "persona": persona,
                "seed": seed,
                "decision": decision_to_json(board, decision),
                "compute_s": compute,
            }

        return self._decision(client, lambda: self._run_engine(run))
