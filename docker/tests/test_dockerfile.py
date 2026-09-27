"""Checks on the Dockerfile itself that need no Docker engine.

The image-level checks live in docker/smoke.py; these catch drift between the Dockerfile and
smoke.py earlier, and they run the HEALTHCHECK probe (plain Python) against a stub /healthz.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
import smoke

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
TARGETS = {"default": "runtime", "maia2": "runtime-maia2"}
PROBE_URL = f"http://127.0.0.1:{smoke.WEB_PORT}/healthz"


def stages(text: str) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """``{stage: (parent, [(INSTRUCTION, arguments), ...])}``, continuation lines joined."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append(pending + line)
        pending = ""
    assert not pending, "the Dockerfile ends inside a continuation"
    out: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    current: list[tuple[str, str]] | None = None
    for line in logical:
        word, _, rest = line.partition(" ")
        if word.upper() == "FROM":
            match = re.fullmatch(r"(\S+) AS (\S+)", rest.strip(), flags=re.IGNORECASE)
            assert match, f"every FROM names its stage: {line}"
            current = []
            out[match[2]] = (match[1], current)
        else:
            assert current is not None, f"instruction before FROM: {line}"
            current.append((word.upper(), rest.strip()))
    return out


def effective(target: str, instruction: str) -> str:
    """The last value of ``instruction`` in ``target`` or the stages it is built on."""
    all_stages = stages(DOCKERFILE.read_text(encoding="utf-8"))
    chain: list[str] = []
    name = target
    while name in all_stages:
        chain.insert(0, name)
        name = all_stages[name][0]
    values = [arg for stage in chain for word, arg in all_stages[stage][1] if word == instruction]
    assert values, f"{target} has no {instruction}"
    return values[-1]


def healthcheck(target: str) -> tuple[dict[str, str], list[str]]:
    """``(--options, exec-form command)`` of the target's HEALTHCHECK."""
    options, _, command = effective(target, "HEALTHCHECK").partition(" CMD ")
    opts = dict(opt.removeprefix("--").split("=", 1) for opt in options.split())
    argv: list[str] = json.loads(command)
    return opts, argv


def seconds(duration: str) -> float:
    match = re.fullmatch(r"(\d+)(s|m)", duration)
    assert match, duration
    return int(match[1]) * (60 if match[2] == "m" else 1)


