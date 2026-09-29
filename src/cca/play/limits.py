"""Request limits of ``cca play`` for a public deployment; every one is off by default.

*Who a client is* (:func:`client_id`): the TCP peer, or, behind ``trusted_proxies`` reverse
proxies, the ``X-Forwarded-For`` entry that many hops from the right (each proxy appends the
address it received the request from, so only the entries the trusted proxies wrote are
reliable; everything left of them was sent by the client and may be forged). IPv6 clients are
counted by /64, the block a subscriber usually gets, so rotating addresses inside it does not
multiply a quota. The identity only keys in-memory counters: it is never logged or stored.

*The limits*: a token bucket of CCA decisions per client and minute (:class:`RateLimiter`), a
wall-clock cap on each decision (``max_think_seconds``), a token bucket of the reads whose size
grows with a game (``reads_per_minute``), a bounded waiting line in front of the single engine
lock that serves waiting clients in turn (:class:`EngineGate`), the per-client and idle rules
of the session table (applied by :class:`~cca.play.app.PlayApp`) and bounds on the server's
threads (``max_connections``, and ``max_connections_per_peer`` for a server that clients reach
directly, applied by :class:`~cca.play.server.PlayServer` and the app). A
refusal is a 429 (this client is too fast, or has too many requests at once), a 503 (the server
is busy or full) or a 409 (this game is busy), each with a ``Retry-After`` in whole seconds and
a ``limit`` naming the limit that refused.
"""

from __future__ import annotations

import ipaddress
import itertools
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cca.play.game import ApiError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

MAX_TRACKED_CLIENTS = 4096
"""Clients whose decision budget is remembered; beyond, full buckets and then the least
recently active ones are forgotten (a forgotten client simply starts with a full bucket)."""
_PRUNE_TO = 0.75
"""When more than ``max_clients`` buckets are remembered, they are pruned down to this share
of it in one pass, so that the pass (a scan of every bucket) runs rarely, not on every take."""
_EPS = 1e-9  # float slack at the boundaries (coarse monotonic clocks, see CLAUDE.md gotchas)


@dataclass(frozen=True, slots=True)
class Limits:
    """Request limits of ``cca play`` (``None`` = no limit, the local default)."""

    public: bool = False
    """Public mode (``--public``): the limits below default on and each request is logged."""
    max_sessions_per_client: int | None = None
    """Games one client keeps; a new game replaces its least recently used one."""
    decisions_per_minute: int | None = None
    """CCA decisions (``/think`` and ``/api/analyse``) per client and minute (token bucket)."""
    max_queue: int | None = None
    """Requests that may wait for the engine while it works; one more gets 503 (busy)."""
    session_idle_minutes: int | None = None
    """A game nobody touched for this long is dropped."""
    max_connections: int | None = None
    """Connections handled at once (one thread each); one more gets 503 at once. Also bounds
    per client (API requests in progress) and per game (requests waiting for it)."""
    max_think_seconds: float | None = None
    """Wall-clock cap of one CCA decision, once it has the engine: the agent gets a deadline
    that many seconds ahead (the earlier of it and a timed game's own clock deadline) and
    shortens its search to meet it, down to reflex mode (the engine's best move)."""
    reads_per_minute: int | None = None
    """Reads of a game's decisions or PGN (the responses that grow with the game) per client
    and minute (token bucket)."""
    max_connections_per_peer: int | None = None
    """With ``max_connections`` and no trusted proxy: connections one TCP peer (an IPv6 /64 as
    one) may have open at once, whatever they are doing; one more gets 503 at once. Off unless
    asked for, even in public mode: behind a proxy (also one not trusted, ``trusted_proxies``
    0) every connection comes from the proxy, and this would cap the whole site."""

    @property
    def active(self) -> bool:
        """Whether public mode or any limit is on."""
        return self.public or any(
            value is not None
            for value in (
                self.max_sessions_per_client,
                self.decisions_per_minute,
                self.max_queue,
                self.session_idle_minutes,
                self.max_connections,
                self.max_think_seconds,
                self.reads_per_minute,
                self.max_connections_per_peer,
            )
        )

    def describe(self) -> dict[str, object]:
        """The limits as ``/api/info`` shows them (``None`` = no limit)."""
        return {
            "public": self.public,
            "max_sessions_per_client": self.max_sessions_per_client,
            "decisions_per_minute": self.decisions_per_minute,
            "max_queue": self.max_queue,
            "session_idle_minutes": self.session_idle_minutes,
            "max_connections": self.max_connections,
            "max_think_seconds": self.max_think_seconds,
            "reads_per_minute": self.reads_per_minute,
            "max_connections_per_peer": self.max_connections_per_peer,
        }


