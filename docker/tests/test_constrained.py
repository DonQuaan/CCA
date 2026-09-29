"""docker/constrained.py: the Render free-plan run, without Docker.

The docker CLI is a scripted fake; the container's web server is a local HTTP server on
127.0.0.1 that plays the part of ``cca play`` (python-chess keeps its board); the replay runs the
script the image would run, with this interpreter's python-chess.
"""

from __future__ import annotations

import ast
import http.server
import importlib
import io
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import chess
import constrained
import pytest
from smoke import VENV_PYTHON, Result

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# PyYAML comes with the dev tools (pre-commit depends on it); it ships no type stubs.
yaml = importlib.import_module("yaml")

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT = ROOT / "deploy" / "render" / "render.yaml"
IMAGE_WORKFLOW = ROOT / ".github" / "workflows" / "image.yml"
GAME_ID = "g" * 32
MIB = 2**20


# ---------------------------------------------------------------------------- the Blueprint
def pyyaml_words(text: str) -> list[str]:
    data = yaml.safe_load(text)
    command = data["services"][0]["dockerCommand"]
    assert isinstance(command, str)
    return command.split()


@pytest.fixture(autouse=True)
def _off_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that wants GitHub's annotations sets them; a CI run's own summary stays clean."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def test_the_driver_reads_the_blueprint_with_the_deploys_reader() -> None:
    reader = constrained._reader  # the module it loaded
    assert reader.__file__ is not None
    assert Path(reader.__file__) == ROOT / "deploy" / "render" / "blueprint.py"


def test_the_real_blueprint_reads_as_pyyaml_reads_it() -> None:
    text = BLUEPRINT.read_text(encoding="utf-8")
    words = constrained.blueprint_command(text)
    assert words == pyyaml_words(text)
    assert words[0] == "play"
    port, cap = constrained.command_limits(words)
    assert (port, cap) == (8765, 20.0)


def blueprint(command_yaml: str) -> str:
    return (
        "services:\n  - type: web\n    name: x\n    runtime: image\n"
        "    image:\n      url: ghcr.io/donquaan/cca:0.1.0\n"
        f"{command_yaml}"
        "    plan: free\n"
    )


@pytest.mark.parametrize(
    "command_yaml",
    [
        "    dockerCommand: play --port 8765 --max-think-seconds 20\n",
        "    dockerCommand: >-\n      play --port 8765\n      --max-think-seconds 20\n",
        "    dockerCommand: >\n      play --port 8765\n      --max-think-seconds 20\n",
        "    dockerCommand: >-\n      play --port 8765\n      --max-think-seconds 20\n\n",
        "    dockerCommand:   >-  \n      play   --port 8765\n      --max-think-seconds 20\n",
    ],
)
def test_accepted_forms_give_the_words_pyyaml_gives(command_yaml: str) -> None:
    text = blueprint(command_yaml)
    assert constrained.blueprint_command(text) == pyyaml_words(text)


@pytest.mark.parametrize(
    ("command_yaml", "problem"),
    [
        ("", "one dockerCommand, found 0"),
        ("    dockerCommand: play\n    dockerCommand: play\n", "found 2"),
        ('    dockerCommand: "play --port 8765"\n', "plain words"),
        ("    dockerCommand: 'play'\n", "plain words"),
        ("    dockerCommand: |\n      play\n", "plain words"),
        ("    dockerCommand: play # comment\n", "plain words"),
        ("    dockerCommand:\n", "plain words"),
        ("    dockerCommand: >-\n", "non-empty lines"),
        ("    dockerCommand: >-\n      play\n\n      --port 8765\n", "non-empty lines"),
        ("    dockerCommand: >-\n      play\n        --port 8765\n", "one indentation"),
        ("    dockerCommand: >-\n      play\n      # a comment\n", "must match"),
        ("    dockerCommand: play --port $PORT\n", "must match"),
        ("    dockerCommand: play --host=a;b\n", "must match"),
        ("    dockerCommand: play && sh\n", "must match"),
    ],
)
def test_other_forms_are_refused(command_yaml: str, problem: str) -> None:
    with pytest.raises(constrained.ConstrainedError, match=problem):
        constrained.blueprint_command(blueprint(command_yaml))


