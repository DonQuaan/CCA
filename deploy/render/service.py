"""Deploy a released CCA image to the Render web service of the public demo, then check it.

.github/workflows/deploy-render.yml runs these steps and docs/deploy.md shows them by hand.
Standard library only (the runner's system Python runs it)::

    python deploy/render/service.py check-config --url https://NAME.onrender.com --hook-present true
    python deploy/render/service.py version v0.2.0             # prints 0.2.0
    python deploy/render/service.py blueprint-check [0.2.0]    # needs the tag v0.2.0
    python deploy/render/service.py digest 0.2.0               # prints sha256:<64 hex>
    python deploy/render/service.py trigger sha256:<64 hex>    # hook URL from the environment
    python deploy/render/service.py wait https://NAME.onrender.com 0.2.0 [--timeout 600]

``version`` and ``digest`` are those of deploy/huggingface/space.py, loaded from there: one
registry client for both deploys. ``digest`` pulls the manifest of the release image anonymously,
which proves that the tag exists, and hashes it. ``trigger`` sends the service's deploy hook
(read from the environment variable ``RENDER_DEPLOY_HOOK_URL``, never from the command line) a
POST with ``imgURL`` set to the image pinned by that digest: "you can optionally specify a tag
or digest by appending an imgURL query parameter to the deploy hook URL"
(https://render.com/docs/deploying-an-image). The hook URL, its key and the response body are
never printed. ``blueprint-check`` compares the options of the Blueprint's command here with
those of the Blueprint in a release's tag (the release, by default, that the Blueprint's image
tag names): the service keeps the command of its last Blueprint sync whatever image a deploy
names. ``wait`` polls ``https://HOST/healthz`` until it reports the version with its
engine ready. Every request is HTTPS and follows no redirect. Errors go to stderr, as GitHub
Actions error annotations when run there.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # the repository
BLUEPRINT = "deploy/render/render.yaml"  # relative to ROOT, as in a git tree


def _load(name: str, path: Path) -> ModuleType:
    """A helper module of this repository, loaded from its file."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in the repository
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


# deploy/huggingface/space.py: the registry and HTTPS helpers this deploy shares.
space = _load("cca_render_space", HERE.parent / "huggingface" / "space.py")
# deploy/render/blueprint.py: the Blueprint reader docker/constrained.py uses too.
blueprint = _load("cca_render_blueprint", HERE / "blueprint.py")

IMAGE: str = space.IMAGE  # ghcr.io/donquaan/cca
HOOK_ENV = "RENDER_DEPLOY_HOOK_URL"
HOOK_HOST = "api.render.com"
# https://api.render.com/deploy/srv-XXYYZZ?key=AABBCC (https://render.com/docs/deploy-hooks)
HOOK_PATH = re.compile(r"/deploy/srv-[A-Za-z0-9]+")
HOOK_KEY = re.compile(r"[A-Za-z0-9._~-]+")
DEPLOY_ID = re.compile(r"dep-[A-Za-z0-9]+")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TOP = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"  # a top-level label starts with a letter: no IPs
# A public host name with at least two labels, lower case (the service's onrender.com name or a
# custom domain); also what keeps it safe inside a URL.
HOST_NAME = re.compile(rf"(?:{_LABEL}\.)+{_TOP}")
TIMEOUT_S = 30.0
USER_AGENT = {"User-Agent": "cca-deploy-render (+https://github.com/DonQuaan/CCA)"}
ATTEMPTS = 3
PAUSE_S = 10.0
MAX_BODY = 64 << 10  # bytes; a deploy hook answer or /healthz is a few hundred
_EPS = 1e-9  # time.monotonic() ticks coarsely on Windows: compare deadlines with a tolerance
# What each refusal of the deploy hook means (https://render.com/docs/deploy-hooks; for a bad
# imgURL, https://render.com/docs/deploying-an-image says 404 where deploy-hooks says 400).
HOOK_REFUSALS = {
    400: "Render refused the image: imgURL must name the service's own image "
    f"({IMAGE}) with another tag or digest",
    401: "Render refused the hook's key: copy the deploy hook again from the service's "
    "Settings into the secret RENDER_DEPLOY_HOOK_URL (it changes when it is regenerated)",
    404: f"Render found no such service, or refused the image (the service's image must be "
    f"{IMAGE}): check the hook and the service's image URL",
    405: "Render refused the method (the hook takes GET or POST)",
    409: "the service is suspended or cannot be deployed now (free instance hours or "
    "bandwidth used up?): see the Render dashboard",
}


