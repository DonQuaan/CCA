"""The Render deploy: the Blueprint, the workflow's guards and deploy/render/service.py.

Nothing here touches the network. The helper's HTTP calls go to fakes; the workflow's steps run
under bash, as the runner runs them, with a ``python3`` whose HTTPS client is scripted, in a git
checkout whose tag does or does not ship the Blueprint.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import smoke

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# PyYAML comes with the dev tools (pre-commit depends on it); it ships no type stubs.
yaml = importlib.import_module("yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-render.yml"
DEPLOY = ROOT / "deploy" / "render"
BLUEPRINT = DEPLOY / "render.yaml"
DOCS = ROOT / "docs" / "deploy.md"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("cca_deploy_render", DEPLOY / "service.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


service = _load_helper()

VERSION = "0.1.1"
HOST = "cca-demo.onrender.com"
URL = f"https://{HOST}"
KEY = "rndr_Fake-Key.value_42"  # what the tests pretend the hook's key is
HOOK = f"https://api.render.com/deploy/srv-abc123def?key={KEY}"
DIGEST = "sha256:" + "7f" * 32
PACKAGE_VERSION = re.search(
    r'^__version__ = "([^"]+)"$',
    (ROOT / "src" / "cca" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE,
)
# The Blueprint's command after the image's ENTRYPOINT ["/usr/bin/tini", "--", "cca"]: the part
# that is the contract, then the two lines the owner sets after the first deploy.
FIXED = [
    "play", "--public", "--host", smoke.ALL_INTERFACES, "--port", str(smoke.WEB_PORT),
    "--no-browser", "--human", "qre",
    "--hash", "16", "--threads", "1", "--nodes", "20000", "--max-sessions", "12",
    "--max-think-seconds", "20",
]  # fmt: skip
# The CLIENT-IP line: the count of proxies that append to X-Forwarded-For (0: the header is
# ignored), with or without the log of each request's entry count.
CLIENT_IP_LINE = re.compile(r"--trusted-proxies [0-8](?: --log-forwarded-hops)?")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+?)(?:@(\S+))?(?:\s+#\s*(\S+))?\s*$")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
OUTPUT_REF = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
OUTPUT_LINE = re.compile(r'^\s*echo "(\w+)=\$(\w+)" >> "\$GITHUB_OUTPUT"$', re.MULTILINE)
RUNNER_VARIABLES = frozenset({"GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_REPOSITORY"})
ASSIGNED = re.compile(r"(?:^|[\s;(])([A-Za-z_]\w*)=|\bfor\s+([A-Za-z_]\w*)\s+in\b", re.MULTILINE)
USED = re.compile(r"\$\{?([A-Za-z_]\w*)")
XTRACE = re.compile(
    r"\bset\s+(?:-[A-Za-z]*x|-o\s+xtrace)|\bbash\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*x"
    r"|\bBASH_XTRACEFD\b|\bSHELLOPTS\b"
)
BASH = shutil.which("bash")
GIT = shutil.which("git")


# ---------------------------------------------------------------------------- the Blueprint
def blueprint() -> dict[str, Any]:
    data = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert list(data) == ["services"]
    (web,) = data["services"]
    assert isinstance(web, dict)
    return web


def words() -> list[str]:
    command = blueprint()["dockerCommand"]
    assert isinstance(command, str)
    return command.split()


def test_the_blueprint_is_one_free_image_backed_web_service() -> None:
    web = blueprint()
    assert set(web) == {
        "type", "name", "runtime", "image", "plan", "region", "numInstances",
        "healthCheckPath", "envVars", "dockerCommand",
    }  # fmt: skip
    assert (web["type"], web["runtime"], web["plan"], web["numInstances"]) == (
        "web",
        "image",
        "free",  # the Blueprint default for a web service is a paid plan
        1,
    )
    assert web["healthCheckPath"] == "/healthz"
    assert web["region"] in {"oregon", "ohio", "virginia", "frankfurt", "singapore"}
    assert re.fullmatch(r"[a-z0-9-]+", web["name"])


def test_the_image_is_the_release_of_this_commit_by_tag() -> None:
    assert PACKAGE_VERSION is not None
    url = blueprint()["image"]
    assert url == {"url": f"{service.IMAGE}:{PACKAGE_VERSION[1]}"}
    assert service.IMAGE == smoke.IMAGE_NAME


def allowed_hosts(found: list[str]) -> list[str]:
    return [found[i + 1] for i, word in enumerate(found[:-1]) if word == "--allowed-host"]


def test_the_command_is_the_contract_then_the_host_and_client_ip_lines() -> None:
    found = words()
    assert found[: len(FIXED)] == FIXED
    hosts = allowed_hosts(found)
    assert hosts  # public mode needs one; a custom domain adds a second
    assert all(service.HOST_NAME.fullmatch(host) for host in hosts), hosts
    host_line = [w for host in hosts for w in ("--allowed-host", host)]
    rest = found[len(FIXED) :]
    assert rest[: len(host_line)] == host_line
    client_ip = rest[len(host_line) :]
    assert CLIENT_IP_LINE.fullmatch(" ".join(client_ip)), client_ip
    assert "--frame-ancestor" not in found  # nothing embeds the demo on Render
    assert found.count("--public") == 1


def test_the_client_ip_and_host_settings_are_lines_of_their_own() -> None:
    text = BLUEPRINT.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("    dockerCommand: >-")
    block = [line.strip() for line in lines[start + 1 :]]
    assert all(line.startswith("      ") for line in lines[start + 1 :])  # the block ends the file
    host_line = block[-2].split()
    assert host_line[::2] == ["--allowed-host"] * (len(host_line) // 2)
    assert len(host_line) % 2 == 0
    assert CLIENT_IP_LINE.fullmatch(block[-1])
    assert "CLIENT-IP line" in text  # the comment that names it


def test_the_command_is_plain_words_for_a_shell_or_a_split() -> None:
    for word in words():
        assert re.fullmatch(r"[A-Za-z0-9._/=+-]+", word), word  # no quotes, $, ; or globs


def test_the_command_follows_the_images_entrypoint() -> None:
    assert words()[0] == "play"
    assert smoke.ENTRYPOINT == ["/usr/bin/tini", "--", "cca"]
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "cca"]' in (ROOT / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_port_env_command_and_image_agree() -> None:
    web = blueprint()
    assert web["envVars"] == [{"key": "PORT", "value": str(smoke.WEB_PORT)}]
    assert words()[words().index("--port") + 1] == str(smoke.WEB_PORT)
    assert f"EXPOSE {smoke.WEB_PORT}" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_command_parses_with_the_current_cca_cli() -> None:
    cli = pytest.importorskip("cca.cli")
    parser = cli.build_parser()
    play = parser._subparsers._group_actions[0].choices["play"]
    known = set(play._option_string_actions)
    unknown = [w for w in words() if w.startswith("--") and w not in known]
    assert unknown == []
    args = parser.parse_args(words())
    assert (args.command, args.public, args.host, args.port) == (
        "play",
        True,
        smoke.ALL_INTERFACES,
        smoke.WEB_PORT,
    )
    assert (args.human, args.hash, args.threads, args.nodes, args.max_sessions) == (
        "qre",
        16,
        1,
        20000,
        12,
    )
    assert args.max_think_seconds == 20.0
    assert args.allowed_host == allowed_hosts(words())
    assert args.trusted_proxies == int(words()[words().index("--trusted-proxies") + 1])
    assert args.log_forwarded_hops is ("--log-forwarded-hops" in words())
    assert args.frame_ancestor is None


def test_new_files_use_lf_line_endings() -> None:
    for path in (BLUEPRINT, DEPLOY / "service.py", DEPLOY / "blueprint.py", WORKFLOW, DOCS):
        assert b"\r" not in path.read_bytes(), path.name


# ---------------------------------------------------------------------------- workflow
def workflow() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def job() -> dict[str, Any]:
    jobs = workflow()["jobs"]
    assert list(jobs) == ["deploy"]
    found = jobs["deploy"]
    assert isinstance(found, dict)
    return found


def steps() -> list[dict[str, Any]]:
    found = job()["steps"]
    assert isinstance(found, list)
    return found


def step(text: str) -> dict[str, Any]:
    """The only step whose ``run`` or ``uses`` contains ``text``."""
    hits = [s for s in steps() if text in str(s.get("run", "")) or text in str(s.get("uses", ""))]
    assert len(hits) == 1, f"{len(hits)} steps contain {text!r}"
    return hits[0]


def index(text: str) -> int:
    return steps().index(step(text))


def uses_lines(path: Path) -> list[tuple[str, str | None, str | None]]:
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = USES.match(line)
        if match:
            action, ref, comment = match.groups()
            found.append((action, ref, comment))
    return found


def test_the_only_action_is_checkout_pinned_exactly_as_in_ci() -> None:
    ours = uses_lines(WORKFLOW)
    assert ours == [("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1")]
    assert set(ours) <= set(uses_lines(WORKFLOW.parent / "ci.yml"))
    checkout = step("actions/checkout")
    assert checkout["with"] == {"persist-credentials": False, "fetch-tags": True}


def test_triggers_manual_release_and_reusable() -> None:
    wf = workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as true
    assert isinstance(on, dict)
    assert set(on) == {"release", "workflow_dispatch", "workflow_call"}
    assert on["release"] == {"types": ["published"]}
    manual = on["workflow_dispatch"]["inputs"]
    assert set(manual) == {"version"}
    version = manual["version"]
    assert (version["type"], version["required"], version["default"]) == ("string", True, "latest")
    called = on["workflow_call"]["inputs"]
    assert set(called) == {"version"}
    assert (called["version"]["type"], called["version"]["required"]) == ("string", True)


def test_permissions_are_read_only() -> None:
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in job()
    assert wf["defaults"] == {"run": {"shell": "bash"}}
    assert [s.get("name") for s in steps() if "shell" in s] == []  # no step drops -eo pipefail


def test_the_job_runs_in_its_environment_one_at_a_time_and_bounded() -> None:
    deploy = job()
    assert deploy["environment"] == "render"
    assert deploy["concurrency"] == {"group": "deploy-render", "cancel-in-progress": False}
    assert deploy["runs-on"] == "ubuntu-latest"
    assert 10 < deploy["timeout-minutes"] <= 30  # the wait alone may take 10 minutes
    condition = deploy["if"].replace(" ", "")
    assert condition == "${{github.event_name!='release'||!github.event.release.prerelease}}"


def test_steps_run_in_order() -> None:
    order = [
        index("actions/checkout"),
        index("service.py check-config"),
        index("service.py version"),
        index("service.py blueprint-check"),
        index("service.py digest"),
        index("service.py trigger"),
        index("service.py wait"),
    ]
    assert order == sorted(order) == list(range(len(steps())))


def test_no_expression_is_pasted_into_a_script_and_nothing_traces() -> None:
    for s in steps():
        run = str(s.get("run", ""))
        assert "${{" not in run, s.get("name")
        assert not XTRACE.search(run), s.get("name")


def test_every_step_output_read_is_written_by_an_earlier_step() -> None:
    written: dict[str, set[str]] = {}
    reads = []
    for s in steps():
        for value in s.get("env", {}).values():
            for expression in EXPRESSION.findall(str(value)):
                for sid, name in OUTPUT_REF.findall(expression):
                    assert name in written.get(sid, set()), f"{s.get('name')}: {sid}.{name}"
                    reads.append(f"{sid}.{name}")
        run = str(s.get("run", ""))
        lines = OUTPUT_LINE.findall(run)
        for _name, variable in lines:
            assert f"{variable}=$(" in run, f"{s.get('name')}: ${variable} is not a command's"
        if "id" in s:
            written[s["id"]] = {name for name, _ in lines}
        else:
            assert not lines, s.get("name")
    assert written == {"version": {"version"}, "digest": {"digest"}}
    assert sorted(reads) == sorted(["version.version"] * 3 + ["digest.digest"] * 2)


def test_every_shell_variable_is_set_before_use() -> None:
    for s in steps():
        run = str(s.get("run", ""))
        assigned = {a or b for a, b in ASSIGNED.findall(run)}
        known = set(s.get("env", {})) | RUNNER_VARIABLES | assigned
        assert set(USED.findall(run)) <= known, (s.get("name"), set(USED.findall(run)) - known)


def test_the_hook_is_only_in_the_trigger_step() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.") == 2
    assert step("service.py check-config")["env"] == {
        "RENDER_URL": "${{ vars.RENDER_URL }}",
        "HOOK_PRESENT": "${{ secrets.RENDER_DEPLOY_HOOK_URL != '' }}",
    }
    trigger = step("service.py trigger")
    assert trigger["env"] == {
        "RENDER_DEPLOY_HOOK_URL": "${{ secrets.RENDER_DEPLOY_HOOK_URL }}",
        "DIGEST": "${{ steps.digest.outputs.digest }}",
    }
    # The helper reads the hook from its environment: never on a command line or in the log.
    assert trigger["run"] == 'python3 deploy/render/service.py trigger "$DIGEST"'
    holders = [s.get("name") for s in steps() if "RENDER_DEPLOY_HOOK_URL" in s.get("env", {})]
    assert holders == [trigger["name"]]


def test_the_settings_version_digest_and_wait_steps() -> None:
    check = " ".join(step("service.py check-config")["run"].split())
    assert check == (
        'python3 deploy/render/service.py check-config --url "$RENDER_URL" '
        '--hook-present "$HOOK_PRESENT"'
    )
    version = step("service.py version")
    assert version["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REQUESTED": (
            "${{ github.event_name == 'release' && github.event.release.tag_name "
            "|| inputs.version }}"
        ),
    }
    assert 'version=$(python3 deploy/render/service.py version "$REQUESTED")' in version["run"]
    known = step("service.py blueprint-check")
    assert known["env"] == {"VERSION": "${{ steps.version.outputs.version }}"}
    assert known["run"] == 'python3 deploy/render/service.py blueprint-check "$VERSION"'
    digest = step("service.py digest")
    assert digest["env"] == {"VERSION": "${{ steps.version.outputs.version }}"}
    wait = step("service.py wait")
    assert wait["env"] == {
        "RENDER_URL": "${{ vars.RENDER_URL }}",
        "VERSION": "${{ steps.version.outputs.version }}",
        "DIGEST": "${{ steps.digest.outputs.digest }}",
    }
    assert (
        'python3 deploy/render/service.py wait "$RENDER_URL" "$VERSION" --timeout 600 '
        "--interval 15" in wait["run"]
    )


def test_every_helper_call_names_a_real_subcommand() -> None:
    choices = set(service.build_parser()._subparsers._group_actions[0].choices)
    called = {
        m.group(1)
        for s in steps()
        for m in re.finditer(r"deploy/render/service\.py (\S+)", str(s.get("run", "")))
    }
    assert called == choices == {
        "check-config", "version", "blueprint-check", "digest", "trigger", "wait",
    }  # fmt: skip


def test_the_docs_name_the_settings_the_workflow_reads() -> None:
    docs = DOCS.read_text(encoding="utf-8")
    for needle in (
        "`render`",
        "`RENDER_DEPLOY_HOOK_URL`",
        "`RENDER_URL`",
        "deploy/render/render.yaml",
        "Deploy Render",
    ):
        assert needle in docs, needle


# ---------------------------------------------------------------------------- helper: settings
def test_good_settings_have_no_problems() -> None:
    assert service.config_problems(URL, hook_present=True) == []
    assert service.config_problems(URL + "/", hook_present=True) == []


def test_missing_settings_are_each_reported() -> None:
    problems = service.config_problems("", hook_present=False)
    assert len(problems) == 2
    assert problems[0].startswith("the secret RENDER_DEPLOY_HOOK_URL is missing")
    assert problems[1].startswith("the variable RENDER_URL is missing")
    assert all("docs/deploy.md" in p for p in problems)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}",
        HOST,
        f"{URL}/healthz",
        f"{URL}:443",
        f"https://user@{HOST}",
        f"{URL}?x=1",
        "https://localhost",
        "https://-bad.onrender.com",
        "https://10.0.0.1",
    ],
)
def test_a_malformed_service_url_is_reported(url: str) -> None:
    (problem,) = service.config_problems(url, hook_present=True)
    assert problem.startswith("the variable RENDER_URL is not a service URL")


def test_service_host_normalises_case_and_a_trailing_slash() -> None:
    assert service.service_host("https://CCA-Demo.OnRender.com/") == HOST


def test_check_config_annotates_every_problem_on_stderr_on_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert service.main(["check-config", "--url", "", "--hook-present", "false"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("::error title=deploy-render::") for line in lines)


def test_annotations_escape_newlines_and_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    err = io.StringIO()
    service.report_error("50% done\nnext", err)
    assert err.getvalue() == "::error title=deploy-render::50%25 done%0Anext\n"
    monkeypatch.delenv("GITHUB_ACTIONS")
    err = io.StringIO()
    service.report_error("plain", err)
    assert err.getvalue() == "error: plain\n"


# ---------------------------------------------------------------------------- helper: the hook
def test_the_hook_request_adds_the_image_by_digest() -> None:
    host, path = service.hook_request(HOOK, DIGEST)
    assert host == "api.render.com"
    image = f"ghcr.io%2Fdonquaan%2Fcca%40sha256%3A{'7f' * 32}"
    assert path == f"/deploy/srv-abc123def?key={KEY}&imgURL={image}"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert query == {"key": [KEY], "imgURL": [f"ghcr.io/donquaan/cca@{DIGEST}"]}


@pytest.mark.parametrize(
    "hook",
    [
        f"http://api.render.com/deploy/srv-abc?key={KEY}",
        f"https://evil.example/deploy/srv-abc?key={KEY}",
        f"https://api.render.com.evil.example/deploy/srv-abc?key={KEY}",
        f"https://x@api.render.com/deploy/srv-abc?key={KEY}",
        f"https://api.render.com:8443/deploy/srv-abc?key={KEY}",
        f"https://api.render.com/deploy/abc?key={KEY}",
        f"https://api.render.com/deploy/srv-abc/x?key={KEY}",
        "https://api.render.com/deploy/srv-abc",
        f"https://api.render.com/deploy/srv-abc?key={KEY}&imgURL=docker.io%2Fx",
        f"https://api.render.com/deploy/srv-abc?key={KEY}&ref=main",
        f"https://api.render.com/deploy/srv-abc?key={KEY}&key={KEY}",
        f"https://api.render.com/deploy/srv-abc?key={KEY}%0Aevil",
        f"https://api.render.com/deploy/srv-abc?key={KEY}#frag",
        f"https://[api.render.com/deploy/srv-abc?key={KEY}",
        f"curl https://api.render.com/deploy/srv-abc?key={KEY}",
    ],
)
def test_anything_but_a_deploy_hook_is_refused_without_quoting_it(hook: str) -> None:
    with pytest.raises(service.RenderError, match="is not a Render deploy hook") as caught:
        service.hook_request(hook, DIGEST)
    assert KEY not in str(caught.value)
    assert "srv-abc" not in str(caught.value)


def test_the_hook_request_checks_the_digest() -> None:
    with pytest.raises(service.space.SpaceError, match="not an image digest"):
        service.hook_request(HOOK, "sha256:abc")


class FakePost:
    """Answers the hook's POST from a script of replies (the last one repeats)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, host: str, path: str, timeout: float) -> Any:
        self.calls.append((host, path, timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def reply(status: int, body: bytes | dict[str, Any] = b"", **headers: str) -> Any:
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    return service.Reply(status, {k.replace("_", "-"): v for k, v in headers.items()}, raw)


STARTED = {"deploy": {"id": "dep-d1a2b3c4"}}


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (reply(200, STARTED), "deploy started: dep-d1a2b3c4"),
        (reply(202, STARTED), "deploy queued behind another one: dep-d1a2b3c4"),
        (reply(200, b"ok"), "deploy started: (no id)"),
        (reply(200, {"deploy": {"id": "dep-x y"}}), "deploy started: (no id)"),
    ],
)
def test_a_started_deploy_is_reported(answer: Any, said: str) -> None:
    fake = FakePost(answer)
    assert service.trigger(DIGEST, HOOK, fake, lambda _: None) == said
    ((host, path, _),) = fake.calls
    assert (host, path) == service.hook_request(HOOK, DIGEST)


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (400, "imgURL must name the service's own image"),
        (401, "refused the hook's key"),
        (404, "no such service, or refused the image"),
        (405, "refused the method"),
        (409, "suspended"),
        (301, "unexpected answer"),
        (418, "unexpected answer"),
    ],
)
def test_a_refusal_is_explained_and_not_retried(status: int, why: str) -> None:
    sleeps: list[float] = []
    fake = FakePost(reply(status, {"message": KEY}, location="https://elsewhere"))
    with pytest.raises(service.RenderError, match=f"HTTP {status}") as caught:
        service.trigger(DIGEST, HOOK, fake, sleeps.append)
    assert why in str(caught.value)
    assert KEY not in str(caught.value)
    assert (len(fake.calls), sleeps) == (1, [])


