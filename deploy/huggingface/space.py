"""Render and check the Hugging Face Space that runs the public CCA demo.

.github/workflows/deploy-space.yml runs these steps and docs/deploy.md shows them by hand.
Standard library only (the runner's system Python runs it); every request is an HTTPS GET that
follows no redirect, and the Hugging Face token never reaches this script::

    python deploy/huggingface/space.py check-config --space OWNER/NAME --token-present true
    python deploy/huggingface/space.py version v0.1.1          # prints 0.1.1
    python deploy/huggingface/space.py digest 0.1.1            # prints sha256:<64 hex>
    python deploy/huggingface/space.py host OWNER/NAME         # prints <subdomain>.hf.space
    python deploy/huggingface/space.py render [--source DIR] 0.1.1 sha256:<64 hex> HOST OUT_DIR
    python deploy/huggingface/space.py wait OWNER/NAME 0.1.1 [--commit SHA] [--timeout 900]

``render`` writes the two files a Docker Space needs, ``Dockerfile`` (FROM the release image
pinned by digest, the Space's host name in ``--allowed-host``) and ``README.md`` (the Space
card), from the templates in DIR (default: this directory) into an empty directory. ``wait``
polls until the Space runs a container built from the expected commit of its repository
(default: its HEAD when the wait starts) and that container's ``/healthz`` reports the version
with its engines ready. Errors go to stderr, as GitHub Actions error annotations when run there.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REGISTRY = "ghcr.io"
REPOSITORY = "donquaan/cca"  # the registry wants lower case; the GitHub repository is DonQuaan/CCA
IMAGE = f"{REGISTRY}/{REPOSITORY}"
HUB = "huggingface.co"
VERSION_SLOT = "{{CCA_VERSION}}"
DIGEST_SLOT = "{{CCA_IMAGE_DIGEST}}"
HOST_SLOT = "{{CCA_SPACE_HOST}}"
FROM_LINE = f"FROM {IMAGE}:{VERSION_SLOT}@{DIGEST_SLOT}"
SLOT = re.compile(r"\{\{[^{}\n]*\}\}")
# The image tags that docker/metadata-action writes for a release (type=pep440, pattern
# {{version}} in .github/workflows/image.yml): a normalised PEP 440 public version, no "v".
_N = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{_N}\.{_N}\.{_N}(?:(?:a|b|rc){_N})?(?:\.post{_N})?(?:\.dev{_N})?")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
# A Hub repository id: OWNER/NAME, each part letters, digits, "-", "_" or ".", not starting or
# ending with "-" or "."; also what keeps the id safe inside a URL and a shell word.
_PART = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?"
SPACE_ID = re.compile(rf"{_PART}/{_PART}")
USERNAME = re.compile(_PART)
# A Space's own host: one DNS label under hf.space (lower case, as the Hub API reports it).
HOST_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.hf\.space")
# Accepted manifest media types; the release images are single-platform OCI manifests.
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
# Space stages as huggingface_hub's SpaceStage lists them (src/huggingface_hub/_space_api.py).
# The runtime API's "sha" is the commit the serving container was built from: observed, not
# documented (docs/deploy.md). RUNNING shows the repository's HEAD; RUNNING_BUILDING and
# RUNNING_APP_STARTING still show the previous commit while the new one builds and starts.
RUNNING = "RUNNING"
PROGRESS_STAGES = frozenset(
    {"BUILDING", "RUNNING_BUILDING", "APP_STARTING", "RUNNING_APP_STARTING"}
)
STUCK_STAGES = frozenset(
    {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE", "PAUSED", "DELETING"}
)
# A stuck stage seen before any build started may be left over from before the push: it fails
# the wait only once it has lasted this long (the Hub has not started the pushed commit).
STUCK_GRACE_S = 120.0
TIMEOUT_S = 30.0
MAX_BODY = 4 << 20  # bytes; a manifest, a token or /healthz is a few kilobytes
ATTEMPTS = 3
PAUSE_S = 10.0
_PERMANENT = (401, 403, 404, 410)
_EPS = 1e-9  # time.monotonic() ticks coarsely on Windows: compare deadlines with a tolerance
_T = TypeVar("_T")


class SpaceError(Exception):
    """A deploy step cannot succeed as configured (bad input, missing image, private Space...)."""


class _RetryableError(Exception):
    """An answer worth asking again for (rate limit, server error)."""


@dataclass(frozen=True)
class Reply:
    """One HTTP answer; header names are lower case."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class SpaceInfo:
    """What the Hub's public API says about a Space."""

    host: str
    """Its own host name, ``<subdomain>.hf.space``."""
    commit: str | None
    """The HEAD commit of its repository (field ``sha``), if the API reports one."""


