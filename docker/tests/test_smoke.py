"""Tests for docker/smoke.py, the container smoke-test runner, against a scripted docker CLI.

Each check is exercised once with a passing image and once per way it must fail, so removing a
guard from smoke.py turns at least one test red.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import smoke
from smoke import Options, Page, Result, Smoke, SmokeError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

IMAGE = "cca:test"
VERSION = "0.1.0"
REVISION = "0123456789abcdef0123456789abcdef01234567"
LABEL = "org.opencontainers.image."
DOCTOR_OK = "cca 0.1.0 | python 3.12.14\n  engine ok: Stockfish 19 (/usr/local/bin/stockfish)\n"
UCI_OK = "id name CCA 0.1.0\nid author DonQuaan\nuciok\nreadyok\n"
ANALYSE_OK = json.dumps({"move": "e2e4", "policy": {"e2e4": 0.6, "d2d4": 0.4}})
HEALTHY = {"Status": "running", "Health": {"Status": "healthy"}}
STARTING = {"Status": "running", "Health": {"Status": "starting"}}
HEALTHZ_OK = (200, b'{"status": "ok", "version": "0.1.0", "ready": true}')
HOST_PORT = 49153  # what the scripted `docker port` reports

# ------------------------------------------------------------------ a scripted web page
HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
CSS = "text/css; charset=utf-8"
SVG = "image/svg+xml"
PLAIN = "text/plain; charset=utf-8"
NOT_FOUND = Page(404, "application/json", b'{"error": "not found"}')
MANIFEST = "/static/vendor/MANIFEST.json"
VENDORED = {
    "lib/LICENSE": b"MIT License\n",
    "lib/lib.js": b"export const x = 1\n",
    "lib/sprite.svg": b"<svg/>",  # its URL is computed at run time: only the manifest reaches it
}
INDEX = """<!doctype html>
<link rel="icon" href="/static/img/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/css/app.css?v=1">
<script type="module" src="/static/js/main.js"></script>
<a href="#top">top</a> <a href="https://github.com/DonQuaan/CCA">source</a>
<a href="mailto:someone@example.org">mail</a> <img src="data:image/png;base64,AAAA" alt="">
<a href="/static/vendor/lib/LICENSE">licence</a> <a href="/static/vendor/MANIFEST.json">hashes</a>
"""
MAIN_JS = """import {a} from "./a.js"
import {
  b,
  c,
} from "../js/b.js"
export * from "./c.js"
import "./side.js"
const later = () => import("./lazy.js")
import {x} from "/static/vendor/lib/lib.js"
// the 'from' variable, copied from "elsewhere"; see import("bare-name")
const pieces = "/static/img/pieces.svg", assets = "/static/vendor/lib/"
const remote = import("https://cdn.example.org/x.js")
"""
APP_CSS = """@import "./extra.css";
body { background: url("../img/bg.svg") }
.icon { background: url(data:image/png;base64,AAAA) }
"""


def site() -> dict[str, Page]:
    """Path -> answer for a small page that uses every kind of reference smoke.py follows."""
    manifest = {
        "files": [
            {"path": rel, "sha256": hashlib.sha256(body).hexdigest()}
            for rel, body in VENDORED.items()
        ]
    }
    pages = {
        "/": Page(200, HTML, INDEX.encode()),
        "/static/img/icon.svg": Page(200, SVG, b"<svg/>"),
        "/static/css/app.css": Page(200, CSS, APP_CSS.encode()),
        "/static/css/extra.css": Page(200, CSS, b"p { margin: 0 }"),
        "/static/img/bg.svg": Page(200, SVG, b"<svg/>"),
        "/static/js/main.js": Page(200, JS, MAIN_JS.encode()),
        "/static/img/pieces.svg": Page(200, SVG, b"<svg/>"),
        MANIFEST: Page(200, "application/json", json.dumps(manifest).encode()),
    }
    for name in ("a", "b", "c", "side", "lazy"):
        pages[f"/static/js/{name}.js"] = Page(200, JS, f"export const {name} = 1\n".encode())
    types = {"lib/LICENSE": PLAIN, "lib/lib.js": JS, "lib/sprite.svg": SVG}
    for rel, body in VENDORED.items():
        pages["/static/vendor/" + rel] = Page(200, types[rel], body)
    return pages


def inspect_doc(variant: str = "default") -> dict[str, Any]:
    """``docker image inspect`` of a correct image (one element of the JSON array)."""
    env = ["PATH=/opt/cca/bin:/usr/local/bin:/usr/bin", f"CCA_STOCKFISH={smoke.STOCKFISH}"]
    cfg: dict[str, Any] = {
        "User": "10001:10001",
        "Entrypoint": list(smoke.ENTRYPOINT),
        "Cmd": list(smoke.EXPECTED_CMD[variant]),
        "ExposedPorts": {"8765/tcp": {}},
        "Env": env,
        "Healthcheck": {
            "Test": [
                "CMD",
                "/usr/local/bin/python",
                "-c",
                "d = json.load(urlopen('http://127.0.0.1:8765/healthz')); d.get('ready')",
            ],
            "StartPeriod": 30 * 10**9,
        },
        "Labels": {
            LABEL + "licenses": smoke.LICENSES,
            LABEL + "source": smoke.SOURCE,
            LABEL + "description": "CCA test image",
            LABEL + "version": VERSION,
            LABEL + "revision": REVISION,
        },
    }
    if variant == "maia2":
        env.append(f"CCA_WEIGHTS={smoke.WEIGHTS_DIR}")
        cfg["Volumes"] = {"/data": {}}
        cfg["Healthcheck"]["StartPeriod"] = 600 * 10**9
    return {"Os": "linux", "Architecture": "amd64", "Config": cfg}


def probe_doc(variant: str = "default") -> dict[str, Any]:
    """What PROBE prints inside a correct image."""
    report: dict[str, Any] = {
        "uid": 10001,
        "gid": 10001,
        "unreadable": [],
        "stale_notices": [],
        "nets": ["nn-1a298aa575a0.nnue"],
        "dists": ["cca-chess", "chess"],
        "tools": [],
        "pip_in_venv": False,
        "pip_in_base": False,
        "torch": None,
        "cuda": None,
        "weights_writable": None,
    }
    if variant == "maia2":
        report["dists"] += ["maia2", "numpy", "torch"]
        report.update(torch="2.8.0+cpu", weights_writable=True)
    return report


class Clock:
    """Fake monotonic clock; ``sleep`` advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeDocker:
    """Scripted docker CLI: answers per sub-command and records every call."""

    def __init__(self, variant: str = "default") -> None:
        self.calls: list[tuple[list[str], str | None, float]] = []
        self.inspect: list[dict[str, Any]] = [inspect_doc(variant)]
        self.probe = probe_doc(variant)
        self.out: dict[str, Result] = {
            "version": Result(0, VERSION + "\n", ""),
            "doctor": Result(0, DOCTOR_OK, ""),
            "uci": Result(0, UCI_OK, ""),
            "analyse": Result(0, ANALYSE_OK, ""),
            "serve": Result(0, "0123abcd\n", ""),
            "port": Result(0, f"127.0.0.1:{HOST_PORT}\n", ""),
            "logs": Result(0, "server log line\n", ""),
            "rm": Result(0, "", ""),
        }
        self.states: list[Result] = [
            Result(0, json.dumps(STARTING), ""),
            Result(0, json.dumps(HEALTHY), ""),
        ]

    @staticmethod
    def key(args: Sequence[str]) -> str:
        """Which scripted answer a docker command line gets."""
        first = args[0]
        if first == "image":
            return "image-inspect"
        if first == "inspect":
            return "state"
        if first in {"logs", "rm", "port"}:
            return first
        if "-d" in args:
            return "serve"
        if "--entrypoint" in args:
            return "probe"
        return args[list(args).index(IMAGE) + 1]

    def __call__(self, args: Sequence[str], stdin: str | None, timeout: float) -> Result:
        self.calls.append((list(args), stdin, timeout))
        key = self.key(args)
        if key == "image-inspect":
            return self.out.get(key, Result(0, json.dumps(self.inspect), ""))
        if key == "probe":
            return self.out.get(key, Result(0, "noise\n" + json.dumps(self.probe) + "\n", ""))
        if key == "state":
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return self.out[key]

    def commands(self, key: str) -> list[list[str]]:
        """Recorded command lines of one kind."""
        return [args for args, _, _ in self.calls if self.key(args) == key]


