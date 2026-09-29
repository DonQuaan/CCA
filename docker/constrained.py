"""Run the Render Blueprint's command under the free plan's limits and play a short game.

Usage (from the repository root)::

    python docker/constrained.py IMAGE [--blueprint deploy/render/render.yaml] [--cpus 0.1]
        [--memory 488m] [--port N] [--start-timeout 600] [--slack 10]

Render's free plan gives a web service 0.1 CPU and 512 MB (https://render.com/docs/free). This
starts IMAGE with those limits (``--cpus 0.1 --memory 488m --memory-swap 488m``: no swap either;
488 MiB is 511,705,088 bytes, under 512 MB whether Render counts a MB as 10**6 or 2**20 bytes,
which its docs do not say) and, after the image's ENTRYPOINT, the words of the Blueprint's
``dockerCommand`` as deploy/render/blueprint.py reads them. Its port is
published on 127.0.0.1 only, and the image's HEALTHCHECK is off (Render's documentation never
mentions it, and it would spend the same 0.1 CPU). Once ``/healthz`` reports the engine ready,
the script plays a short game through the HTTP API as the page does: a new game with the visitor
as White, three visitor moves (Nf3, Nc3, e3) and three CCA decisions (``POST .../think``). The
three moves are legal whatever Black answers: no Black piece can reach f3, c3 or e3, or give
check, in its first two moves. Meanwhile ``docker stats`` is sampled for memory and CPU, and
``/healthz`` is timed every second, as Render's health checks would ask it (they wait five
seconds; https://render.com/docs/health-checks). Afterwards the game is replayed with the
image's own python-chess, in a second container without network, and every position is compared
with the server's.

The run fails if the container is OOM-killed or stops, if a decision takes longer than the
command's ``--max-think-seconds`` plus the slack (10 s), if a move is illegal or a position
differs from the replay, or if the server refuses a request of the game. A summary table goes to
stdout and, on GitHub Actions, to the job summary. Nothing is pushed; the container is removed
with its anonymous volumes. Needs only Python 3.11+, the docker CLI (smoke.py's runner) and
deploy/render/blueprint.py.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, TextIO

from smoke import VENV_PYTHON, Result, SmokeError, gh_escape, run_docker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    Runner = Callable[[Sequence[str], str | None, float], Result]

BLUEPRINT = Path(__file__).resolve().parents[1] / "deploy" / "render" / "render.yaml"
HUMAN_MOVES = ("g1f3", "b1c3", "e2e3")
SEED = "render-limits"  # a fixed game seed: the same decisions on every run of one image
UCI = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")
CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
MEMORY = re.compile(r"[1-9][0-9]*[bkmg]?")
# Render's "512 MB" (https://render.com/docs/free) in the stricter reading: 488 MiB is
# 511,705,088 bytes, under 512 * 10**6 as well as 512 * 2**20 (the docs do not say which).
MEMORY_LIMIT = "488m"
SIZE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\s*", re.IGNORECASE)
UNITS = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
}
MIB = 2**20
# cgroup v2, then v1: the largest memory use the kernel saw, which sampling can miss.
PEAK_FILES = (
    "/sys/fs/cgroup/memory.peak",
    "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
)
# cgroup v2 event counters: "oom_kill N" counts processes the kernel killed for the limit, also
# a child such as Stockfish while the server itself keeps running (State.OOMKilled may not).
EVENTS_FILE = "/sys/fs/cgroup/memory.events"
RENDER_HEALTH_WAIT_S = 5.0  # "responds ... within five seconds" (render.com/docs/health-checks)

# Runs inside the image with its virtual environment's interpreter; prints one JSON object.
REPLAY = """
import json, sys
import chess

cfg = json.loads(sys.argv[1])
board = chess.Board(cfg["start_fen"])
fens, illegal = [], None
for i, uci in enumerate(cfg["moves"]):
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        move = None
    if move is None or not board.is_legal(move):
        illegal = i
        break
    board.push(move)
    fens.append(board.fen())