class RenderError(Exception):
    """A deploy step cannot succeed as configured (bad input, refused hook, service down...)."""


class _RetryableError(Exception):
    """An answer worth asking again for (rate limit, server error)."""


class Answer(Protocol):
    """An HTTP answer: this module's :class:`Reply` or space.py's, header names lower case."""

    @property
    def status(self) -> int: ...  # noqa: D102

    @property
    def body(self) -> bytes: ...  # noqa: D102


@dataclass(frozen=True)
class Reply:
    """One HTTP answer; header names are lower case."""

    status: int
    headers: Mapping[str, str]
    body: bytes


if TYPE_CHECKING:
    # (host, path, headers, timeout) -> answer, as space.https_get; tests pass fakes.
    Get = Callable[[str, str, Mapping[str, str], float], Answer]
    # (host, path, timeout) -> answer, as https_post.
    Post = Callable[[str, str, float], Answer]


def https_post(host: str, path: str, timeout: float) -> Reply:
    """``POST https://HOST/PATH`` without a body; redirects are returned, not followed.

    Raises:
        RenderError: the body is larger than ``MAX_BODY``.
    """
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("POST", path, body=b"", headers={"Content-Length": "0", **USER_AGENT})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise RenderError(f"https://{host} answered more than {MAX_BODY} bytes")
        return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    finally:
        conn.close()


def _json_object(body: bytes) -> dict[str, object]:
    """``body`` as a JSON object, or an empty dict if it is not one."""
    try:
        data = json.loads(body)
    except ValueError:  # also UnicodeDecodeError
        return {}
    return data if isinstance(data, dict) else {}


def service_host(url: str) -> str:
    """The host name of the service URL ``https://HOST`` (a trailing ``/`` is allowed).

    Raises:
        RenderError: ``url`` is anything else (http, a path, a port, credentials...).
    """
    host = url.removeprefix("https://").removesuffix("/").lower()
    if not url.startswith("https://") or not HOST_NAME.fullmatch(host):
        raise RenderError(
            f"not a service URL: {url!r:.100} (expected https://NAME.onrender.com or "
            "https://your.domain, without a path)"
        )
    return host


def config_problems(url: str, *, hook_present: bool) -> list[str]:
    """What is missing or malformed in the repository's deploy settings (empty: all good)."""
    where = (
        "in the GitHub environment render (Settings > Environments) or in the repository "
        "(Settings > Secrets and variables > Actions)"
    )
    problems = []
    if not hook_present:
        problems.append(
            f"the secret RENDER_DEPLOY_HOOK_URL is missing: set it {where} to the deploy hook "
            "from the Render service's Settings, see docs/deploy.md"
        )
    if not url:
        problems.append(
            f"the variable RENDER_URL is missing: set it {where} to the service's URL, e.g. "
            "https://cca-demo.onrender.com, see docs/deploy.md"
        )
    else:
        try:
            service_host(url)
        except RenderError as exc:
            problems.append(f"the variable RENDER_URL is {exc}")
    return problems


def hook_request(hook: str, digest: str) -> tuple[str, str]:
    """``(host, path)`` of the deploy request: the hook with ``imgURL`` = the image by digest.

    Raises:
        RenderError: the hook is not ``https://api.render.com/deploy/srv-...?key=...`` or the
            digest is malformed. The message never contains the hook or its key.
    """
    digest = space.check_digest(digest)
    try:  # a ValueError's text may quote the hook: it is replaced by the message below
        parts = urllib.parse.urlsplit(hook.strip())
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        parts, query = urllib.parse.urlsplit(""), {}
    keys = query.get("key", [])
    if (
        parts.scheme != "https"
        or parts.netloc != HOOK_HOST
        or not HOOK_PATH.fullmatch(parts.path)
        or parts.fragment
        or set(query) != {"key"}
        or len(keys) != 1
        or not HOOK_KEY.fullmatch(keys[0])
    ):
        raise RenderError(
            f"the secret {HOOK_ENV} is not a Render deploy hook "
            f"(https://{HOOK_HOST}/deploy/srv-...?key=... with no other parameter): copy it "
            "again from the service's Settings, see docs/deploy.md"
        )
    image = urllib.parse.quote(f"{IMAGE}@{digest}", safe="")
    return HOOK_HOST, f"{parts.path}?{parts.query}&imgURL={image}"