class FakeFetch:
    """Scripted web server: ``GET /healthz`` answers in turn (the last one repeats); every other
    path is looked up in ``pages`` (default: :func:`site`), 404 when absent."""

    def __init__(self, *answers: tuple[int, bytes], pages: dict[str, Page] | None = None) -> None:
        self.answers = [Page(status, "application/json", body) for status, body in answers]
        self.answers = self.answers or [Page(HEALTHZ_OK[0], "application/json", HEALTHZ_OK[1])]
        self.pages = site() if pages is None else pages
        self.calls: list[tuple[int, str]] = []

    def __call__(self, port: int, path: str, timeout: float) -> Page:
        self.calls.append((port, path))
        if path != "/healthz":
            return self.pages.get(path, NOT_FOUND)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def paths(self, *, healthz: bool = False) -> list[str]:
        """Requested paths, in order; ``/healthz`` only or everything else."""
        return [path for _, path in self.calls if (path == "/healthz") is healthz]


def make(
    variant: str = "default",
    *,
    fetch: FakeFetch | None = None,
    **options: Any,
) -> tuple[Smoke, FakeDocker, FakeFetch, Clock]:
    docker = FakeDocker(variant)
    fetch = fetch or FakeFetch()
    clock = Clock()
    opts = Options(image=IMAGE, variant=variant, **options)
    return Smoke(opts, docker, fetch, clock.sleep, clock), docker, fetch, clock


def fails(check: Callable[[], str], *fragments: str) -> str:
    with pytest.raises(SmokeError) as info:
        check()
    message = str(info.value)
    for fragment in fragments:
        assert fragment in message, message
    return message


# ------------------------------------------------------------------ whole runs
def test_every_check_passes_on_a_good_default_image() -> None:
    runner, docker, fetch, _ = make(expect_version=VERSION, expect_revision=REVISION)
    out = io.StringIO()
    assert runner.run_all(out) == 0
    lines = out.getvalue().splitlines()
    assert [line.split(":")[0] for line in lines[:-1]] == [
        "ok   config", "ok   version", "ok   doctor", "ok   filesystem", "ok   uci",
        "ok   analyse", "ok   web",
    ]  # fmt: skip
    assert lines[-1] == f"{IMAGE} (default): 7/7 checks passed"
    # one-shot containers never run the healthcheck and are removed on exit
    for args in docker.commands("version") + docker.commands("uci"):
        assert args[:3] == ["run", "--rm", "--no-healthcheck"]
    assert docker.commands("uci")[0][3] == "-i"
    (serve,) = docker.commands("serve")
    assert serve[serve.index("-p") + 1] == "127.0.0.1::8765"  # loopback, a port Docker picks
    assert serve[-1] == IMAGE  # the image's own command line
    name = serve[serve.index("--name") + 1]
    assert docker.commands("port") == [["port", name, "8765/tcp"]]
    assert docker.commands("rm") == [["rm", "-f", "-v", name]]
    assert fetch.paths(healthz=True) == ["/healthz"]
    assert sorted(fetch.paths()) == sorted(fetch.pages)  # the whole page, each URL once
    assert {port for port, _ in fetch.calls} == {HOST_PORT}


def test_maia2_image_without_weights_uses_qre_and_never_downloads() -> None:
    runner, docker, _, _ = make("maia2")
    assert runner.run_all(io.StringIO()) == 0
    uci_stdin = next(stdin for args, stdin, _ in docker.calls if docker.key(args) == "uci")
    assert uci_stdin == "uci\nsetoption name CCA_HumanModel value qre\nisready\nquit\n"
    (analyse,) = docker.commands("analyse")
    assert analyse[analyse.index("--human") + 1] == "qre"
    (serve,) = docker.commands("serve")
    assert serve[serve.index(IMAGE) + 1 :] == [*smoke.WEB_CMD, "--human", "qre"]
    assert smoke.EXPECTED_CMD["default"] == [*smoke.WEB_CMD, "--human", "qre"]
    assert not any("--mount" in args for args, _, _ in docker.calls)