print(json.dumps({"fens": fens, "illegal": illegal}))
"""


class ConstrainedError(Exception):
    """The constrained run cannot go on; the message says why."""


# ---------------------------------------------------------------------------- the Blueprint
def _load_blueprint_reader() -> ModuleType:
    """deploy/render/blueprint.py: the Blueprint reader the deploy uses too, one for both."""
    path = BLUEPRINT.parent / "blueprint.py"
    spec = importlib.util.spec_from_file_location("cca_render_blueprint", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in the repository
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_reader = _load_blueprint_reader()


def blueprint_command(text: str) -> list[str]:
    """The words of ``dockerCommand`` in a Blueprint that has exactly one.

    deploy/render/blueprint.py reads it, without a YAML library: a plain one-line value, or a
    folded block (``>`` or ``>-``) of equally indented lines. docker/tests checks the result
    against PyYAML.

    Raises:
        ConstrainedError: no or several ``dockerCommand`` keys, another form, or a word with a
            character outside plain words (no quotes, ``$``, ``#``, globs or separators).
    """
    try:
        words: list[str] = _reader.command_words(text)
    except _reader.BlueprintError as exc:
        raise ConstrainedError(str(exc)) from None
    return words


def option(words: Sequence[str], name: str) -> str | None:
    """The value after the last ``name`` in ``words`` (argparse keeps the last one), or None."""
    value = None
    for i, word in enumerate(words[:-1]):
        if word == name:
            value = words[i + 1]
    return value


def command_limits(words: Sequence[str]) -> tuple[int, float]:
    """The container port (``--port``) and the decision cap (``--max-think-seconds``).

    Raises:
        ConstrainedError: the command is not ``play`` or lacks either value.
    """
    if not words or words[0] != "play":
        raise ConstrainedError("the Blueprint's command must start with play")
    port, cap = option(words, "--port"), option(words, "--max-think-seconds")
    if port is None or not port.isdigit() or not 0 < int(port) < 2**16:
        raise ConstrainedError("the Blueprint's command needs --port with a TCP port")
    try:
        seconds = float(cap) if cap is not None else -1.0
    except ValueError:
        seconds = -1.0
    if not seconds > 0:
        raise ConstrainedError("the Blueprint's command needs --max-think-seconds (seconds > 0)")
    return int(port), seconds


# ---------------------------------------------------------------------------- measurements
def parse_size(text: str) -> int:
    """Bytes of a ``docker stats`` size such as ``12.5MiB``, ``980kB`` or ``0B``.

    Raises:
        ValueError: not a size.
    """
    match = SIZE.fullmatch(text)
    if match is None:
        raise ValueError(f"not a size: {text!r:.40}")
    return round(float(match[1]) * UNITS[match[2].upper()])


def parse_stats(line: str) -> tuple[int, float]:
    """Memory used (bytes) and CPU (percent of one CPU) from ``docker stats --format '{{json .}}'``.

    Raises:
        ValueError: the line is not JSON, or a field is not a size or a number.
        TypeError: it is not a JSON object.
        KeyError: a field is missing.
    """
    data = json.loads(line)
    if not isinstance(data, dict):
        raise TypeError("docker stats printed no JSON object")
    used = parse_size(str(data["MemUsage"]).split("/", 1)[0])
    cpu = float(str(data["CPUPerc"]).strip().removesuffix("%"))
    return used, cpu


@dataclass
class Samples:
    """What the background threads measured (read only after they stopped)."""

    peak_bytes: int = 0
    max_cpu: float = 0.0
    stats_ok: int = 0
    stats_failed: int = 0
    health_max_s: float = 0.0
    health_slow: int = 0
    """Probes slower than Render's five seconds, or unanswered."""
    health_count: int = 0
    health_statuses: set[int] = field(default_factory=set)


class _Loop(threading.Thread):
    """A daemon thread that calls ``tick`` every ``interval`` seconds until stopped."""

    def __init__(self, tick: Callable[[], None], interval: float) -> None:
        super().__init__(daemon=True)
        self._tick, self._interval = tick, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self._interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=60)