def _deploy_once(host: str, path: str, post: Post) -> str:
    """One POST to the hook: the line to print on success.

    Raises:
        RenderError: a refusal worth no second try.
        _RetryableError: rate limit or server error.
    """
    reply = post(host, path, TIMEOUT_S)
    if reply.status in (200, 201, 202):
        deploy = _json_object(reply.body).get("deploy")
        found = deploy.get("id") if isinstance(deploy, dict) else None
        name = found if isinstance(found, str) and DEPLOY_ID.fullmatch(found) else "(no id)"
        if reply.status == 202:
            return f"deploy queued behind another one: {name}"
        return f"deploy started: {name}"
    if reply.status == 429 or 500 <= reply.status <= 599:
        raise _RetryableError(f"deploy hook: HTTP {reply.status}")
    why = HOOK_REFUSALS.get(reply.status, "unexpected answer")
    raise RenderError(f"the deploy hook answered HTTP {reply.status}: {why}")


def trigger(
    digest: str,
    hook: str | None = None,
    post: Post = https_post,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Deploy ``ghcr.io/donquaan/cca@DIGEST`` through the hook; returns what Render said.

    ``hook`` defaults to the environment variable ``RENDER_DEPLOY_HOOK_URL``. A rate limit, a
    server error or a network error is tried again (``ATTEMPTS`` in all): a second deploy of
    the same digest does no harm.

    Raises:
        RenderError: no or a malformed hook, a refusal, or every attempt failed.
    """
    if hook is None:
        hook = os.environ.get(HOOK_ENV, "")
    if not hook:
        raise RenderError(f"the secret {HOOK_ENV} is missing, see docs/deploy.md")
    host, path = hook_request(hook, digest)
    problem = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _deploy_once(host, path, post)
        except _RetryableError as exc:
            problem = str(exc)
        except (OSError, http.client.HTTPException) as exc:
            problem = type(exc).__name__  # never str(exc): it could quote the request
        if attempt < ATTEMPTS:
            sleep(PAUSE_S)
    raise RenderError(f"the deploy hook failed {ATTEMPTS} times: {problem}")


@dataclass(frozen=True)
class Health:
    """What ``/healthz`` said, judged against the expected version."""

    ok: bool
    """HTTP 200, status "ok", ready true and the expected version."""
    failed: bool
    """HTTP 503, status "error" and the expected version: that container's engine failed."""
    wrong_host: bool
    """HTTP 421: the app does not answer this host name (``--allowed-host``)."""
    said: str
    """The answer, escaped, for the log."""


def probe(host: str, version: str, get: Get) -> Health:
    """What ``https://HOST/healthz`` says about ``version``; never raises."""
    try:
        reply = get(host, "/healthz", {"Accept": "application/json", **USER_AGENT}, TIMEOUT_S)
    except (OSError, http.client.HTTPException, space.SpaceError) as exc:
        return Health(
            ok=False, failed=False, wrong_host=False, said=f"healthz: {type(exc).__name__}"
        )
    if reply.status == 421:
        return Health(ok=False, failed=False, wrong_host=True, said="healthz: HTTP 421")
    # 200 while starting or ready, 503 with status "error" once the engine failed.
    data = _json_object(reply.body) if reply.status in (200, 503) else {}
    if not data:
        said = f"healthz: HTTP {reply.status}"
        if reply.status in (200, 503):
            said += " without a JSON object"  # e.g. Render's page while the service spins up
        return Health(ok=False, failed=False, wrong_host=False, said=said)
    status, seen, ready = data.get("status"), data.get("version"), data.get("ready")
    said = (
        f"healthz: HTTP {reply.status} status={status!r:.20} version={seen!r:.40} "
        f"ready={ready!r:.10}"
    )
    if "error" in data:
        said += f" error={data['error']!r:.200}"
    return Health(
        ok=reply.status == 200 and status == "ok" and seen == version and ready is True,
        failed=reply.status == 503 and status == "error" and seen == version,
        wrong_host=False,
        said=said,
    )


def wait_live(
    url: str,
    version: str,
    *,
    timeout: float,
    interval: float,
    get: Get = space.https_get,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> str:
    """Poll until the service at ``url`` serves ``version`` with its engine ready.

    The first request also wakes a Free service that was spun down ("about one minute",
    https://render.com/docs/free). Each attempt prints one line. /healthz reports no image
    digest: when the service already ran ``version`` before the deploy, the old instance's
    answer is taken for the new one (the Render dashboard shows the deploy itself).

    Raises:
        RenderError: bad input; the app answers 421 for the URL's host (not an
            ``--allowed-host``); a container of ``version`` reports that its engine failed; or
            ``timeout`` seconds passed first.
    """
    host = service_host(url)
    version = space.check_version(version)
    start = clock()
    while True:
        health = probe(host, version, get)
        elapsed = clock() - start
        target = f"https://{host}/healthz"
        if health.ok:
            print(f"live: {target} serves version {version}, engine ready", file=out)
            return target
        if health.wrong_host:
            raise RenderError(
                f"{target} answered 421: the service does not accept the host name {host}. "
                "Add it to the --allowed-host line of deploy/render/render.yaml (and sync the "
                "Blueprint), see docs/deploy.md"
            )
        if health.failed:
            raise RenderError(f"{target}: the engine of version {version} failed ({health.said})")
        print(f"[{elapsed:5.0f}s] {health.said}", file=out, flush=True)
        if elapsed + interval > timeout + _EPS:
            raise RenderError(
                f"{target} did not serve version {version} with its engine ready within "
                f"{timeout:.0f}s (last: {health.said}); see the service's Events and Logs in "
                "the Render dashboard"
            )
        sleep(interval)


def git_read(spec: str) -> bytes | None:
    """``git cat-file -p SPEC`` in this repository: an object's content, None if there is none.

    Raises:
        RenderError: git cannot be run.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            ["git", "-C", str(ROOT), "cat-file", "-p", spec],  # noqa: S607 - git from PATH
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RenderError(f"git cannot be run: {type(exc).__name__}") from None
    return done.stdout if done.returncode == 0 else None


def _command_options(text: str, where: str) -> set[str]:
    try:
        found: set[str] = blueprint.options(blueprint.command_words(text))
    except blueprint.BlueprintError as exc:
        raise RenderError(f"{where}: {exc}") from None
    return found


def blueprint_check(
    version: str | None = None,
    *,
    read: Callable[[str], bytes | None] = git_read,
    here: Path = ROOT / BLUEPRINT,
) -> str:
    """Check that release ``version`` knows every option of the Blueprint's command here.

    A deploy through the hook with ``imgURL`` deploys that image "instead of using the tag or
    digest in your service's settings" (https://render.com/docs/deploying-an-image); nothing in
    it names a command, so the service runs the Docker command of its settings, the one of its
    last Blueprint sync. A release's image is pushed only after its own docker/tests parsed its
    Blueprint's command with its own ``cca`` (image.yml: publish needs smoke-unit), so the
    options of that Blueprint are ones its ``cca play`` knows. The Blueprint here (the working
    tree; run from ``main``, the branch the Blueprint syncs from) stands for the command the
    service runs, which this cannot see: an option it passes that the release's Blueprint does
    not may stop that release's container at start ("unrecognized arguments").

    With ``version`` None, the release is the Blueprint's own image tag: whether a Blueprint
    sync, which starts that tag with this command, is safe.

    Returns:
        The line to print when every option is known.

    Raises:
        RenderError: no tag ``v<version>`` in this checkout, a tag without the Blueprint, a
            Blueprint that cannot be read, or an option the release's Blueprint lacks.
    """
    sync = version is None
    if version is None:
        try:
            text = here.read_text(encoding="utf-8")
            version = str(blueprint.image_tag(text, IMAGE))
        except OSError as exc:
            raise RenderError(f"cannot read {BLUEPRINT}: {type(exc).__name__}") from None
        except blueprint.BlueprintError as exc:
            raise RenderError(f"{BLUEPRINT}: {exc}") from None
    version = space.check_version(version)
    tag = f"refs/tags/v{version}"
    if read(tag) is None:
        raise RenderError(
            f"there is no tag v{version} in this checkout: fetch the tags (git fetch --tags), "
            "or wait until that version is released"
        )
    tagged = read(f"{tag}:{BLUEPRINT}")
    if tagged is None:
        raise RenderError(
            f"the tag v{version} has no {BLUEPRINT}: a version released before the Render "
            "deploy cannot run the Blueprint's command"
            + (
                ": do not sync the Blueprint until a release ships it (docs/deploy.md)"
                if sync
                else ""
            )
        )
    try:
        text = here.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"cannot read {BLUEPRINT}: {type(exc).__name__}") from None
    ours = _command_options(text, BLUEPRINT)
    theirs = _command_options(tagged.decode("utf-8", "replace"), f"{BLUEPRINT} of v{version}")
    missing = sorted(ours - theirs)
    if missing:
        advice = (
            "Do not sync the Blueprint until a release ships this command (docs/deploy.md)"
            if sync
            else "The service keeps the command of its last Blueprint sync, and a deploy through "
            "the hook changes only the image: deploy a release whose Blueprint passes them, or "
            f"put the command of v{version} on the Blueprint's branch and sync it first "
            "(docs/deploy.md)"
        )
        raise RenderError(
            f"{BLUEPRINT} passes {', '.join(missing)}, which the Blueprint of v{version} does "
            f"not: the cca play of v{version} may not know them and would then stop at start "
            f"(unrecognized arguments). {advice}"
        )
    said = f"the Blueprint of v{version} passes every option of {BLUEPRINT} here ({len(ours)})"
    if sync:
        said += f": a Blueprint sync may start it, once {IMAGE}:{version} exists"
    return said


def report_error(message: str, out: TextIO | None = None) -> None:
    """Print ``message`` to stderr, as a GitHub Actions error annotation when run there.

    A step's ``x=$(service.py ...)`` captures stdout only, and the runner reads workflow
    commands on stderr too, so an annotation there stays visible (as in space.py).
    """
    stream = out or sys.stderr
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=deploy-render::{escaped}", file=stream)
    else:
        print(f"error: {message}", file=stream)


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface (exposed for tests)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser("check-config", help="check the repository's deploy settings")
    cfg.add_argument("--url", required=True, help="vars.RENDER_URL (may be empty)")
    # Only whether secrets.RENDER_DEPLOY_HOOK_URL is set ("true" or "false"), never the hook.
    cfg.add_argument("--hook-present", required=True, choices=["true", "false"])
    ver = sub.add_parser("version", help="print the image tag of a release name")
    ver.add_argument("release")
    dig = sub.add_parser("digest", help="print the manifest digest of a release image")
    dig.add_argument("version")
    trg = sub.add_parser("trigger", help=f"deploy {IMAGE}@DIGEST through the hook in ${HOOK_ENV}")
    trg.add_argument("digest")
    wai = sub.add_parser("wait", help="poll the service until the version answers ready")
    wai.add_argument("url")
    wai.add_argument("version")
    wai.add_argument("--timeout", type=float, default=600.0, help="seconds (default 600)")
    wai.add_argument("--interval", type=float, default=15.0, help="seconds (default 15)")
    chk = sub.add_parser(
        "blueprint-check",
        help="check that a release knows every option of the Blueprint's command here",
    )
    chk.add_argument(
        "version", nargs="?", help="the release (default: the Blueprint's image tag, for a sync)"
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    """Run the step ``args`` names."""
    if args.command == "check-config":
        problems = config_problems(args.url, hook_present=args.hook_present == "true")
        for problem in problems:
            report_error(problem)
        return 1 if problems else 0
    if args.command == "version":
        print(space.check_version(args.release))
    elif args.command == "digest":
        print(space.resolve_digest(args.version))
    elif args.command == "trigger":
        print(trigger(args.digest))
    elif args.command == "blueprint-check":
        print(blueprint_check(args.version))
    else:
        wait_live(args.url, args.version, timeout=args.timeout, interval=args.interval)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step; 0 on success, 1 with an error message otherwise."""
    args = build_parser().parse_args(argv)
    try:
        code = _run(args)
    except (RenderError, space.SpaceError) as exc:
        report_error(str(exc))
        code = 1
    except (OSError, http.client.HTTPException) as exc:
        report_error(f"network error: {type(exc).__name__}")
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