def test_maia2_image_with_weights_mounts_them_read_only_and_runs_maia2(tmp_path: Path) -> None:
    runner, docker, _, _ = make("maia2", maia2_weights=tmp_path)
    docker.probe["weights_writable"] = False  # a read-only mount is expected here
    assert runner.run_all(io.StringIO()) == 0
    mount = f"type=bind,source={tmp_path.resolve()},target=/data/maia2,readonly"
    for key in ("version", "doctor", "probe", "uci", "analyse", "serve"):
        for args in docker.commands(key):
            assert args[args.index("--mount") + 1] == mount
    uci_stdin = next(stdin for args, stdin, _ in docker.calls if docker.key(args) == "uci")
    assert uci_stdin == "uci\nisready\nquit\n"
    (analyse,) = docker.commands("analyse")
    assert analyse[analyse.index("--human") + 1] == "maia2"
    (serve,) = docker.commands("serve")
    assert serve[-1] == IMAGE


def test_a_failing_check_does_not_stop_the_others_and_sets_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    runner, docker, _, _ = make()
    docker.out["version"] = Result(0, "9.9.9\n", "")
    out = io.StringIO()
    assert runner.run_all(out) == 1
    text = out.getvalue()
    assert "FAIL version: cca version printed '9.9.9', expected '0.1.0'" in text
    assert "::error title=smoke version::cca version printed" in text
    assert "ok   web" in text
    assert text.splitlines()[-1] == f"{IMAGE} (default): 6/7 checks passed"


def test_unexpected_output_is_reported_as_a_failure_not_a_crash() -> None:
    runner, docker, _, _ = make()
    docker.inspect = []  # docker answered with an empty array
    out = io.StringIO()
    assert runner.run_all(out) == 1
    assert "FAIL config: IndexError" in out.getvalue()


def test_no_annotations_outside_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    runner, docker, _, _ = make()
    docker.out["doctor"] = Result(1, "", "boom")
    out = io.StringIO()
    assert runner.run_all(out) == 1
    assert "::error" not in out.getvalue()


def test_gh_escape() -> None:
    assert smoke.gh_escape("50% done\r\nnext") == "50%25 done%0D%0Anext"


# ------------------------------------------------------------------ image configuration
def _set(path: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        *parents, leaf = path.split("/")
        node = doc
        for part in parents:
            node = node[part]
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value

    return mutate


CONFIG_BREAKS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("os", _set("Os", "windows"), "os is 'windows'"),
    ("arch", _set("Architecture", "arm64"), "architecture is 'arm64'"),
    ("root user", _set("Config/User", "root"), "user is 'root'"),
    ("no user", _set("Config/User", None), "user is None"),
    ("entrypoint", _set("Config/Entrypoint", ["cca"]), "entrypoint is ['cca']"),
    ("cmd", _set("Config/Cmd", ["uci"]), "cmd is ['uci']"),
    ("port", _set("Config/ExposedPorts", {"8000/tcp": {}}), "port 8765 not exposed"),
    ("stockfish env", _set("Config/Env", ["PATH=/usr/bin"]), "CCA_STOCKFISH is None"),
    ("no healthcheck", _set("Config/Healthcheck", None), "healthcheck is []"),
    (
        "healthcheck ignores ready",
        _set("Config/Healthcheck", {"Test": ["CMD", "python", "-c", "urlopen('/healthz')"]}),
        "that checks 'ready'",
    ),
    (
        "shell healthcheck",
        _set("Config/Healthcheck", {"Test": ["CMD-SHELL", "curl /healthz"]}),
        "expected an exec-form probe",
    ),
    (
        # probes /healthz and reads "ready", so only the exec-form guard can catch it
        "shell healthcheck that checks ready",
        _set(
            "Config/Healthcheck",
            {"Test": ["CMD-SHELL", "python -c \"urlopen('/healthz'); d.get('ready')\""]},
        ),
        "expected an exec-form probe",
    ),
    ("licence", _set(f"Config/Labels/{LABEL}licenses", "Apache-2.0"), "licenses label is"),
    ("source", _set(f"Config/Labels/{LABEL}source", "https://x"), "source label is"),
    ("no description", _set(f"Config/Labels/{LABEL}description", None), "description label has 0"),
    (
        "long description",
        _set(f"Config/Labels/{LABEL}description", "x" * 513),
        "description label has 513",
    ),
    ("version", _set(f"Config/Labels/{LABEL}version", "0.0.9"), "version label is '0.0.9'"),
    ("no version", _set(f"Config/Labels/{LABEL}version", None), "version label is empty"),
    ("revision", _set(f"Config/Labels/{LABEL}revision", "abc"), "revision label is 'abc'"),
]


@pytest.mark.parametrize(
    ("mutate", "problem"), [c[1:] for c in CONFIG_BREAKS], ids=[c[0] for c in CONFIG_BREAKS]
)
def test_config_problems_are_caught(mutate: Callable[[dict[str, Any]], None], problem: str) -> None:
    doc = inspect_doc()
    assert smoke.validate_config(doc, "default", VERSION, REVISION) == []
    mutate(doc)
    problems = smoke.validate_config(doc, "default", VERSION, REVISION)
    assert any(problem in p for p in problems), problems


MAIA2_CONFIG_BREAKS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("cmd", _set("Config/Cmd", list(smoke.WEB_CMD)), "cmd is"),
    (
        "weights env",
        _set("Config/Env", [f"CCA_STOCKFISH={smoke.STOCKFISH}"]),
        "CCA_WEIGHTS is None",
    ),
    ("volume", _set("Config/Volumes", None), "/data is not a volume"),
    ("short start", _set("Config/Healthcheck/StartPeriod", 30 * 10**9), "start period is 30s"),
]


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [c[1:] for c in MAIA2_CONFIG_BREAKS],
    ids=[c[0] for c in MAIA2_CONFIG_BREAKS],
)
def test_maia2_config_problems_are_caught(
    mutate: Callable[[dict[str, Any]], None], problem: str
) -> None:
    doc = inspect_doc("maia2")
    assert smoke.validate_config(doc, "maia2", None, None) == []
    mutate(doc)
    problems = smoke.validate_config(doc, "maia2", None, None)
    assert any(problem in p for p in problems), problems


def test_expectations_are_optional() -> None:
    doc = inspect_doc()
    doc["Config"]["Labels"][LABEL + "revision"] = ""
    assert smoke.validate_config(doc, "default", None, None) == []