def test_rate_limits_server_errors_and_outages_are_retried() -> None:
    sleeps: list[float] = []
    fake = FakePost(reply(429), http.client.RemoteDisconnected(HOOK), reply(200, STARTED))
    assert service.trigger(DIGEST, HOOK, fake, sleeps.append) == "deploy started: dep-d1a2b3c4"
    assert sleeps == [service.PAUSE_S] * 2


def test_the_trigger_gives_up_after_its_attempts_without_quoting_the_request() -> None:
    sleeps: list[float] = []
    fake = FakePost(OSError(f"cannot reach {HOOK}"))
    with pytest.raises(service.RenderError, match="failed 3 times: OSError") as caught:
        service.trigger(DIGEST, HOOK, fake, sleeps.append)
    assert KEY not in str(caught.value)
    assert (len(fake.calls), len(sleeps)) == (3, 2)
    fake = FakePost(reply(503))
    with pytest.raises(service.RenderError, match=r"failed 3 times: deploy hook: HTTP 503"):
        service.trigger(DIGEST, HOOK, fake, sleeps.append)


def test_the_hook_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePost(reply(200, STARTED))
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert service.trigger(DIGEST, post=fake).startswith("deploy started")
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", "")
    with pytest.raises(service.RenderError, match="RENDER_DEPLOY_HOOK_URL is missing"):
        service.trigger(DIGEST, post=fake)
    assert len(fake.calls) == 1