PUBLIC_LIMITS = Limits(
    public=True,
    max_sessions_per_client=3,
    decisions_per_minute=20,
    max_queue=6,
    session_idle_minutes=30,
    max_connections=64,
    max_think_seconds=20.0,
    reads_per_minute=60,
)
"""The defaults of ``--public`` (each can be overridden by its own flag;
``max_connections_per_peer`` stays off unless given)."""


def resolve_limits(
    *,
    public: bool,
    max_sessions_per_client: int | None = None,
    decisions_per_minute: int | None = None,
    max_queue: int | None = None,
    session_idle_minutes: int | None = None,
    max_connections: int | None = None,
    max_think_seconds: float | None = None,
    reads_per_minute: int | None = None,
    max_connections_per_peer: int | None = None,
) -> Limits:
    """The limits in force: the given values, else the public-mode defaults, else none."""
    base = PUBLIC_LIMITS if public else Limits()

    def pick(value: int | None, default: int | None) -> int | None:
        return default if value is None else value

    return Limits(
        public=public,
        max_sessions_per_client=pick(max_sessions_per_client, base.max_sessions_per_client),
        decisions_per_minute=pick(decisions_per_minute, base.decisions_per_minute),
        max_queue=pick(max_queue, base.max_queue),
        session_idle_minutes=pick(session_idle_minutes, base.session_idle_minutes),
        max_connections=pick(max_connections, base.max_connections),
        max_think_seconds=(
            base.max_think_seconds if max_think_seconds is None else max_think_seconds
        ),
        reads_per_minute=pick(reads_per_minute, base.reads_per_minute),
        max_connections_per_peer=pick(max_connections_per_peer, base.max_connections_per_peer),
    )