def test_config_check_fails_on_a_bad_or_missing_image() -> None:
    runner, docker, _, _ = make()
    docker.inspect[0]["Config"]["User"] = "root"
    docker.inspect[0]["Config"]["Labels"][LABEL + "licenses"] = "MIT"
    fails(runner.check_config, "user is 'root'", "; licenses label is 'MIT'")
    docker.out["image-inspect"] = Result(1, "", "Error: No such image: cca:test")
    fails(runner.check_config, "docker image inspect failed", "No such image")


# ------------------------------------------------------------------ inside the image
PROBE_BREAKS: list[tuple[str, str, dict[str, Any], str]] = [
    ("root", "default", {"uid": 0}, "runs as uid 0"),
    ("root group", "default", {"gid": 0}, "runs as gid 0"),
    ("docs", "default", {"unreadable": ["stockfish/Copying.txt"]}, "stockfish/Copying.txt"),
    (
        "stale notice",
        "default",
        {"stale_notices": ["cca/NOTICE"]},
        "['cca/NOTICE'] do not describe the image (no 'ghcr.io/donquaan/cca' in them)",
    ),
    ("nets", "default", {"nets": []}, "no exported Stockfish NNUE"),
    ("tools", "default", {"tools": ["uv"]}, "build tools present"),
    ("pip", "default", {"pip_in_venv": True}, "pip is installed in the runtime virtual"),
    ("base pip", "default", {"pip_in_base": True}, "pip is installed in the base image"),
    ("pip tool", "default", {"tools": ["pip3"]}, "build tools present"),
    ("no chess", "default", {"dists": ["cca-chess"]}, "cca-chess and chess"),
    (
        "cuda libs",
        "default",
        {"dists": ["cca-chess", "chess", "nvidia-cublas-cu12"]},
        "CUDA packages",
    ),
    ("triton", "maia2", {"dists": ["cca-chess", "chess", "maia2", "triton"]}, "CUDA packages"),
    ("torch in default", "default", {"torch": "2.8.0+cpu"}, "torch/maia2 found"),
    (
        "maia2 in default",
        "default",
        {"dists": ["cca-chess", "chess", "maia2"]},
        "torch/maia2 found",
    ),
    ("no maia2", "maia2", {"dists": ["cca-chess", "chess", "torch"]}, "maia2 is not installed"),
    ("cuda torch", "maia2", {"torch": "2.8.0+cu128"}, "expected a +cpu build"),
    ("no torch", "maia2", {"torch": None}, "expected a +cpu build"),
    ("cuda build", "maia2", {"cuda": "12.8"}, "built with CUDA 12.8"),
    ("weights dir", "maia2", {"weights_writable": False}, "/data/maia2 is not writable"),
]


@pytest.mark.parametrize(
    ("variant", "change", "problem"),
    [c[1:] for c in PROBE_BREAKS],
    ids=[c[0] for c in PROBE_BREAKS],
)
def test_probe_problems_are_caught(variant: str, change: dict[str, Any], problem: str) -> None:
    report = probe_doc(variant)
    assert smoke.validate_probe(report, variant, weights_mounted=False) == []
    report.update(change)
    problems = smoke.validate_probe(report, variant, weights_mounted=False)
    assert any(problem in p for p in problems), problems


def test_filesystem_check_runs_the_probe_with_the_venv_python() -> None:
    runner, docker, _, _ = make()
    assert "nn-1a298aa575a0.nnue" in runner.check_filesystem()
    (probe,) = docker.commands("probe")
    assert probe[probe.index("--entrypoint") + 1] == smoke.VENV_PYTHON
    assert probe[probe.index(IMAGE) + 1 : probe.index(IMAGE) + 3] == ["-c", smoke.PROBE]
    cfg = json.loads(probe[-1])
    assert cfg == {
        "doc_root": smoke.DOC_ROOT,
        "docs": list(smoke.DOC_FILES),
        "notices": ["cca/NOTICE", "cca/THIRD_PARTY_NOTICES.md"],
        "image_name": "ghcr.io/donquaan/cca",
        "tools": list(smoke.BUILD_TOOLS),
    }
    # the base image ships pip/pip3 on PATH; the runtime stage removes them
    assert {"uv", "pip", "pip3", "gcc", "git"} <= set(cfg["tools"])


def test_filesystem_check_reports_probe_problems_and_garbage() -> None:
    runner, docker, _, _ = make()
    docker.probe["uid"] = 0
    fails(runner.check_filesystem, "runs as uid 0")
    docker.out["probe"] = Result(0, "Traceback: boom\n", "")
    fails(runner.check_filesystem, "probe printed no JSON", "Traceback")
    docker.out["probe"] = Result(0, "", "")
    fails(runner.check_filesystem, "probe printed no JSON")


def test_probe_reports_expected_fields() -> None:
    # The probe is plain Python: make sure it at least compiles and names every field we read.
    compile(smoke.PROBE, "<probe>", "exec")
    for field in probe_doc("maia2"):
        assert f'"{field}"' in smoke.PROBE


