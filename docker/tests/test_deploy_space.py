"""The Hugging Face Space deploy: workflow guards, Space files and deploy/huggingface/space.py.

Nothing here touches the network. The helper's HTTP calls go to fakes; the workflow's steps run
under bash, as the runner runs them, with a ``python3`` whose HTTPS client is scripted, and git
talks to a local smart-HTTP server standing in for the Space's repository (anyone clones, a push
needs the right user name and token).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
import smoke

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

# PyYAML comes with the dev tools (pre-commit depends on it); it ships no type stubs.
yaml = importlib.import_module("yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-space.yml"
DEPLOY = ROOT / "deploy" / "huggingface"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("cca_deploy_space", DEPLOY / "space.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


space = _load_helper()

VERSION = "0.1.1"
DIGEST = "sha256:" + "7f" * 32
HOST = "donquaan-cca.hf.space"
SPACE = "DonQuaan/CCA"
PORT = 8765
CREDENTIAL = "hf_fake_token_value"  # what the tests pretend secrets.HF_TOKEN holds
OLD = "a" * 40  # the commit the Space ran before a deploy
NEW = "b" * 40  # the commit a deploy pushed
# The Space's command after the inherited ENTRYPOINT ["/usr/bin/tini", "--", "cca"].
CMD = [
    "play",
    "--public",
    "--allowed-host",
    HOST,
    "--host",
    smoke.ALL_INTERFACES,
    "--port",
    str(PORT),
    "--no-browser",
    "--human",
    "qre",
    "--trusted-proxies",
    "1",
    "--frame-ancestor",
    "https://huggingface.co",
    "--max-sessions",
    "48",
]
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+?)(?:@(\S+))?(?:\s+#\s*(\S+))?\s*$")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
OUTPUT_REF = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
OUTPUT_LINE = re.compile(r'^\s*echo "(\w+)=\$(\w+)" >> "\$GITHUB_OUTPUT"$', re.MULTILINE)
# Variables the runner sets for every step (the ones this workflow reads).
RUNNER_VARIABLES = frozenset(
    {
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "RUNNER_TEMP",
    }
)
ASSIGNED = re.compile(r"(?:^|[\s;(])([A-Za-z_]\w*)=|\bfor\s+([A-Za-z_]\w*)\s+in\b", re.MULTILINE)
USED = re.compile(r"\$\{?([A-Za-z_]\w*)")
# Anything that would make bash print expanded commands, secrets included.
XTRACE = re.compile(
    r"\bset\s+(?:-[A-Za-z]*x|-o\s+xtrace)|\bbash\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*x"
    r"|\bBASH_XTRACEFD\b|\bSHELLOPTS\b"
)
SH = shutil.which("sh")
BASH = shutil.which("bash")
GIT = shutil.which("git")


# ---------------------------------------------------------------------------- workflow helpers
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


def instructions(dockerfile: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` of a Dockerfile without continuation lines."""
    found = []
    for line in dockerfile.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert not line.rstrip().endswith("\\"), line
        keyword, _, rest = line.strip().partition(" ")
        found.append((keyword.upper(), rest))
    return found


def rendered_dockerfile(digest: str = DIGEST, extra: str = "") -> str:
    template = (DEPLOY / "Dockerfile").read_text(encoding="utf-8") + extra
    rendered: str = space.render_dockerfile(template, VERSION, digest, HOST)
    return rendered


def card_metadata() -> dict[str, Any]:
    card = (DEPLOY / "README.md").read_text(encoding="utf-8")
    data = yaml.safe_load(space.front_matter(card))
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------- workflow
def test_the_only_action_is_checkout_pinned_exactly_as_in_ci() -> None:
    ours = uses_lines(WORKFLOW)
    assert ours == [("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1")]
    assert set(ours) <= set(uses_lines(WORKFLOW.parent / "ci.yml"))
    checkout = step("actions/checkout")
    # No token left in .git/config; every tag (depth 1) for the templates of the version's tag.
    assert checkout["with"] == {"persist-credentials": False, "fetch-tags": True}


def test_triggers_manual_release_and_reusable() -> None:
    wf = workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as true
    assert isinstance(on, dict)
    assert set(on) == {"release", "workflow_dispatch", "workflow_call"}
    assert on["release"] == {"types": ["published"]}
    manual = on["workflow_dispatch"]["inputs"]
    assert set(manual) == {"version", "templates"}
    version = manual["version"]
    assert (version["type"], version["required"], version["default"]) == ("string", True, "latest")
    templates = manual["templates"]
    assert (templates["type"], templates["options"], templates["default"]) == (
        "choice",
        ["release", "checkout"],
        "release",
    )
    called = on["workflow_call"]["inputs"]
    assert set(called) == {"version", "templates"}
    assert (called["version"]["type"], called["version"]["required"]) == ("string", True)
    assert "default" not in called["version"]
    assert (called["templates"]["required"], called["templates"]["default"]) == (False, "release")


def test_permissions_are_read_only() -> None:
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in job()
    assert wf["defaults"] == {"run": {"shell": "bash"}}
    assert [s.get("name") for s in steps() if "shell" in s] == []  # no step drops -eo pipefail