# ---------------------------------------------------------------------------- HTTP
@dataclass(frozen=True)
class Answer:
    """One API answer: status, JSON object ({} if none) and the wall time it took."""

    status: int
    data: dict[str, Any]
    seconds: float


# The container is on this host's loopback: never through an http_proxy of the environment.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(
    port: int, method: str, path: str, body: dict[str, Any] | None, timeout: float
) -> Answer:
    """``METHOD http://127.0.0.1:PORT/PATH`` with a JSON body; error statuses are returned.

    Raises:
        OSError: no answer (refused, reset, timed out).
    """
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method
    )
    start = time.monotonic()
    try:
        with _DIRECT.open(request, timeout=timeout) as resp:
            status, raw = int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except http.client.HTTPException as exc:  # a broken answer (not an OSError)
        raise ConnectionError(f"broken HTTP answer: {type(exc).__name__}") from exc
    seconds = time.monotonic() - start
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return Answer(status, parsed if isinstance(parsed, dict) else {}, seconds)


# ---------------------------------------------------------------------------- the run
@dataclass(frozen=True)
class Options:
    """What to run and the limits to run it under."""

    image: str
    blueprint: Path = BLUEPRINT
    cpus: str = "0.1"
    memory: str = MEMORY_LIMIT
    port: int | None = None  # host port on 127.0.0.1; None: Docker picks a free one
    start_timeout: float = 600.0
    slack: float = 10.0


@dataclass(frozen=True)
class Row:
    """One line of the game table."""

    step: str
    request: str
    status: int
    seconds: float
    move: str = ""
    compute_s: float | None = None


def run_args(opts: Options, words: Sequence[str], name: str, port: int) -> list[str]:
    """``docker run`` arguments: Render's free-plan limits, the port on loopback, no push."""
    host_port = "" if opts.port is None else str(opts.port)
    return [
        "run", "-d", "--name", name,
        "--cpus", opts.cpus, "--memory", opts.memory, "--memory-swap", opts.memory,
        "--no-healthcheck", "-p", f"127.0.0.1:{host_port}:{port}",
        opts.image, *words,
    ]  # fmt: skip