@dataclass(frozen=True)
class Runtime:
    """The Space's runtime: its stage and the commit its serving container was built from."""

    stage: str
    commit: str | None
    error: str | None

    def describe(self, want: str | None) -> str:
        """One log field: stage, the running commit, the expected one and the Hub's error."""
        running = self.commit[:7] if self.commit else "?"
        text = f"stage={self.stage!r:.40} running={running} want={want[:7] if want else '?'}"
        return f"{text} error={self.error!r:.200}" if self.error else text


@dataclass(frozen=True)
class Health:
    """What ``/healthz`` said, judged against the expected version."""

    ok: bool
    """HTTP 200, status "ok", ready true and the expected version."""
    failed: bool
    """HTTP 503, status "error" and the expected version: that container's engine failed."""
    said: str
    """The answer, escaped, for the log."""


if TYPE_CHECKING:
    # (host, path, headers, timeout) -> reply; tests pass a fake instead of the network.
    Get = Callable[[str, str, Mapping[str, str], float], Reply]


def https_get(host: str, path: str, headers: Mapping[str, str], timeout: float) -> Reply:
    """``GET https://HOST/PATH`` with certificate checks; redirects are returned, not followed.

    Raises:
        SpaceError: the body is larger than ``MAX_BODY``.
    """
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("GET", path, headers=dict(headers))
        resp = conn.getresponse()
        body = resp.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise SpaceError(f"https://{host}{path} answered more than {MAX_BODY} bytes")
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


def _retryable(status: int) -> bool:
    """Whether an HTTP status is worth asking again for: a rate limit or a server error."""
    return status == 429 or 500 <= status <= 599


def _retrying(call: Callable[[], _T], what: str, sleep: Callable[[float], None]) -> _T:
    """``call()``, tried up to ``ATTEMPTS`` times through rate limits, server errors and outages.

    Raises:
        SpaceError: a permanent problem (raised by ``call`` at once), or every attempt failed.
    """
    problem = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return call()
        except _RetryableError as exc:
            problem = str(exc)
        except (OSError, http.client.HTTPException) as exc:
            problem = type(exc).__name__
        if attempt < ATTEMPTS:
            sleep(PAUSE_S)
    raise SpaceError(f"{what} after {ATTEMPTS} tries: {problem}")


def check_version(raw: str) -> str:
    """The image tag for a release name: ``v0.1.1`` and ``0.1.1`` both give ``0.1.1``.

    Raises:
        SpaceError: ``raw`` is not a normalised PEP 440 release version.
    """
    version = raw.removeprefix("v")
    if not VERSION.fullmatch(version):
        raise SpaceError(f"not a release version: {raw!r:.80} (expected e.g. 0.1.1 or v0.1.1)")
    return version


def check_digest(digest: str) -> str:
    """``digest`` if it is ``sha256:`` and 64 lower-case hex digits.

    Raises:
        SpaceError: it is not.
    """
    if not DIGEST.fullmatch(digest):
        raise SpaceError(f"not an image digest: {digest!r:.80} (expected sha256:<64 hex digits>)")
    return digest


def check_host(host: str) -> str:
    """``host`` if it is a Space host name such as ``owner-name.hf.space``.

    Raises:
        SpaceError: it is not.
    """
    if not HOST_NAME.fullmatch(host):
        raise SpaceError(f"not a Space host name: {host!r:.100} (expected OWNER-NAME.hf.space)")
    return host


