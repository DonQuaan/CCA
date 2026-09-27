"""Smoke-test a CCA container image: the checks CI runs before and after pushing to ghcr.io.

Usage (from the repository root)::

    python docker/smoke.py IMAGE [--variant default|maia2] [--expect-version V]
        [--expect-revision SHA] [--port N] [--health-timeout 240] [--maia2-weights DIR]

Needs only Python 3.11+ and the docker CLI. Every check runs even after an earlier one failed;
the exit status is 1 if any failed. Every container started here is removed together with its
anonymous volumes.

The web check publishes the container's port 8765 on 127.0.0.1 only, on a free host port that
Docker picks (read back with ``docker port``); ``--port N`` asks for a fixed one instead. On
Windows, ``netsh interface ipv4 show excludedportrange protocol=tcp`` lists ports that cannot be
used there. Once the container is healthy and ``/healthz`` says ready, the check loads the page
the way a browser would: ``GET /``, then every same-server script, module import, stylesheet,
image and link it references, transitively; each must answer 200, and scripts and stylesheets
must carry a MIME type a browser accepts for them. Every file listed in ``cca play``'s
``/static/vendor/MANIFEST.json`` must also be served, with the listed SHA-256.

Without ``--maia2-weights`` the maia2 variant runs its UCI, analyse and web checks with the QRE
human model, so CI never downloads the 267 MB checkpoint from Google Drive (torch and maia2 are
still imported and inspected). With it, that directory is mounted read-only on /data/maia2 and
the image's own Maia-2 command line is exercised.
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

IMAGE_UID = 10001
WEB_PORT = 8765
LICENSES = "Apache-2.0 AND GPL-3.0-or-later"
SOURCE = "https://github.com/DonQuaan/CCA"
ENTRYPOINT = ["/usr/bin/tini", "--", "cca"]
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - bind address inside the container only
WEB_CMD = ["play", "--host", ALL_INTERFACES, "--port", str(WEB_PORT), "--no-browser"]
QRE_WEB_CMD = [*WEB_CMD, "--human", "qre"]
EXPECTED_CMD = {"default": QRE_WEB_CMD, "maia2": [*WEB_CMD, "--human", "maia2", "--device", "cpu"]}
STOCKFISH = "/usr/local/bin/stockfish"
WEIGHTS_DIR = "/data/maia2"
VENV_PYTHON = "/opt/cca/bin/python"
DOC_ROOT = "/usr/local/share/doc"
# Licence texts and the corresponding source that must travel with the image (GPL-3.0 for
# Stockfish; MIT for tini; Apache-2.0 NOTICE for CCA).
DOC_FILES = (
    "stockfish/Copying.txt",
    "stockfish/AUTHORS",
    "stockfish/src/Makefile",
    "stockfish/src/evaluate.h",
    "tini/LICENSE",
    "cca/LICENSE",
    "cca/NOTICE",
    "cca/THIRD_PARTY_NOTICES.md",
)
IMAGE_NAME = "ghcr.io/donquaan/cca"
# The image bundles third-party programs, so its notices must describe the image itself (they
# name it) rather than only the Python packages, which bundle none.
NOTICE_FILES = ("cca/NOTICE", "cca/THIRD_PARTY_NOTICES.md")
BUILD_TOOLS = ("uv", "pip", "pip3", "gcc", "cc", "make", "git")
# The healthcheck waits for "ready" (engines built); on a first start of the maia2 image that
# includes maia2's download of the 267 MB checkpoint.
MIN_MAIA2_START_PERIOD_S = 300
MAX_DESCRIPTION = 512  # ghcr.io shows at most 512 characters
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
UCI_MOVE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")

# The web page and what a browser loads for it. Browsers refuse module scripts and stylesheets
# served with another MIME type, so those are checked as well as the status.
DOCUMENT, SCRIPT, STYLE, OTHER = "document", "script", "stylesheet", "other"
WANTED_TYPES: dict[str, tuple[str, ...]] = {
    DOCUMENT: ("text/html",),
    SCRIPT: ("text/javascript", "application/javascript", "application/x-javascript"),
    STYLE: ("text/css",),
}
VENDOR_DIR = "/static/vendor/"
# cca play's list of vendored files: {"files": [{"path": <under VENDOR_DIR>, "sha256": ...}]}.
VENDOR_MANIFEST = VENDOR_DIR + "MANIFEST.json"
MAX_URLS = 400  # a page that references more is taken to be looping
MAX_LISTED = 12  # problems quoted in one failure message
_ORIGIN = "http://cca.invalid"  # stands for the server while references are resolved
# Module specifiers after `from` / `import` / `import(`; only "/", "./" and "../" ones are loaded
# by a browser (bare names need an import map), which also skips prose such as `'from' variable`.
_JS_SPECIFIER = re.compile(r"""\b(?:from|import)\s*\(?\s*["']([^"'\s]+)["']""")
# Absolute /static/ file paths in string literals (sprites, piece sets); directories are skipped.
_JS_STATIC_PATH = re.compile(r"""["'](/static/[^"'\s?#]+\.[A-Za-z0-9]+)["']""")
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)""")
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')\s]+)["']?\s*\)""")

# Runs inside the image with its virtual environment's interpreter; prints one JSON object.
PROBE = """
import importlib.metadata, importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path