def test_the_job_runs_in_the_protected_environment() -> None:
    # Environment secrets reach only jobs that name the environment, after its rules pass.
    assert job()["environment"] == "huggingface-space"
    assert "`huggingface-space`" in (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")


def test_pre_releases_are_not_deployed_by_the_release_event() -> None:
    condition = job()["if"].replace(" ", "")
    assert condition == "${{github.event_name!='release'||!github.event.release.prerelease}}"


def test_one_deploy_at_a_time_and_a_bounded_job() -> None:
    deploy = job()
    assert deploy["concurrency"] == {"group": "deploy-space", "cancel-in-progress": False}
    assert deploy["runs-on"] == "ubuntu-latest"
    assert 15 < deploy["timeout-minutes"] <= 60


def test_steps_run_in_order() -> None:
    order = [
        index("actions/checkout"),
        index("space.py check-config"),
        index("space.py version"),
        index("space.py digest"),
        index("space.py host"),
        index("space.py render"),
        index("git push origin HEAD:main"),
        index("space.py wait"),
    ]
    assert order == sorted(order) == list(range(len(steps())))


def test_no_expression_is_pasted_into_a_script() -> None:
    """``${{ }}`` in a ``run`` would be template-expanded into the shell (script injection)."""
    for s in steps():
        assert "${{" not in str(s.get("run", "")), s.get("name")


def test_no_step_traces_its_commands() -> None:
    """bash's xtrace prints every command after expansion: the push step's token included."""
    for s in steps():
        assert not XTRACE.search(str(s.get("run", ""))), s.get("name")
    for bad in (
        "set -x",
        "set -ex",
        "set -o xtrace",
        "bash -x f",
        "bash -e -x f",
        "BASH_XTRACEFD=2",
    ):
        assert XTRACE.search(bad), bad


def test_every_step_output_read_is_written_by_an_earlier_step() -> None:
    """Each ``steps.X.outputs.Y`` names an earlier step X that writes ``Y=`` from a command."""
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
            assert f"{variable}=$(" in run, (
                f"{s.get('name')}: ${variable} is not a command's output"
            )
        if "id" in s:
            written[s["id"]] = {name for name, _ in lines}
        else:
            assert not lines, s.get("name")
    assert written == {
        "version": {"version"},
        "digest": {"digest"},
        "space": {"host"},
        "push": {"commit"},
    }
    assert sorted(reads) == sorted(
        ["version.version"] * 4 + ["digest.digest"] * 2 + ["space.host", "push.commit"]
    )


def test_every_shell_variable_is_set_before_use() -> None:
    """A misspelt variable expands to nothing under bash -e: only env, runner or own ones."""
    for s in steps():
        run = str(s.get("run", ""))
        assigned = {a or b for a, b in ASSIGNED.findall(run)}
        known = set(s.get("env", {})) | RUNNER_VARIABLES | assigned
        assert set(USED.findall(run)) <= known, (s.get("name"), set(USED.findall(run)) - known)


PUSH_ENV = {
    "HF_TOKEN": "${{ secrets.HF_TOKEN }}",
    "HF_SPACE": "${{ vars.HF_SPACE }}",
    "HF_USERNAME": "${{ vars.HF_USERNAME }}",
    "VERSION": "${{ steps.version.outputs.version }}",
    "DIGEST": "${{ steps.digest.outputs.digest }}",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
}


def test_the_token_is_only_in_the_push_step() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.") == 2
    assert step("git push origin HEAD:main")["env"] == PUSH_ENV
    holders = [s.get("name") for s in steps() if "HF_TOKEN" in s.get("env", {})]
    assert holders == [step("git push origin HEAD:main")["name"]]


def test_missing_settings_stop_the_run_first() -> None:
    check = step("space.py check-config")
    # Whether the secret is set, as "true" or "false": the step never holds the token itself.
    assert check["env"] == {
        "HF_SPACE": "${{ vars.HF_SPACE }}",
        "HF_USERNAME": "${{ vars.HF_USERNAME }}",
        "HF_TOKEN_PRESENT": "${{ secrets.HF_TOKEN != '' }}",
    }
    run = " ".join(check["run"].split())
    assert run == (
        'python3 deploy/huggingface/space.py check-config --space "$HF_SPACE" '
        '--username "$HF_USERNAME" --token-present "$HF_TOKEN_PRESENT"'
    )


def test_version_comes_from_the_release_or_the_input() -> None:
    version = step("space.py version")
    assert version["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REQUESTED": (
            "${{ github.event_name == 'release' && github.event.release.tag_name "
            "|| inputs.version }}"
        ),
    }
    run = version["run"]
    assert 'if [ "$REQUESTED" = latest ]' in run
    assert 'gh api "repos/${GITHUB_REPOSITORY}/releases/latest" --jq .tag_name' in run
    assert 'version=$(python3 deploy/huggingface/space.py version "$REQUESTED")' in run
    assert version["id"] == "version"


def test_digest_host_render_push_and_wait_steps_feed_each_other() -> None:
    digest = step("space.py digest")
    assert digest["id"] == "digest"
    assert digest["env"] == {"VERSION": "${{ steps.version.outputs.version }}"}
    host = step("space.py host")
    assert host["id"] == "space"
    assert host["env"] == {"HF_SPACE": "${{ vars.HF_SPACE }}"}
    render = step("space.py render")
    assert render["env"] == {
        "VERSION": "${{ steps.version.outputs.version }}",
        "DIGEST": "${{ steps.digest.outputs.digest }}",
        "HOST_NAME": "${{ steps.space.outputs.host }}",
        "TEMPLATES": "${{ inputs.templates }}",
    }
    assert (
        'space.py render --source "$templates" "$VERSION" "$DIGEST" "$HOST_NAME" '
        '"$RUNNER_TEMP/space-files"' in render["run"]
    )
    assert step("git push origin HEAD:main")["id"] == "push"
    wait = step("space.py wait")
    assert wait["env"] == {
        "HF_SPACE": "${{ vars.HF_SPACE }}",
        "VERSION": "${{ steps.version.outputs.version }}",
        "COMMIT": "${{ steps.push.outputs.commit }}",
    }
    assert (
        'python3 deploy/huggingface/space.py wait "$HF_SPACE" "$VERSION" --commit "$COMMIT" '
        "--timeout 900 --interval 15" in wait["run"]
    )


def test_every_helper_call_names_a_real_subcommand() -> None:
    choices = set(space.build_parser()._subparsers._group_actions[0].choices)
    called = {
        m.group(1)
        for s in steps()
        for m in re.finditer(r"deploy/huggingface/space\.py (\S+)", str(s.get("run", "")))
    }
    assert called == choices == {"check-config", "version", "digest", "host", "render", "wait"}


def test_push_step_keeps_the_token_off_command_lines_urls_and_logs() -> None:
    push = step("git push origin HEAD:main")
    run: str = push["run"]
    assert push["env"]["GIT_TERMINAL_PROMPT"] == "0"
    token_lines = [line.strip() for line in run.splitlines() if "HF_TOKEN" in line]
    assert token_lines == [
        "# git gets the password from this helper, which reads HF_TOKEN from its own environment:",
        'helper=\'!f() { test "$1" = get || return 0; echo "username=${GIT_USER}"; '
        'echo "password=${HF_TOKEN}"; }; f\'',
    ]
    assert "@huggingface.co" not in run
    assert 'git() { command git -c credential.helper= -c "credential.helper=$helper" "$@"; }' in (
        run
    )
    assert 'git clone --depth 1 "https://huggingface.co/spaces/${HF_SPACE}" "$repo"' in run


def test_the_helper_and_the_release_images_agree_on_the_image_name() -> None:
    assert space.IMAGE == smoke.IMAGE_NAME
    image = yaml.safe_load((WORKFLOW.parent / "image.yml").read_text(encoding="utf-8"))
    assert image["env"]["IMAGE"] == space.IMAGE


# ---------------------------------------------------------------------------- Space files
def test_the_space_dockerfile_only_pins_the_release_image_and_sets_the_command() -> None:
    rendered = rendered_dockerfile()
    found = instructions(rendered)
    assert [k for k, _ in found] == ["FROM", "USER", "WORKDIR", "ENV", "CMD"]
    assert dict(found)["FROM"] == f"{space.IMAGE}:{VERSION}@{DIGEST}"
    assert "{{" not in rendered
    assert "}}" not in rendered


def test_the_space_command_is_public_mode_behind_the_inherited_tini_entrypoint() -> None:
    found = dict(instructions(rendered_dockerfile()))
    assert "ENTRYPOINT" not in found
    assert "HEALTHCHECK" not in found
    assert json.loads(found["CMD"]) == CMD


def test_the_space_runs_as_uid_1000_from_a_readable_directory() -> None:
    found = dict(instructions(rendered_dockerfile()))
    assert found["USER"] == "1000:1000"
    assert found["WORKDIR"] == "/"
    assert found["ENV"] == "HOME=/tmp"
    root = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # The release image's user is 10001 and its home (created by useradd) is private.
    assert "USER 10001:10001" in root
    assert "WORKDIR /home/cca" in root


def test_the_space_command_parses_with_the_current_cca_cli() -> None:
    cli = pytest.importorskip("cca.cli")
    args = cli.build_parser().parse_args(CMD)
    assert (args.command, args.host, args.port, args.human) == (
        "play",
        smoke.ALL_INTERFACES,
        PORT,
        "qre",
    )
    assert args.public is True
    assert args.allowed_host == [HOST]
    assert args.trusted_proxies == 1
    assert args.frame_ancestor == ["https://huggingface.co"]
    assert args.max_sessions == 48


def test_the_space_card_front_matter() -> None:
    meta = card_metadata()
    colours = {"red", "yellow", "green", "blue", "indigo", "purple", "pink", "gray"}
    assert meta["title"] == "CCA"
    assert meta["sdk"] == "docker"
    assert meta["license"] == "apache-2.0"
    assert meta["pinned"] is False
    assert meta["colorFrom"] in colours
    assert meta["colorTo"] in colours
    assert meta["emoji"] == "♟️"
    # The Hub shows it on the thumbnail; 60 characters is the longest seen on public Spaces.
    assert 0 < len(meta["short_description"]) <= 60
    assert set(meta) == {
        "title",
        "emoji",
        "colorFrom",
        "colorTo",
        "sdk",
        "app_port",
        "pinned",
        "license",
        "short_description",
    }


def test_app_port_is_the_port_the_command_and_the_healthcheck_use() -> None:
    port = card_metadata()["app_port"]
    assert port == PORT
    assert type(port) is int
    cmd = json.loads(dict(instructions(rendered_dockerfile()))["CMD"])
    assert cmd[cmd.index("--port") + 1] == str(port)
    root = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"http://127.0.0.1:{port}/healthz" in root
    assert f"EXPOSE {port}" in root


def test_the_space_card_tells_visitors_what_they_get() -> None:
    card = space.render_card((DEPLOY / "README.md").read_text(encoding="utf-8"), VERSION)
    for needle in (
        f"CCA {VERSION}",
        "QRE",
        "free CPU",
        "memory only",
        "## Fair play",
        "rated",
        "cheating",
        "hypotheses",
        f"https://github.com/DonQuaan/CCA/tree/v{VERSION}",
        f"https://github.com/DonQuaan/CCA/blob/v{VERSION}/THIRD_PARTY_NOTICES.md",
        f"https://github.com/DonQuaan/CCA/blob/v{VERSION}/LICENSE",
        f"https://github.com/DonQuaan/CCA/blob/v{VERSION}/docs/simulator.md",
        "GPL-3.0-or-later",
    ):
        assert needle in card, needle
    assert "{{" not in card


def test_templates_use_lf_line_endings() -> None:
    for path in (
        DEPLOY / "Dockerfile",
        DEPLOY / "README.md",
        DEPLOY / "space.py",
        WORKFLOW,
        ROOT / "docs" / "deploy.md",
    ):
        assert b"\r" not in path.read_bytes(), path.name


# ---------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("raw", "tag"),
    [
        ("0.1.1", "0.1.1"),
        ("v0.1.1", "0.1.1"),
        ("v10.20.30", "10.20.30"),
        ("0.2.0rc1", "0.2.0rc1"),
        ("0.2.0a2", "0.2.0a2"),
        ("0.2.0.post1", "0.2.0.post1"),
        ("0.2.0.dev3", "0.2.0.dev3"),
    ],
)
def test_release_names_map_to_image_tags(raw: str, tag: str) -> None:
    assert space.check_version(raw) == tag


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "latest",
        "v",
        "0.1",
        "01.2.3",
        "V0.1.1",
        "vv0.1.1",
        "0.1.1-rc1",
        "0.1.1 ",
        "0.1.1\n",
        "0.1.1;id",
        "0.1.1/../x",
        "$(id)",
    ],
)
def test_other_release_names_are_refused(raw: str) -> None:
    with pytest.raises(space.SpaceError, match="not a release version"):
        space.check_version(raw)


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "7f" * 32,
        "sha256:" + "7F" * 32,
        "sha256:" + "7f" * 31,
        "sha256:" + "7f" * 33,
        "sha256:" + "7f" * 32 + "\n",
        " sha256:" + "7f" * 32,
        "sha512:" + "7f" * 64,
    ],
)
def test_bad_digests_are_refused(digest: str) -> None:
    with pytest.raises(space.SpaceError, match="not an image digest"):
        space.check_digest(digest)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "hf.space",
        "x.hf.space.evil.com",
        "https://x.hf.space",
        "X.hf.space",
        "-x.hf.space",
        "a.b.hf.space",
        "x.hf.space:443",
        "x_y.hf.space",
    ],
)
def test_bad_host_names_are_refused(host: str) -> None:
    with pytest.raises(space.SpaceError, match="not a Space host name"):
        space.check_host(host)