# ---------------------------------------------------------------- client identity
def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _key(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:  # ::ffff:a.b.c.d is the IPv4 client a.b.c.d
            return str(address.ipv4_mapped)
        prefix = ipaddress.IPv6Address(int(address) >> 64 << 64)  # (drops any %scope too)
        return f"{prefix}/64"
    return str(address)


def _hops(forwarded_for: Iterable[str]) -> list[str]:
    return [hop.strip() for line in forwarded_for for hop in line.split(",")]


def forwarded_hops(forwarded_for: Iterable[str]) -> int:
    """How many entries ``X-Forwarded-For`` has, over every header line.

    Counted as :func:`forwarded_client` counts them. Only this number is ever logged
    (``--log-forwarded-hops``), never an entry.
    """
    return len(_hops(forwarded_for))


def forwarded_client(forwarded_for: Iterable[str], trusted_proxies: int) -> str | None:
    """The address ``trusted_proxies`` hops from the right of ``X-Forwarded-For``, if valid.

    ``forwarded_for`` holds every ``X-Forwarded-For`` header line in order (several lines form
    one list). ``None`` when no proxy is trusted, when the list is shorter than the number of
    trusted proxies, or when that entry is not a bare IP address (a port, ``unknown``, a name).
    """
    if trusted_proxies <= 0:
        return None
    hops = _hops(forwarded_for)
    if len(hops) < trusted_proxies:
        return None
    address = _ip(hops[-trusted_proxies])
    return None if address is None else str(address)


def client_id(peer: str, forwarded_for: Iterable[str], trusted_proxies: int) -> str:
    """The key a client's quotas are counted under (see the module docstring)."""
    chosen = forwarded_client(forwarded_for, trusted_proxies)
    address = _ip(chosen if chosen is not None else peer.split("%", 1)[0])
    return peer if address is None else _key(address)


# ---------------------------------------------------------------- decision rate
class RateLimiter:
    """Token bucket per client: ``per_minute`` tokens, refilled evenly; a request takes one.

    ``limit`` names the limit in a refusal and ``what`` the counted requests in its message
    (by default CCA decisions: ``decisions_per_minute``).
    """

    def __init__(
        self,
        per_minute: int,
        *,
        now: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
        limit: str = "decisions_per_minute",
        what: str = "CCA decisions",
    ) -> None:
        if per_minute < 1 or max_clients < 1:
            raise ValueError("per_minute and max_clients must be at least 1")
        self.per_minute = per_minute
        self._rate = per_minute / 60.0  # tokens per second
        self._now = now
        self._max_clients = max_clients
        self._limit = limit
        self._what = what
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # tokens, at

    def _tokens(self, client: str, now: float) -> float:
        saved = self._buckets.get(client)
        if saved is None:
            return float(self.per_minute)
        tokens, at = saved
        return min(float(self.per_minute), tokens + max(0.0, now - at) * self._rate)

    def take(self, client: str) -> None:
        """Spend one token of ``client``; 429 with ``Retry-After`` when none is left."""
        with self._lock:
            now = self._now()
            tokens = self._tokens(client, now)
            if tokens + _EPS < 1.0:
                wait = max(1, math.ceil((1.0 - tokens) / self._rate - _EPS))
                raise ApiError(
                    429,
                    f"slow down: at most {self.per_minute} {self._what} per minute; "
                    f"try again in {wait} s",
                    retry_after=wait,
                    limit=self._limit,
                )
            self._buckets[client] = (tokens - 1.0, now)
            self._buckets.move_to_end(client)
            if len(self._buckets) > self._max_clients:
                self._forget(now)

    def give_back(self, client: str) -> None:
        """Return the token of a decision the server could not make (busy, engine failure)."""
        with self._lock:
            if client not in self._buckets:
                return  # forgotten meanwhile: it is full already
            now = self._now()  # (_tokens caps every read at per_minute: no cap needed here)
            self._buckets[client] = (self._tokens(client, now) + 1.0, now)

    def _forget(self, now: float) -> None:
        """Prune the table to :data:`_PRUNE_TO` of ``max_clients`` in one pass.

        Full buckets go first (a full bucket is the same as no bucket at all), then the least
        recently active clients. The next pass is then a quarter of ``max_clients`` takes away.
        """
        keep = max(1, math.floor(self._max_clients * _PRUNE_TO))
        full = [c for c in self._buckets if self._tokens(c, now) + _EPS >= self.per_minute]
        for client in full:
            del self._buckets[client]
        while len(self._buckets) > keep:
            self._buckets.popitem(last=False)  # the least recently active client

    def tracked(self) -> int:
        """Number of clients whose bucket is remembered."""
        with self._lock:
            return len(self._buckets)


# ---------------------------------------------------------------- engine queue
class EngineLock(Protocol):
    """What :class:`EngineGate` needs of its lock (a :class:`threading.Lock` has it)."""

    def acquire(self, blocking: bool = ..., timeout: float = ...) -> bool:
        """Take the lock."""
        ...

    def release(self) -> None:
        """Give the lock back."""
        ...

    def locked(self) -> bool:
        """Whether someone holds the lock."""
        ...


class _Ticket:
    """A place in the engine's waiting line (see :class:`EngineGate`)."""

    __slots__ = ("key", "refused", "seq")

    def __init__(self, key: object, seq: int) -> None:
        self.key = key  # the client, or a unique object for an anonymous request
        self.seq = seq  # arrival order
        self.refused = False  # set by EngineGate.close(): the server is shutting down


class EngineGate:
    """The engine lock behind a bounded waiting line that serves its clients in turn.

    With ``max_queue`` set, at most that many requests wait while the engine works; the next
    one is refused at once with 503 (busy) and a ``Retry-After`` estimated from recent hold
    times, rather than holding a server thread (and the client) for as long as the line is.
    One client may hold at most :attr:`PER_CLIENT_WAITING` places in the line (more: 429), so a
    single client cannot fill it; the page itself never has more than two engine requests
    open (a move and a position-lab analysis). With ``None`` it is the plain lock (local).

    The line is served round-robin by client, not in the order the lock happens to be won:
    each time the engine is taken, the next turn goes to the waiting client whose last turn is
    the oldest (a client never served comes first; arrival order breaks ties). A client that
    keeps its two places filled therefore gets one decision in turn with every other waiting
    client, and a newcomer waits for at most the decision running and the turn already given,
    however many greedy clients there are. Exactly one waiter at a time (the one whose turn it
    is) waits on the engine lock itself; the others wait for their turn.

    The caps also hold for a burst on a free engine: a request that arrives while others were
    let in but have not taken the engine yet waits behind them, so it is counted against the
    line too (a free engine with nobody about to take it admits at once).

    :meth:`close` (the server is shutting down) refuses every request still waiting for its
    turn and every later one at once (503), instead of letting them run decisions one by one.

    ``with gate:`` waits as an anonymous request; ``with gate.slot(client):`` as ``client``.
    """

    INITIAL_HOLD_S = 2.0
    """Hold-time estimate before any decision has been measured."""
    PER_CLIENT_WAITING = 2
    """Places in the waiting line one client may hold at a time."""
    MAX_RETRY_AFTER_S = 300
    """Upper bound of the ``Retry-After`` estimate (a hint, never a promise)."""
    TURN_MEMORY = 256
    """Clients whose last turn is remembered for the round-robin order (a forgotten client
    counts as never served, i.e. as the one whose turn is the oldest)."""
    CLOSING_RETRY_AFTER_S = 5
    """``Retry-After`` of a request refused because the server is shutting down."""

    def __init__(
        self,
        lock: EngineLock,
        max_queue: int | None,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock: EngineLock = lock
        self.max_queue = max_queue
        self._now = now
        self._mutex = threading.Lock()
        self._turn_changed = threading.Condition(self._mutex)
        self._waiting = 0
        self._waiting_by: dict[str, int] = {}
        self._held_since = 0.0
        self._hold_s = self.INITIAL_HOLD_S  # moving average of hold times
        # The line (under _mutex): tickets by client, the one whose turn it is, and when each
        # client last had a turn (turn numbers; least recent first).
        self._queues: dict[object, deque[_Ticket]] = {}
        self._head: _Ticket | None = None
        self._last_turn: OrderedDict[object, int] = OrderedDict()
        self._turns = itertools.count()
        self._arrivals = itertools.count()
        self._closed = False

    @property
    def waiting(self) -> int:
        """Requests waiting for the engine right now."""
        with self._mutex:
            return self._waiting

    def slot(self, client: str | None) -> _Slot:
        """A context manager that waits for the engine on behalf of ``client``."""
        return _Slot(self, client)

    def __enter__(self) -> None:
        self.acquire(None)

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _estimate(self) -> int:
        """Whole seconds until the line now waiting is served (called under ``_mutex``)."""
        wait = math.ceil(self._hold_s * (self._waiting + 1) - _EPS)
        return min(self.MAX_RETRY_AFTER_S, max(1, wait))

    def estimate(self) -> int:
        """Whole seconds until the engine is likely free for one more request (a hint)."""
        with self._mutex:
            return self._estimate()

    def _refusal(self, status: int, why: str, limit: str) -> ApiError:
        wait = self._estimate()
        return ApiError(status, f"{why}; try again in {wait} s", retry_after=wait, limit=limit)

    def _closing(self) -> ApiError:
        wait = self.CLOSING_RETRY_AFTER_S
        return ApiError(503, f"cca play is shutting down; try again in {wait} s", retry_after=wait)

    def acquire(self, client: str | None) -> None:
        """Take the engine, waiting in line (503 when the line is full, 429 per client)."""
        if self.max_queue is None:
            self._lock.acquire()
            return
        with self._mutex:
            if self._closed:
                raise self._closing()
            free = not self._lock.locked()
            if not free or self._waiting:  # this request will wait (see the class docstring)
                ahead = self._waiting - 1 if free else self._waiting
                if ahead >= self.max_queue:
                    raise self._refusal(
                        503,
                        f"busy: CCA is thinking for other players ({self._waiting} waiting)",
                        "max_queue",
                    )
                if client is not None and self._waiting_by.get(client, 0) >= (
                    self.PER_CLIENT_WAITING
                ):
                    raise self._refusal(
                        429,
                        "too many requests at once: wait for your previous ones",
                        "requests_at_once",
                    )
            self._count(client, +1)
            ticket = _Ticket(client if client is not None else object(), next(self._arrivals))
            self._queues.setdefault(ticket.key, deque()).append(ticket)
            if self._head is None:
                self._next_turn()
            try:
                while self._head is not ticket and not ticket.refused:
                    self._turn_changed.wait()
            except BaseException:  # pragma: no cover - an asynchronous exception while waiting
                self._leave(ticket, client)
                raise
            if ticket.refused:  # close() took it out of the line (and uncounted it)
                raise self._closing()
        try:
            self._lock.acquire()  # its turn: only this waiter waits on the engine lock
        finally:
            with self._mutex:
                self._count(client, -1)
                self._head = None
                self._next_turn()
                self._turn_changed.notify_all()
        if self.closed:  # shutdown began while it waited: leave the engine to close()
            self._lock.release()
            raise self._closing()
        self._held_since = self._now()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called (the server is shutting down)."""
        with self._mutex:
            return self._closed

    def _count(self, client: str | None, step: int) -> None:
        """Count a waiting request of ``client`` in or out (under ``_mutex``)."""
        self._waiting += step
        if client is None:
            return
        left = self._waiting_by.pop(client, 0) + step
        if left:
            self._waiting_by[client] = left

    def _next_turn(self) -> None:
        """Give the turn to the waiting client whose last turn is the oldest (under ``_mutex``)."""
        if not self._queues:
            return
        key = min(
            self._queues,
            key=lambda k: (self._last_turn.get(k, -1), self._queues[k][0].seq),
        )
        queue = self._queues[key]
        self._head = queue.popleft()
        if not queue:
            del self._queues[key]
        self._last_turn[key] = next(self._turns)
        self._last_turn.move_to_end(key)
        while len(self._last_turn) > self.TURN_MEMORY:
            self._last_turn.popitem(last=False)
        self._turn_changed.notify_all()

    def _leave(self, ticket: _Ticket, client: str | None) -> None:  # pragma: no cover
        """Take ``ticket`` out of the line (under ``_mutex``), e.g. after an interrupt."""
        if self._head is ticket:
            self._head = None
            self._next_turn()
        else:
            queue = self._queues.get(ticket.key)
            if queue is not None and ticket in queue:
                queue.remove(ticket)
                if not queue:
                    del self._queues[ticket.key]
        self._count(client, -1)

    def close(self) -> None:
        """Refuse every request waiting for its turn, and every later one, at once (503).

        The request whose turn it is waits on the engine lock itself; once it has the engine it
        gives it back unused. A decision already running is not interrupted here.
        """
        with self._mutex:
            self._closed = True
            for queue in self._queues.values():
                for ticket in queue:
                    ticket.refused = True
                    key = ticket.key
                    self._count(key if isinstance(key, str) else None, -1)
            self._queues.clear()
            self._turn_changed.notify_all()

    def release(self) -> None:
        """Give the engine back (and learn how long it was held)."""
        if self.max_queue is not None:
            held = max(0.0, self._now() - self._held_since)
            with self._mutex:
                self._hold_s = 0.7 * self._hold_s + 0.3 * held
        self._lock.release()


class _Slot:
    """``with gate.slot(client):`` (see :class:`EngineGate`)."""

    def __init__(self, gate: EngineGate, client: str | None) -> None:
        self._gate = gate
        self._client = client

    def __enter__(self) -> None:
        self._gate.acquire(self._client)

    def __exit__(self, *exc: object) -> None:
        self._gate.release()