def test_probe_runs_and_finds_stale_notices_and_unreadable_docs(tmp_path: Path) -> None:
    doc = tmp_path / "doc"
    (doc / "cca").mkdir(parents=True)
    (doc / "stockfish").mkdir()
    (doc / "cca" / "NOTICE").write_text(f"Images: {smoke.IMAGE_NAME}\n", encoding="utf-8")
    (doc / "cca" / "THIRD_PARTY_NOTICES.md").write_text("Never bundled.\n", encoding="utf-8")
    (doc / "stockfish" / "nn-0123456789ab.nnue").write_bytes(b"net")
    (doc / "stockfish" / "empty.txt").write_bytes(b"")
    cfg = {
        "doc_root": str(doc),
        "docs": ["cca/NOTICE", "stockfish/empty.txt", "stockfish/gone.txt"],
        "notices": ["cca/NOTICE", "cca/THIRD_PARTY_NOTICES.md", "cca/gone.md"],
        "image_name": smoke.IMAGE_NAME,
        "tools": ["no-such-tool-for-cca-smoke"],
    }
    # The probe runs in a Linux image; os.getuid/os.getgid do not exist on Windows.
    code = "import os\nos.getuid = os.getgid = lambda: 10001\n" + smoke.PROBE
    proc = subprocess.run(
        [sys.executable, "-c", code, json.dumps(cfg)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
        env={**os.environ, "CCA_WEIGHTS": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["stale_notices"] == ["cca/THIRD_PARTY_NOTICES.md", "cca/gone.md"]
    assert report["unreadable"] == ["stockfish/empty.txt", "stockfish/gone.txt"]
    assert report["nets"] == ["nn-0123456789ab.nnue"]
    assert report["tools"] == []
    assert (report["uid"], report["gid"], report["weights_writable"]) == (10001, 10001, True)
    assert "cca-chess" in report["dists"]
    problems = smoke.validate_probe(report, "default", weights_mounted=False)
    assert any("cca/THIRD_PARTY_NOTICES.md" in p for p in problems), problems


# ------------------------------------------------------------------ commands
def test_version_must_match_the_label_or_the_expectation() -> None:
    runner, docker, _, _ = make()
    runner.check_config()
    assert runner.check_version() == VERSION
    docker.out["version"] = Result(0, "0.2.0\n", "")
    fails(runner.check_version, "printed '0.2.0', expected '0.1.0'")
    runner, docker, _, _ = make()  # config check never ran: no label known
    fails(runner.check_version, "has no version label")
    runner, docker, _, _ = make(expect_version="0.1.0")
    assert runner.check_version() == VERSION


def test_doctor_needs_the_stockfish_19_banner_and_exit_0() -> None:
    runner, docker, _, _ = make()
    assert "Stockfish 19" in runner.check_doctor()
    docker.out["doctor"] = Result(0, "  engine ok: Stockfish 18 (/usr/local/bin/stockfish)\n", "")
    fails(runner.check_doctor, "doctor output lacks")
    docker.out["doctor"] = Result(1, "  engine FAIL: not found\n", "")
    fails(runner.check_doctor, "`doctor` exited with 1", "engine FAIL")


@pytest.mark.parametrize(
    ("transcript", "problem"),
    [
        ("id name CCA\nuciok\n", "no readyok"),
        ("id name CCA\nreadyok\n", "no uciok"),
        ("uciok\nreadyok\n", "no 'id name' line"),
        ("id name CCA\nuciok\ninfo string error: boom\nreadyok\n", "info string error: boom"),
    ],
)
def test_uci_handshake_problems(transcript: str, problem: str) -> None:
    runner, docker, _, _ = make()
    docker.out["uci"] = Result(0, transcript, "")
    fails(runner.check_uci, problem)


def test_uci_with_real_maia2_must_not_fall_back(tmp_path: Path) -> None:
    runner, docker, _, _ = make("maia2", maia2_weights=tmp_path)
    docker.out["uci"] = Result(
        0, "id name CCA\nuciok\ninfo string maia2 unavailable (x); falling back\nreadyok\n", ""
    )
    fails(runner.check_uci, "maia2 unavailable")
    runner, docker, _, _ = make("maia2")  # QRE on purpose: the fallback line is fine there
    docker.out["uci"] = Result(
        0, "id name CCA\nuciok\ninfo string maia2 unavailable\nreadyok\n", ""
    )
    assert "qre" in runner.check_uci()


@pytest.mark.parametrize(
    ("stdout", "problem"),
    [
        ("not json", "analyse printed no JSON"),
        (json.dumps(["e2e4"]), "no legal-looking move"),
        (json.dumps({"move": "e2e9", "policy": {"e2e9": 1.0}}), "no legal-looking move"),
        (json.dumps({"move": "e2e4", "policy": {"d2d4": 1.0}}), "no legal-looking move"),
        (json.dumps({"move": "e2e4"}), "no legal-looking move"),
    ],
)
def test_analyse_problems(stdout: str, problem: str) -> None:
    runner, docker, _, _ = make()
    docker.out["analyse"] = Result(0, stdout, "")
    fails(runner.check_analyse, problem)


def test_analyse_accepts_promotions_and_rejects_a_maia2_fallback(tmp_path: Path) -> None:
    runner, docker, _, _ = make()
    docker.out["analyse"] = Result(0, json.dumps({"move": "a7a8q", "policy": {"a7a8q": 1}}), "")
    assert runner.check_analyse() == "move a7a8q (qre human model)"
    runner, docker, _, _ = make("maia2", maia2_weights=tmp_path)
    docker.out["analyse"] = Result(0, ANALYSE_OK, "warning: x; falling back to --human qre\n")
    fails(runner.check_analyse, "fell back from Maia-2")


# ------------------------------------------------------------------ web server
def test_web_waits_for_healthy_and_ready() -> None:
    fetch = FakeFetch(
        (200, b'{"status": "ok", "ready": false}'),
        (200, b'{"status": "ok", "ready": false}'),
        HEALTHZ_OK,
    )
    runner, _, _, clock = make(fetch=fetch)
    assert runner.check_web() == (
        f"healthy after 2s, ready after 6s, GET 127.0.0.1:{HOST_PORT}/healthz -> 200 ok; "
        "page: 16 URLs 200 with usable types, 3 vendored files match MANIFEST.json"
    )
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert fetch.paths(healthz=True) == ["/healthz"] * 3
    assert fetch.paths()[0] == "/"  # the page is loaded only once the engines are ready
    assert fetch.calls.index((HOST_PORT, "/")) == 3


def test_web_can_use_a_fixed_host_port() -> None:
    runner, docker, fetch, _ = make(port=28765)
    docker.out["port"] = Result(0, "127.0.0.1:28765\n", "")
    assert "GET 127.0.0.1:28765/healthz -> 200 ok" in runner.check_web()
    (serve,) = docker.commands("serve")
    assert serve[serve.index("-p") + 1] == "127.0.0.1:28765:8765"
    assert {port for port, _ in fetch.calls} == {28765}


def test_web_accepts_a_healthz_without_ready() -> None:
    runner, _, _, _ = make(fetch=FakeFetch((200, b'{"status": "ok"}')))
    assert f"GET 127.0.0.1:{HOST_PORT}/healthz -> 200 ok" in runner.check_web()


def _web_failure(docker: FakeDocker, fetch: FakeFetch, *fragments: str, **options: Any) -> None:
    clock = Clock()
    runner = Smoke(Options(image=IMAGE, **options), docker, fetch, clock.sleep, clock)
    message = fails(runner.check_web, *fragments)
    assert "--- container logs ---\nserver log line" in message
    (serve,) = docker.commands("serve")
    name = serve[serve.index("--name") + 1]
    assert docker.commands("rm") == [["rm", "-f", "-v", name]]  # cleaned up in every case


def test_web_failures_keep_logs_and_always_remove_the_container() -> None:
    def docker_with(**changes: Any) -> FakeDocker:
        docker = FakeDocker()
        for key, value in changes.items():
            setattr(docker, key, value)
        return docker

    exited = [Result(0, json.dumps({"Status": "exited", "ExitCode": 2}), "")]
    _web_failure(docker_with(states=exited), FakeFetch(), "container is exited (exit code 2)")
    unhealthy = {"Status": "running", "Health": {"Status": "unhealthy", "Log": [{"Output": "503"}]}}
    _web_failure(
        docker_with(states=[Result(0, json.dumps(unhealthy), "")]),
        FakeFetch(),
        "container is unhealthy: 503",
    )
    bare = [Result(0, json.dumps({"Status": "running"}), "")]
    _web_failure(docker_with(states=bare), FakeFetch(), "the container has no healthcheck")
    _web_failure(
        docker_with(states=[Result(0, json.dumps(STARTING), "")]),
        FakeFetch(),
        "not healthy after 10s (still starting)",
        health_timeout=10.0,
    )
    _web_failure(
        docker_with(states=[Result(1, "", "Error: No such object")]),
        FakeFetch(),
        "docker inspect failed",
    )
    _web_failure(FakeDocker(), FakeFetch((503, b'{"status": "error"}')), "returned HTTP 503")
    for answer, problem in [
        (Result(1, "", "Error: No public port '8765/tcp'"), "printed no binding"),
        (Result(0, "\n", ""), "printed no binding"),
        (Result(1, "127.0.0.1:49153\n", "Error response from daemon"), "printed no binding"),
        (Result(0, "0.0.0.0:49153\n", ""), "published on '0.0.0.0:49153', expected 127.0.0.1"),
        (Result(0, "127.0.0.1:http\n", ""), "published on '127.0.0.1:http'"),
    ]:
        docker = FakeDocker()
        docker.out["port"] = answer
        _web_failure(docker, FakeFetch(), problem)
    _web_failure(FakeDocker(), FakeFetch((200, b"<html>")), "returned no JSON")
    _web_failure(FakeDocker(), FakeFetch((200, b'{"status": "starting"}')), "expected status 'ok'")
    _web_failure(FakeDocker(), FakeFetch((200, b'["ok"]')), "expected status 'ok'")
    _web_failure(
        FakeDocker(),
        FakeFetch((200, b'{"status": "ok", "ready": false}')),
        "engines not ready after 10s",
        health_timeout=10.0,
    )


def test_web_container_that_cannot_start_is_still_removed() -> None:
    docker = FakeDocker()
    docker.out["serve"] = Result(125, "", "ports are not available")
    _web_failure(docker, FakeFetch(), "docker run failed", "ports are not available")


# ------------------------------------------------------------------ the web page itself
def test_references_follow_what_a_browser_loads() -> None:
    pages = site()
    assert smoke.references("/", pages["/"]) == [
        ("/static/img/icon.svg", "other"),
        ("/static/css/app.css", "stylesheet"),
        ("/static/js/main.js", "script"),
        ("/static/vendor/lib/LICENSE", "other"),
        ("/static/vendor/MANIFEST.json", "other"),
    ]
    assert smoke.references("/static/js/main.js", pages["/static/js/main.js"]) == [
        ("/static/js/a.js", "script"),
        ("/static/js/b.js", "script"),
        ("/static/js/c.js", "script"),
        ("/static/js/side.js", "script"),
        ("/static/js/lazy.js", "script"),
        ("/static/vendor/lib/lib.js", "script"),
        ("/static/vendor/lib/lib.js", "other"),  # also a /static/ string literal: harmless
        ("/static/img/pieces.svg", "other"),
    ]
    assert smoke.references("/static/css/app.css", pages["/static/css/app.css"]) == [
        ("/static/css/extra.css", "stylesheet"),
        ("/static/img/bg.svg", "other"),
    ]
    assert smoke.references("/static/img/bg.svg", pages["/static/img/bg.svg"]) == []
    css = Page(200, CSS, b"@import url(\"x.css\"); a { background: url( '../i/y.svg' ) }")
    assert smoke.references("/static/css/a.css", css) == [
        ("/static/css/x.css", "stylesheet"),
        ("/static/css/x.css", "other"),
        ("/static/i/y.svg", "other"),
    ]


@pytest.mark.parametrize(
    ("base", "ref", "path"),
    [
        ("/", "/static/a.js?v=2#x", "/static/a.js"),
        ("/static/js/m.js", "../vendor/x.js", "/static/vendor/x.js"),
        ("/static/js/m.js", "./y.js", "/static/js/y.js"),
        ("/", "  /static/sp.svg ", "/static/sp.svg"),
        ("/", "", None),
        ("/", "#main", None),
        ("/", "https://example.org/x.js", None),
        ("/", "//cdn.example.org/x.js", None),
        ("/", "data:image/png;base64,AA", None),
        ("/", "mailto:a@example.org", None),
        ("/", "javascript:void(0)", None),
    ],
)
def test_resolve(base: str, ref: str, path: str | None) -> None:
    assert smoke.resolve(base, ref) == path


def test_a_complete_page_passes_and_every_url_is_loaded_once() -> None:
    fetch = FakeFetch()
    runner, _, _, _ = make(fetch=fetch)
    assert "page: 16 URLs 200 with usable types, 3 vendored files match" in runner.check_web()
    assert sorted(fetch.paths()) == sorted(site())
    assert "https://cdn.example.org/x.js" not in fetch.paths()


def _drop(path: str) -> Callable[[dict[str, Page]], None]:
    def change(pages: dict[str, Page]) -> None:
        del pages[path]

    return change


def _retype(path: str, ctype: str) -> Callable[[dict[str, Page]], None]:
    def change(pages: dict[str, Page]) -> None:
        pages[path] = Page(200, ctype, pages[path].body)

    return change


def _rewrite(path: str, body: bytes) -> Callable[[dict[str, Page]], None]:
    def change(pages: dict[str, Page]) -> None:
        pages[path] = Page(200, pages[path].content_type, body)

    return change


PAGE_BREAKS: list[tuple[str, Callable[[dict[str, Page]], None], str]] = [
    ("no page", _drop("/"), "/: HTTP 404"),
    ("page type", _retype("/", "application/json"), "/: served as 'application/json', a document"),
    ("module script", _drop("/static/js/main.js"), "/static/js/main.js (from /): HTTP 404"),
    ("named import", _drop("/static/js/a.js"), "/static/js/a.js (from /static/js/main.js): HTTP"),
    ("multi-line import", _drop("/static/js/b.js"), "/static/js/b.js (from /static/js/main.js)"),
    ("export from", _drop("/static/js/c.js"), "/static/js/c.js (from /static/js/main.js)"),
    ("side-effect import", _drop("/static/js/side.js"), "/static/js/side.js (from /static/js/"),
    ("dynamic import", _drop("/static/js/lazy.js"), "/static/js/lazy.js (from /static/js/"),
    ("absolute import", _drop("/static/vendor/lib/lib.js"), "/static/vendor/lib/lib.js (from /"),
    ("sprite literal", _drop("/static/img/pieces.svg"), "/static/img/pieces.svg (from /static/"),
    ("icon", _drop("/static/img/icon.svg"), "/static/img/icon.svg (from /): HTTP 404"),
    ("stylesheet", _drop("/static/css/app.css"), "/static/css/app.css (from /): HTTP 404"),
    ("css import", _drop("/static/css/extra.css"), "/static/css/extra.css (from /static/css/app"),
    ("css url", _drop("/static/img/bg.svg"), "/static/img/bg.svg (from /static/css/app.css)"),
    (
        "module mime",
        _retype("/static/js/a.js", PLAIN),
        "/static/js/a.js (from /static/js/main.js): served as 'text/plain', "
        "a script needs text/javascript",
    ),
    ("script mime", _retype("/static/js/main.js", ""), "served as 'no type', a script needs"),
    (
        "stylesheet mime",
        _retype("/static/css/app.css", PLAIN),
        "/static/css/app.css (from /): served as 'text/plain', a stylesheet needs text/css",
    ),
    ("css import mime", _retype("/static/css/extra.css", PLAIN), "a stylesheet needs text/css"),
    ("no manifest", _drop(MANIFEST), "/static/vendor/MANIFEST.json: HTTP 404"),
    (
        "vendored file missing",
        _drop("/static/vendor/lib/sprite.svg"),
        "/static/vendor/lib/sprite.svg (in MANIFEST.json): HTTP 404",
    ),
    (
        "vendored file changed",
        _rewrite("/static/vendor/lib/sprite.svg", b"<svg><!-- changed --></svg>"),
        "/static/vendor/lib/sprite.svg: SHA-256 differs from MANIFEST.json",
    ),
    ("manifest not json", _rewrite(MANIFEST, b"<html>"), "MANIFEST.json has no 'files' list"),
    ("manifest array", _rewrite(MANIFEST, b"[]"), "MANIFEST.json has no 'files' list"),
    ("manifest empty", _rewrite(MANIFEST, b'{"files": []}'), "MANIFEST.json has no 'files' list"),
    (
        "manifest entry",
        _rewrite(MANIFEST, b'{"files": [{"path": "lib/LICENSE"}]}'),
        "MANIFEST.json entry without path and sha256: {'path': 'lib/LICENSE'}",
    ),
    ("manifest item", _rewrite(MANIFEST, b'{"files": ["lib/LICENSE"]}'), "entry without path"),
]


@pytest.mark.parametrize(
    ("change", "problem"), [c[1:] for c in PAGE_BREAKS], ids=[c[0] for c in PAGE_BREAKS]
)
def test_page_problems_are_caught(change: Callable[[dict[str, Page]], None], problem: str) -> None:
    pages = site()
    change(pages)
    message = fails(make(fetch=FakeFetch(pages=pages))[0].check_web, "the web page is broken: ")
    assert problem in message, message
    assert "--- container logs ---" in message


def test_a_script_mime_is_checked_even_when_first_seen_as_a_link() -> None:
    pages = site()
    index = pages["/"].body.replace(b"<script", b'<a href="/static/js/main.js">code</a><script')
    pages["/"] = Page(200, HTML, index)
    _retype("/static/js/main.js", PLAIN)(pages)
    fails(
        make(fetch=FakeFetch(pages=pages))[0].check_web,
        "/static/js/main.js (from /): served as 'text/plain', a script needs text/javascript",
    )


def test_a_script_mime_is_still_checked_when_a_link_follows() -> None:
    # The same URL loaded as a script and later only linked to keeps both uses: the later link
    # must not replace the script's MIME requirement.
    pages = site()
    pages["/"] = Page(200, HTML, pages["/"].body + b'<a href="/static/js/main.js">code</a>\n')
    _retype("/static/js/main.js", PLAIN)(pages)
    fails(
        make(fetch=FakeFetch(pages=pages))[0].check_web,
        "/static/js/main.js (from /): served as 'text/plain', a script needs text/javascript",
    )


def test_long_problem_lists_are_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "MAX_LISTED", 2)
    pages = site()
    for name in ("a", "b", "c", "side"):
        del pages[f"/static/js/{name}.js"]
    message = fails(make(fetch=FakeFetch(pages=pages))[0].check_web, "(and 2 more)")
    assert message.count("HTTP 404") == 2


def test_a_problem_is_reported_once() -> None:
    pages = site()
    entry = {"path": "lib/gone.js", "sha256": "0" * 64}
    pages[MANIFEST] = Page(200, "application/json", json.dumps({"files": [entry] * 3}).encode())
    message = fails(make(fetch=FakeFetch(pages=pages))[0].check_web, "lib/gone.js")
    assert message.count("/static/vendor/lib/gone.js (in MANIFEST.json): HTTP 404") == 1


def test_a_page_that_never_ends_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "MAX_URLS", 5)
    fails(make()[0].check_web, "the page references more than 5 URLs")


STATIC = Path(__file__).resolve().parents[2] / "src" / "cca" / "play" / "static"
STATIC_TYPES = {".html": HTML, ".js": JS, ".css": CSS, ".svg": SVG, ".json": "application/json"}


def static_dir_fetch(hidden: str | None = None) -> Callable[[int, str, float], Page]:
    """``cca play``'s routes for / and /static/, answered from the repository's static files."""

    def fetch(port: int, path: str, timeout: float) -> Page:
        if path == "/healthz":
            return Page(200, "application/json", HEALTHZ_OK[1])
        rel = "index.html" if path == "/" else path.removeprefix("/static/")
        file = STATIC / rel
        if rel in {path, hidden} or not file.is_file():
            return NOT_FOUND
        return Page(200, STATIC_TYPES.get(file.suffix, PLAIN), file.read_bytes())

    return fetch


def test_the_real_web_page_loads_completely() -> None:
    fetch = static_dir_fetch()
    runner = Smoke(Options(image=IMAGE), FakeDocker(), fetch, Clock().sleep, Clock())
    detail = runner.check_web()
    manifest = json.loads((STATIC / "vendor" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert f"{len(manifest['files'])} vendored files match MANIFEST.json" in detail


def test_the_real_web_page_without_chess_js_is_caught() -> None:
    # What the wheel built with the root .gitignore rule `dist/` shipped (review round 3).
    chess_js = "vendor/chess.js/dist/esm/chess.js"
    fetch = static_dir_fetch(hidden=chess_js)
    runner = Smoke(Options(image=IMAGE), FakeDocker(), fetch, Clock().sleep, Clock())
    fails(
        runner.check_web,
        f"/static/{chess_js} (from /static/js/board.js): HTTP 404",
        f"/static/{chess_js} (in MANIFEST.json): HTTP 404",
    )


# ------------------------------------------------------------------ plumbing
def test_result_tail_prefers_stderr_and_keeps_the_last_lines() -> None:
    assert Result(1, "out", "e1\ne2\ne3").tail(2) == "e2\ne3"
    assert Result(1, "o1\no2", "  ").tail() == "o1\no2"


def test_parse_args_defaults_and_validation(tmp_path: Path) -> None:
    opts = smoke.parse_args([IMAGE])
    assert opts == Options(image=IMAGE)
    assert opts.port is None  # Docker picks a free host port
    opts = smoke.parse_args(
        [IMAGE, "--variant", "maia2", "--expect-version", "1", "--expect-revision", "r",
         "--port", "28765", "--health-timeout", "60", "--maia2-weights", str(tmp_path)]
    )  # fmt: skip
    assert opts == Options(IMAGE, "maia2", "1", "r", 28765, 60.0, tmp_path)
    with pytest.raises(SystemExit):
        smoke.parse_args([IMAGE, "--maia2-weights", str(tmp_path)])  # default variant
    with pytest.raises(SystemExit):
        smoke.parse_args([IMAGE, "--variant", "maia2", "--maia2-weights", str(tmp_path / "no")])
    with pytest.raises(SystemExit):
        smoke.parse_args([IMAGE, "--variant", "gpu"])
    for bad in ("0", "65536", "-1"):
        with pytest.raises(SystemExit):
            smoke.parse_args([IMAGE, "--port", bad])
    assert smoke.parse_args([IMAGE, "--port", "65535"]).port == 65535


def test_main_wires_the_default_runner_and_fetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker()
    docker.states = [Result(0, json.dumps(HEALTHY), "")]
    monkeypatch.setattr(smoke, "run_docker", docker)
    monkeypatch.setattr(smoke, "http_get", FakeFetch())
    assert smoke.main([IMAGE, "--expect-version", VERSION]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"{IMAGE} (default): 7/7 checks passed"


def test_run_docker_runs_the_cli_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: sys.executable)
    code = "import sys; print(sys.stdin.read().upper()); print('err', file=sys.stderr); sys.exit(3)"
    res = smoke.run_docker(["-c", code], "hi; echo no-shell", 60.0)
    assert res == Result(3, "HI; ECHO NO-SHELL\n", "err\n")


def test_run_docker_timeout_and_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: sys.executable)
    with pytest.raises(SmokeError, match=r"timed out after 0\.5s"):
        smoke.run_docker(["-c", "import time; time.sleep(30)"], None, 0.5)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SmokeError, match="docker CLI not found"):
        smoke.run_docker(["version"], None, 5.0)


@pytest.fixture
def http_port() -> Iterator[int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = 200 if self.path == "/healthz" else 404
            body = b'{"status": "ok"}' if status == 200 else b"missing " + self.path.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if status == 200 else PLAIN)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep test output quiet."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_http_get_returns_status_type_and_body(http_port: int) -> None:
    assert smoke.http_get(http_port, "/healthz", 5.0) == Page(
        200, "application/json", b'{"status": "ok"}'
    )
    assert smoke.http_get(http_port, "/nope", 5.0) == Page(404, PLAIN, b"missing /nope")
    # a path taken from the page is quoted, not sent raw
    assert smoke.http_get(http_port, "/static/a b/é.js", 5.0).body == (
        b"missing /static/a%20b/%C3%A9.js"
    )
    assert Page(200, "Text/JavaScript; charset=utf-8", b"").mime == "text/javascript"


def dead_port() -> int:
    """A free port on 127.0.0.1 that nothing listens on (connections are refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_http_get_ignores_proxies_from_the_environment(
    http_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = f"http://127.0.0.1:{dead_port()}"
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    # urlopen would send this through the (dead) proxy and fail
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{http_port}/healthz", timeout=5)
    assert smoke.http_get(http_port, "/healthz", 5.0).status == 200


def test_http_get_turns_connection_errors_into_smoke_errors() -> None:
    port = dead_port()
    with pytest.raises(SmokeError, match=f"GET http://127.0.0.1:{port}/healthz failed"):
        smoke.http_get(port, "/healthz", 5.0)


def test_fake_docker_documents_every_command_smoke_uses() -> None:
    # Guard for the fakes themselves: a new docker call in smoke.py must be scripted here.
    runner, docker, _, _ = make("maia2")
    runner.run_all(io.StringIO())
    kinds = {docker.key(args) for args, _, _ in docker.calls}
    assert kinds == {
        "image-inspect", "version", "doctor", "probe", "uci", "analyse", "serve", "port", "state",
        "rm",
    }  # fmt: skip