def test_the_trigger_command_prints_only_what_render_said(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(service, "https_post", FakePost(reply(200, STARTED)))
    monkeypatch.setattr(service.trigger, "__defaults__", (None, service.https_post, lambda _: None))
    assert service.main(["trigger", DIGEST]) == 0
    out, err = capsys.readouterr()
    assert (out, err) == ("deploy started: dep-d1a2b3c4\n", "")


class FakeConnection:
    """http.client.HTTPSConnection for https_post: one scripted answer."""

    answer: tuple[int, list[tuple[str, str]], bytes] = (200, [], b"")
    requests: list[tuple[str, str, str, bytes, dict[str, str]]] = []  # noqa: RUF012

    def __init__(self, host: str, timeout: float) -> None:
        self.host = host

    def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        FakeConnection.requests.append((method, self.host, path, body, headers))

    def getresponse(self) -> Any:
        status, headers, body = FakeConnection.answer

        class Response:
            def __init__(self) -> None:
                self.status = status

            def read(self, limit: int) -> bytes:
                return body[:limit]

            def getheaders(self) -> list[tuple[str, str]]:
                return headers

        return Response()

    def close(self) -> None:
        pass


def test_https_post_sends_an_empty_post_and_returns_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.http.client, "HTTPSConnection", FakeConnection)
    FakeConnection.requests = []
    FakeConnection.answer = (302, [("Location", "https://evil.example")], b"")
    got = service.https_post("api.render.com", "/deploy/srv-x?key=k", 5.0)
    assert (got.status, got.headers) == (302, {"location": "https://evil.example"})
    ((method, host, path, body, headers),) = FakeConnection.requests
    assert (method, host, path, body) == ("POST", "api.render.com", "/deploy/srv-x?key=k", b"")
    assert headers["Content-Length"] == "0"
    assert headers["User-Agent"].startswith("cca-deploy-render")
    FakeConnection.answer = (200, [], b"x" * (service.MAX_BODY + 1))
    with pytest.raises(service.RenderError, match="more than"):
        service.https_post("api.render.com", "/", 5.0)