def test_option_keeps_the_last_value_as_argparse_does() -> None:
    words = ["play", "--port", "1", "--port", "8765", "--public"]
    assert constrained.option(words, "--port") == "8765"
    assert constrained.option(words, "--public") is None  # a flag, no value after it
    assert constrained.option(words, "--hash") is None


@pytest.mark.parametrize(
    ("words", "problem"),
    [
        ([], "start with play"),
        (["uci"], "start with play"),
        (["play", "--max-think-seconds", "20"], "needs --port"),
        (["play", "--port", "0", "--max-think-seconds", "20"], "needs --port"),
        (["play", "--port", "70000", "--max-think-seconds", "20"], "needs --port"),
        (["play", "--port", "8765"], "needs --max-think-seconds"),
        (["play", "--port", "8765", "--max-think-seconds", "0"], "needs --max-think-seconds"),
        (["play", "--port", "8765", "--max-think-seconds", "x"], "needs --max-think-seconds"),
    ],
)
def test_the_command_needs_play_a_port_and_a_decision_cap(words: list[str], problem: str) -> None:
    with pytest.raises(constrained.ConstrainedError, match=problem):
        constrained.command_limits(words)


# ---------------------------------------------------------------------------- measurements
@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("0B", 0),
        ("512MiB", 512 * MIB),
        ("12.5MiB", round(12.5 * MIB)),
        ("1.5GiB", round(1.5 * 2**30)),
        ("980kB", 980_000),
        ("3KiB", 3072),
        (" 7.1 MB ", 7_100_000),
    ],
)
def test_docker_stats_sizes(text: str, size: int) -> None:
    assert constrained.parse_size(text) == size


@pytest.mark.parametrize("text", ["", "12", "MiB", "12 parsecs", "-1MiB"])
def test_other_sizes_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a size"):
        constrained.parse_size(text)


def stats_line(used: str = "100MiB", cpu: str = "9.87%") -> str:
    return json.dumps({"MemUsage": f"{used} / 512MiB", "CPUPerc": cpu, "Name": "x"})


def test_docker_stats_lines() -> None:
    assert constrained.parse_stats(stats_line()) == (100 * MIB, 9.87)
    with pytest.raises(TypeError):
        constrained.parse_stats("[1]")
    with pytest.raises(KeyError):
        constrained.parse_stats("{}")
    with pytest.raises(ValueError, match=r"Expecting value|not a size"):
        constrained.parse_stats("n/a")


def test_run_args_apply_the_free_plan_limits_on_loopback() -> None:
    opts = constrained.Options(image="cca:ci")
    args = constrained.run_args(opts, ["play", "--port", "8765"], "cca-x", 8765)
    assert args == [
        "run", "-d", "--name", "cca-x",
        "--cpus", "0.1", "--memory", "488m", "--memory-swap", "488m",
        "--no-healthcheck", "-p", "127.0.0.1::8765",
        "cca:ci", "play", "--port", "8765",
    ]  # fmt: skip
    fixed = constrained.run_args(constrained.Options(image="i", port=18765), ["play"], "n", 8765)
    assert "127.0.0.1:18765:8765" in fixed