def config_problems(space: str, *, token_present: bool, username: str = "") -> list[str]:
    """What is missing or malformed in the repository's deploy settings (empty: all good)."""
    where = (
        "in the GitHub environment huggingface-space (Settings > Environments) or in the "
        "repository (Settings > Secrets and variables > Actions)"
    )
    problems = []
    if not token_present:
        problems.append(
            f"the secret HF_TOKEN is missing: set it {where} to a Hugging Face fine-grained "
            "token with write access to the Space, see docs/deploy.md"
        )
    if not space:
        problems.append(
            f"the variable HF_SPACE is missing: set it {where} to the Space as OWNER/NAME, "
            "e.g. DonQuaan/CCA, see docs/deploy.md"
        )
    elif not SPACE_ID.fullmatch(space):
        problems.append(
            "the variable HF_SPACE must be OWNER/NAME (letters, digits, '-', '_' and '.'), "
            "e.g. DonQuaan/CCA"
        )
    if username and not USERNAME.fullmatch(username):
        problems.append(
            "the variable HF_USERNAME, when set, must be a Hugging Face user name (letters, "
            "digits, '-', '_' and '.')"
        )
    return problems


def _manifest_digest(version: str, get: Get) -> str:
    """One attempt of :func:`resolve_digest`.

    Raises:
        SpaceError: a permanent problem.
        _RetryableError: rate limit or server error.
    """
    scope = f"repository:{REPOSITORY}:pull"
    reply = get(REGISTRY, f"/token?scope={scope}&service={REGISTRY}", {}, TIMEOUT_S)
    if _retryable(reply.status):
        raise _RetryableError(f"{REGISTRY} token: HTTP {reply.status}")
    token = _json_object(reply.body).get("token") if reply.status == 200 else None
    if not isinstance(token, str) or not token:
        raise SpaceError(
            f"{REGISTRY} gave no anonymous pull token for {IMAGE} (HTTP {reply.status}); "
            "is the package public?"
        )
    headers = {"Authorization": f"Bearer {token}", "Accept": ", ".join(MANIFEST_TYPES)}
    reply = get(REGISTRY, f"/v2/{REPOSITORY}/manifests/{version}", headers, TIMEOUT_S)
    if reply.status == 404:
        raise SpaceError(
            f"{IMAGE}:{version} does not exist: was the release published and did its "
            "image job push the images?"
        )
    if _retryable(reply.status):
        raise _RetryableError(f"{REGISTRY} manifest: HTTP {reply.status}")
    if reply.status != 200:
        raise SpaceError(f"{IMAGE}:{version}: the registry answered HTTP {reply.status}")
    media = reply.headers.get("content-type", "").split(";")[0].strip()
    if media not in MANIFEST_TYPES:
        raise SpaceError(f"{IMAGE}:{version}: unexpected manifest media type {media!r:.80}")
    digest = "sha256:" + hashlib.sha256(reply.body).hexdigest()
    announced = reply.headers.get("docker-content-digest")
    if announced is not None and announced != digest:
        raise SpaceError(
            f"{IMAGE}:{version}: the registry announced {announced!r:.80}, but the manifest "
            f"it sent hashes to {digest}"
        )
    return digest