def ignore_pattern(pattern: str) -> re.Pattern[str]:
    """A .dockerignore pattern as a regex over slash-separated context paths."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        elif pattern[i] == "[":
            end = pattern.index("]", i)
            out, i = out + pattern[i : end + 1], end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def in_build_context(path: str) -> bool:
    """Whether .dockerignore lets ``path`` into the build context.

    Docker's rules: a pattern that matches a directory also covers everything below it, ``!``
    re-includes, and the last matching pattern wins.
    """
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            keep = line.startswith("!")
            rules.append((keep, ignore_pattern(line.removeprefix("!").strip().strip("/"))))
    parts = path.strip("/").split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    included = True
    for keep, rule in rules:
        if any(rule.fullmatch(prefix) for prefix in prefixes):
            included = keep
    return included


def test_dockerignore_matcher() -> None:
    assert in_build_context("src/cca/cli.py")
    assert in_build_context("engines/stockfish.lock.json")
    assert not in_build_context("src/cca/__pycache__/cli.cpython-312.pyc")
    assert not in_build_context("src/cca/stray.pyc")
    assert not in_build_context("engines/stockfish-linux-x86-64-universal/stockfish/x")
    assert not in_build_context("CLAUDE.md")
    assert not in_build_context(".git/HEAD")
    assert not in_build_context("weights/maia2/rapid_model.pt")
    assert not in_build_context("docker/smoke.py")  # CI runs it from the checkout, not the image


def test_every_copied_file_exists_and_is_in_the_build_context() -> None:
    # A COPY of a file that .dockerignore leaves out fails only when the image is built.
    sources: list[str] = []
    for _parent, instructions in stages(DOCKERFILE.read_text(encoding="utf-8")).values():
        for word, arg in instructions:
            tokens = arg.split()
            if word in {"COPY", "ADD"} and not any(t.startswith("--from=") for t in tokens):
                sources += [t for t in tokens[:-1] if not t.startswith("--")]
    assert "docker/check_dist.py" in sources
    assert "docker/fetch_pinned.py" in sources
    for source in sources:
        assert (ROOT / source).exists(), f"COPY {source}: no such file in the repository"
        assert in_build_context(source), f"COPY {source}: .dockerignore keeps it out of the context"


def test_the_build_stage_checks_the_wheel_for_left_out_files() -> None:
    build = stages(DOCKERFILE.read_text(encoding="utf-8"))["build"][1]
    runs = " ".join(arg for word, arg in build if word == "RUN")
    assert 'python docker/check_dist.py --package src/cca "$wheel"' in runs


def test_runtime_stages_install_nothing_from_package_indexes() -> None:
    all_stages = stages(DOCKERFILE.read_text(encoding="utf-8"))
    for target in TARGETS.values():
        for word, arg in all_stages[target][1]:
            if word == "RUN":
                assert not re.search(r"\b(apt-get|apt|pip install|uv)\b", arg), (target, arg)


def test_the_entrypoint_is_the_pinned_tini() -> None:
    all_stages = stages(DOCKERFILE.read_text(encoding="utf-8"))
    copies = [arg for word, arg in all_stages["runtime"][1] if word == "COPY"]
    assert f"--from=tini /out/bin/tini {smoke.ENTRYPOINT[0]}" in copies
    assert "--from=tini /out/doc/ /usr/local/share/doc/tini/" in copies
    assert "tini/LICENSE" in smoke.DOC_FILES
    runs = " ".join(arg for word, arg in all_stages["tini"][1] if word == "RUN")
    pins = re.findall(r"(https://\S+)\s+([0-9a-f]{64})\s+(/out/[^\s;]+)", runs)
    assert [dest for _url, _sha, dest in pins] == ["/out/bin/tini", "/out/doc/LICENSE"]
    assert all("/v0.19.0/" in url for url, _sha, _dest in pins)
    assert '"$(/out/bin/tini --version)" = "tini version 0.19.0 - git.de40ad0"' in runs


@pytest.mark.parametrize("variant", sorted(TARGETS))
def test_command_line_user_and_port_match_smoke(variant: str) -> None:
    target = TARGETS[variant]
    assert json.loads(effective(target, "ENTRYPOINT")) == smoke.ENTRYPOINT
    assert json.loads(effective(target, "CMD")) == smoke.EXPECTED_CMD[variant]
    assert effective(target, "USER") == f"{smoke.IMAGE_UID}:{smoke.IMAGE_UID}"
    assert effective(target, "EXPOSE") == str(smoke.WEB_PORT)


def test_both_images_probe_health_the_same_way_with_room_for_the_first_download() -> None:
    default_opts, default_argv = healthcheck("runtime")
    maia2_opts, maia2_argv = healthcheck("runtime-maia2")
    assert default_argv == maia2_argv
    assert default_argv[:2] == ["/usr/local/bin/python", "-c"]
    assert seconds(maia2_opts["start-period"]) >= smoke.MIN_MAIA2_START_PERIOD_S
    http_timeout = re.search(r"timeout=(\d+)\)", default_argv[2])
    assert http_timeout, "the probe sets its own HTTP timeout"
    for opts in (default_opts, maia2_opts):
        assert int(http_timeout[1]) < seconds(opts["timeout"])  # fails before Docker kills it


class Healthz(http.server.BaseHTTPRequestHandler):
    """Answers every GET with the class-level ``answer``."""

    answer: ClassVar[tuple[int, bytes]] = (200, b"{}")

    def do_GET(self) -> None:
        status, body = self.answer
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""


@pytest.fixture(scope="module")
def healthz_port() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Healthz)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def run_probe(port: int, env: dict[str, str] | None = None) -> int:
    """Exit status of the image's HEALTHCHECK code, pointed at 127.0.0.1:PORT."""
    code = healthcheck("runtime")[1][2]
    assert PROBE_URL in code
    code = code.replace(PROBE_URL, f"http://127.0.0.1:{port}/healthz")
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=60, check=False, env=env
    )
    return proc.returncode