# ---------------------------------------------------------------------------- fakes
class PlayServer:
    """The container's ``cca play``: /healthz and the game API, with scripted behaviour."""

    def __init__(self) -> None:
        self.board = chess.Board()
        self.moves: list[dict[str, str]] = []
        self.replies = ["e7e5", "b8c6", "g8f6"]
        self.not_ready = 2  # /healthz answers ready false this many times first
        self.health_status = 200
        self.think_delay = 0.0
        self.move_status = 200
        self.wrong_fen_at: int | None = None
        self.drop_moves = False  # a visitor move answered 200 but not played
        self.to_move: str | None = None  # a to_move that contradicts the board
        self.repeat_first = False  # the move list shows its first move twice
        self.health_after_ready: int | None = None  # /healthz's status once it said ready
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self._lock = threading.Lock()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                server.handle(self, "GET")

            def do_POST(self) -> None:
                server.handle(self, "POST")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def state(self) -> dict[str, Any]:
        moves = [dict(m) for m in self.moves]
        if self.wrong_fen_at is not None and self.wrong_fen_at < len(moves):
            moves[self.wrong_fen_at]["fen"] = chess.STARTING_FEN
        if self.repeat_first and moves:
            moves.insert(0, moves[0])
        turn = "human" if self.board.turn == chess.WHITE else "cca"
        return {"start_fen": chess.STARTING_FEN, "moves": moves, "to_move": self.to_move or turn}

    def _push(self, move: chess.Move) -> None:
        san = self.board.san(move) if self.board.is_legal(move) else move.uci()
        self.board.push(move)  # as cca play pushes CCA's move: without a legality check
        self.moves.append({"uci": move.uci(), "san": san, "fen": self.board.fen()})

    def handle(self, h: http.server.BaseHTTPRequestHandler, method: str) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        self.requests.append((method, h.path, dict(h.headers), body))
        status, payload = self.route(method, h.path, body)
        raw = json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        routes = {
            ("GET", "/healthz"): self.healthz,
            ("POST", "/api/games"): self.new_game,
            ("POST", f"/api/games/{GAME_ID}/move"): self.move,
            ("POST", f"/api/games/{GAME_ID}/think"): self.think,
        }
        handler = routes.get((method, path))
        if handler is None:
            return 404, {"error": "not found"}
        return handler(json.loads(body) if body else {})

    def healthz(self, _: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            ready = self.not_ready <= 0
            said_ready = self.not_ready < 0  # an earlier answer said ready
            self.not_ready -= 1
        status = self.health_status
        if said_ready and self.health_after_ready is not None:
            status = self.health_after_ready
        payload = {"status": "ok" if status == 200 else "error"}
        return status, {**payload, "version": "0.1.0", "ready": ready}

    def new_game(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert data == {"human_color": "white", "seed": constrained.SEED}
        return 201, {"game_id": GAME_ID, "state": self.state()}

    def move(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.move_status != 200:
            return self.move_status, {"error": "illegal move"}
        move = chess.Move.from_uci(data["uci"])
        if not self.board.is_legal(move):
            return 400, {"error": f"illegal move {data['uci']}"}
        if not self.drop_moves:
            self._push(move)
        return 200, {"state": self.state()}

    def think(self, _: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        time.sleep(self.think_delay)
        reply = chess.Move.from_uci(self.replies.pop(0))
        san = self.board.san(reply) if self.board.is_legal(reply) else reply.uci()
        self._push(reply)
        return 200, {
            "move": {"uci": reply.uci(), "san": san},
            "compute_s": 0.25,
            "state": self.state(),
        }


class FakeDocker:
    """The docker CLI: canned answers per subcommand; the replay runs this interpreter."""

    def __init__(self, server: PlayServer) -> None:
        self.server = server
        self.calls: list[list[str]] = []
        self.state: dict[str, Any] = {"Status": "running", "OOMKilled": False, "ExitCode": 0}
        self.stats = [
            Result(0, stats_line("100MiB") + "\n", ""),
            Result(0, stats_line("80MiB"), ""),
        ]
        self.peak = Result(0, f"{150 * MIB}\n", "")
        self.events = Result(0, "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n", "")
        self.run_result = Result(0, "0123abcd\n", "")
        self.replay_override: Result | None = None
        self.port_line: str | None = None  # what `docker port` prints instead of loopback

    def __call__(self, args: Sequence[str], stdin: str | None, timeout: float) -> Result:
        args = list(args)
        self.calls.append(args)
        answers: dict[str, Callable[[], Result]] = {
            "run": lambda: self.run_result if "-d" in args else self.replay(args),
            "port": lambda: Result(0, self.port_line or f"127.0.0.1:{self.server.port}\n", ""),
            "inspect": lambda: Result(0, json.dumps(self.state), ""),
            "stats": lambda: self.stats.pop(0) if len(self.stats) > 1 else self.stats[0],
            "exec": lambda: self.peak if args[-1] in constrained.PEAK_FILES else self.events,
            "logs": lambda: Result(0, "cca play log line\n", ""),
            "rm": lambda: Result(0, "", ""),
        }
        assert args[0] in answers, f"unexpected docker call {args}"
        return answers[args[0]]()

    def replay(self, args: list[str]) -> Result:
        head = ["run", "--rm", "--network", "none", "--no-healthcheck", "--entrypoint"]
        assert args[:7] == [*head, VENV_PYTHON]
        assert args[8] == "-c"
        assert args[9] == constrained.REPLAY
        if self.replay_override is not None:
            return self.replay_override
        done = subprocess.run(
            [sys.executable, "-c", args[9], args[10]],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def subcommands(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def server() -> Iterator[PlayServer]:
    found = PlayServer()
    yield found
    found.close()


def write_blueprint(tmp_path: Path, cap: str = "20") -> Path:
    path = tmp_path / "render.yaml"
    path.write_text(
        blueprint(
            "    dockerCommand: >-\n"
            "      play --public --host 0.0.0.0 --port 8765 --no-browser --human qre\n"
            f"      --max-think-seconds {cap}\n"
        ),
        encoding="utf-8",
    )
    return path


def run(
    docker: FakeDocker,
    blueprint_path: Path = BLUEPRINT,
    *,
    slack: float = 10.0,
    start_timeout: float = 30.0,
) -> tuple[int, str, constrained.Constrained]:
    out = io.StringIO()
    opts = constrained.Options(
        image="cca:ci", blueprint=blueprint_path, slack=slack, start_timeout=start_timeout
    )
    job = constrained.Constrained(
        opts, docker, poll=0.01, sample_every=0.01, health_every=0.01, out=out
    )
    code = job.run()
    return code, out.getvalue(), job


# ---------------------------------------------------------------------------- whole runs
def test_a_good_run_plays_three_moves_replays_them_and_passes(
    server: PlayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    docker = FakeDocker(server)
    code, out, job = run(docker)
    assert code == 0, out
    assert "**PASS**" in out
    # The Blueprint's words follow the image, with the limits before it.
    started = docker.calls[0]
    image_at = started.index("cca:ci")
    assert started[image_at + 1 :] == constrained.blueprint_command(
        BLUEPRINT.read_text(encoding="utf-8")
    )
    assert started[:image_at] == constrained.run_args(job.opts, [], job.name, 8765)[:-1]
    # The limited container is removed first; the replay runs in a second one, without network.
    removed = docker.calls.index(["rm", "-f", "-v", job.name])
    assert docker.calls[removed + 1 :] == [docker.calls[-1]]
    assert docker.calls[-1][:4] == ["run", "--rm", "--network", "none"]
    subs = docker.subcommands()
    assert "stats" in subs
    assert subs.count("rm") == 1
    # The game: the visitor's three moves, each answered, as the table shows.
    assert [r.move for r in job.rows] == ["", "g1f3", "e7e5", "b1c3", "b8c6", "e2e3", "g8f6"]
    for step in ("new game", "visitor move 1", "CCA decision 3"):
        assert f"| {step} |" in out
    assert job.replayed == 6
    assert "| Moves replayed legal with python-chess | 6 |" in out
    # Memory: the larger of the sampled peak and the kernel's.
    assert "| Peak memory (max of the two below) | 150.0 MiB | 488m |" in out
    assert "| - docker stats," in out
    assert "100.0 MiB" in out
    assert "| Processes OOM-killed (memory.events oom_kill) | 0 | |" in out
    assert job.samples.health_count > 0
    assert job.ready_s is not None
    # JSON requests as the page sends them; no Origin header (a same-origin page sends none on
    # GET, and the API accepts a POST without one).
    posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 7
    for _, _, headers, _ in posts:
        assert headers["Content-Type"] == "application/json"
        assert "Origin" not in headers


def test_the_step_summary_and_annotations_on_github(
    server: PlayServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    server.think_delay = 0.3
    code, out, _ = run(FakeDocker(server), write_blueprint(tmp_path, cap="0.1"), slack=0.05)
    assert code == 1
    written = summary.read_text(encoding="utf-8")
    assert written.startswith("### Render free-plan limits: cca:ci (--cpus 0.1, --memory 488m)")
    assert "**FAIL**" in written
    errors = [line for line in out.splitlines() if line.startswith("::error title=render-limits::")]
    assert len(errors) == 3  # each of the three decisions was too slow
    assert all("more than --max-think-seconds 0.1 + 0.05s" in line for line in errors)


def test_a_slow_decision_fails(server: PlayServer, tmp_path: Path) -> None:
    server.think_delay = 0.3
    code, out, job = run(FakeDocker(server), write_blueprint(tmp_path, cap="0.1"), slack=0.05)
    assert code == 1
    assert "CCA decision 1 took" in out
    assert job.replayed == 6  # the game itself was fine


def test_a_decision_within_the_cap_and_slack_passes(server: PlayServer, tmp_path: Path) -> None:
    server.think_delay = 0.05
    code, out, _ = run(FakeDocker(server), write_blueprint(tmp_path, cap="0.1"), slack=0.5)
    assert code == 0, out


def test_an_illegal_cca_move_fails(server: PlayServer) -> None:
    server.replies = ["e7e5", "e5e3", "g8f6"]  # a pawn two squares forward from e5: illegal
    code, out, job = run(FakeDocker(server))
    assert code == 1
    assert "move 4 (e5e3) is illegal" in out
    assert job.replayed == 3


def test_a_position_that_differs_from_the_replay_fails(server: PlayServer) -> None:
    server.wrong_fen_at = 2
    code, out, _ = run(FakeDocker(server))
    assert code == 1
    assert "after move 3 the server shows" in out


def test_a_refused_visitor_move_stops_the_game(server: PlayServer) -> None:
    server.move_status = 400
    code, out, job = run(FakeDocker(server))
    assert code == 1
    assert "visitor move 1: POST /api/games/:id/move answered HTTP 400 'illegal move'" in out
    assert GAME_ID not in out  # the game id is a bearer secret: never printed
    assert "container logs (last 40 lines)" in out
    assert job.rows[-1].step == "new game"


def test_a_visitor_move_answered_but_not_played_stops_the_game(server: PlayServer) -> None:
    server.drop_moves = True
    code, out, job = run(FakeDocker(server))
    assert code == 1
    assert "visitor move 1: the game does not end with g1f3" in out
    assert job.rows[-1].step == "new game"


def test_a_decision_that_leaves_cca_to_move_stops_the_game(server: PlayServer) -> None:
    server.to_move = "cca"
    code, out, job = run(FakeDocker(server))
    assert code == 1
    assert "CCA decision 1: the game does not end with e7e5, visitor to move" in out
    assert job.rows[-1].step == "visitor move 1"


def test_a_move_list_that_is_not_the_game_played_fails(server: PlayServer) -> None:
    server.repeat_first = True  # every answer still ends with the move just played
    code, out, job = run(FakeDocker(server))
    assert code == 1
    assert "the server's move list ['g1f3', 'g1f3', 'e7e5'," in out
    assert "is not the game played ['g1f3', 'e7e5'," in out
    assert len(job.rows) == 7  # the whole game was played


def test_a_port_published_beyond_loopback_is_refused(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.port_line = f"0.0.0.0:{server.port}\n"
    code, out, job = run(docker)
    assert code == 1
    assert "8765/tcp is not published on 127.0.0.1" in out
    assert server.requests == []  # nothing was asked of the server
    assert job.rows == []
    assert docker.subcommands().count("rm") == 1


def test_a_failing_healthz_during_the_game_is_counted_and_warned(
    server: PlayServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    server.health_after_ready = 503
    server.think_delay = 0.05  # time for the health loop to ask during the game
    code, out, job = run(FakeDocker(server))
    assert code == 0, out  # a warning, as Render would only count failed checks
    assert 503 in job.samples.health_statuses
    assert job.samples.health_slow == job.samples.health_count > 0
    assert "::warning title=render-limits::/healthz was slower than 5 s or failed" in out


class Proxy:
    """An HTTP proxy that answers 502 to everything and counts what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                proxy.asked.append(self.path)
                self.send_error(502)

            def do_POST(self) -> None:
                self.do_GET()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_the_container_is_reached_directly_whatever_the_proxy_settings(
    server: PlayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = Proxy()
    try:
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        # urllib's default opener reads the proxy settings once: as in a fresh process.
        monkeypatch.setattr(urllib.request, "_opener", None)
        code, out, _ = run(FakeDocker(server), start_timeout=2.0)
    finally:
        proxy.close()
    assert code == 0, out
    assert proxy.asked == []


def test_an_oom_killed_container_fails(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.state = {"Status": "exited", "OOMKilled": True, "ExitCode": 137}
    code, out, _ = run(docker)
    assert code == 1
    assert "the container was OOM-killed (limit 488m)" in out
    assert "| Container OOM-killed (State.OOMKilled) | yes | |" in out
    assert "cca play log line" in out  # its logs are shown


def test_a_process_oom_killed_inside_a_running_container_fails(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.events = Result(0, "oom 1\noom_kill 1\n", "")
    code, out, _ = run(docker)
    assert code == 1
    assert "the kernel OOM-killed 1 process(es) in it" in out


def test_a_container_that_exits_at_start_fails_with_its_logs(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.state = {"Status": "exited", "OOMKilled": False, "ExitCode": 2}
    code, out, job = run(docker)
    assert code == 1
    assert "the container is exited (exit code 2) before the engine was ready" in out
    assert "cca play log line" in out
    assert job.rows == []
    assert "run" not in docker.subcommands()[1:]  # no replay without a game


def test_a_failed_engine_fails(server: PlayServer) -> None:
    server.health_status = 503
    code, out, _ = run(FakeDocker(server))
    assert code == 1
    assert "/healthz reports a failed engine: HTTP 503" in out


def test_an_engine_never_ready_fails_at_the_start_timeout(server: PlayServer) -> None:
    server.not_ready = 10**9
    code, out, _ = run(FakeDocker(server), start_timeout=0.2)
    assert code == 1
    assert "the engine was not ready within 0.2s" in out


def test_docker_run_failing_fails_and_cleans_up(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.run_result = Result(125, "", "docker: invalid --cpus")
    code, out, _ = run(docker)
    assert code == 1
    assert "docker run failed: docker: invalid --cpus" in out
    assert docker.subcommands() == ["run", "rm"]


def test_a_broken_blueprint_fails_before_docker(server: PlayServer, tmp_path: Path) -> None:
    path = tmp_path / "render.yaml"
    path.write_text(blueprint("    dockerCommand: play --port 8765\n"), encoding="utf-8")
    docker = FakeDocker(server)
    code, out, _ = run(docker, path)
    assert code == 1
    assert "needs --max-think-seconds" in out
    assert docker.calls == []


def test_unreadable_stats_are_counted_not_fatal(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.stats = [Result(1, "", "Error: No such container")]
    docker.peak = Result(1, "", "No such file")
    code, out, job = run(docker)
    assert code == 0, out
    assert job.samples.stats_ok == 0
    assert job.samples.stats_failed > 0
    assert "| - cgroup peak (memory.peak) | n/a | |" in out


def test_a_replay_without_a_report_fails(server: PlayServer) -> None:
    docker = FakeDocker(server)
    docker.replay_override = Result(1, "", "exec: /opt/cca/bin/python: not found")
    code, out, _ = run(docker)
    assert code == 1
    assert "the replay printed no report" in out


def test_the_replay_script_reports_legal_and_illegal_moves() -> None:
    def replay(moves: list[str]) -> dict[str, Any]:
        cfg = json.dumps({"start_fen": chess.STARTING_FEN, "moves": moves})
        done = subprocess.run(
            [sys.executable, "-c", constrained.REPLAY, cfg],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        report = json.loads(done.stdout)
        assert isinstance(report, dict)
        return report

    board = chess.Board()
    board.push_uci("g1f3")
    assert replay(["g1f3"]) == {"fens": [board.fen()], "illegal": None}
    assert replay(["g1f3", "e7e4"])["illegal"] == 1
    assert replay(["x"])["illegal"] == 0
    assert replay(["e1e2"])["illegal"] == 0  # a king move blocked by its own pawn


def test_the_visitor_moves_are_legal_whatever_black_answers() -> None:
    """The fixed moves Nf3, Nc3, e3 against every pair of Black answers."""
    board = chess.Board()
    board.push_uci(constrained.HUMAN_MOVES[0])
    for first in list(board.legal_moves):
        board.push(first)
        assert board.is_legal(chess.Move.from_uci(constrained.HUMAN_MOVES[1]))
        board.push_uci(constrained.HUMAN_MOVES[1])
        for second in list(board.legal_moves):
            board.push(second)
            assert board.is_legal(chess.Move.from_uci(constrained.HUMAN_MOVES[2])), board.fen()
            board.pop()
        board.pop()
        board.pop()


# ---------------------------------------------------------------------------- command line
@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["i", "--cpus", "0"], "not a positive number"),
        (["i", "--cpus", "abc"], "not a positive number"),
        (["i", "--memory", "lots"], "not a docker size"),
        (["i", "--port", "0"], "not a TCP port"),
        (["i", "--slack", "-1"], "--slack not negative"),
        (["i", "--start-timeout", "0"], "--start-timeout must be positive"),
    ],
)
def test_bad_options_are_refused(
    argv: list[str], problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        constrained.parse_args(argv)
    assert problem in capsys.readouterr().err


def test_default_options_are_the_free_plan() -> None:
    opts = constrained.parse_args(["cca:ci"])
    assert (opts.cpus, opts.memory, opts.blueprint, opts.slack) == ("0.1", "488m", BLUEPRINT, 10.0)
    # Render's "512 MB" in either unit: 488 MiB is under 512 * 10**6 bytes and 512 MiB.
    assert 488 * MIB < 512 * 10**6 < 512 * MIB


# ---------------------------------------------------------------------------- image.yml
def build_test_steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(IMAGE_WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["build-test"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_image_workflow_runs_the_constrained_game_on_the_default_image_only() -> None:
    steps = build_test_steps()
    runs = [str(s.get("run", "")) for s in steps]
    (index,) = [i for i, r in enumerate(runs) if "docker/constrained.py" in r]
    step = steps[index]
    assert step["if"] == "${{ matrix.variant == 'default' }}"
    assert " ".join(step["run"].split()) == (
        "python3 docker/constrained.py cca:ci --blueprint deploy/render/render.yaml "
        "--cpus 0.1 --memory 488m"
    )
    smoke_at = next(i for i, r in enumerate(runs) if "python3 docker/smoke.py cca:ci" in r)
    build_at = next(i for i, s in enumerate(steps) if "build-push-action" in str(s.get("uses")))
    assert build_at < smoke_at < index  # the image built above, already smoke-tested
    assert "env" not in step


def test_the_build_test_job_pushes_nothing() -> None:
    steps = build_test_steps()
    for step in steps:
        assert "login-action" not in str(step.get("uses", ""))
        assert "docker push" not in str(step.get("run", ""))
        if "build-push-action" in str(step.get("uses", "")):
            assert step["with"]["push"] is False


def test_the_driver_needs_only_the_standard_library_and_smoke() -> None:
    tree = ast.parse((ROOT / "docker" / "constrained.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    stdlib = set(sys.stdlib_module_names)
    assert {name.split(".")[0] for name in imported} - stdlib == {"smoke"}