cfg = json.loads(sys.argv[1])
doc = Path(cfg["doc_root"])
unreadable = []
for rel in cfg["docs"]:
    try:
        with (doc / rel).open("rb") as fh:
            if not fh.read(1):
                unreadable.append(rel)
    except OSError:
        unreadable.append(rel)
stale_notices = []
for rel in cfg["notices"]:
    try:
        text = (doc / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    if cfg["image_name"] not in text:
        stale_notices.append(rel)
report = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "unreadable": unreadable,
    "stale_notices": stale_notices,
    "nets": sorted(p.name for p in (doc / "stockfish").glob("nn-*.nnue") if p.stat().st_size),
    "dists": sorted(
        {(d.metadata["Name"] or "").lower().replace("_", "-")
         for d in importlib.metadata.distributions()}
    ),
    "tools": sorted(t for t in cfg["tools"] if shutil.which(t)),
    "pip_in_venv": importlib.util.find_spec("pip") is not None,
    "pip_in_base": any(Path(sys.base_prefix, "lib").glob("python3*/site-packages/pip")),
    "torch": None,
    "cuda": None,
    "weights_writable": None,
}
if importlib.util.find_spec("torch"):
    import torch
    report["torch"] = torch.__version__
    report["cuda"] = torch.version.cuda
weights = os.environ.get("CCA_WEIGHTS")
if weights:
    try:
        with tempfile.TemporaryFile(dir=weights):
            report["weights_writable"] = True
    except OSError:
        report["weights_writable"] = False
print(json.dumps(report))
"""


class SmokeError(Exception):
    """A smoke check failed; the message says why."""


@dataclass(frozen=True)
class Result:
    """Outcome of one docker CLI call."""

    returncode: int
    stdout: str
    stderr: str

    def tail(self, lines: int = 20) -> str:
        """Last lines of stderr (or stdout when stderr is empty), for error messages."""
        text = self.stderr.strip() or self.stdout.strip()
        return "\n".join(text.splitlines()[-lines:])


@dataclass(frozen=True)
class Page:
    """One HTTP answer."""

    status: int
    content_type: str
    body: bytes

    @property
    def mime(self) -> str:
        """The media type without parameters, lower case (``text/javascript``)."""
        return self.content_type.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class Options:
    """What to test and what to expect."""

    image: str
    variant: str = "default"
    expect_version: str | None = None
    expect_revision: str | None = None
    port: int | None = None  # host port for the web check; None: Docker picks a free one
    health_timeout: float = 240.0
    maia2_weights: Path | None = None


if TYPE_CHECKING:
    Runner = Callable[[Sequence[str], str | None, float], Result]
    Fetch = Callable[[int, str, float], Page]


def run_docker(args: Sequence[str], stdin: str | None, timeout: float) -> Result:
    """Run ``docker ARGS`` without a shell and capture its output as UTF-8 text."""
    docker = shutil.which("docker")
    if docker is None:
        raise SmokeError("docker CLI not found on PATH")
    try:
        proc = subprocess.run(  # noqa: S603 - argument list built by this script, no shell
            [docker, *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeError(f"`docker {' '.join(args[:2])}` timed out after {timeout:g}s") from exc
    return Result(proc.returncode, proc.stdout, proc.stderr)


# The container is on this host's loopback: never go through an http_proxy from the environment
# (urlopen would, unless no_proxy lists 127.0.0.1). The image's HEALTHCHECK does the same.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(port: int, path: str, timeout: float) -> Page:
    """GET ``http://127.0.0.1:PORT/PATH``; HTTP error statuses are returned, not raised."""
    quoted = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
    try:
        with _DIRECT.open(f"http://127.0.0.1:{port}{quoted}", timeout=timeout) as resp:
            return Page(int(resp.status), resp.headers.get("Content-Type", ""), bytes(resp.read()))
    except urllib.error.HTTPError as exc:
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        return Page(exc.code, ctype, exc.read())
    except OSError as exc:
        raise SmokeError(f"GET http://127.0.0.1:{port}{path} failed: {exc}") from exc