def test_good_settings_have_no_problems() -> None:
    assert space.config_problems(SPACE, token_present=True) == []
    assert space.config_problems("my-org/cca.demo_1", token_present=True, username="don") == []


def test_a_missing_token_is_reported() -> None:
    (problem,) = space.config_problems(SPACE, token_present=False)
    assert "secret HF_TOKEN is missing" in problem
    assert "environment huggingface-space" in problem
    assert "docs/deploy.md" in problem


def test_a_missing_space_is_reported_with_the_token() -> None:
    problems = space.config_problems("", token_present=False)
    assert len(problems) == 2
    assert "variable HF_SPACE is missing" in problems[1]


@pytest.mark.parametrize(
    "value",
    ["DonQuaan", "a/b/c", "Don Quaan/CCA", "-x/y", "x/y.", "x/.y", "$(id)/x", "x/y\n", "/CCA"],
)
def test_a_malformed_space_is_reported(value: str) -> None:
    (problem,) = space.config_problems(value, token_present=True)
    assert "HF_SPACE must be OWNER/NAME" in problem


def test_a_malformed_username_is_reported() -> None:
    (problem,) = space.config_problems(SPACE, token_present=True, username="a b")
    assert "HF_USERNAME" in problem


def test_check_config_annotates_every_problem_on_stderr_on_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code = space.main(["check-config", "--space", "", "--token-present", "false"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    err = captured.err.splitlines()
    assert len(err) == 2
    assert all(line.startswith("::error title=deploy-space::the ") for line in err)
    assert space.main(["check-config", "--space", SPACE, "--token-present", "true"]) == 0
    assert capsys.readouterr() == ("", "")


def test_errors_outside_github_go_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert space.main(["version", "latest"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: not a release version: 'latest'")


def test_annotations_escape_newlines_and_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    out = io.StringIO()
    space.report_error("50%\n::warning::x\r", out)
    assert out.getvalue() == "::error title=deploy-space::50%25%0A::warning::x%0D\n"


# ---------------------------------------------------------------------------- rendering
TEMPLATE = (
    "# comment\n"
    "FROM ghcr.io/donquaan/cca:{{CCA_VERSION}}@{{CCA_IMAGE_DIGEST}}\n"
    'CMD ["play", "--allowed-host", "{{CCA_SPACE_HOST}}"]\n'
)


def test_render_fills_every_slot() -> None:
    assert space.render_dockerfile(TEMPLATE, "v" + VERSION, DIGEST, HOST) == (
        "# comment\n"
        f"FROM ghcr.io/donquaan/cca:{VERSION}@{DIGEST}\n"
        f'CMD ["play", "--allowed-host", "{HOST}"]\n'
    )


@pytest.mark.parametrize(
    "template",
    [
        TEMPLATE.replace("FROM ghcr.io/donquaan/cca", "FROM ghcr.io/someone/else"),
        TEMPLATE + "FROM ghcr.io/donquaan/cca:{{CCA_VERSION}}@{{CCA_IMAGE_DIGEST}}\n",
        TEMPLATE.replace('"{{CCA_SPACE_HOST}}"', '"x.hf.space"'),
        TEMPLATE + "# {{CCA_VERSION}}\n",
        TEMPLATE.replace("\nFROM", "\nfrom"),
    ],
)
def test_render_needs_the_from_line_and_each_slot_once(template: str) -> None:
    with pytest.raises(space.SpaceError, match="exactly once"):
        space.render_dockerfile(template, VERSION, DIGEST, HOST)


def test_render_refuses_unknown_placeholders() -> None:
    with pytest.raises(space.SpaceError, match=re.escape("unknown placeholder '{{OTHER}}'")):
        space.render_dockerfile(TEMPLATE + "LABEL x={{OTHER}}\n", VERSION, DIGEST, HOST)


@pytest.mark.parametrize(
    ("version", "digest", "host", "problem"),
    [
        ("latest", DIGEST, HOST, "release version"),
        (VERSION, "sha256:x", HOST, "image digest"),
        (VERSION, DIGEST, "evil.example", "host name"),
    ],
)
def test_render_checks_its_inputs(version: str, digest: str, host: str, problem: str) -> None:
    with pytest.raises(space.SpaceError, match=problem):
        space.render_dockerfile(TEMPLATE, version, digest, host)


def test_card_placeholders_are_body_only() -> None:
    card = "---\ntitle: CCA\n---\n\nCCA {{CCA_VERSION}}\n"
    assert space.render_card(card, "v0.2.0") == "---\ntitle: CCA\n---\n\nCCA 0.2.0\n"
    with pytest.raises(space.SpaceError, match="front matter must not hold placeholders"):
        space.render_card("---\ntitle: CCA {{CCA_VERSION}}\n---\n", VERSION)
    for broken in ("title: CCA\n", "---\ntitle: CCA\n", ""):
        with pytest.raises(space.SpaceError, match="must start with YAML front matter"):
            space.render_card(broken, VERSION)
    with pytest.raises(space.SpaceError, match="unknown placeholder"):
        space.render_card("---\ntitle: CCA\n---\n{{CCA_IMAGE_DIGEST}}\n", VERSION)


def test_write_space_renders_both_files_into_an_empty_directory(tmp_path: Path) -> None:
    out = tmp_path / "new" / "space"
    written = space.write_space(out, VERSION, DIGEST, HOST)
    assert [p.name for p in written] == ["Dockerfile", "README.md"]
    assert sorted(p.name for p in out.iterdir()) == ["Dockerfile", "README.md"]
    assert (out / "Dockerfile").read_text(encoding="utf-8") == rendered_dockerfile()
    card = (out / "README.md").read_bytes()
    assert b"\r" not in card
    assert f"CCA {VERSION}".encode() in card
    empty = tmp_path / "empty"
    empty.mkdir()
    assert len(space.write_space(empty, VERSION, DIGEST, HOST)) == 2


def test_write_space_refuses_a_used_directory_or_a_file(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(space.SpaceError, match="must be a new or empty directory"):
        space.write_space(tmp_path, VERSION, DIGEST, HOST)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]
    with pytest.raises(space.SpaceError, match="must be a new or empty directory"):
        space.write_space(tmp_path / "keep.txt", VERSION, DIGEST, HOST)


def test_the_render_command_prints_what_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert space.main(["render", VERSION, DIGEST, HOST, str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [Path(p).name for p in printed] == ["Dockerfile", "README.md"]


def test_the_render_command_reads_the_templates_from_source(tmp_path: Path) -> None:
    source = tmp_path / "templates"
    source.mkdir()
    (source / "Dockerfile").write_text(TEMPLATE, encoding="utf-8")
    (source / "README.md").write_text("---\ntitle: T\n---\nv{{CCA_VERSION}}\n", encoding="utf-8")
    out = tmp_path / "out"
    assert space.main(["render", "--source", str(source), VERSION, DIGEST, HOST, str(out)]) == 0
    assert (out / "Dockerfile").read_text(encoding="utf-8") == space.render_dockerfile(
        TEMPLATE, VERSION, DIGEST, HOST
    )
    assert (out / "README.md").read_text(encoding="utf-8") == f"---\ntitle: T\n---\nv{VERSION}\n"


# ---------------------------------------------------------------------------- HTTP fakes
class FakeHttp:
    """Answers ``(host, path)`` from a script of replies (the last one repeats)."""

    def __init__(self, routes: dict[tuple[str, str], list[Any]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, host: str, path: str, headers: Any, timeout: float) -> Any:
        self.calls.append((host, path, dict(headers), timeout))
        script = self.routes[(host, path)]
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def reply(status: int, body: bytes | dict[str, Any] = b"", **headers: str) -> Any:
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    return space.Reply(status, {k.replace("_", "-"): v for k, v in headers.items()}, raw)


TOKEN_PATH = ("ghcr.io", "/token?scope=repository:donquaan/cca:pull&service=ghcr.io")
MANIFEST_PATH = ("ghcr.io", f"/v2/donquaan/cca/manifests/{VERSION}")
MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
OCI = "application/vnd.oci.image.manifest.v1+json"


def registry(manifest: Any, token: Any = None) -> FakeHttp:
    token = reply(200, {"token": "anon-pull-token"}) if token is None else token
    return FakeHttp({TOKEN_PATH: [token], MANIFEST_PATH: [manifest]})


# ---------------------------------------------------------------------------- digest
def test_digest_is_the_sha256_of_the_manifest_the_registry_sent() -> None:
    fake = registry(reply(200, MANIFEST, content_type=OCI, docker_content_digest=MANIFEST_DIGEST))
    assert space.resolve_digest("v" + VERSION, fake) == MANIFEST_DIGEST
    (_, _, token_headers, _), (_, _, headers, _) = fake.calls
    assert token_headers == {}
    assert headers["Authorization"] == "Bearer anon-pull-token"
    assert OCI in headers["Accept"]
    assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]


def test_digest_without_a_digest_header_is_still_computed() -> None:
    fake = registry(reply(200, MANIFEST, content_type=f"{OCI}; charset=utf-8"))
    assert space.resolve_digest(VERSION, fake) == MANIFEST_DIGEST


@pytest.mark.parametrize(
    ("manifest", "token", "problem"),
    [
        (reply(200, MANIFEST, content_type=OCI, docker_content_digest=DIGEST), None, "hashes to"),
        (reply(404), None, f"{VERSION} does not exist"),
        (reply(401), None, "answered HTTP 401"),
        (reply(200, MANIFEST, content_type="text/html"), None, "unexpected manifest media type"),
        (reply(200, MANIFEST), None, "unexpected manifest media type"),
        (reply(200, MANIFEST, content_type=OCI), reply(401), "no anonymous pull token"),
        (reply(200, MANIFEST, content_type=OCI), reply(200, {"token": ""}), "no anonymous"),
        (reply(200, MANIFEST, content_type=OCI), reply(200, b"<html>"), "no anonymous"),
    ],
)
def test_digest_refuses_inconsistent_or_missing_images_at_once(
    manifest: Any, token: Any, problem: str
) -> None:
    sleeps: list[float] = []
    with pytest.raises(space.SpaceError, match=problem) as caught:
        space.resolve_digest(VERSION, registry(manifest, token), sleeps.append)
    assert "anon-pull-token" not in str(caught.value)
    assert sleeps == []  # a permanent answer is not asked again


def test_digest_retries_rate_limits_server_errors_and_outages() -> None:
    sleeps: list[float] = []
    fake = FakeHttp(
        {
            TOKEN_PATH: [reply(429), reply(200, {"token": "anon-pull-token"})],
            MANIFEST_PATH: [
                http.client.RemoteDisconnected("closed"),
                reply(503),
                reply(200, MANIFEST, content_type=OCI),
            ],
        }
    )
    with pytest.raises(space.SpaceError, match=r"after 3 tries: ghcr\.io manifest: HTTP 503"):
        space.resolve_digest(VERSION, fake, sleeps.append)
    assert sleeps == [space.PAUSE_S] * 2
    assert space.resolve_digest(VERSION, fake, sleeps.append) == MANIFEST_DIGEST


def test_digest_gives_up_after_its_attempts() -> None:
    sleeps: list[float] = []
    fake = registry(reply(200, MANIFEST, content_type=OCI), token=reply(502))
    with pytest.raises(space.SpaceError, match=r"after 3 tries: ghcr\.io token: HTTP 502"):
        space.resolve_digest(VERSION, fake, sleeps.append)
    assert (len(fake.calls), len(sleeps)) == (3, 2)


def test_digest_checks_the_version_before_any_request() -> None:
    fake = registry(reply(200, MANIFEST, content_type=OCI))
    with pytest.raises(space.SpaceError, match="not a release version"):
        space.resolve_digest("latest", fake)
    assert fake.calls == []


def test_digest_and_network_errors_through_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(space, "resolve_digest", lambda version: f"sha256:{version}")
    assert space.main(["digest", VERSION]) == 0
    assert capsys.readouterr().out == f"sha256:{VERSION}\n"

    def offline(version: str) -> str:
        raise TimeoutError(version)

    monkeypatch.setattr(space, "resolve_digest", offline)
    assert space.main(["digest", VERSION]) == 1
    assert "network error: TimeoutError" in capsys.readouterr().err


# ---------------------------------------------------------------------------- host lookup
API = ("huggingface.co", f"/api/spaces/{SPACE}")
RUNTIME = ("huggingface.co", f"/api/spaces/{SPACE}/runtime")
HEALTH = (HOST, "/healthz")


def space_api(sha: str | None = NEW) -> Any:
    body: dict[str, Any] = {"host": f"https://{HOST}", "subdomain": "x"}
    if sha is not None:
        body["sha"] = sha
    return reply(200, body)


def test_space_info_comes_from_the_hub_api() -> None:
    assert space.space_info(SPACE, FakeHttp({API: [space_api()]})) == space.SpaceInfo(HOST, NEW)
    for odd in (None, "abc", "B" * 40, 40):
        body = {"host": f"https://{HOST}", "sha": odd}
        assert space.space_info(SPACE, FakeHttp({API: [reply(200, body)]})).commit is None


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_a_missing_or_private_space_is_permanent(status: int) -> None:
    with pytest.raises(space.SpaceError, match="create it first, and make it public"):
        space.space_info(SPACE, FakeHttp({API: [reply(status)]}))


@pytest.mark.parametrize(
    "host", [None, "", HOST, "http://" + HOST, "https://evil.example", "https://X.hf.space"]
)
def test_an_unexpected_host_is_refused(host: str | None) -> None:
    fake = FakeHttp({API: [reply(200, {"host": host})]})
    with pytest.raises(space.SpaceError, match="unexpected host"):
        space.space_info(SPACE, fake)


def test_a_bad_space_id_is_refused_without_a_request() -> None:
    fake = FakeHttp({})
    with pytest.raises(space.SpaceError, match="not a Space id"):
        space.space_info("../x", fake)
    assert fake.calls == []


def test_lookup_host_retries_rate_limits_and_outages() -> None:
    sleeps: list[float] = []
    fake = FakeHttp(
        {API: [reply(429), http.client.BadStatusLine("x"), OSError("reset"), space_api()]}
    )
    with pytest.raises(space.SpaceError, match="after 3 tries: OSError"):
        space.lookup_host(SPACE, fake, sleeps.append)
    assert sleeps == [space.PAUSE_S] * 2
    assert space.lookup_host(SPACE, fake, sleeps.append) == HOST


def test_lookup_host_gives_up_after_its_attempts() -> None:
    sleeps: list[float] = []
    fake = FakeHttp({API: [reply(503)]})
    with pytest.raises(space.SpaceError, match="after 3 tries: Hub API: HTTP 503"):
        space.lookup_host(SPACE, fake, sleeps.append)
    assert len(fake.calls) == space.ATTEMPTS == 3
    assert len(sleeps) == 2


def test_lookup_host_does_not_retry_a_private_space() -> None:
    sleeps: list[float] = []
    fake = FakeHttp({API: [reply(404)]})
    with pytest.raises(space.SpaceError, match="make it public"):
        space.lookup_host(SPACE, fake, sleeps.append)
    assert (len(fake.calls), sleeps) == (1, [])


# ---------------------------------------------------------------------------- health polling
def healthz(version: str = VERSION, *, ready: object = True, status: str = "ok") -> Any:
    return reply(200, {"status": status, "version": version, "ready": ready})


def engine_failed(version: str = VERSION, error: str = "the engine process stopped") -> Any:
    body = {"status": "error", "version": version, "ready": False, "error": error}
    return reply(503, body)


def rt(stage: str, sha: str | None = None, error: str | None = None) -> Any:
    body: dict[str, Any] = {"stage": stage}
    if sha is not None:
        body["sha"] = sha
    if error is not None:
        body["errorMessage"] = error
    return reply(200, body)


@pytest.mark.parametrize(
    ("answer", "ok", "failed"),
    [
        (healthz(), True, False),
        (healthz("0.1.0"), False, False),
        (healthz(ready=False), False, False),
        (healthz(ready="true"), False, False),
        (healthz(status="error"), False, False),
        (reply(200, {"status": "ok", "ready": True}), False, False),
        (reply(200, b"<html>building</html>"), False, False),
        (reply(200, b"[1]"), False, False),
        (reply(503, {"status": "ok", "version": VERSION, "ready": True}), False, False),
        (engine_failed(), False, True),
        (engine_failed("0.1.0"), False, False),
        (reply(502, {"status": "error", "version": VERSION}), False, False),
        (reply(302, location="https://huggingface.co/login"), False, False),
        (TimeoutError("slow"), False, False),
        (space.SpaceError("too big"), False, False),
    ],
)
def test_healthy_means_the_version_with_its_engines_ready(
    answer: Any, ok: bool, failed: bool
) -> None:
    health = space.probe(HOST, VERSION, FakeHttp({HEALTH: [answer]}))
    assert (health.ok, health.failed) == (ok, failed)
    assert health.said.startswith("healthz: ")


def test_what_the_space_said_is_printed_escaped() -> None:
    health = space.probe(HOST, VERSION, FakeHttp({HEALTH: [healthz("0.1\n::error::x")]}))
    assert "\n" not in health.said
    assert "version='0.1\\n::error::x'" in health.said
    health = space.probe(HOST, VERSION, FakeHttp({HEALTH: [engine_failed(error="a\nb")]}))
    assert health.said == (
        "healthz: HTTP 503 status='error' version='0.1.1' ready=False error='a\\nb'"
    )
    assert space.probe(HOST, VERSION, FakeHttp({HEALTH: [reply(200)]})).said == (
        "healthz: HTTP 200 without a JSON object"
    )
    assert space.probe(HOST, VERSION, FakeHttp({HEALTH: [reply(404)]})).said == "healthz: HTTP 404"


def test_runtime_reports_stage_commit_and_error_and_never_raises() -> None:
    runtime = space.runtime
    assert runtime(SPACE, FakeHttp({RUNTIME: [rt("RUNNING", NEW)]})) == space.Runtime(
        "RUNNING", NEW, None
    )
    bad = FakeHttp({RUNTIME: [rt("RUNTIME_ERROR", error="x\ny")]})
    assert runtime(SPACE, bad) == space.Runtime("RUNTIME_ERROR", None, "x\ny")
    odd = FakeHttp({RUNTIME: [reply(200, {"stage": 3, "sha": "short", "errorMessage": ""})]})
    assert runtime(SPACE, odd) == space.Runtime("", None, None)
    assert runtime(SPACE, FakeHttp({RUNTIME: [reply(429)]})) == "HTTP 429"
    assert runtime(SPACE, FakeHttp({RUNTIME: [ConnectionResetError()]})) == "ConnectionResetError"
    assert runtime(SPACE, FakeHttp({RUNTIME: [http.client.BadStatusLine("x")]})) == (
        "BadStatusLine"
    )
    assert runtime(SPACE, FakeHttp({RUNTIME: [space.SpaceError("big")]})) == "SpaceError"


def test_runtime_describes_itself_escaped() -> None:
    now = space.Runtime("RUNTIME_ERROR", OLD, "boom\n::x")
    assert now.describe(NEW) == (
        "stage='RUNTIME_ERROR' running=aaaaaaa want=bbbbbbb error='boom\\n::x'"
    )
    assert space.Runtime("BUILDING", None, None).describe(None) == (
        "stage='BUILDING' running=? want=?"
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        assert len(self.sleeps) < 1000, "the poll never gives up"


def poll(
    routes: dict[tuple[str, str], list[Any]],
    *,
    timeout: float = 60.0,
    commit: str | None = NEW,
    clock: Clock | None = None,
) -> tuple[str, str]:
    """``wait_healthy`` against scripted answers: the URL and the log it printed."""
    clock = clock or Clock()
    out = io.StringIO()
    try:
        url = space.wait_healthy(
            SPACE,
            VERSION,
            commit=commit,
            timeout=timeout,
            interval=15.0,
            get=FakeHttp(routes),
            clock=clock,
            sleep=clock.sleep,
            out=out,
        )
    except space.SpaceError as exc:
        exc.add_note(out.getvalue())
        raise
    return url, out.getvalue()


def test_wait_returns_once_the_new_commit_runs_and_is_ready() -> None:
    clock = Clock()
    routes = {
        API: [OSError("down"), space_api(OLD)],
        # The Hub has not noticed the push yet, then builds and starts it: the previous
        # container, same version, answers /healthz all along.
        RUNTIME: [
            rt(space.RUNNING, OLD),
            rt("RUNNING_BUILDING", OLD),
            rt("RUNNING_APP_STARTING", OLD),
            rt(space.RUNNING, NEW),
        ],
        HEALTH: [healthz(), healthz(), healthz(ready=False), healthz()],
    }
    url, log = poll(routes, clock=clock)
    assert url == f"https://{HOST}/healthz"
    assert clock.sleeps == [15.0] * 4
    lines = log.splitlines()
    assert lines[0] == "[    0s] stage='RUNNING' running=aaaaaaa want=bbbbbbb Hub API: OSError"
    assert lines[1].startswith("[   15s] stage='RUNNING_BUILDING' running=aaaaaaa want=bbbbbbb")
    assert "version='0.1.1' ready=True" in lines[1]
    assert "ready=False" in lines[3]
    assert (
        lines[-1] == f"healthy: https://{HOST}/healthz runs commit {NEW} with version 0.1.1 ready"
    )


@pytest.mark.parametrize(
    "still",
    [
        rt("BUILDING"),
        rt("APP_STARTING"),
        rt("RUNNING_BUILDING", OLD),
        rt("RUNNING_APP_STARTING", OLD),
        rt(space.RUNNING, OLD),
        rt("STOPPED"),
        reply(500),
        # Only RUNNING counts, even with the right commit (a rebuild of the same commit).
        rt("RUNNING_BUILDING", NEW),
        rt("APP_STARTING", NEW),
    ],
)
def test_the_previous_container_with_the_same_version_is_not_the_deploy(still: Any) -> None:
    """A redeploy of the same version (changed Space files) waits for its own commit."""
    routes = {API: [space_api(OLD)], RUNTIME: [still], HEALTH: [healthz()]}
    with pytest.raises(
        space.SpaceError, match=r"did not serve version 0\.1\.1 from commit bbbbbbb"
    ):
        poll(routes)


def test_a_running_space_without_a_reported_commit_is_never_confirmed() -> None:
    routes = {API: [space_api()], RUNTIME: [rt(space.RUNNING)], HEALTH: [healthz()]}
    with pytest.raises(space.SpaceError, match="the runtime API reports no commit"):
        poll(routes)


def test_wait_expects_the_space_head_by_default() -> None:
    routes = {API: [space_api(NEW)], RUNTIME: [rt(space.RUNNING, NEW)], HEALTH: [healthz()]}
    url, _ = poll(routes, commit=None)
    assert url == f"https://{HOST}/healthz"
    routes = {API: [space_api(NEW)], RUNTIME: [rt(space.RUNNING, OLD)], HEALTH: [healthz()]}
    with pytest.raises(space.SpaceError, match="from commit bbbbbbb"):
        poll(routes, commit=None)


def test_wait_needs_a_commit_when_the_hub_reports_none() -> None:
    routes = {API: [space_api(None)], RUNTIME: [rt(space.RUNNING, NEW)], HEALTH: [healthz()]}
    with pytest.raises(space.SpaceError, match="reports no commit for the Space DonQuaan/CCA"):
        poll(routes, commit=None)
    url, _ = poll(routes, commit=NEW)
    assert url.endswith("/healthz")


@pytest.mark.parametrize("commit", ["", "B" * 40, "b" * 39, "b" * 41, "HEAD", NEW + "\n"])
def test_wait_refuses_a_bad_commit_without_a_request(commit: str) -> None:
    fake = FakeHttp({})
    with pytest.raises(space.SpaceError, match="not a commit id"):
        space.wait_healthy(SPACE, VERSION, commit=commit, timeout=60, interval=15, get=fake)
    assert fake.calls == []


@pytest.mark.parametrize("stage", ["BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"])
def test_a_failed_stage_after_the_build_started_fails_at_once(stage: str) -> None:
    clock = Clock()
    routes = {
        API: [space_api()],
        RUNTIME: [rt("RUNNING_BUILDING", OLD), rt(stage, error="exit 2\n::x")],
        HEALTH: [healthz()],
    }
    with pytest.raises(space.SpaceError) as caught:
        poll(routes, timeout=900, clock=clock)
    assert clock.sleeps == [15.0]
    message = str(caught.value)
    assert message == (
        f"the Space DonQuaan/CCA is at stage {stage}: 'exit 2\\n::x' (after a build had "
        "started); see the Space's logs"
    )


def test_a_failed_stage_left_over_from_before_the_push_gets_a_grace() -> None:
    clock = Clock()
    routes = {
        API: [space_api()],
        RUNTIME: [
            rt("RUNTIME_ERROR", error="old"),
            rt("RUNTIME_ERROR", error="old"),
            rt("BUILDING"),
            rt(space.RUNNING, NEW),
        ],
        HEALTH: [reply(503), healthz()],
    }
    url, _ = poll(routes, timeout=900, clock=clock)
    assert url.endswith("/healthz")
    assert clock.sleeps == [15.0] * 3


@pytest.mark.parametrize(
    ("stage", "hint"),
    [
        ("CONFIG_ERROR", "see the Space's logs"),
        ("PAUSED", "only the owner can restart a paused Space (its Settings)"),
        ("DELETING", "see the Space's logs"),
    ],
)
def test_a_stuck_stage_fails_after_its_grace(stage: str, hint: str) -> None:
    clock = Clock()
    routes = {API: [space_api()], RUNTIME: [rt(stage)], HEALTH: [reply(503)]}
    with pytest.raises(space.SpaceError) as caught:
        poll(routes, timeout=900, clock=clock)
    # asks at 0, 15, ..., 120 s: the stage has then lasted STUCK_GRACE_S
    assert space.STUCK_GRACE_S == 120.0
    assert clock.sleeps == [15.0] * 8
    assert str(caught.value) == f"the Space DonQuaan/CCA is at stage {stage} (for 120s); {hint}"


def test_a_stuck_stage_that_clears_restarts_its_grace() -> None:
    clock = Clock()
    routes = {
        API: [space_api()],
        RUNTIME: [rt("PAUSED")] * 8 + [rt("STOPPED")] + [rt("PAUSED")] * 8 + [rt("RUNNING", NEW)],
        HEALTH: [healthz()],
    }
    url, _ = poll(routes, timeout=900, clock=clock)
    assert url.endswith("/healthz")
    assert len(clock.sleeps) == 17


def test_the_new_container_reporting_a_failed_engine_fails_at_once() -> None:
    clock = Clock()
    routes = {
        API: [space_api()],
        RUNTIME: [rt(space.RUNNING, NEW)],
        HEALTH: [engine_failed(error="the engine process stopped\n::x")],
    }
    with pytest.raises(space.SpaceError) as caught:
        poll(routes, timeout=900, clock=clock)
    assert clock.sleeps == []
    assert str(caught.value) == (
        f"https://{HOST}/healthz: the new container's engine failed (healthz: HTTP 503 "
        "status='error' version='0.1.1' ready=False error='the engine process stopped\\n::x')"
    )


def test_the_previous_container_reporting_a_failed_engine_is_not_the_deploy() -> None:
    routes = {
        API: [space_api()],
        RUNTIME: [rt("RUNNING_BUILDING", OLD), rt(space.RUNNING, NEW)],
        HEALTH: [engine_failed(), healthz()],
    }
    url, log = poll(routes, timeout=900)
    assert url.endswith("/healthz")
    assert "error='the engine process stopped'" in log.splitlines()[0]


def test_wait_fails_when_the_version_never_becomes_ready() -> None:
    routes = {
        API: [space_api()],
        RUNTIME: [rt(space.RUNNING, NEW)],
        HEALTH: [healthz("0.1.0")],
    }
    with pytest.raises(space.SpaceError, match=r"did not serve version 0\.1\.1") as caught:
        poll(routes, timeout=60.0)
    message = str(caught.value)
    assert message.startswith(f"https://{HOST} did not serve version 0.1.1 from commit bbbbbbb")
    assert "version='0.1.0'" in message
    assert "stage='RUNNING' running=bbbbbbb want=bbbbbbb" in message
    assert "within 60s" in message


def test_wait_stops_at_its_timeout() -> None:
    clock = Clock()
    routes = {API: [space_api()], RUNTIME: [reply(500)], HEALTH: [reply(503)]}
    with pytest.raises(space.SpaceError, match=r"stage=\? \(HTTP 500\)"):
        poll(routes, timeout=60.0, clock=clock)
    assert clock.sleeps == [15.0] * 4  # asks at 0, 15, 30, 45 and 60 s; the next would be late
    assert clock.now - 1000.0 == 60.0


def test_wait_rides_out_a_broken_hub_answer() -> None:
    routes = {
        API: [http.client.BadStatusLine("x"), space_api()],
        RUNTIME: [rt(space.RUNNING, NEW)],
        HEALTH: [healthz()],
    }
    url, log = poll(routes)
    assert url.endswith("/healthz")
    assert log.splitlines()[0].endswith("Hub API: BadStatusLine")


def test_wait_without_a_host_names_the_space() -> None:
    routes = {API: [reply(503)], RUNTIME: [reply(503)]}
    with pytest.raises(space.SpaceError, match=r"^the Space DonQuaan/CCA did not serve"):
        poll(routes, timeout=30.0)


def test_wait_fails_at_once_for_a_private_space() -> None:
    routes = {API: [reply(404)], RUNTIME: [reply(404)], HEALTH: [healthz()]}
    clock = Clock()
    with pytest.raises(space.SpaceError, match="make it public"):
        poll(routes, timeout=900.0, clock=clock)
    assert clock.sleeps == []


def test_wait_command_passes_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_wait(space_id: str, version: str, **options: Any) -> str:
        seen.update(options, space=space_id, version=version)
        return "url"

    monkeypatch.setattr(space, "wait_healthy", fake_wait)
    assert space.main(["wait", SPACE, VERSION]) == 0
    assert seen == {
        "space": SPACE,
        "version": VERSION,
        "commit": None,
        "timeout": 900.0,
        "interval": 15.0,
    }
    assert space.main(["wait", SPACE, VERSION, "--commit", NEW, "--timeout", "5"]) == 0
    assert (seen["commit"], seen["timeout"]) == (NEW, 5.0)


def test_host_command_prints_the_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(space, "lookup_host", lambda space_id: f"{space_id.lower()}.hf.space")
    assert space.main(["host", "a/b"]) == 0
    assert capsys.readouterr().out == "a/b.hf.space\n"


# ---------------------------------------------------------------------------- real HTTPS client
class FakeConnection:
    instances: ClassVar[list[FakeConnection]] = []
    body = b"{}"

    def __init__(self, host: str, timeout: float) -> None:
        self.host, self.timeout, self.closed = host, timeout, False
        self.request_args: tuple[str, str, dict[str, str]] | None = None
        FakeConnection.instances.append(self)

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.request_args = (method, path, headers)

    def getresponse(self) -> Any:
        body = self.body

        class Response:
            status = 302

            @staticmethod
            def read(limit: int) -> bytes:
                return body[:limit]

            @staticmethod
            def getheaders() -> list[tuple[str, str]]:
                return [("Location", "http://elsewhere/"), ("Content-Type", "x/y")]

        return Response()

    def close(self) -> None:
        self.closed = True


def test_https_get_returns_redirects_instead_of_following_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeConnection.instances = []
    monkeypatch.setattr(space.http.client, "HTTPSConnection", FakeConnection)
    got = space.https_get("example.org", "/a?b", {"X": "1"}, 7.0)
    assert (got.status, got.headers["location"], got.body) == (302, "http://elsewhere/", b"{}")
    (conn,) = FakeConnection.instances
    assert (conn.host, conn.timeout, conn.closed) == ("example.org", 7.0, True)
    assert conn.request_args == ("GET", "/a?b", {"X": "1"})


def test_https_get_reads_up_to_max_body_and_refuses_more(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnection.instances = []
    monkeypatch.setattr(space.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(space, "MAX_BODY", 4)
    monkeypatch.setattr(FakeConnection, "body", b"1234")
    assert space.https_get("example.org", "/", {}, 1.0).body == b"1234"
    monkeypatch.setattr(FakeConnection, "body", b"12345")
    with pytest.raises(space.SpaceError, match="more than 4 bytes"):
        space.https_get("example.org", "/", {}, 1.0)
    assert all(conn.closed for conn in FakeConnection.instances)


# ---------------------------------------------------------------------------- the steps, for real
def test_the_credential_helper_answers_get_only() -> None:
    if SH is None:
        pytest.skip("no POSIX sh on PATH")
    run: str = step("git push origin HEAD:main")["run"]
    (line,) = [x.strip() for x in run.splitlines() if x.strip().startswith("helper='")]
    helper = line.removeprefix("helper='").removesuffix("'")
    assert helper.startswith("!")
    env = {"HF_TOKEN": CREDENTIAL, "GIT_USER": "someone", "PATH": str(Path(SH).parent)}
    # git runs a "!" helper as a shell snippet with the action appended.
    got = subprocess.run(
        [SH, "-c", helper[1:] + " get"], env=env, capture_output=True, text=True, check=True
    )
    assert got.stdout == f"username=someone\npassword={CREDENTIAL}\n"
    for action in ("store", "erase"):
        got = subprocess.run(
            [SH, "-c", f"{helper[1:]} {action}"],
            env=env,
            input="protocol=https\n",
            capture_output=True,
            text=True,
            check=True,
        )
        assert got.stdout == ""


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
    inherited = ("GIT_", "GITHUB_", "RUNNER_", "ACTIONS_", "CCA_")
    return {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith(inherited)},
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class HubServer:
    """huggingface.co's git server, locally: anyone may clone; a push needs user and token."""

    def __init__(self, root: Path, env: Mapping[str, str], user: str, credential: str) -> None:
        self.root, self.env, self.user, self.credential = root, dict(env), user, credential
        self.logins: list[tuple[str, bool]] = []  # (user name sent, whether the token matched)
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _hub_handler(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def spaces_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/spaces/"

    def redirect(self) -> dict[str, str]:
        """git configuration (as environment) that sends the Space's https URL here."""
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.spaces_url}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://huggingface.co/spaces/",
        }

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(10)

    def backend(
        self, method: str, path: str, *, query: str, headers: Any, body: bytes, user: str
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """``git http-backend`` (CGI) for one request: status, headers and body."""
        env = {
            **self.env,
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
        }
        for header, variable in (
            ("Git-Protocol", "GIT_PROTOCOL"),
            ("Content-Encoding", "HTTP_CONTENT_ENCODING"),
        ):
            if headers.get(header):
                env[variable] = headers[header]
        if user:
            env["REMOTE_USER"] = user  # http-backend accepts pushes from authenticated users only
        assert GIT is not None
        done = subprocess.run(
            [GIT, "http-backend"], input=body, env=env, capture_output=True, check=False, timeout=60
        )
        blank = b"\r\n\r\n" if b"\r\n\r\n" in done.stdout else b"\n\n"
        head, _, payload = done.stdout.partition(blank)
        status, fields = 200, []
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                fields.append((name.strip(), value.strip()))
        return status, fields, payload


def _hub_handler(hub: HubServer) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            """Keep test output quiet."""

        def _serve(self) -> None:
            path, _, query = self.path.partition("?")
            user = ""
            if path.endswith("/git-receive-pack") or query == "service=git-receive-pack":
                sent = self._basic_auth()
                if sent is not None:
                    hub.logins.append((sent[0], sent[1] == hub.credential))
                if sent != (hub.user, hub.credential):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="hub"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                user = hub.user
            status, fields, payload = hub.backend(
                self.command,
                path.removeprefix("/spaces"),
                query=query,
                headers=self.headers,
                body=self._body(),
                user=user,
            )
            self.send_response(status)
            for name, value in fields:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _basic_auth(self) -> tuple[str, str] | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
            return user, password

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0].strip(), 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            self.rfile.readline()
            return b"".join(chunks)

    return Handler


def make_hub(tmp_path: Path, env: dict[str, str], *, seed: bool) -> Path:
    """A bare repository at <tmp>/hub/DonQuaan/CCA, optionally with a Hub-like first commit."""
    remote = tmp_path / "hub" / "DonQuaan" / "CCA"
    _git("init", "--bare", "--quiet", "--initial-branch=main", str(remote), env=env)
    if seed:
        work = tmp_path / "seed"
        _git("init", "--quiet", "--initial-branch=main", str(work), env=env)
        (work / "README.md").write_text("---\ntitle: old\n---\n", encoding="utf-8")
        (work / ".gitattributes").write_text("*.bin filter=lfs -text\n", encoding="utf-8")
        (work / "app.py").write_text("print('stale')\n", encoding="utf-8")
        _git("add", "--all", cwd=work, env=env)
        ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        _git(*ident, "commit", "--quiet", "-m", "initial", cwd=work, env=env)
        _git("push", "--quiet", remote.as_uri(), "HEAD:main", cwd=work, env=env)
    return remote


@pytest.fixture
def hub(tmp_path: Path, git_env: dict[str, str]) -> Iterator[HubServer]:
    """The Space's git server with a Hub-like first commit; pushes need DonQuaan, CREDENTIAL."""
    remote = make_hub(tmp_path, git_env, seed=True)
    server = HubServer(remote.parent.parent, git_env, "DonQuaan", CREDENTIAL)
    try:
        yield server
    finally:
        server.close()


def tree(remote: Path, env: dict[str, str]) -> list[str]:
    return sorted(
        _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main", env=env).split()
    )


def head(remote: Path, env: dict[str, str]) -> str:
    return _git("--git-dir", str(remote), "rev-parse", "main", env=env).strip()


# A python3 for the steps: the interpreter running these tests, with a scripted HTTPS client
# (CCA_FAKE_HTTP: a JSON file of "host path" -> [status, headers, body]); every request is logged
# to CCA_FAKE_HTTP_LOG. A request without a scripted answer fails like a refused connection.
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
        self.host, self.path = host, None

    def request(self, method, path, headers=None):
        self.path = path
        with open(os.environ["CCA_FAKE_HTTP_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps([method, self.host, path, dict(headers or {})]) + "\n")

    def getresponse(self):
        with open(os.environ["CCA_FAKE_HTTP"], encoding="utf-8") as fh:
            answer = json.load(fh).get(f"{self.host} {self.path}")
        if answer is None:
            raise ConnectionRefusedError(f"no scripted answer for https://{self.host}{self.path}")
        status, headers, body = answer
        return Response(status, headers, body.encode("utf-8"))

    def close(self):
        pass


http.client.HTTPSConnection = Connection
script, sys.argv = sys.argv[1], sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""
# gh for the version step: only the one call it makes, with a token.
FAKE_GH = """#!/bin/sh
if [ "$*" = "api repos/DonQuaan/CCA/releases/latest --jq .tag_name" ] && [ -n "$GH_TOKEN" ]; then
  echo "v0.1.1"
  exit 0
fi
echo "gh (test double): unexpected call: $*" >&2
exit 1
"""
EDIT = "# edited on main after the release\n"


def make_bin(tmp_path: Path) -> Path:
    """A directory with the ``python3`` and ``gh`` the steps call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake_python.py"
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
    return bin_dir


def make_checkout(tmp_path: Path, env: dict[str, str], *, tag_has_templates: bool = True) -> Path:
    """The repository as actions/checkout leaves it: tag v0.1.1, then main moved on.

    After the tag, main edits the Dockerfile template (or adds the templates, when the tag
    predates them), so each test can tell which templates a run rendered.
    """
    repo = tmp_path / "checkout"
    target = repo / "deploy" / "huggingface"
    target.mkdir(parents=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    _git("init", "--quiet", "--initial-branch=main", str(repo), env=env)
    shutil.copyfile(DEPLOY / "space.py", target / "space.py")
    if tag_has_templates:
        shutil.copyfile(DEPLOY / "Dockerfile", target / "Dockerfile")
        shutil.copyfile(DEPLOY / "README.md", target / "README.md")
    _git("add", "--all", cwd=repo, env=env)
    _git(*ident, "commit", "--quiet", "-m", "release", cwd=repo, env=env)
    _git(*ident, "tag", "-a", f"v{VERSION}", "-m", f"CCA {VERSION}", cwd=repo, env=env)
    template = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    (target / "Dockerfile").write_text(template + EDIT, encoding="utf-8", newline="\n")
    shutil.copyfile(DEPLOY / "README.md", target / "README.md")
    _git("add", "--all", cwd=repo, env=env)
    _git(*ident, "commit", "--quiet", "-m", "after the release", cwd=repo, env=env)
    return repo


class JobRun:
    """The deploy job's ``run`` steps as the runner runs them: bash -eo pipefail, env from YAML.

    ``${{ }}`` values come from ``context`` (exact expression text) or from the outputs earlier
    steps wrote to ``$GITHUB_OUTPUT``; an expression the harness does not know fails the test.
    """

    def __init__(
        self,
        tmp: Path,
        env: dict[str, str],
        hub: HubServer,
        context: Mapping[str, str],
        *,
        tag_has_templates: bool = True,
    ) -> None:
        self.tmp, self.context = tmp, dict(context)
        self.checkout = make_checkout(tmp, env, tag_has_templates=tag_has_templates)
        self.outputs: dict[str, dict[str, str]] = {}
        self.results: list[subprocess.CompletedProcess[str]] = []
        self.routes: dict[str, list[Any]] = {}
        runner_temp = tmp / "runner"
        runner_temp.mkdir()
        self.summary = tmp / "summary.md"
        self.summary.write_text("", encoding="utf-8")
        self.http_log = tmp / "http.log"
        self.http_log.write_text("", encoding="utf-8")
        self.routes_file = tmp / "routes.json"
        self.env = {
            **env,
            **hub.redirect(),
            "PATH": f"{make_bin(tmp)}{os.pathsep}{env['PATH']}",
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "DonQuaan/CCA",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "42",
            "GITHUB_SHA": _git("rev-parse", "HEAD", cwd=self.checkout, env=env).strip(),
            "GITHUB_STEP_SUMMARY": self.summary.as_posix(),
            "RUNNER_TEMP": runner_temp.as_posix(),
            "CCA_FAKE_HTTP": self.routes_file.as_posix(),
            "CCA_FAKE_HTTP_LOG": self.http_log.as_posix(),
        }
        self.answer("ghcr.io", TOKEN_PATH[1], 200, json.dumps({"token": "anon-pull-token"}))
        self.answer("ghcr.io", MANIFEST_PATH[1], 200, MANIFEST.decode(), **{"content-type": OCI})
        self.answer(*API, 200, json.dumps({"host": f"https://{HOST}", "sha": OLD}))
        self.answer(*HEALTH, 200, json.dumps({"status": "ok", "version": VERSION, "ready": True}))

    def answer(self, host: str, path: str, status: int, body: str, **headers: str) -> None:
        self.routes[f"{host} {path}"] = [status, headers, body]
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

    def run_until(self, text: str) -> subprocess.CompletedProcess[str] | None:
        """Run the steps before the one containing ``text``; the first failure, if any."""
        for s in steps()[1:]:  # steps()[0] is actions/checkout: the checkout is ready
            if text and text in str(s.get("run", "")):
                return None
            done = self.run(s)
            if done.returncode != 0:
                return done
        return None

    def logs(self) -> str:
        return "".join(r.stdout + r.stderr for r in self.results) + self.http_log.read_text(
            encoding="utf-8"
        )


@pytest.fixture
def new_job(tmp_path: Path, git_env: dict[str, str], hub: HubServer) -> Callable[..., JobRun]:
    """``new_job(context, tag_has_templates=True)``: a :class:`JobRun` in this test's tmp_path."""

    def make(values: Mapping[str, str], *, tag_has_templates: bool = True) -> JobRun:
        return JobRun(tmp_path, git_env, hub, values, tag_has_templates=tag_has_templates)

    return make


def context(version: str = "latest", templates: str = "release") -> dict[str, str]:
    """The ``${{ }}`` values of a manual run (workflow_dispatch) with these inputs."""
    return {
        "vars.HF_SPACE": SPACE,
        "vars.HF_USERNAME": "",
        "secrets.HF_TOKEN": CREDENTIAL,
        "secrets.HF_TOKEN != ''": "true",
        "github.token": "ghs_fake_github_token",
        "github.event_name == 'release' && github.event.release.tag_name || inputs.version": (
            version
        ),
        "inputs.templates": templates,
    }


def test_the_whole_job_deploys_the_latest_release_from_its_tag(
    tmp_path: Path, git_env: dict[str, str], hub: HubServer
) -> None:
    job_run = JobRun(tmp_path, git_env, hub, context())
    assert job_run.run_until("space.py wait") is None, job_run.logs()
    assert job_run.outputs["version"] == {"version": VERSION}
    assert job_run.outputs["digest"] == {"digest": MANIFEST_DIGEST}
    assert job_run.outputs["space"] == {"host": HOST}
    remote = hub.root / "DonQuaan" / "CCA"
    pushed = head(remote, git_env)
    assert job_run.outputs["push"] == {"commit": pushed}
    # The tag's templates, not main's: a rollback runs what the release shipped.
    shown = _git("--git-dir", str(remote), "show", "main:Dockerfile", env=git_env)
    assert shown == rendered_dockerfile(MANIFEST_DIGEST)
    assert EDIT not in shown
    assert tree(remote, git_env) == [".gitattributes", "Dockerfile", "README.md"]
    assert hub.logins == [("DonQuaan", True)] * len(hub.logins)
    assert hub.logins

    # The Hub builds the pushed commit and runs it: the wait step sees that at once.
    job_run.answer(*RUNTIME, 200, json.dumps({"stage": "RUNNING", "sha": pushed}))
    done = job_run.run(step("space.py wait"))
    assert done.returncode == 0, job_run.logs()
    assert f"runs commit {pushed} with version {VERSION} ready" in done.stdout
    summary = job_run.summary.read_text(encoding="utf-8")
    assert f"FROM ghcr.io/donquaan/cca:{VERSION}@{MANIFEST_DIGEST}" in summary
    assert f"Deployed CCA {VERSION} to https://huggingface.co/spaces/{SPACE}" in summary
    requests = [json.loads(line) for line in job_run.http_log.read_text("utf-8").splitlines()]
    assert {(method, host) for method, host, _, _ in requests} == {
        ("GET", "ghcr.io"),
        ("GET", "huggingface.co"),
        ("GET", HOST),
    }
    assert CREDENTIAL not in job_run.logs()
    assert CREDENTIAL not in (tmp_path / "runner" / "space-repo" / ".git" / "config").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("templates", "extra"),
    [
        ("checkout", EDIT),  # a manual run that asks for the branch's templates
        ("", ""),  # a release event or a call without the input: the tag's
    ],
)
def test_the_job_renders_the_templates_it_is_asked_for(
    new_job: Callable[..., JobRun],
    git_env: dict[str, str],
    hub: HubServer,
    templates: str,
    extra: str,
) -> None:
    job_run = new_job(context(version="v0.1.1", templates=templates))
    assert job_run.run_until("space.py wait") is None, job_run.logs()
    remote = hub.root / "DonQuaan" / "CCA"
    shown = _git("--git-dir", str(remote), "show", "main:Dockerfile", env=git_env)
    assert shown == rendered_dockerfile(MANIFEST_DIGEST, extra=extra)


@pytest.mark.parametrize(
    ("templates", "tag_has_templates", "problem"),
    [
        # a version released before deploy/huggingface/ existed
        ("release", False, "the tag v0.1.1 has no deploy/huggingface/Dockerfile"),
        ("other", True, "templates must be release or checkout"),
    ],
)
def test_the_job_refuses_templates_it_cannot_trust(
    new_job: Callable[..., JobRun],
    hub: HubServer,
    templates: str,
    tag_has_templates: bool,
    problem: str,
) -> None:
    job_run = new_job(context(templates=templates), tag_has_templates=tag_has_templates)
    failed = job_run.run_until("space.py wait")
    assert failed is not None
    annotations = [x for x in failed.stderr.splitlines() if x.startswith("::")]
    assert len(annotations) == 1  # stops at the first problem: no render of a missing file
    assert annotations[0].startswith(f"::error title=deploy-space::{problem}")
    assert "space.py render" in str(steps()[len(job_run.results)]["run"])  # the render step
    assert hub.logins == []  # nothing was pushed


def test_the_job_fails_fast_when_the_new_container_reports_a_failed_engine(
    tmp_path: Path, git_env: dict[str, str], hub: HubServer
) -> None:
    job_run = JobRun(tmp_path, git_env, hub, context())
    assert job_run.run_until("space.py wait") is None, job_run.logs()
    pushed = job_run.outputs["push"]["commit"]
    job_run.answer(*RUNTIME, 200, json.dumps({"stage": "RUNNING", "sha": pushed}))
    body = {"status": "error", "version": VERSION, "ready": False, "error": "no engine"}
    job_run.answer(*HEALTH, 503, json.dumps(body))
    done = job_run.run(step("space.py wait"))
    assert done.returncode == 1
    assert "::error title=deploy-space::https://donquaan-cca.hf.space/healthz: the new " in (
        done.stderr
    )
    assert "error='no engine'" in done.stderr
    assert "Deployed" not in job_run.summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("needle", "env", "problem"),
    [
        ("space.py version", {"REQUESTED": "v9.9"}, "not a release version: 'v9.9'"),
        ("space.py digest", {"VERSION": "latest"}, "not a release version: 'latest'"),
        ("space.py digest", {"VERSION": "9.9.9"}, "ghcr.io/donquaan/cca:9.9.9 does not exist"),
        ("space.py host", {"HF_SPACE": "bad id"}, "not a Space id: 'bad id'"),
        ("space.py host", {"HF_SPACE": "DonQuaan/Gone"}, "create it first, and make it public"),
    ],
)
def test_a_failing_helper_shows_its_annotation_in_the_log(
    new_job: Callable[..., JobRun], needle: str, env: dict[str, str], problem: str
) -> None:
    """The helper runs inside ``x=$(...)``: its error must not be captured with its stdout."""
    job_run = new_job(context())
    job_run.answer("ghcr.io", "/v2/donquaan/cca/manifests/9.9.9", 404, "{}")
    job_run.answer("huggingface.co", "/api/spaces/DonQuaan/Gone", 401, "{}")
    s = step(needle)
    done = job_run.run({**s, "env": env | {"GH_TOKEN": "x"}})
    assert done.returncode == 1
    (line,) = done.stderr.splitlines()
    assert line.startswith("::error title=deploy-space::"), done.stderr
    assert problem in line
    assert (done.stdout, job_run.outputs[s["id"]]) == ("", {})


# ---------------------------------------------------------------------------- the push, for real
def run_push(
    tmp_path: Path, env: dict[str, str], hub: HubServer, run: str = "1", username: str = ""
) -> tuple[subprocess.CompletedProcess[str], str]:
    """The workflow's push step, as the runner runs it, in a fresh ``$RUNNER_TEMP``.

    Returns the finished process and the ``commit`` it wrote to ``$GITHUB_OUTPUT``.
    """
    push = step("git push origin HEAD:main")
    runner_temp = tmp_path / f"runner{run}"
    space.write_space(runner_temp / "space-files", VERSION, DIGEST, HOST)
    script = tmp_path / "push.sh"
    script.write_text(push["run"], encoding="utf-8", newline="\n")
    output = tmp_path / f"output{run}"
    output.write_text("", encoding="utf-8")
    step_env = {
        "HF_TOKEN": CREDENTIAL,
        "HF_SPACE": SPACE,
        "HF_USERNAME": username,
        "VERSION": VERSION,
        "DIGEST": DIGEST,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    assert set(step_env) == set(push["env"])
    full = {
        **env,
        **step_env,
        **hub.redirect(),
        "RUNNER_TEMP": runner_temp.as_posix(),
        "GITHUB_OUTPUT": output.as_posix(),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "DonQuaan/CCA",
        "GITHUB_RUN_ID": "42",
        "GITHUB_SHA": "0" * 40,
    }
    assert BASH is not None
    done = subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", script.as_posix()],
        env=full,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    written = output.read_text(encoding="utf-8")
    return done, written.removeprefix("commit=").strip() if written.startswith("commit=") else ""


def test_push_replaces_the_space_files_and_is_idempotent(
    tmp_path: Path, git_env: dict[str, str], hub: HubServer
) -> None:
    remote = hub.root / "DonQuaan" / "CCA"
    before = head(remote, git_env)
    first, commit = run_push(tmp_path, git_env, hub)
    assert first.returncode == 0, first.stderr
    assert tree(remote, git_env) == [".gitattributes", "Dockerfile", "README.md"]
    shown = _git("--git-dir", str(remote), "show", "main:Dockerfile", env=git_env)
    assert shown == rendered_dockerfile()
    message = _git("--git-dir", str(remote), "log", "-1", "--format=%B", "main", env=git_env)
    assert message.startswith(f"Deploy CCA {VERSION}\n")
    assert f"ghcr.io/donquaan/cca:{VERSION}@{DIGEST}" in message
    assert "/actions/runs/42" in message
    assert commit == head(remote, git_env) != before  # the wait step expects this commit
    assert hub.logins
    assert set(hub.logins) == {("DonQuaan", True)}  # the owner of HF_SPACE, with the token

    again, same = run_push(tmp_path, git_env, hub, run="2")
    assert again.returncode == 0, again.stderr
    assert "nothing to push" in again.stdout
    assert same == commit == head(remote, git_env)
    for done in (first, again):
        assert CREDENTIAL not in done.stdout + done.stderr
    for run in ("1", "2"):
        config = tmp_path / f"runner{run}" / "space-repo" / ".git" / "config"
        assert CREDENTIAL not in config.read_text(encoding="utf-8")


def test_push_to_an_empty_space_repository(tmp_path: Path, git_env: dict[str, str]) -> None:
    remote = make_hub(tmp_path / "empty", git_env, seed=False)
    server = HubServer(remote.parent.parent, git_env, "DonQuaan", CREDENTIAL)
    try:
        done, commit = run_push(tmp_path, git_env, server)
    finally:
        server.close()
    assert done.returncode == 0, done.stderr
    assert tree(remote, git_env) == ["Dockerfile", "README.md"]
    assert commit == head(remote, git_env)


def test_push_sends_hf_username_for_an_organisation_space(
    tmp_path: Path, git_env: dict[str, str], hub: HubServer
) -> None:
    hub.user = "don-member"
    done, commit = run_push(tmp_path, git_env, hub, username="don-member")
    assert done.returncode == 0, done.stderr
    assert set(hub.logins) == {("don-member", True)}
    assert commit == head(hub.root / "DonQuaan" / "CCA", git_env)


def test_a_rejected_token_fails_the_push_without_showing_it(
    tmp_path: Path, git_env: dict[str, str], hub: HubServer
) -> None:
    hub.credential = "hf_the_token_the_hub_expects"
    remote = hub.root / "DonQuaan" / "CCA"
    before = head(remote, git_env)
    done, commit = run_push(tmp_path, git_env, hub)
    assert done.returncode != 0
    assert (commit, head(remote, git_env)) == ("", before)
    assert hub.logins
    assert set(hub.logins) == {("DonQuaan", False)}
    assert CREDENTIAL not in done.stdout + done.stderr