def resolve_digest(
    version: str,
    get: Get = https_get,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """The manifest digest of ``ghcr.io/donquaan/cca:VERSION``, computed from the manifest itself.

    Pulls anonymously (the package is public), asking up to ``ATTEMPTS`` times through rate
    limits and outages. The digest is the SHA-256 of the manifest bytes; a
    ``Docker-Content-Digest`` header that disagrees with it is an error.

    Raises:
        SpaceError: no such tag, the registry refused, the answer is inconsistent, or every
            attempt failed.
    """
    version = check_version(version)
    return _retrying(
        lambda: _manifest_digest(version, get), f"no digest for {IMAGE}:{version}", sleep
    )


def space_info(space: str, get: Get = https_get) -> SpaceInfo:
    """The Space's host name and HEAD commit, from the Hub's public API.

    Raises:
        SpaceError: the Space is missing or not public, or the API answer is unexpected.
        _RetryableError: rate limit or server error.
    """
    if not SPACE_ID.fullmatch(space):
        raise SpaceError(f"not a Space id: {space!r:.100} (expected OWNER/NAME)")
    reply = get(HUB, f"/api/spaces/{space}", {"Accept": "application/json"}, TIMEOUT_S)
    if reply.status in _PERMANENT:
        raise SpaceError(
            f"the Hub does not show the Space {space} to anonymous visitors "
            f"(HTTP {reply.status}): create it first, and make it public"
        )
    if reply.status != 200:
        raise _RetryableError(f"Hub API: HTTP {reply.status}")
    data = _json_object(reply.body)
    url = data.get("host")
    name = url.removeprefix("https://") if isinstance(url, str) else ""
    if url != f"https://{name}" or not HOST_NAME.fullmatch(name):
        raise SpaceError(f"the Hub reports an unexpected host for {space}: {url!r:.100}")
    sha = data.get("sha")
    return SpaceInfo(name, sha if isinstance(sha, str) and COMMIT.fullmatch(sha) else None)


def lookup_host(
    space: str,
    get: Get = https_get,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """The Space's host name (:func:`space_info`), asked up to ``ATTEMPTS`` times.

    Raises:
        SpaceError: a permanent problem, or every attempt failed.
    """
    info = _retrying(lambda: space_info(space, get), f"no host name for the Space {space}", sleep)
    return info.host


def fill(template: str, values: Mapping[str, str]) -> str:
    """``template`` with each ``{{NAME}}`` in ``values`` replaced.

    Raises:
        SpaceError: a placeholder not in ``values`` is left over.
    """
    for slot, value in values.items():
        template = template.replace(slot, value)
    left = SLOT.search(template)
    if left:
        raise SpaceError(f"unknown placeholder {left.group(0)!r}")
    return template


def render_dockerfile(template: str, version: str, digest: str, host: str) -> str:
    """The Space's Dockerfile: the release image pinned by digest, serving ``host``.

    Raises:
        SpaceError: bad version, digest or host, or the template lost its FROM line or a slot.
    """
    values = {
        VERSION_SLOT: check_version(version),
        DIGEST_SLOT: check_digest(digest),
        HOST_SLOT: check_host(host),
    }
    if template.splitlines().count(FROM_LINE) != 1 or any(
        template.count(slot) != 1 for slot in values
    ):
        raise SpaceError(
            f"the Dockerfile template must hold {FROM_LINE!r} and each placeholder exactly once"
        )
    return fill(template, values)


def front_matter(card: str) -> str:
    """The YAML between the leading ``---`` lines of a Space card.

    Raises:
        SpaceError: the card does not start with a closed front matter block.
    """
    lines = card.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise SpaceError("the Space card must start with YAML front matter between '---' lines")
    return "\n".join(lines[1 : lines.index("---", 1)])


def render_card(template: str, version: str) -> str:
    """The Space card (README.md) for ``version``; its front matter holds no placeholder.

    Raises:
        SpaceError: bad version, or a placeholder in the front matter.
    """
    if SLOT.search(front_matter(template)):
        raise SpaceError("the Space card's front matter must not hold placeholders")
    return fill(template, {VERSION_SLOT: check_version(version)})


def write_space(
    out_dir: Path, version: str, digest: str, host: str, source: Path = HERE
) -> list[Path]:
    """Render ``Dockerfile`` and ``README.md`` from ``source`` into ``out_dir`` (new or empty).

    Raises:
        SpaceError: bad input, or ``out_dir`` is not an empty directory.
    """
    dockerfile = render_dockerfile(
        (source / "Dockerfile").read_text(encoding="utf-8"), version, digest, host
    )
    card = render_card((source / "README.md").read_text(encoding="utf-8"), version)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise SpaceError(f"{out_dir} must be a new or empty directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in (("Dockerfile", dockerfile), ("README.md", card)):
        path = out_dir / name
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(path)
    return written


def runtime(space: str, get: Get = https_get) -> Runtime | str:
    """The Space's runtime, or why it could not be read (for the log); never raises."""
    try:
        reply = get(HUB, f"/api/spaces/{space}/runtime", {"Accept": "application/json"}, TIMEOUT_S)
    except (OSError, http.client.HTTPException, SpaceError) as exc:
        return type(exc).__name__
    if reply.status != 200:
        return f"HTTP {reply.status}"
    data = _json_object(reply.body)
    stage, sha, error = data.get("stage"), data.get("sha"), data.get("errorMessage")
    return Runtime(
        stage if isinstance(stage, str) else "",
        sha if isinstance(sha, str) and COMMIT.fullmatch(sha) else None,
        error if isinstance(error, str) and error else None,
    )


def probe(host: str, version: str, get: Get = https_get) -> Health:
    """What ``https://HOST/healthz`` says about ``version`` (the image's HEALTHCHECK, plus it)."""
    try:
        reply = get(host, "/healthz", {"Accept": "application/json"}, TIMEOUT_S)
    except (OSError, http.client.HTTPException, SpaceError) as exc:
        return Health(ok=False, failed=False, said=f"healthz: {type(exc).__name__}")
    # 200 while starting or ready, 503 with status "error" once the engine failed.
    data = _json_object(reply.body) if reply.status in (200, 503) else {}
    if not data:
        said = f"healthz: HTTP {reply.status}"
        if reply.status in (200, 503):
            said += " without a JSON object"
        return Health(ok=False, failed=False, said=said)
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
        said=said,
    )


@dataclass
class _Progress:
    """The runtime side of :func:`wait_healthy`, across its attempts."""

    space: str
    started: bool = False
    """A build or start was seen: a failed stage after it belongs to this deploy."""
    stuck_since: float | None = None
    """When the current run of stuck stages began (seconds into the wait)."""

    def judge(self, now: Runtime | str, want: str | None, elapsed: float) -> tuple[bool, str]:
        """Whether a container built from ``want`` serves the Space, and a log field.

        Raises:
            SpaceError: a failed or paused stage after a build started, or for too long.
        """
        if not isinstance(now, Runtime):
            return False, f"stage=? ({now})"
        self.started = self.started or now.stage in PROGRESS_STAGES
        if now.stage not in STUCK_STAGES:
            self.stuck_since = None
        elif self.stuck_since is None:
            self.stuck_since = elapsed
        if self.stuck_since is not None and (
            self.started or elapsed - self.stuck_since >= STUCK_GRACE_S - _EPS
        ):
            raise SpaceError(self._stuck(now))
        state = now.describe(want)
        if now.stage == RUNNING and now.commit is None:
            state += " (the runtime API reports no commit: the new build cannot be confirmed)"
        return now.stage == RUNNING and now.commit is not None and now.commit == want, state

    def _stuck(self, now: Runtime) -> str:
        """Why a Space at a failed or stopped stage fails the wait."""
        why = "after a build had started" if self.started else f"for {STUCK_GRACE_S:.0f}s"
        hint = (
            "only the owner can restart a paused Space (its Settings)"
            if now.stage == "PAUSED"
            else "see the Space's logs"
        )
        error = f": {now.error!r:.300}" if now.error else ""
        return f"the Space {self.space} is at stage {now.stage}{error} ({why}); {hint}"


def _try_space_info(space: str, get: Get) -> SpaceInfo | str:
    """:func:`space_info`, or why it failed this time (rate limit, outage).

    Raises:
        SpaceError: a permanent problem (the Space is missing or private...).
    """
    try:
        return space_info(space, get)
    except _RetryableError as exc:
        return str(exc)
    except (OSError, http.client.HTTPException) as exc:
        return f"Hub API: {type(exc).__name__}"


def wait_healthy(
    space: str,
    version: str,
    *,
    commit: str | None = None,
    timeout: float,
    interval: float,
    get: Get = https_get,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> str:
    """Poll until the Space serves ``version`` from ``commit``, ready to play; returns the URL.

    Done means that two answers agree: the runtime API reports stage RUNNING with a container
    built from ``commit`` (default: the repository's HEAD when the wait starts), and
    ``/healthz`` reports status "ok", ready and ``version``. Right after a push the previous
    container keeps answering, possibly with the same version, while the new commit builds: the
    commit is what tells them apart. Each attempt prints one line.

    Raises:
        SpaceError: bad input; the Space is not public or not found; it reached a failed or
            paused stage after a build started, or stayed at one for ``STUCK_GRACE_S``; its
            container built from ``commit`` reports that its engine failed; or ``timeout``
            seconds passed first.
    """
    version = check_version(version)
    if commit is not None and not COMMIT.fullmatch(commit):
        raise SpaceError(f"not a commit id: {commit!r:.80} (expected 40 lower-case hex digits)")
    start = clock()
    info: SpaceInfo | None = None
    progress = _Progress(space)
    said = "no answer yet"
    while True:
        if info is None:
            found = _try_space_info(space, get)
            info, said = (found, said) if isinstance(found, SpaceInfo) else (None, found)
        want = commit or (info.commit if info else None)
        if info is not None and want is None:
            raise SpaceError(f"the Hub reports no commit for the Space {space}: pass --commit")
        now = runtime(space, get)
        elapsed = clock() - start
        live, state = progress.judge(now, want, elapsed)
        if info is not None:
            health = probe(info.host, version, get)
            said = health.said
            url = f"https://{info.host}/healthz"
            if live and health.ok:
                print(f"healthy: {url} runs commit {want} with version {version} ready", file=out)
                return url
            if live and health.failed:
                raise SpaceError(f"{url}: the new container's engine failed ({said})")
        print(f"[{elapsed:5.0f}s] {state} {said}", file=out, flush=True)
        if elapsed + interval > timeout + _EPS:
            where = f"https://{info.host}" if info else f"the Space {space}"
            raise SpaceError(
                f"{where} did not serve version {version} from commit "
                f"{want[:7] if want else '?'} with its engines ready within {timeout:.0f}s "
                f"(last: {said}; {state}); see the Space's logs"
            )
        sleep(interval)


def report_error(message: str, out: TextIO | None = None) -> None:
    """Print ``message`` to stderr, as a GitHub Actions error annotation when run there.

    The runner reads workflow commands on stderr as on stdout (actions/runner, ScriptHandler.cs),
    and a step's ``x=$(space.py ...)`` captures stdout only: an annotation on stdout would vanish.
    """
    stream = out or sys.stderr
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=deploy-space::{escaped}", file=stream)
    else:
        print(f"error: {message}", file=stream)


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface (exposed for tests)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser("check-config", help="check the repository's deploy settings")
    cfg.add_argument("--space", required=True, help="vars.HF_SPACE (may be empty)")
    cfg.add_argument("--username", default="", help="vars.HF_USERNAME (optional)")
    # Only whether secrets.HF_TOKEN is set ("true" or "false"), never the token itself.
    cfg.add_argument(
        "--token-present", dest="credential_set", required=True, choices=["true", "false"]
    )
    ver = sub.add_parser("version", help="print the image tag of a release name")
    ver.add_argument("release")
    dig = sub.add_parser("digest", help="print the manifest digest of a release image")
    dig.add_argument("version")
    hst = sub.add_parser("host", help="print the host name of a public Space")
    hst.add_argument("space")
    ren = sub.add_parser("render", help="write Dockerfile and README.md for the Space")
    ren.add_argument("version")
    ren.add_argument("digest")
    ren.add_argument("host")
    ren.add_argument("out_dir", type=Path)
    ren.add_argument(
        "--source", type=Path, default=HERE, help="directory of the two templates (default: here)"
    )
    wai = sub.add_parser("wait", help="poll the Space until the version answers ready")
    wai.add_argument("space")
    wai.add_argument("version")
    wai.add_argument("--commit", help="the Space commit to expect (default: its HEAD)")
    wai.add_argument("--timeout", type=float, default=900.0, help="seconds (default 900)")
    wai.add_argument("--interval", type=float, default=15.0, help="seconds (default 15)")
    return parser


def _run(args: argparse.Namespace) -> int:
    """Run the step ``args`` names."""
    if args.command == "check-config":
        problems = config_problems(
            args.space, token_present=args.credential_set == "true", username=args.username
        )
        for problem in problems:
            report_error(problem)
        return 1 if problems else 0
    if args.command == "version":
        print(check_version(args.release))
    elif args.command == "digest":
        print(resolve_digest(args.version))
    elif args.command == "host":
        print(lookup_host(args.space))
    elif args.command == "render":
        for path in write_space(args.out_dir, args.version, args.digest, args.host, args.source):
            print(path)
    else:
        wait_healthy(
            args.space,
            args.version,
            commit=args.commit,
            timeout=args.timeout,
            interval=args.interval,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step; 0 on success, 1 with an error message otherwise."""
    args = build_parser().parse_args(argv)
    try:
        code = _run(args)
    except SpaceError as exc:
        report_error(str(exc))
        code = 1
    except (OSError, http.client.HTTPException) as exc:
        report_error(f"network error: {type(exc).__name__}: {exc}")
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