@pytest.mark.parametrize(
    ("answer", "healthy"),
    [
        ((200, b'{"status": "ok", "version": "0.1.0", "ready": true}'), True),
        ((200, b'{"status": "ok"}'), True),  # the minimal contract: no "ready" field
        ((200, b'{"status": "ok", "ready": false}'), False),  # engines still warming up
        ((503, b'{"status": "error", "ready": false, "error": "boom"}'), False),
        ((200, b'{"status": "starting"}'), False),
        ((200, b'{"status": "ok", "ready": "yes"}'), False),
        ((200, b"<html>"), False),
    ],
    ids=["ready", "no-ready-field", "warming-up", "503", "not-ok", "ready-not-bool", "not-json"],
)
def test_healthcheck_probe(answer: tuple[int, bytes], healthy: bool, healthz_port: int) -> None:
    Healthz.answer = answer
    assert (run_probe(healthz_port) == 0) is healthy


def test_healthcheck_probe_fails_when_nothing_listens() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), Healthz)
    port = int(server.server_address[1])
    server.server_close()  # the port is free again: the connection is refused
    assert run_probe(port) != 0


def test_healthcheck_probe_ignores_proxies_from_the_environment(healthz_port: int) -> None:
    # Docker can pass http_proxy into containers (~/.docker/config.json "proxies"); without
    # 127.0.0.1 in no_proxy, urlopen would ask the proxy and the container would turn unhealthy.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_proxy = f"http://127.0.0.1:{sock.getsockname()[1]}"
    env = {k: v for k, v in os.environ.items() if k.lower() not in {"no_proxy", "http_proxy"}}
    env |= {"http_proxy": dead_proxy, "HTTP_PROXY": dead_proxy, "all_proxy": dead_proxy}
    Healthz.answer = (200, b'{"status": "ok", "ready": true}')
    assert run_probe(healthz_port, env) == 0


# ------------------------------------------------------------------ build-stage shell code, run
# The RUN steps below guard the build (hash pins, uv.lock freshness, the CUDA-free torch set, the
# NNUE file, the Stockfish banner). A real build is the only full test, so their shell code is also
# run here, with the same SHELL flags, against stubs of uv and Stockfish.


def run_body(stage: str, marker: str) -> str:
    """The one RUN in ``stage`` that contains ``marker``, without its ``--mount`` options."""
    runs = [
        arg
        for word, arg in stages(DOCKERFILE.read_text(encoding="utf-8"))[stage][1]
        if word == "RUN" and marker in arg
    ]
    assert len(runs) == 1, f"{stage}: {len(runs)} RUN steps contain {marker!r}"
    return re.sub(r"^(?:--\S+\s+)+", "", runs[0])


def run_bash(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in ``cwd`` with bash and the Dockerfile's SHELL options."""
    bash = shutil.which("bash")
    if bash is None or (os.name == "nt" and "\\windows\\" in bash.lower()):
        # C:\Windows\System32\bash.exe starts WSL; it is not a bash for this filesystem.
        pytest.skip("needs bash (Linux, or Git for Windows' bash first on PATH)")
    shell = json.loads(effective("build", "SHELL"))
    assert shell[0] == "/bin/bash", shell
    assert shell[-1] == "-c", shell
    (cwd / "step.sh").write_text(script, encoding="utf-8", newline="\n")
    full_env = dict(os.environ, **env)
    full_env["PATH"] = str(Path(bash).parent) + os.pathsep + full_env.get("PATH", "")
    return subprocess.run(
        [bash, *shell[1:-1], "step.sh"],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


# `uv export`, scripted: requirement lines come from stub/base.txt (+ stub/maia2.txt with
# --extra maia2), with a --hash unless --no-hashes; --no-emit-package drops a package (except
# $STUB_KEEP, standing for a package that slips through). Without --locked it refuses.
STUB_UV = r"""
uv() {
  [ "$1" = export ] || { echo "stub uv: only 'uv export' is scripted: $*" >&2; return 2; }
  shift
  local locked=0 hashes=1 extra=0 out="" skip=" " line name src text=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --locked) locked=1 ;;
      --no-hashes) hashes=0 ;;
      --extra) if [ "$2" = maia2 ]; then extra=1; fi; shift ;;
      --no-emit-package) skip+="$2 "; shift ;;
      --output-file) out=$2; shift ;;
      --format) shift ;;
    esac
    shift
  done
  if [ "$locked" != 1 ]; then echo "stub uv: export without --locked" >&2; return 3; fi
  src=$(cat stub/base.txt; if [ "$extra" = 1 ]; then cat stub/maia2.txt; fi)
  while IFS= read -r line; do
    name=${line%%[=; ]*}
    case "$skip" in *" $name "*) if [ "$name" != "${STUB_KEEP:-}" ]; then continue; fi ;; esac
    if [ "$hashes" = 1 ]; then line="$line --hash=sha256:$STUB_HASH"; fi
    text+="$line"$'\n'
  done <<<"$src"
  if [ -n "$out" ]; then printf '%s' "$text" > "$out"; else printf '%s' "$text"; fi
}
"""


def locked_versions() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if "version" in p}


def export_step(tmp_path: Path, torch_cpu: str | None = None, **env: str) -> tuple[int, str]:
    """Run the build stage's ``uv export`` step with the stub; ``(exit status, stderr)``."""
    versions = locked_versions()
    cuda = sorted(n for n in versions if n in {"torch", "triton"} or n.startswith("nvidia-"))
    assert "torch" in cuda, "uv.lock pins no torch"
    linux = " ; platform_machine == 'x86_64' and sys_platform == 'linux'"
    (tmp_path / "stub").mkdir()
    maia2 = [f"maia2=={versions['maia2']}"] + [f"{n}=={versions[n]}{linux}" for n in cuda]
    (tmp_path / "docker").mkdir()
    real = (ROOT / "docker" / "torch-cpu.txt").read_text(encoding="utf-8")
    for rel, text in (
        ("stub/base.txt", f"chess=={versions['chess']}\n"),
        ("stub/maia2.txt", "\n".join(maia2) + "\n"),
        ("docker/torch-cpu.txt", torch_cpu or real),
    ):
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "dist").mkdir()
    body = run_body("build", "uv export").replace("/dist/", "dist/")
    proc = run_bash(STUB_UV + body + "\n", tmp_path, STUB_HASH="ab" * 32, **env)
    return proc.returncode, proc.stderr