class Constrained:
    """One constrained run of the Blueprint's command against one image."""

    def __init__(
        self,
        opts: Options,
        runner: Runner = run_docker,
        *,
        poll: float = 1.0,
        sample_every: float = 1.0,
        health_every: float = 1.0,
        out: TextIO = sys.stdout,
    ) -> None:
        self.opts = opts
        self._runner = runner
        self._poll, self._sample_every, self._health_every = poll, sample_every, health_every
        self._out = out
        self.name = f"cca-render-limits-{secrets.token_hex(4)}"
        self.samples = Samples()
        self.rows: list[Row] = []
        self.notes: list[str] = []
        self.ready_s: float | None = None
        self.state: dict[str, Any] = {}
        self.cgroup_peak: int | None = None
        self.oom_kills: int | None = None
        self.replayed = 0
        self.max_think = 0.0
        self.last_state: dict[str, Any] = {}
        self._game_id = ""
        self._started = 0.0

    # ------------------------------------------------------------------ docker
    def _docker(self, args: Sequence[str], timeout: float = 120.0) -> Result:
        """A docker CLI call; a missing CLI or a timeout becomes a failed result."""
        try:
            return self._runner(args, None, timeout)
        except SmokeError as exc:
            return Result(1, "", str(exc))

    def _published_port(self, port: int) -> int:
        res = self._docker(["port", self.name, f"{port}/tcp"], timeout=30)
        lines = res.stdout.split()
        host, _, number = lines[0].rpartition(":") if lines else ("", "", "")
        if res.returncode != 0 or host != "127.0.0.1" or not number.isdigit():
            raise ConstrainedError(f"{port}/tcp is not published on 127.0.0.1: {res.tail()}")
        return int(number)

    def _container_state(self) -> dict[str, Any]:
        res = self._docker(["inspect", "--format", "{{json .State}}", self.name], timeout=30)
        try:
            state = json.loads(res.stdout) if res.returncode == 0 else None
        except ValueError:
            state = None
        return state if isinstance(state, dict) else {}

    def _stopped(self, state: dict[str, Any]) -> str | None:
        if not state:
            return "docker inspect shows no state for the container"
        if state.get("OOMKilled") is True:
            return f"the container was OOM-killed (limit {self.opts.memory})"
        if state.get("Status") != "running":
            return f"the container is {state.get('Status')} (exit code {state.get('ExitCode')})"
        return None

    def _sample_stats(self) -> None:
        res = self._docker(
            ["stats", "--no-stream", "--format", "{{json .}}", self.name], timeout=30
        )
        try:
            used, cpu = parse_stats(res.stdout.strip().splitlines()[-1])
        except (IndexError, KeyError, TypeError, ValueError):
            self.samples.stats_failed += 1
            return
        self.samples.stats_ok += 1
        self.samples.peak_bytes = max(self.samples.peak_bytes, used)
        self.samples.max_cpu = max(self.samples.max_cpu, cpu)

    def _read_cgroup(self) -> None:
        """The kernel's own peak memory and OOM-kill count, when the cgroup files exist."""
        for path in PEAK_FILES:
            res = self._docker(["exec", self.name, "cat", path], timeout=30)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                self.cgroup_peak = int(res.stdout.strip())
                break
        res = self._docker(["exec", self.name, "cat", EVENTS_FILE], timeout=30)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill" and value.strip().isdigit():
                    self.oom_kills = int(value.strip())

    # ------------------------------------------------------------------ HTTP
    def _time_health(self, port: int) -> None:
        try:
            answer = http_json(port, "GET", "/healthz", None, 10.0)
        except OSError:
            self.samples.health_slow += 1
            self.samples.health_count += 1
            self.samples.health_statuses.add(0)
            return
        self.samples.health_count += 1
        self.samples.health_statuses.add(answer.status)
        self.samples.health_max_s = max(self.samples.health_max_s, answer.seconds)
        if answer.seconds > RENDER_HEALTH_WAIT_S or answer.status != 200:
            self.samples.health_slow += 1

    def _wait_ready(self, port: int) -> float:
        """Seconds until ``/healthz`` said ready (the container was started just before)."""
        start = time.monotonic()
        said = "no answer"
        while True:
            stopped = self._stopped(self._container_state())
            if stopped:
                raise ConstrainedError(f"{stopped} before the engine was ready")
            try:
                answer = http_json(port, "GET", "/healthz", None, 10.0)
            except OSError as exc:
                said = f"no answer ({type(exc).__name__})"
            else:
                said = f"HTTP {answer.status} {json.dumps(answer.data)[:200]}"
                if answer.status == 503:
                    raise ConstrainedError(f"/healthz reports a failed engine: {said}")
                if answer.status == 200 and answer.data.get("ready") is True:
                    return time.monotonic() - self._started
            if time.monotonic() - start >= self.opts.start_timeout:
                raise ConstrainedError(
                    f"the engine was not ready within {self.opts.start_timeout:g}s (last: {said})"
                )
            time.sleep(self._poll)

    def _call(
        self, port: int, step: str, path: str, *, body: dict[str, Any], timeout: float
    ) -> Answer:
        """``POST PATH`` for one step of the game; anything but the expected status stops it."""
        method = "POST"
        label = path.replace(self._game_id, ":id") if self._game_id else path
        try:
            answer = http_json(port, method, path, body, timeout)
        except OSError as exc:
            raise ConstrainedError(f"{step}: {method} {label} got no answer ({exc})") from None
        want = 201 if path == "/api/games" else 200
        if answer.status != want:
            error = answer.data.get("error", "")
            raise ConstrainedError(
                f"{step}: {method} {label} answered HTTP {answer.status} {error!r:.200}"
            )
        return answer

    def _row(self, step: str, request: str, answer: Answer, move: str = "") -> None:
        compute = answer.data.get("compute_s")
        self.rows.append(
            Row(
                step,
                request,
                answer.status,
                answer.seconds,
                move,
                float(compute) if isinstance(compute, int | float) else None,
            )
        )

    def _play(self, port: int) -> dict[str, Any]:
        """Create a game, then three visitor moves each answered by a CCA decision."""
        created = self._call(
            port,
            "new game",
            "/api/games",
            body={"human_color": "white", "seed": SEED},
            timeout=60.0 + self.max_think,
        )
        game_id = created.data.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            raise ConstrainedError("new game: the answer has no game_id")
        self._game_id = game_id
        self._row("new game", "POST /api/games", created)
        state: dict[str, Any] = created.data.get("state") or {}
        self.last_state = state
        decision_timeout = self.max_think + self.opts.slack + 30.0
        for n, uci in enumerate(HUMAN_MOVES, 1):
            step = f"visitor move {n}"
            moved = self._call(
                port, step, f"/api/games/{game_id}/move", body={"uci": uci}, timeout=60.0
            )
            state = self.last_state = moved.data.get("state") or {}
            if _last_move(state) != uci:
                raise ConstrainedError(f"{step}: the game does not end with {uci}")
            self._row(step, "POST /api/games/:id/move", moved, uci)
            step = f"CCA decision {n}"
            thought = self._call(
                port, step, f"/api/games/{game_id}/think", body={}, timeout=decision_timeout
            )
            move = thought.data.get("move")
            reply = move.get("uci") if isinstance(move, dict) else None
            state = self.last_state = thought.data.get("state") or {}
            if not isinstance(reply, str) or not UCI.fullmatch(reply):
                raise ConstrainedError(f"{step}: no move in the answer ({move!r:.80})")
            if _last_move(state) != reply or state.get("to_move") != "human":
                raise ConstrainedError(
                    f"{step}: the game does not end with {reply}, visitor to move"
                )
            self._row(step, "POST /api/games/:id/think", thought, reply)
        return state

    def _replay(self, state: dict[str, Any]) -> list[str]:
        """Replay the game with the image's python-chess; the problems found."""
        moves = state.get("moves")
        if not isinstance(moves, list) or not all(isinstance(m, dict) for m in moves):
            return ["the final state has no move list"]
        ucis = [str(m.get("uci")) for m in moves]
        expected = [row.move for row in self.rows if row.move]
        problems = []
        if ucis != expected:
            problems.append(f"the server's move list {ucis} is not the game played {expected}")
        cfg = json.dumps({"start_fen": state.get("start_fen"), "moves": ucis})
        res = self._docker(
            [
                "run", "--rm", "--network", "none", "--no-healthcheck",
                "--entrypoint", VENV_PYTHON, self.opts.image, "-c", REPLAY, cfg,
            ],
            timeout=180,
        )  # fmt: skip
        try:
            report = json.loads(res.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            report = None
        illegal = report.get("illegal") if isinstance(report, dict) else None
        fens = report.get("fens") if isinstance(report, dict) else None
        if not isinstance(fens, list) or not (
            illegal is None or (isinstance(illegal, int) and 0 <= illegal < len(ucis))
        ):
            return [*problems, f"the replay printed no report: {res.tail()}"]
        if illegal is not None:
            problems.append(f"move {illegal + 1} ({ucis[illegal]}) is illegal")
        for i, (theirs, ours) in enumerate(zip([m.get("fen") for m in moves], fens, strict=False)):
            if theirs != ours:
                problems.append(f"after move {i + 1} the server shows {theirs!r}, not {ours!r}")
                break
        self.replayed = len(fens) if illegal is None else illegal
        return problems

    # ------------------------------------------------------------------ the whole run
    def run(self) -> int:
        """Run, report, and return the exit status (0: every check passed)."""
        try:
            text = self.opts.blueprint.read_text(encoding="utf-8")
            words = blueprint_command(text)
            port, self.max_think = command_limits(words)
        except (OSError, ConstrainedError) as exc:
            return self._finish([f"the Blueprint {self.opts.blueprint}: {exc}"])
        self._started = time.monotonic()
        started = self._docker(run_args(self.opts, words, self.name, port))
        if started.returncode != 0:
            self._docker(["rm", "-f", "-v", self.name], timeout=60)  # in case it was created
            return self._finish([f"docker run failed: {started.tail()}"])
        stats = _Loop(self._sample_stats, self._sample_every)
        health: _Loop | None = None
        problems: list[str] = []
        try:
            stats.start()
            host_port = self._published_port(port)
            self.ready_s = self._wait_ready(host_port)
            health = _Loop(lambda: self._time_health(host_port), self._health_every)
            health.start()
            self._play(host_port)
        except ConstrainedError as exc:
            problems.append(str(exc))
        finally:
            for loop in (health, stats):
                if loop is not None:
                    loop.stop()
            self.state = self._container_state()
            if self.state.get("Status") == "running":
                self._read_cgroup()
            if problems or self._stopped(self.state):
                logs = self._docker(["logs", "--tail", "40", self.name], timeout=30)
                self.notes.append(f"container logs (last 40 lines):\n{logs.stdout}{logs.stderr}")
            self._docker(["rm", "-f", "-v", self.name], timeout=60)
        stopped = self._stopped(self.state)
        if stopped:
            problems.insert(0, stopped)
        if self.oom_kills:
            problems.insert(0, f"the kernel OOM-killed {self.oom_kills} process(es) in it")
        limit = self.max_think + self.opts.slack
        problems.extend(
            f"{row.step} took {row.seconds:.1f}s, more than --max-think-seconds "
            f"{self.max_think:g} + {self.opts.slack:g}s"
            for row in self.rows
            if row.step.startswith("CCA decision") and row.seconds > limit
        )
        if self.last_state:  # also after another failure: an illegal move explains a lot
            problems += self._replay(self.last_state)
        return self._finish(problems)

    # ------------------------------------------------------------------ report
    def summary(self, problems: Sequence[str]) -> str:
        """The Markdown summary: the game table, then the measurements."""
        o = self.opts
        lines = [
            f"### Render free-plan limits: {o.image} (--cpus {o.cpus}, --memory {o.memory})",
            "",
            "| Step | Request | HTTP | Seconds | Move | Engine compute (s) |",
            "|---|---|---|---|---|---|",
        ]
        if self.ready_s is not None:
            lines.append(f"| engine ready | GET /healthz | 200 | {self.ready_s:.1f} | | |")
        for r in self.rows:
            compute = "" if r.compute_s is None else f"{r.compute_s:.2f}"
            lines.append(
                f"| {r.step} | {r.request} | {r.status} | {r.seconds:.2f} | {r.move} | {compute} |"
            )
        s = self.samples
        decisions = [r.seconds for r in self.rows if r.step.startswith("CCA decision")]
        peak = max(s.peak_bytes, self.cgroup_peak or 0)
        cgroup = "n/a" if self.cgroup_peak is None else f"{self.cgroup_peak / MIB:.1f} MiB"
        oom = self.state.get("OOMKilled")
        killed = "yes" if oom else "no" if oom is False else "unknown"
        kills = "n/a" if self.oom_kills is None else str(self.oom_kills)
        lines += [
            "",
            "| Measure | Value | Limit |",
            "|---|---|---|",
            f"| Peak memory (max of the two below) | {peak / MIB:.1f} MiB | {o.memory} |",
            f"| - docker stats, {s.stats_ok} samples ({s.stats_failed} failed) | "
            f"{s.peak_bytes / MIB:.1f} MiB | |",
            f"| - cgroup peak (memory.peak) | {cgroup} | |",
            f"| CPU, highest sample (% of one CPU) | {s.max_cpu:.1f}% | {o.cpus} CPU |",
            f"| Slowest decision | {max(decisions, default=0.0):.1f} s | "
            f"{self.max_think + o.slack:g} s (--max-think-seconds {self.max_think:g} + "
            f"{o.slack:g}) |",
            f"| /healthz during the game: slowest of {s.health_count} | {s.health_max_s:.2f} s "
            f"({s.health_slow} over {RENDER_HEALTH_WAIT_S:g} s or failed) | "
            f"Render's check waits {RENDER_HEALTH_WAIT_S:g} s |",
            f"| Container OOM-killed (State.OOMKilled) | {killed} | |",
            f"| Processes OOM-killed (memory.events oom_kill) | {kills} | |",
            f"| Moves replayed legal with python-chess | {self.replayed} | |",
            "",
            f"**{'FAIL' if problems else 'PASS'}**" + "".join(f"\n- {p}" for p in problems),
        ]
        return "\n".join(lines) + "\n"

    def _finish(self, problems: Sequence[str]) -> int:
        text = self.summary(problems)
        print(text, file=self._out)
        for note in self.notes:
            print(note, file=self._out)
        annotate = os.environ.get("GITHUB_ACTIONS") == "true"
        if annotate:
            for problem in problems:
                print(f"::error title=render-limits::{gh_escape(problem)}", file=self._out)
            if self.samples.health_slow:
                warning = (
                    f"/healthz was slower than {RENDER_HEALTH_WAIT_S:g} s or failed "
                    f"{self.samples.health_slow} times during the game: Render stops routing to "
                    "an instance after 15 s of failed checks and restarts it after 60 s"
                )
                print(f"::warning title=render-limits::{gh_escape(warning)}", file=self._out)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(text)
        self._out.flush()
        return 1 if problems else 0


def _last_move(state: dict[str, Any]) -> str | None:
    moves = state.get("moves")
    if isinstance(moves, list) and moves and isinstance(moves[-1], dict):
        uci = moves[-1].get("uci")
        return uci if isinstance(uci, str) else None
    return None


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """Command-line options."""
    ap = argparse.ArgumentParser(
        prog="constrained.py",
        description="Run the Render Blueprint's command under the free plan's limits.",
    )
    ap.add_argument("image", help="image reference, e.g. cca:ci")
    ap.add_argument("--blueprint", type=Path, default=BLUEPRINT, help="render.yaml to read")
    ap.add_argument("--cpus", default="0.1", help="docker run --cpus (default 0.1)")
    ap.add_argument(
        "--memory", default=MEMORY_LIMIT, help=f"docker run --memory (default {MEMORY_LIMIT})"
    )
    ap.add_argument("--port", type=int, help="host port on 127.0.0.1 (default: Docker picks)")
    ap.add_argument(
        "--start-timeout", type=float, default=600.0, help="seconds until ready (default 600)"
    )
    ap.add_argument(
        "--slack",
        type=float,
        default=10.0,
        help="seconds a decision may take beyond --max-think-seconds (default 10)",
    )
    ns = ap.parse_args(argv)
    if not CPUS.fullmatch(ns.cpus) or not float(ns.cpus) > 0:
        ap.error(f"--cpus {ns.cpus!r} is not a positive number")
    if not MEMORY.fullmatch(ns.memory):
        ap.error(f"--memory {ns.memory!r} is not a docker size such as {MEMORY_LIMIT}")
    if ns.port is not None and not 0 < ns.port < 2**16:
        ap.error(f"--port {ns.port} is not a TCP port (1-65535)")
    if not ns.start_timeout > 0 or ns.slack < 0:
        ap.error("--start-timeout must be positive and --slack not negative")
    return Options(
        image=ns.image,
        blueprint=ns.blueprint,
        cpus=ns.cpus,
        memory=ns.memory,
        port=ns.port,
        start_timeout=ns.start_timeout,
        slack=ns.slack,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    return Constrained(parse_args(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