# ---------------------------------------------------------------------------- helper: waiting
class FakeGet:
    """Answers /healthz from a script of replies (the last one repeats)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, host: str, path: str, headers: Mapping[str, str], timeout: float) -> Any:
        self.calls.append((host, path, dict(headers)))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def healthz(version: str = VERSION, *, ready: object = True, status: str = "ok") -> Any:
    return reply(200, {"status": status, "version": version, "ready": ready})


def engine_failed(version: str = VERSION) -> Any:
    return reply(503, {"status": "error", "version": version, "ready": False, "error": "gone"})


@pytest.mark.parametrize(
    ("answer", "ok", "failed", "wrong_host"),
    [
        (healthz(), True, False, False),
        (healthz("0.1.0"), False, False, False),
        (healthz(ready=False), False, False, False),
        (healthz(ready="true"), False, False, False),
        (healthz(status="error"), False, False, False),
        (reply(200, b"<html>Render: service waking up</html>"), False, False, False),
        (reply(503, {"status": "ok", "version": VERSION, "ready": True}), False, False, False),
        (engine_failed(), False, True, False),
        (engine_failed("0.1.0"), False, False, False),
        (reply(421, {"error": "unknown Host"}), False, False, True),
        (reply(502, b"bad gateway"), False, False, False),
        (reply(302, location="https://elsewhere"), False, False, False),
        (TimeoutError("slow"), False, False, False),
        (http.client.RemoteDisconnected("x"), False, False, False),
    ],
)
def test_live_means_the_version_with_its_engine_ready(
    answer: Any, ok: bool, failed: bool, wrong_host: bool
) -> None:
    fake = FakeGet(answer)
    health = service.probe(HOST, VERSION, fake)
    assert (health.ok, health.failed, health.wrong_host) == (ok, failed, wrong_host)
    assert health.said.startswith("healthz: ")
    ((host, path, headers),) = fake.calls
    assert (host, path, headers["Accept"]) == (HOST, "/healthz", "application/json")
    assert headers["User-Agent"].startswith("cca-deploy-render")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def wait(fake: FakeGet, timeout: float = 600.0) -> tuple[str, Clock, str]:
    clock, out = Clock(), io.StringIO()
    found = service.wait_live(
        URL,
        "v" + VERSION,
        timeout=timeout,
        interval=15.0,
        get=fake,
        clock=clock,
        sleep=clock.sleep,
        out=out,
    )
    return found, clock, out.getvalue()


def test_wait_returns_once_the_new_version_is_ready() -> None:
    fake = FakeGet(TimeoutError(), reply(200, b"<html>"), healthz("0.1.0"), healthz(ready=False))
    fake.answers.append(healthz())
    found, clock, out = wait(fake)
    assert found == f"{URL}/healthz"
    assert clock.sleeps == [15.0] * 4
    lines = out.splitlines()
    assert len(lines) == 5
    assert lines[-1] == f"live: {URL}/healthz serves version {VERSION}, engine ready"
    assert "version='0.1.0'" in lines[2]


def test_wait_fails_at_once_on_421_with_the_fix() -> None:
    with pytest.raises(service.RenderError, match="answered 421") as caught:
        wait(FakeGet(reply(421)))
    assert "--allowed-host line of deploy/render/render.yaml" in str(caught.value)


def test_wait_fails_at_once_when_the_new_version_reports_a_failed_engine() -> None:
    with pytest.raises(service.RenderError, match=f"the engine of version {VERSION} failed"):
        wait(FakeGet(healthz("0.1.0"), engine_failed()))
    wait(FakeGet(engine_failed("0.1.0"), healthz()))  # the old version's failure: keep waiting


def test_wait_stops_at_its_timeout() -> None:
    clock, fake = Clock(), FakeGet(healthz("0.1.0"))
    with pytest.raises(service.RenderError, match="within 60s") as caught:
        service.wait_live(
            URL,
            VERSION,
            timeout=60.0,
            interval=15.0,
            get=fake,
            clock=clock,
            sleep=clock.sleep,
            out=io.StringIO(),
        )
    assert "Events and Logs" in str(caught.value)
    # Probes at 0, 15, 30, 45 and 60 seconds: the last one at the timeout, none after it.
    assert len(fake.calls) == 5
    assert clock.sleeps == [15.0] * 4
    assert clock.now == 60.0


@pytest.mark.parametrize(("url", "version"), [("http://x.example", VERSION), (URL, "latest")])
def test_wait_checks_its_input_before_any_request(url: str, version: str) -> None:
    fake = FakeGet(healthz())
    with pytest.raises((service.RenderError, service.space.SpaceError)):
        service.wait_live(url, version, timeout=1, interval=1, get=fake)
    assert fake.calls == []


def test_wait_command_passes_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_wait(url: str, version: str, **options: Any) -> str:
        seen.update(options, url=url, version=version)
        return url

    monkeypatch.setattr(service, "wait_live", fake_wait)
    assert service.main(["wait", URL, VERSION, "--timeout", "90", "--interval", "5"]) == 0
    assert seen == {"url": URL, "version": VERSION, "timeout": 90.0, "interval": 5.0}


def test_network_errors_through_the_cli_show_only_their_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def offline(version: str) -> str:
        raise TimeoutError(f"{version} {HOOK}")

    monkeypatch.setattr(service.space, "resolve_digest", offline)
    assert service.main(["digest", VERSION]) == 1
    err = capsys.readouterr().err
    assert err == "error: network error: TimeoutError\n"


def test_version_and_digest_are_space_py_s() -> None:
    assert service.main(["version", "v" + VERSION]) == 0
    assert service.IMAGE == service.space.IMAGE == "ghcr.io/donquaan/cca"


# ---------------------------------------------------------------------------- helper: Blueprint
def test_the_blueprint_reader_reads_the_real_blueprint_as_pyyaml_does() -> None:
    text = BLUEPRINT.read_text(encoding="utf-8")
    assert service.blueprint.command_words(text) == words()
    assert PACKAGE_VERSION is not None
    assert service.blueprint.image_tag(text, service.IMAGE) == PACKAGE_VERSION[1]


@pytest.mark.parametrize(
    ("found", "names"),
    [
        (["play", "--port", "8765", "--public"], {"--port", "--public"}),
        (["play", "--port=8765", "-v", "-1", "--"], {"--port", "-v"}),
        (["/usr/bin/tini", "--", "cca", "play", "--hash", "16"], {"--hash"}),
    ],
)
def test_options_are_option_names_not_values(found: list[str], names: set[str]) -> None:
    assert service.blueprint.options(found) == names


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("x: 1\n", "found 0"),
        ("  url: ghcr.io/donquaan/cca:0.2.0\n  url: ghcr.io/donquaan/cca:0.2.1\n", "found 2"),
        ("  url: docker.io/library/nginx:1.26\n", "not ghcr.io/donquaan/cca:<tag>"),
        ("  url: ghcr.io/donquaan/cca\n", "not ghcr.io/donquaan/cca:<tag>"),
        ("  url: ghcr.io/donquaan/cca:\n", "not ghcr.io/donquaan/cca:<tag>"),
    ],
)
def test_the_image_tag_needs_one_url_of_the_image(text: str, problem: str) -> None:
    with pytest.raises(service.blueprint.BlueprintError, match=re.escape(problem)):
        service.blueprint.image_tag(text, service.IMAGE)


def blueprint_text(command: str, tag: str = VERSION) -> str:
    return (
        "services:\n  - type: web\n    name: cca-demo\n    runtime: image\n"
        f"    image:\n      url: ghcr.io/donquaan/cca:{tag}\n"
        f"    dockerCommand: >-\n      {command}\n"
    )


RELEASED = "play --public --port 8765 --allowed-host a.onrender.com --trusted-proxies 0"
TAG_BLUEPRINT = f"refs/tags/v{VERSION}:deploy/render/render.yaml"


class Git:
    """``git cat-file -p`` of a checkout: its objects by name; every lookup is recorded."""

    def __init__(self, tagged: str | None = RELEASED, *, tag: bool = True) -> None:
        self.objects: dict[str, bytes] = {}
        if tag:
            self.objects[f"refs/tags/v{VERSION}"] = b"object 0123\ntype commit\n"
        if tag and tagged is not None:
            self.objects[TAG_BLUEPRINT] = blueprint_text(tagged).encode()
        self.calls: list[str] = []

    def __call__(self, spec: str) -> bytes | None:
        self.calls.append(spec)
        return self.objects.get(spec)


def check_here(tmp_path: Path, command: str, git: Git, version: str | None = VERSION) -> str:
    here = tmp_path / "render.yaml"
    here.write_text(blueprint_text(command), encoding="utf-8")
    found: str = service.blueprint_check(version, read=git, here=here)
    return found


def test_a_release_with_every_option_of_the_blueprint_here_passes(tmp_path: Path) -> None:
    # The owner's calibration changes values only: other hosts, another proxy count.
    calibrated = (
        "play --public --port 8765 --allowed-host b.onrender.com --allowed-host demo.example "
        "--trusted-proxies 3"
    )
    git = Git()
    said = check_here(tmp_path, calibrated, git)
    assert said == (
        f"the Blueprint of v{VERSION} passes every option of deploy/render/render.yaml here (4)"
    )
    assert git.calls == [f"refs/tags/v{VERSION}", TAG_BLUEPRINT]


def test_an_option_the_release_lacks_is_refused_with_the_way_out(tmp_path: Path) -> None:
    newer = f"{RELEASED} --log-forwarded-hops --max-think-seconds 20"
    with pytest.raises(service.RenderError) as caught:
        check_here(tmp_path, newer, Git())
    said = str(caught.value)
    assert said.startswith(
        "deploy/render/render.yaml passes --log-forwarded-hops, --max-think-seconds, which the "
        f"Blueprint of v{VERSION} does not"
    )
    assert "(unrecognized arguments)" in said
    assert f"put the command of v{VERSION} on the Blueprint's branch and sync it first" in said


def test_a_tag_without_the_blueprint_is_refused_before_the_blueprint_here_is_read(
    tmp_path: Path,
) -> None:
    git = Git(tagged=None)
    with pytest.raises(service.RenderError, match=f"the tag v{VERSION} has no deploy/render/"):
        service.blueprint_check(VERSION, read=git, here=tmp_path / "absent.yaml")
    assert git.calls == [f"refs/tags/v{VERSION}", TAG_BLUEPRINT]


def test_a_version_without_a_tag_here_is_refused(tmp_path: Path) -> None:
    git = Git(tag=False)
    with pytest.raises(service.RenderError, match=f"there is no tag v{VERSION} in this checkout"):
        service.blueprint_check("v" + VERSION, read=git, here=tmp_path / "absent.yaml")
    assert git.calls == [f"refs/tags/v{VERSION}"]


def test_a_bad_version_is_refused_before_git(tmp_path: Path) -> None:
    git = Git()
    with pytest.raises(service.space.SpaceError, match="not a release version"):
        service.blueprint_check("latest", read=git, here=tmp_path / "absent.yaml")
    assert git.calls == []


def test_without_a_version_it_checks_a_sync_of_the_blueprints_own_tag(tmp_path: Path) -> None:
    said = check_here(tmp_path, RELEASED, Git(), version=None)
    assert said.endswith(
        f": a Blueprint sync may start it, once ghcr.io/donquaan/cca:{VERSION} exists"
    )
    with pytest.raises(service.RenderError, match="Do not sync the Blueprint until a release"):
        check_here(tmp_path, f"{RELEASED} --log-forwarded-hops", Git(), version=None)
    with pytest.raises(service.RenderError, match="do not sync the Blueprint until a release"):
        check_here(tmp_path, RELEASED, Git(tagged=None), version=None)


@pytest.mark.parametrize(
    ("here", "tagged", "problem"),
    [
        ('"play --public"', RELEASED, "deploy/render/render.yaml: dockerCommand must be plain"),
        (RELEASED, "play $PORT", "deploy/render/render.yaml of v0.1.1: dockerCommand words"),
    ],
)
def test_an_unreadable_blueprint_is_refused(
    tmp_path: Path, here: str, tagged: str, problem: str
) -> None:
    path = tmp_path / "render.yaml"
    path.write_text(blueprint_text("x").replace(">-\n      x", here), encoding="utf-8")
    with pytest.raises(service.RenderError, match=re.escape(problem)):
        service.blueprint_check(VERSION, read=Git(tagged), here=path)


def test_git_read_reads_objects_of_the_repository(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_checkout(tmp_path, git_env, tag_has_blueprint=True)
    for name, value in git_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(service, "ROOT", repo)
    assert service.git_read(TAG_BLUEPRINT) == BLUEPRINT.read_bytes()
    assert service.git_read(f"refs/tags/v{VERSION}") is not None
    assert service.git_read("refs/tags/v9.9.9") is None
    assert service.git_read(f"refs/tags/v{VERSION}:deploy/render/absent.yaml") is None


def test_git_read_without_git_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(service.subprocess, "run", missing)
    with pytest.raises(service.RenderError, match="git cannot be run: FileNotFoundError"):
        service.git_read("HEAD")


def test_the_blueprint_check_command_prints_its_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str | None] = []

    def fake_check(version: str | None) -> str:
        seen.append(version)
        if version is None:
            raise service.RenderError("refused")
        return "fine"

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(service, "blueprint_check", fake_check)
    assert service.main(["blueprint-check", "0.2.0"]) == 0
    assert service.main(["blueprint-check"]) == 1
    assert seen == ["0.2.0", None]
    out, err = capsys.readouterr()
    assert (out, err) == ("fine\n", "error: refused\n")


# ---------------------------------------------------------------------------- the job, under bash
# A python3 for the steps: the interpreter running these tests, with a scripted HTTPS client
# (CCA_FAKE_HTTP: a JSON file of "METHOD host path" -> [status, headers, body]); every request
# is logged to CCA_FAKE_HTTP_LOG. A request without a scripted answer fails like a refused
# connection.
FAKE_PYTHON = r"""
import http.client
import json
import os
import runpy
import sys