def requirement_names(path: Path) -> set[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all("--hash=sha256:" in line for line in lines), f"{path.name} has unhashed lines"
    return {re.split(r"[=; ]", line, maxsplit=1)[0] for line in lines}


def test_the_export_step_writes_hashed_sets_and_drops_torch_and_cuda_for_maia2(
    tmp_path: Path,
) -> None:
    # The stub refuses an export without --locked, and the torch version comes from uv.lock, so
    # this also checks that docker/torch-cpu.txt pins the torch of uv.lock.
    status, stderr = export_step(tmp_path)
    assert status == 0, stderr
    assert requirement_names(tmp_path / "dist" / "req-base.txt") == {"chess"}
    assert requirement_names(tmp_path / "dist" / "req-maia2.txt") == {"chess", "maia2"}


def test_the_export_step_refuses_a_torch_cpu_pin_of_another_version(tmp_path: Path) -> None:
    real = (ROOT / "docker" / "torch-cpu.txt").read_text(encoding="utf-8")
    torch = locked_versions()["torch"]
    other = real.replace(f"/torch-{torch}%2Bcpu-", "/torch-0.0.1%2Bcpu-")
    assert other != real
    status, stderr = export_step(tmp_path, torch_cpu=other)
    assert status != 0
    assert f"docker/torch-cpu.txt does not pin torch {torch}+cpu" in stderr


def test_the_export_step_stops_when_a_cuda_package_slips_through(tmp_path: Path) -> None:
    status, stderr = export_step(tmp_path, STUB_KEEP="triton")
    assert status != 0
    assert "CUDA packages left in the maia2 requirement set" in stderr


def test_every_requirement_file_install_checks_hashes() -> None:
    # uv only checks the hashes a file happens to carry unless --require-hashes is given.
    for stage, marker in (("venv", "uv pip install"), ("venv-maia2", "uv pip install")):
        body = run_body(stage, marker)
        assert "--build-constraints docker/build-constraints.txt" in body, stage
        with_files = [cmd for cmd in body.split(";") if " -r " in cmd]
        assert with_files, stage
        for cmd in with_files:
            assert "--require-hashes" in cmd, f"{stage}: {cmd.strip()}"
    build = run_body("build", "uv build")
    assert "--build-constraints docker/build-constraints.txt --require-hashes" in build


# Stockfish, scripted: `export_net` writes a net named after its SHA-256 and reports success.
# $STUB_NET breaks one thing at a time: misnamed (wrong file name), none (success reported, no
# file), fail (a good file, but a failure reported). `uci` answers with "id name $STUB_NAME";
# `isready` answers readyok unless STUB_READY=0.
STUB_STOCKFISH = r"""#!/bin/bash
while IFS= read -r cmd; do
  case "$cmd" in
    export_net)
      if [ "${STUB_NET:-good}" != none ]; then
        printf 'stub network\n' > net.tmp
        sha=$(sha256sum net.tmp | cut -c1-12)
        if [ "${STUB_NET:-good}" = misnamed ]; then sha=000000000000; fi
        mv net.tmp "nn-$sha.nnue"
      fi
      if [ "${STUB_NET:-good}" = fail ]; then
        echo "Failed to export a net"
      else
        echo "Network saved successfully to nn-${sha:-000000000000}.nnue"
      fi ;;
    uci) printf 'id name %s\nuciok\n' "${STUB_NAME:-Stockfish 19}" ;;
    isready) if [ "${STUB_READY:-1}" = 1 ]; then echo readyok; fi ;;
    quit) exit 0 ;;
  esac
done
"""


def stockfish_checks(tmp_path: Path, **env: str) -> int:
    """Run the Stockfish stage's steps after `cd /out/doc` (net export + checks) on the stub."""
    body = run_body("stockfish", "export_net")
    start = body.index("cd /out/doc;") + len("cd /out/doc;")
    script = body[start:].replace("/out/bin/stockfish", "./stockfish-stub").replace(" /out;", " .;")
    assert "/out" not in script, script
    stub = tmp_path / "stockfish-stub"
    stub.write_text(STUB_STOCKFISH, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    return run_bash(script + "\n", tmp_path, **env).returncode


@pytest.mark.parametrize(
    ("env", "ok"),
    [
        ({}, True),
        ({"STUB_NET": "misnamed"}, False),  # file name is not the first 12 hex of its SHA-256
        ({"STUB_NET": "none"}, False),  # no nn-*.nnue written
        ({"STUB_NET": "fail"}, False),  # export_net reported no success
        ({"STUB_NAME": "Stockfish 18"}, False),  # another Stockfish than the lock's
        ({"STUB_READY": "0"}, False),
    ],
    ids=["good", "misnamed-net", "no-net", "export-failed", "wrong-banner", "no-readyok"],
)
def test_the_stockfish_stage_checks_its_net_and_banner(
    tmp_path: Path, env: dict[str, str], ok: bool
) -> None:
    assert (stockfish_checks(tmp_path, **env) == 0) is ok
    if ok:
        (net,) = tmp_path.glob("nn-*.nnue")
        assert net.name == f"nn-{hashlib.sha256(net.read_bytes()).hexdigest()[:12]}.nnue"


def test_the_runtime_image_drops_the_base_images_pip() -> None:
    body = run_body("runtime", "pip uninstall")
    assert body.startswith("python -m pip uninstall --yes pip;"), body
    assert 'importlib.util.find_spec("pip") is not None' in body


def test_every_required_doc_file_is_copied_into_the_image() -> None:
    # smoke.DOC_FILES is checked inside the built image; here each entry is traced to the COPY
    # (and, for a file made in a build stage, the RUN) that puts it there.
    all_stages = stages(DOCKERFILE.read_text(encoding="utf-8"))
    copies = [arg.split() for word, arg in all_stages["runtime"][1] if word == "COPY"]
    for rel in smoke.DOC_FILES:
        top, _, name = rel.partition("/")
        dest = f"{smoke.DOC_ROOT}/{top}/"
        matches = [c for c in copies if c[-1] == dest]
        assert len(matches) == 1, f"{rel}: {len(matches)} COPY instructions into {dest}"
        copy = matches[0]
        stage = next((t.removeprefix("--from=") for t in copy if t.startswith("--from=")), None)
        if stage is None:
            assert name in copy[:-1], f"{rel}: COPY into {dest} does not name {name}"
        elif stage == "tini":
            runs = " ".join(arg for word, arg in all_stages[stage][1] if word == "RUN")
            assert f"/out/doc/{name}" in runs, f"{rel}: the tini stage does not fetch it"
        else:
            assert (stage, copy[-2]) == ("stockfish", "/out/doc/"), copy  # the whole archive


def test_the_stockfish_corresponding_source_includes_its_build_files() -> None:
    # GPL-3.0 section 1: the Corresponding Source includes the scripts that control compilation.
    assert {"stockfish/Copying.txt", "stockfish/src/Makefile"} <= set(smoke.DOC_FILES)