class _HtmlReferences(html.parser.HTMLParser):
    """``src``/``href`` values of an HTML page, with how a browser uses each."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value for key, value in attrs if value is not None}
        kind = OTHER
        if tag == "script":
            kind = SCRIPT
        elif tag == "link" and "stylesheet" in values.get("rel", "").lower().split():
            kind = STYLE
        self.found += [(values[key], kind) for key in ("src", "href") if key in values]


def resolve(base: str, ref: str) -> str | None:
    """The server path that ``ref``, found in the resource at path ``base``, points to.

    ``None`` when the reference leaves the server (another host, ``data:``, ``mailto:``, ...)
    or stays inside the page (``#fragment``). Queries and fragments are dropped.
    """
    ref = ref.strip()
    if not ref or ref.startswith("#"):
        return None
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(_ORIGIN + base, ref))
    if f"{parts.scheme}://{parts.netloc}" != _ORIGIN:
        return None
    return parts.path or "/"


def references(path: str, page: Page) -> list[tuple[str, str]]:
    """``(server path, kind of load)`` for everything the resource at ``path`` references."""
    text = page.body.decode("utf-8", errors="replace")
    found: list[tuple[str, str]] = []
    if page.mime == WANTED_TYPES[DOCUMENT][0]:
        parser = _HtmlReferences()
        parser.feed(text)
        parser.close()
        found = parser.found
    elif page.mime in WANTED_TYPES[SCRIPT]:
        found = [
            (spec, SCRIPT)
            for spec in _JS_SPECIFIER.findall(text)
            if spec.startswith(("/", "./", "../"))
        ]
        found += [(ref, OTHER) for ref in _JS_STATIC_PATH.findall(text)]
    elif page.mime in WANTED_TYPES[STYLE]:
        found = [(ref, STYLE) for ref in _CSS_IMPORT.findall(text)]
        found += [(ref, OTHER) for ref in _CSS_URL.findall(text)]
    resolved = [(resolve(path, ref), kind) for ref, kind in found]
    return [(target, kind) for target, kind in resolved if target is not None]


def validate_config(
    inspect: dict[str, Any],
    variant: str,
    expect_version: str | None,
    expect_revision: str | None,
) -> list[str]:
    """Problems with an image's configuration (``docker image inspect`` output, one image)."""
    problems: list[str] = []

    def want(ok: bool, problem: str) -> None:
        if not ok:
            problems.append(problem)

    cfg: dict[str, Any] = inspect.get("Config") or {}
    labels: dict[str, str] = cfg.get("Labels") or {}
    env = dict(item.split("=", 1) for item in cfg.get("Env") or [])
    label = "org.opencontainers.image."
    want(inspect.get("Os") == "linux", f"os is {inspect.get('Os')!r}, expected 'linux'")
    arch = inspect.get("Architecture")
    want(arch == "amd64", f"architecture is {arch!r}, expected 'amd64'")
    want(cfg.get("User") == f"{IMAGE_UID}:{IMAGE_UID}", f"user is {cfg.get('User')!r}")
    want(cfg.get("Entrypoint") == ENTRYPOINT, f"entrypoint is {cfg.get('Entrypoint')!r}")
    want(cfg.get("Cmd") == EXPECTED_CMD[variant], f"cmd is {cfg.get('Cmd')!r}")
    want(f"{WEB_PORT}/tcp" in (cfg.get("ExposedPorts") or {}), f"port {WEB_PORT} not exposed")
    want(env.get("CCA_STOCKFISH") == STOCKFISH, f"CCA_STOCKFISH is {env.get('CCA_STOCKFISH')!r}")
    health: dict[str, Any] = cfg.get("Healthcheck") or {}
    test = health.get("Test") or []
    want(
        test[:1] == ["CMD"] and any("/healthz" in part and "ready" in part for part in test),
        f"healthcheck is {test!r}, expected an exec-form probe of /healthz that checks 'ready'",
    )
    want(
        labels.get(label + "licenses") == LICENSES,
        f"licenses label is {labels.get(label + 'licenses')!r}",
    )
    want(
        labels.get(label + "source") == SOURCE, f"source label is {labels.get(label + 'source')!r}"
    )
    description = labels.get(label + "description", "")
    want(
        0 < len(description) <= MAX_DESCRIPTION,
        f"description label has {len(description)} characters (1-{MAX_DESCRIPTION} allowed)",
    )
    version = labels.get(label + "version", "")
    want(bool(version), "version label is empty")
    if expect_version is not None:
        want(
            version == expect_version, f"version label is {version!r}, expected {expect_version!r}"
        )
    if expect_revision is not None:
        revision = labels.get(label + "revision")
        want(
            revision == expect_revision,
            f"revision label is {revision!r}, expected {expect_revision!r}",
        )
    if variant == "maia2":
        want(env.get("CCA_WEIGHTS") == WEIGHTS_DIR, f"CCA_WEIGHTS is {env.get('CCA_WEIGHTS')!r}")
        want("/data" in (cfg.get("Volumes") or {}), "/data is not a volume")
        start_period = int(health.get("StartPeriod") or 0) / 1e9
        want(
            start_period >= MIN_MAIA2_START_PERIOD_S,
            f"healthcheck start period is {start_period:g}s, "
            f"need >= {MIN_MAIA2_START_PERIOD_S}s for the first weight download",
        )
    return problems


def validate_probe(report: dict[str, Any], variant: str, *, weights_mounted: bool) -> list[str]:
    """Problems found by :data:`PROBE` inside the image."""
    problems: list[str] = []

    def want(ok: bool, problem: str) -> None:
        if not ok:
            problems.append(problem)

    dists = set(report.get("dists") or [])
    want(report.get("uid") == IMAGE_UID, f"runs as uid {report.get('uid')}, expected {IMAGE_UID}")
    want(report.get("gid") == IMAGE_UID, f"runs as gid {report.get('gid')}, expected {IMAGE_UID}")
    want(
        not report.get("unreadable"),
        f"missing or empty licence/source files: {report.get('unreadable')}",
    )
    want(
        not report.get("stale_notices"),
        f"{report.get('stale_notices')} do not describe the image (no {IMAGE_NAME!r} in them)",
    )
    want(bool(report.get("nets")), "no exported Stockfish NNUE network (nn-*.nnue) in the doc dir")
    want(
        not report.get("tools"), f"build tools present in the runtime image: {report.get('tools')}"
    )
    want(not report.get("pip_in_venv"), "pip is installed in the runtime virtual environment")
    want(not report.get("pip_in_base"), "pip is installed in the base image's Python")
    want({"cca-chess", "chess"} <= dists, "cca-chess and chess are not both installed")
    cuda = sorted(d for d in dists if d.startswith("nvidia-") or d == "triton")
    want(not cuda, f"CUDA packages installed: {cuda}")
    torch = report.get("torch")
    if variant == "maia2":
        want("maia2" in dists, "maia2 is not installed")
        want(
            isinstance(torch, str) and torch.endswith("+cpu"),
            f"torch is {torch!r}, expected a +cpu build",
        )
        want(report.get("cuda") is None, f"torch was built with CUDA {report.get('cuda')}")
        if not weights_mounted:
            want(
                report.get("weights_writable") is True,
                f"{WEIGHTS_DIR} is not writable by the image user",
            )
    else:
        want(torch is None and "maia2" not in dists, "torch/maia2 found in the default image")
    return problems


def gh_escape(text: str) -> str:
    """Escape a message for a GitHub Actions workflow command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class Smoke:
    """Runs the checks against one image."""

    def __init__(
        self,
        opts: Options,
        runner: Runner | None = None,
        fetch: Fetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.opts = opts
        self._runner = runner or run_docker
        self._fetch = fetch or http_get
        self._sleep = sleep
        self._clock = clock
        self._label_version: str | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def _real_maia2(self) -> bool:
        return self.opts.variant == "maia2" and self.opts.maia2_weights is not None

    @property
    def _human(self) -> str:
        return "maia2" if self._real_maia2 else "qre"

    def _mounts(self) -> list[str]:
        if self.opts.maia2_weights is None:
            return []
        src = self.opts.maia2_weights.resolve()
        return ["--mount", f"type=bind,source={src},target={WEIGHTS_DIR},readonly"]

    def _docker(
        self, args: Sequence[str], stdin: str | None = None, timeout: float = 300.0
    ) -> Result:
        return self._runner(args, stdin, timeout)

    def _run(self, *args: str, stdin: str | None = None, entrypoint: str | None = None) -> Result:
        """``docker run --rm`` a one-shot command in the image; raise unless it exits 0."""
        cmd = ["run", "--rm", "--no-healthcheck", *self._mounts()]
        if stdin is not None:
            cmd.append("-i")
        if entrypoint is not None:
            cmd += ["--entrypoint", entrypoint]
        res = self._docker([*cmd, self.opts.image, *args], stdin=stdin)
        if res.returncode != 0:
            raise SmokeError(f"`{' '.join(args[:1])}` exited with {res.returncode}:\n{res.tail()}")
        return res

    # ------------------------------------------------------------------ checks
    def check_config(self) -> str:
        """Image configuration: user, entrypoint, command, port, env, healthcheck, labels."""
        res = self._docker(["image", "inspect", self.opts.image], timeout=60)
        if res.returncode != 0:
            raise SmokeError(f"docker image inspect failed:\n{res.tail()}")
        inspect: dict[str, Any] = json.loads(res.stdout)[0]
        labels = (inspect.get("Config") or {}).get("Labels") or {}
        self._label_version = labels.get("org.opencontainers.image.version") or None
        problems = validate_config(
            inspect, self.opts.variant, self.opts.expect_version, self.opts.expect_revision
        )
        if problems:
            raise SmokeError("; ".join(problems))
        return f"uid {IMAGE_UID}, labels and healthcheck ok, version label {self._label_version}"

    def check_version(self) -> str:
        """``cca version`` prints the version the image is labelled with."""
        out = self._run("version").stdout.strip()
        want = self.opts.expect_version or self._label_version
        if want is None:
            raise SmokeError(f"cca version printed {out!r}, but the image has no version label")
        if out != want:
            raise SmokeError(f"cca version printed {out!r}, expected {want!r}")
        return out

    def check_doctor(self) -> str:
        """``cca doctor`` starts the bundled Stockfish 19 and exits 0."""
        out = self._run("doctor").stdout
        banner = f"engine ok: Stockfish 19 ({STOCKFISH})"
        if banner not in out:
            raise SmokeError(f"doctor output lacks {banner!r}:\n{out.strip()}")
        return banner

    def check_filesystem(self) -> str:
        """Inside the image: user, licence/source files, NNUE export, packages, tools."""
        cfg = json.dumps(
            {
                "doc_root": DOC_ROOT,
                "docs": DOC_FILES,
                "notices": NOTICE_FILES,
                "image_name": IMAGE_NAME,
                "tools": BUILD_TOOLS,
            }
        )
        res = self._run("-c", PROBE, cfg, entrypoint=VENV_PYTHON)
        try:
            report: dict[str, Any] = json.loads(res.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise SmokeError(f"probe printed no JSON:\n{res.stdout}{res.stderr}") from exc
        problems = validate_probe(
            report, self.opts.variant, weights_mounted=self.opts.maia2_weights is not None
        )
        if problems:
            raise SmokeError("; ".join(problems))
        torch = f", torch {report['torch']}" if report.get("torch") else ""
        return f"uid/gid {IMAGE_UID}, docs ok, nets {report['nets']}{torch}"

    def check_uci(self) -> str:
        """``cca uci`` answers the handshake (uciok, readyok) over stdin/stdout."""
        commands = ["uci"]
        if self.opts.variant == "maia2" and not self._real_maia2:
            commands.append("setoption name CCA_HumanModel value qre")
        commands += ["isready", "quit"]
        res = self._run("uci", stdin="\n".join(commands) + "\n")
        lines = [line.strip() for line in res.stdout.splitlines()]
        problems = [f"no {word}" for word in ("uciok", "readyok") if word not in lines]
        if not any(line.startswith("id name ") for line in lines):
            problems.append("no 'id name' line")
        problems += [line for line in lines if line.startswith("info string error")]
        if self._real_maia2:
            problems += [line for line in lines if "maia2 unavailable" in line]
        if problems:
            raise SmokeError("; ".join(problems) + "\n" + res.stdout.strip())
        return f"uciok + readyok ({self._human} human model)"

    def check_analyse(self) -> str:
        """``cca analyse`` makes one full decision (Stockfish + human model) from the start."""
        res = self._run(
            "analyse", START_FEN, "--human", self._human, "--device", "cpu",
            "--nodes", "20000", "--seed", "smoke", "--argmax",
        )  # fmt: skip
        try:
            decision = json.loads(res.stdout)
        except ValueError as exc:
            raise SmokeError(f"analyse printed no JSON:\n{res.stdout}") from exc
        move = decision.get("move") if isinstance(decision, dict) else None
        policy = decision.get("policy") if isinstance(decision, dict) else None
        if not (
            isinstance(move, str)
            and UCI_MOVE.fullmatch(move)
            and isinstance(policy, dict)
            and move in policy
        ):
            raise SmokeError(f"analyse returned no legal-looking move: {res.stdout[:300]}")
        if self._real_maia2 and "falling back" in res.stderr:
            raise SmokeError(f"analyse fell back from Maia-2:\n{res.tail()}")
        return f"move {move} ({self._human} human model)"

    def check_web(self) -> str:
        """The web server turns healthy, /healthz answers 200 ok and the page loads completely.

        When /healthz also reports ``ready`` (engines built), the check waits for it too. The
        page itself is checked by :meth:`_check_page`.
        """
        name = f"cca-smoke-{secrets.token_hex(4)}"
        try:
            port, healthy, ready = self._serve(name)
            urls, vendored = self._check_page(port)
        except SmokeError as exc:
            logs = self._docker(["logs", "--tail", "40", name], timeout=30)
            raise SmokeError(f"{exc}\n--- container logs ---\n{logs.stdout}{logs.stderr}") from None
        finally:
            self._docker(["rm", "-f", "-v", name], timeout=60)
        return (
            f"healthy after {healthy:.0f}s, ready after {ready:.0f}s, "
            f"GET 127.0.0.1:{port}/healthz -> 200 ok; page: {urls} URLs 200 with usable types, "
            f"{vendored} vendored files match MANIFEST.json"
        )

    def _check_page(self, port: int) -> tuple[int, int]:
        """Load the page like a browser; return (URLs loaded, vendored files verified)."""
        pages, problems = self._crawl(port)
        vendored, more = self._check_vendored(port, pages)
        problems = list(dict.fromkeys(problems + more))
        if problems:
            extra = len(problems) - MAX_LISTED
            more_text = f" (and {extra} more)" if extra > 0 else ""
            raise SmokeError(
                f"the web page is broken: {'; '.join(problems[:MAX_LISTED])}{more_text}"
            )
        return len(pages), vendored

    def _crawl(self, port: int) -> tuple[dict[str, Page], list[str]]:
        """GET / and, transitively, every same-server resource it loads or links to."""
        pages: dict[str, Page] = {}
        loads: dict[str, dict[str, str]] = {"/": {DOCUMENT: ""}}  # path -> {kind: referrer}
        queue = deque(["/"])
        while queue:
            path = queue.popleft()
            page = pages[path] = self._fetch(port, path, 10.0)
            if page.status != 200:
                continue
            for target, kind in references(path, page):
                if target not in loads:
                    if len(loads) >= MAX_URLS:
                        raise SmokeError(f"the page references more than {MAX_URLS} URLs")
                    loads[target] = {}
                    queue.append(target)
                loads[target].setdefault(kind, path)
        problems: list[str] = []
        for path, kinds in loads.items():
            page = pages[path]
            for kind, referrer in kinds.items():
                where = f"{path} (from {referrer})" if referrer else path
                wanted = WANTED_TYPES.get(kind)
                if page.status != 200:
                    problems.append(f"{where}: HTTP {page.status}")
                    break
                if wanted is not None and page.mime not in wanted:
                    problems.append(
                        f"{where}: served as {page.mime or 'no type'!r}, a {kind} needs {wanted[0]}"
                    )
        return pages, problems

    def _check_vendored(self, port: int, pages: dict[str, Page]) -> tuple[int, list[str]]:
        """Every file in the vendor manifest is served with the SHA-256 it lists."""

        def get(path: str) -> Page:
            if path not in pages:
                pages[path] = self._fetch(port, path, 10.0)
            return pages[path]

        manifest = get(VENDOR_MANIFEST)
        if manifest.status != 200:
            return 0, [f"{VENDOR_MANIFEST}: HTTP {manifest.status}"]
        try:
            files = json.loads(manifest.body)["files"]
        except (ValueError, LookupError, TypeError):
            files = None
        if not isinstance(files, list) or not files:
            return 0, [f"{VENDOR_MANIFEST} has no 'files' list"]
        problems: list[str] = []
        for entry in files:
            rel = entry.get("path") if isinstance(entry, dict) else None
            digest = entry.get("sha256") if isinstance(entry, dict) else None
            if not isinstance(rel, str) or not isinstance(digest, str):
                problems.append(f"{VENDOR_MANIFEST} entry without path and sha256: {entry!r:.80}")
                continue
            page = get(VENDOR_DIR + rel)
            if page.status != 200:
                problems.append(f"{VENDOR_DIR}{rel} (in MANIFEST.json): HTTP {page.status}")
            elif hashlib.sha256(page.body).hexdigest() != digest.lower():
                problems.append(f"{VENDOR_DIR}{rel}: SHA-256 differs from MANIFEST.json")
        return len(files), problems

    def _serve(self, name: str) -> tuple[int, float, float]:
        host_port = "" if self.opts.port is None else str(self.opts.port)
        cmd = ["run", "-d", "--name", name, "-p", f"127.0.0.1:{host_port}:{WEB_PORT}"]
        cmd += [*self._mounts(), self.opts.image]
        if self.opts.variant == "maia2" and not self._real_maia2:
            cmd += QRE_WEB_CMD
        start = self._clock()
        started = self._docker(cmd, timeout=120)
        if started.returncode != 0:
            raise SmokeError(f"docker run failed:\n{started.tail()}")
        port = self._published_port(name)
        healthy = self._wait_healthy(name, start)
        while self._healthz(port).get("ready", True) is not True:
            if self._clock() - start >= self.opts.health_timeout:
                raise SmokeError(f"engines not ready after {self.opts.health_timeout:g}s")
            self._sleep(2.0)
        return port, healthy, self._clock() - start

    def _published_port(self, name: str) -> int:
        """The host port Docker bound to the container's web port (``docker port``)."""
        res = self._docker(["port", name, f"{WEB_PORT}/tcp"], timeout=30)
        lines = res.stdout.split()
        if res.returncode != 0 or not lines:
            raise SmokeError(f"docker port printed no binding for {WEB_PORT}/tcp:\n{res.tail()}")
        host, _, port = lines[0].rpartition(":")
        if host != "127.0.0.1" or not port.isdigit():
            raise SmokeError(f"{WEB_PORT}/tcp is published on {lines[0]!r}, expected 127.0.0.1")
        return int(port)

    def _healthz(self, port: int) -> dict[str, Any]:
        page = self._fetch(port, "/healthz", 10.0)
        if page.status != 200:
            raise SmokeError(f"GET /healthz returned HTTP {page.status}: {page.body[:300]!r}")
        try:
            payload = json.loads(page.body)
        except ValueError as exc:
            raise SmokeError(f"GET /healthz returned no JSON: {page.body[:200]!r}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise SmokeError(f"GET /healthz returned {payload!r}, expected status 'ok'")
        return payload

    def _wait_healthy(self, name: str, start: float) -> float:
        while True:
            res = self._docker(["inspect", "--format", "{{json .State}}", name], timeout=30)
            if res.returncode != 0:
                raise SmokeError(f"docker inspect failed:\n{res.tail()}")
            state: dict[str, Any] = json.loads(res.stdout)
            health: dict[str, Any] = state.get("Health") or {}
            status = health.get("Status")
            if state.get("Status") != "running":
                raise SmokeError(
                    f"container is {state.get('Status')} (exit code {state.get('ExitCode')})"
                )
            if status == "healthy":
                return self._clock() - start
            if status is None:
                raise SmokeError("the container has no healthcheck")
            if status == "unhealthy":
                log = health.get("Log") or [{}]
                raise SmokeError(
                    f"container is unhealthy: {str(log[-1].get('Output', '')).strip()}"
                )
            if self._clock() - start >= self.opts.health_timeout:
                raise SmokeError(
                    f"not healthy after {self.opts.health_timeout:g}s (still {status})"
                )
            self._sleep(2.0)

    def checks(self) -> list[tuple[str, Callable[[], str]]]:
        """All checks, in the order they run."""
        return [
            ("config", self.check_config),
            ("version", self.check_version),
            ("doctor", self.check_doctor),
            ("filesystem", self.check_filesystem),
            ("uci", self.check_uci),
            ("analyse", self.check_analyse),
            ("web", self.check_web),
        ]

    def run_all(self, out: TextIO) -> int:
        """Run every check, report each one, return the process exit status."""
        annotate = os.environ.get("GITHUB_ACTIONS") == "true"
        failed = 0
        for name, check in self.checks():
            try:
                detail = check()
            except (SmokeError, LookupError, TypeError, ValueError) as exc:
                # Anything but SmokeError means the image answered in an unexpected shape.
                message = (
                    str(exc) if isinstance(exc, SmokeError) else f"{type(exc).__name__}: {exc}"
                )
                failed += 1
                print(f"FAIL {name}: {message}", file=out, flush=True)
                if annotate:
                    print(f"::error title=smoke {name}::{gh_escape(message)}", file=out, flush=True)
            else:
                print(f"ok   {name}: {detail}", file=out, flush=True)
        total = len(self.checks())
        print(
            f"{self.opts.image} ({self.opts.variant}): {total - failed}/{total} checks passed",
            file=out,
        )
        return 1 if failed else 0


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """Command-line options."""
    ap = argparse.ArgumentParser(
        prog="smoke.py",
        description="Smoke-test a CCA container image.",
    )
    ap.add_argument(
        "image", help="image reference, e.g. cca:local or ghcr.io/donquaan/cca@sha256:..."
    )
    ap.add_argument("--variant", choices=sorted(EXPECTED_CMD), default="default")
    ap.add_argument("--expect-version", help="required org.opencontainers.image.version label")
    ap.add_argument("--expect-revision", help="required org.opencontainers.image.revision label")
    ap.add_argument(
        "--port",
        type=int,
        help="host port on 127.0.0.1 for the web check (default: a free one Docker picks)",
    )
    ap.add_argument(
        "--health-timeout", type=float, default=240.0, help="seconds to wait for 'healthy'"
    )
    ap.add_argument(
        "--maia2-weights",
        type=Path,
        help="directory with the Maia-2 checkpoint(s), mounted read-only (maia2 variant only)",
    )
    ns = ap.parse_args(argv)
    if ns.port is not None and not 0 < ns.port < 2**16:
        ap.error(f"--port {ns.port} is not a TCP port (1-65535)")
    if ns.maia2_weights is not None:
        if ns.variant != "maia2":
            ap.error("--maia2-weights needs --variant maia2")
        if not ns.maia2_weights.is_dir():
            ap.error(f"--maia2-weights {ns.maia2_weights} is not a directory")
    return Options(
        image=ns.image,
        variant=ns.variant,
        expect_version=ns.expect_version,
        expect_revision=ns.expect_revision,
        port=ns.port,
        health_timeout=ns.health_timeout,
        maia2_weights=ns.maia2_weights,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    return Smoke(parse_args(argv)).run_all(sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