class Response:
    def __init__(self, status, headers, body):
        self.status, self._headers, self._body = status, headers, body

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def getheaders(self):
        return list(self._headers.items())


class Connection:
    def __init__(self, host, timeout=None):
        self.host, self.method, self.path = host, None, None

    def request(self, method, path, body=None, headers=None):
        self.method, self.path = method, path
        with open(os.environ["CCA_FAKE_HTTP_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps([method, self.host, path, dict(headers or {})]) + "\n")

    def getresponse(self):
        with open(os.environ["CCA_FAKE_HTTP"], encoding="utf-8") as fh:
            answer = json.load(fh).get(f"{self.method} {self.host} {self.path}")
        if answer is None:
            raise ConnectionRefusedError("no scripted answer")
        status, headers, body = answer
        return Response(status, headers, body.encode("utf-8"))

    def close(self):
        pass


http.client.HTTPSConnection = Connection
script, sys.argv = sys.argv[1], sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""
FAKE_GH = """#!/bin/sh
if [ "$*" = "api repos/DonQuaan/CCA/releases/latest --jq .tag_name" ] && [ -n "$GH_TOKEN" ]; then
  echo "v0.1.1"
  exit 0
fi
echo "gh (test double): unexpected call: $*" >&2
exit 1
"""
MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
OCI = "application/vnd.oci.image.manifest.v1+json"
PULL_AUTH_PATH = "/token?scope=repository:donquaan/cca:pull&service=ghcr.io"
HOOK_PATH = f"/deploy/srv-abc123def?key={KEY}&imgURL=" + urllib.parse.quote(
    f"ghcr.io/donquaan/cca@{MANIFEST_DIGEST}", safe=""
)


def _git(*args: str, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    assert GIT is not None
    done = subprocess.run(
        [GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=120
    )
    return done.stdout


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """An environment without the machine's git configuration or the runner's own variables."""
    if BASH is None or GIT is None or "system32" in BASH.lower():
        pytest.skip("needs bash and git on PATH")
    config = tmp_path / "gitconfig"
    # Git for Windows only: object paths inside a deep temporary directory pass MAX_PATH.
    config.write_text("[core]\n\tlongpaths = true\n", encoding="utf-8")
    inherited = ("GIT_", "GITHUB_", "RUNNER_", "ACTIONS_", "CCA_", "RENDER_")
    return {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith(inherited)},
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def make_checkout(
    tmp_path: Path,
    env: dict[str, str],
    *,
    tag_has_blueprint: bool,
    edit: Callable[[str], str] | None = None,
) -> Path:
    """The repository as actions/checkout leaves it: the tag v0.1.1 and the helpers.

    With ``edit``, a later commit on the branch changes the Blueprint's text with it: the
    checkout is then the branch (a run from main), not the tag.
    """
    repo = tmp_path / "checkout"
    render, hf = repo / "deploy" / "render", repo / "deploy" / "huggingface"
    render.mkdir(parents=True)
    hf.mkdir(parents=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    _git("init", "--quiet", "--initial-branch=main", str(repo), env=env)
    shutil.copyfile(DEPLOY / "service.py", render / "service.py")
    shutil.copyfile(DEPLOY / "blueprint.py", render / "blueprint.py")
    shutil.copyfile(ROOT / "deploy" / "huggingface" / "space.py", hf / "space.py")
    if tag_has_blueprint:
        shutil.copyfile(BLUEPRINT, render / "render.yaml")
    _git("add", "--all", cwd=repo, env=env)
    _git(*ident, "commit", "--quiet", "-m", "release", cwd=repo, env=env)
    _git(*ident, "tag", "-a", f"v{VERSION}", "-m", f"CCA {VERSION}", cwd=repo, env=env)
    if edit is not None:
        text = BLUEPRINT.read_text(encoding="utf-8")
        changed = edit(text)
        assert changed != text
        (render / "render.yaml").write_text(changed, encoding="utf-8", newline="\n")
        _git("add", "--all", cwd=repo, env=env)
        _git(*ident, "commit", "--quiet", "-m", "Blueprint on main", cwd=repo, env=env)
    return repo


class JobRun:
    """The deploy job's ``run`` steps as the runner runs them: bash -eo pipefail, env from YAML."""

    def __init__(
        self,
        tmp: Path,
        env: dict[str, str],
        context: Mapping[str, str],
        *,
        tag_has_blueprint: bool = True,
        edit: Callable[[str], str] | None = None,
    ) -> None:
        self.tmp, self.context = tmp, dict(context)
        self.checkout = make_checkout(tmp, env, tag_has_blueprint=tag_has_blueprint, edit=edit)
        self.outputs: dict[str, dict[str, str]] = {}
        self.results: list[subprocess.CompletedProcess[str]] = []
        self.routes: dict[str, list[Any]] = {}
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake = tmp / "fake_python.py"
        fake.write_text(FAKE_PYTHON, encoding="utf-8")
        python = Path(sys.executable).as_posix()
        shims = {
            "python3": f'#!/bin/sh\nexec "{python}" "{fake.as_posix()}" "$@"\n',
            "gh": FAKE_GH,
        }
        for name, text in shims.items():
            path = bin_dir / name
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.summary = tmp / "summary.md"
        self.summary.write_text("", encoding="utf-8")
        self.http_log = tmp / "http.log"
        self.http_log.write_text("", encoding="utf-8")
        self.routes_file = tmp / "routes.json"
        self.env = {
            **env,
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "DonQuaan/CCA",
            "GITHUB_STEP_SUMMARY": self.summary.as_posix(),
            "CCA_FAKE_HTTP": self.routes_file.as_posix(),
            "CCA_FAKE_HTTP_LOG": self.http_log.as_posix(),
        }
        self.answer("GET", "ghcr.io", PULL_AUTH_PATH, 200, json.dumps({"token": "anon-pull-token"}))
        manifest = f"/v2/donquaan/cca/manifests/{VERSION}"
        self.answer("GET", "ghcr.io", manifest, 200, MANIFEST.decode(), **{"content-type": OCI})
        self.answer("POST", "api.render.com", HOOK_PATH, 200, json.dumps(STARTED))
        body = {"status": "ok", "version": VERSION, "ready": True}
        self.answer("GET", HOST, "/healthz", 200, json.dumps(body))

    def answer(
        self, method: str, host: str, path: str, status: int, body: str, **headers: str
    ) -> None:
        self.routes[f"{method} {host} {path}"] = [status, headers, body]
        self.routes_file.write_text(json.dumps(self.routes), encoding="utf-8")

    def resolve(self, value: str) -> str:
        def one(match: re.Match[str]) -> str:
            expression = match.group(1)
            output = OUTPUT_REF.fullmatch(expression)
            if output:
                return self.outputs.get(output[1], {}).get(output[2], "")
            assert expression in self.context, f"the harness does not know {expression!r}"
            return self.context[expression]

        return EXPRESSION.sub(one, value)

    def run(self, s: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        n = len(self.results)
        output = self.tmp / f"output-{n}"
        output.write_text("", encoding="utf-8")
        script = self.tmp / f"step-{n}.sh"
        script.write_text(str(s["run"]), encoding="utf-8", newline="\n")
        env = {**self.env, "GITHUB_OUTPUT": output.as_posix()}
        env.update({k: self.resolve(str(v)) for k, v in s.get("env", {}).items()})
        assert BASH is not None
        done = subprocess.run(
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", script.as_posix()],
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        self.results.append(done)
        if "id" in s:
            lines = output.read_text(encoding="utf-8").splitlines()
            self.outputs[s["id"]] = {k: v for k, _, v in (x.partition("=") for x in lines)}
        return done

    def run_all(self) -> subprocess.CompletedProcess[str] | None:
        """Run every step after the checkout; the first failure, if any."""
        for s in steps()[1:]:  # steps()[0] is actions/checkout: the checkout is ready
            done = self.run(s)
            if done.returncode != 0:
                return done
        return None

    def logs(self) -> str:
        return "".join(r.stdout + r.stderr for r in self.results)

    def requests(self) -> list[list[Any]]:
        lines = self.http_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


def context(version: str = "latest", *, hook: str = HOOK, url: str = URL) -> dict[str, str]:
    """The ``${{ }}`` values of a manual run (workflow_dispatch) with this input."""
    return {
        "vars.RENDER_URL": url,
        "secrets.RENDER_DEPLOY_HOOK_URL": hook,
        "secrets.RENDER_DEPLOY_HOOK_URL != ''": "true" if hook else "false",
        "github.token": "ghs_fake_github_token",
        "github.event_name == 'release' && github.event.release.tag_name || inputs.version": (
            version
        ),
    }


@pytest.fixture
def new_job(tmp_path: Path, git_env: dict[str, str]) -> Callable[..., JobRun]:
    def make(
        values: Mapping[str, str],
        *,
        tag_has_blueprint: bool = True,
        edit: Callable[[str], str] | None = None,
    ) -> JobRun:
        return JobRun(tmp_path, git_env, values, tag_has_blueprint=tag_has_blueprint, edit=edit)

    return make


def test_the_whole_job_deploys_the_latest_release_by_digest(
    new_job: Callable[..., JobRun],
) -> None:
    job_run = new_job(context())
    assert job_run.run_all() is None, job_run.logs()
    assert job_run.outputs["version"] == {"version": VERSION}
    assert job_run.outputs["digest"] == {"digest": MANIFEST_DIGEST}
    posts = [r for r in job_run.requests() if r[0] == "POST"]
    assert [(m, h, p) for m, h, p, _ in posts] == [("POST", "api.render.com", HOOK_PATH)]
    methods_hosts = {(m, h) for m, h, _, _ in job_run.requests()}
    assert methods_hosts == {("GET", "ghcr.io"), ("POST", "api.render.com"), ("GET", HOST)}
    logs = job_run.logs()
    assert "deploy started: dep-d1a2b3c4" in logs
    assert f"live: {URL}/healthz serves version {VERSION}, engine ready" in logs
    summary = job_run.summary.read_text(encoding="utf-8")
    assert f"Deployed CCA {VERSION} (ghcr.io/donquaan/cca@{MANIFEST_DIGEST}) to {URL}" in summary
    # The hook and its key never reach a log, an output or the summary.
    for text in (logs, summary, json.dumps(job_run.outputs)):
        assert KEY not in text
        assert "srv-abc123def" not in text


@pytest.mark.parametrize(
    ("values", "problems"),
    [
        (context(hook=""), ["the secret RENDER_DEPLOY_HOOK_URL is missing"]),
        (context(url=""), ["the variable RENDER_URL is missing"]),
        (context(hook="", url="http://x"), ["the secret", "the variable RENDER_URL is not"]),
    ],
)
def test_missing_settings_stop_the_run_first(
    new_job: Callable[..., JobRun], values: dict[str, str], problems: list[str]
) -> None:
    job_run = new_job(values)
    failed = job_run.run_all()
    assert failed is not None
    assert len(job_run.results) == 1  # the settings check, nothing after it
    annotations = failed.stderr.splitlines()
    assert len(annotations) == len(problems)
    for line, problem in zip(annotations, problems, strict=True):
        assert line.startswith(f"::error title=deploy-render::{problem}")
    assert job_run.requests() == []


def test_a_version_without_the_blueprint_is_refused_before_render_is_asked(
    new_job: Callable[..., JobRun],
) -> None:
    job_run = new_job(context(version="v0.1.1"), tag_has_blueprint=False)
    failed = job_run.run_all()
    assert failed is not None
    assert failed.stderr.startswith(
        "::error title=deploy-render::the tag v0.1.1 has no deploy/render/render.yaml"
    )
    assert job_run.requests() == []


CLIENT_IP_AS_SHIPPED = "--trusted-proxies 0 --log-forwarded-hops"


def test_a_version_without_an_option_of_the_branchs_blueprint_is_refused(
    new_job: Callable[..., JobRun],
) -> None:
    """A rollback past an added option: the service would keep main's command."""

    def add_option(text: str) -> str:
        assert text.count(CLIENT_IP_AS_SHIPPED) == 1
        return text.replace(CLIENT_IP_AS_SHIPPED, f"{CLIENT_IP_AS_SHIPPED} --new-option 1")

    job_run = new_job(context(version="v0.1.1"), edit=add_option)
    failed = job_run.run_all()
    assert failed is not None
    (line,) = failed.stderr.splitlines()
    assert line.startswith(
        "::error title=deploy-render::deploy/render/render.yaml passes --new-option, which the "
        "Blueprint of v0.1.1 does not"
    )
    assert job_run.requests() == []  # neither the registry nor Render was asked


def test_a_calibrated_branch_blueprint_deploys_the_release(new_job: Callable[..., JobRun]) -> None:
    """The owner's host name and proxy count on main change values, not options."""

    def calibrate(text: str) -> str:
        return text.replace(
            CLIENT_IP_AS_SHIPPED, "--trusted-proxies 3 --log-forwarded-hops"
        ).replace(
            f"--allowed-host {HOST}\n",
            f"--allowed-host cca-demo-ab12.onrender.com --allowed-host {HOST}\n",
        )

    job_run = new_job(context(), edit=calibrate)
    assert job_run.run_all() is None, job_run.logs()
    assert "the Blueprint of v0.1.1 passes every option of deploy/render/render.yaml here" in (
        job_run.logs()
    )


def test_a_refused_hook_fails_the_trigger_without_showing_it(
    new_job: Callable[..., JobRun],
) -> None:
    job_run = new_job(context())
    job_run.answer("POST", "api.render.com", HOOK_PATH, 401, '{"message":"bad key"}')
    failed = job_run.run_all()
    assert failed is not None
    (line,) = failed.stderr.splitlines()
    assert line.startswith("::error title=deploy-render::the deploy hook answered HTTP 401")
    assert KEY not in job_run.logs()
    assert not [r for r in job_run.requests() if r[1] == HOST]  # no wait after a refusal


def test_a_malformed_hook_secret_is_never_sent_or_shown(new_job: Callable[..., JobRun]) -> None:
    job_run = new_job(context(hook=f"https://evil.example/deploy/srv-abc123def?key={KEY}"))
    failed = job_run.run_all()
    assert failed is not None
    assert "is not a Render deploy hook" in failed.stderr
    assert KEY not in job_run.logs()
    assert [r for r in job_run.requests() if r[0] == "POST"] == []


def test_a_421_fails_the_wait_with_the_allowed_host_fix(new_job: Callable[..., JobRun]) -> None:
    job_run = new_job(context())
    job_run.answer("GET", HOST, "/healthz", 421, '{"error":"unknown Host"}')
    failed = job_run.run_all()
    assert failed is not None
    assert "answered 421" in failed.stderr
    assert "--allowed-host line of deploy/render/render.yaml" in failed.stderr
    assert "Deployed" not in job_run.summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("needle", "env", "problem"),
    [
        ("service.py version", {"REQUESTED": "v9.9"}, "not a release version: 'v9.9'"),
        ("service.py digest", {"VERSION": "9.9.9"}, "ghcr.io/donquaan/cca:9.9.9 does not exist"),
    ],
)
def test_a_failing_helper_shows_its_annotation_in_the_log(
    new_job: Callable[..., JobRun], needle: str, env: dict[str, str], problem: str
) -> None:
    """The helper runs inside ``x=$(...)``: its error must not be captured with its stdout."""
    job_run = new_job(context())
    job_run.answer("GET", "ghcr.io", "/v2/donquaan/cca/manifests/9.9.9", 404, "{}")
    s = step(needle)
    done = job_run.run({**s, "env": env | {"GH_TOKEN": "x"}})
    assert done.returncode == 1
    (line,) = done.stderr.splitlines()
    assert line.startswith("::error title=deploy-render::"), done.stderr
    assert problem in line
    assert (done.stdout, job_run.outputs[s["id"]]) == ("", {})
