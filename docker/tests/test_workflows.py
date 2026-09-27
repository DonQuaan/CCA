"""The release chain's guards in .github/workflows, checked without running GitHub Actions.

A workflow only runs on GitHub, so a guard dropped from one (the release gate, the wheel check,
the tag-only publish condition, the attestation) would show up only at the next release.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest
import smoke

# PyYAML comes with the dev tools (pre-commit depends on it); it ships no type stubs, so it is
# imported as a plain module object.
yaml = importlib.import_module("yaml")

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# Every third-party action, pinned to the commit of a release that runs on Node 24.
PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "astral-sh/setup-uv": ("c18668ad3cf93ea998bef934396af7bb5c839dc7", "v10.2.0"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7.0.1"),
    "actions/download-artifact": ("3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", "v8.0.1"),
    "softprops/action-gh-release": ("efb35369e0ad2afab669f228072c1b0d510eae64", "v3.0.3"),
    "docker/setup-buildx-action": ("f87e5991a6d7451dcb8d9637bfbc97413f497069", "v4.4.1"),
    "docker/login-action": ("dbcb813823bdd20940b903addbd779551569679f", "v4.6.0"),
    "docker/metadata-action": ("dc802804100637a589fabce1cb79ff13a1411302", "v6.2.0"),
    "docker/build-push-action": ("c3c9e263c25d99ce0380d002d59b67737d91b0dc", "v7.4.0"),
    "actions/attest": ("1e69f48acb82d1966a394da916b4c1698aa569d6", "v4.2.2"),
}
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+?)(?:@(\S+))?(?:\s+#\s*(\S+))?\s*$")


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found = job["steps"]
    assert isinstance(found, list)
    return found


def step_index(job: dict[str, Any], text: str) -> int:
    """Index of the only step whose ``run`` or ``uses`` contains ``text``."""
    hits = [
        i
        for i, step in enumerate(steps(job))
        if text in str(step.get("run", "")) or text in str(step.get("uses", ""))
    ]
    assert len(hits) == 1, f"{len(hits)} steps contain {text!r}"
    return hits[0]


def by_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    (step,) = [s for s in steps(job) if s.get("id") == step_id]
    return step


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as true
    assert isinstance(on, dict)
    return on


@pytest.mark.parametrize("name", ["ci.yml", "image.yml", "release.yml"])
def test_every_action_is_pinned_to_its_node24_release(name: str) -> None:
    seen = 0
    for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines():
        match = USES.match(line)
        if not match:
            continue
        action, ref, comment = match.groups()
        if action.startswith("./"):
            assert ref is None, line  # a reusable workflow of this repository
            continue
        seen += 1
        assert action in PINS, f"{name}: unreviewed action {action}"
        assert (ref, comment) == PINS[action], f"{name}: {line.strip()}"
    assert seen


def test_release_order_and_permissions() -> None:
    wf = load("release.yml")
    jobs = wf["jobs"]
    assert triggers(wf) == {"push": {"tags": ["v*"]}}
    assert wf["permissions"] == {"contents": "read"}
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["verify"]["needs"] == "ci"
    assert jobs["image"]["needs"] == "verify"
    assert jobs["image"]["uses"] == "./.github/workflows/image.yml"
    assert jobs["image"]["with"] == {"push": True}
    assert jobs["image"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert sorted(jobs["publish"]["needs"]) == ["image", "verify"]
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    writers = [
        n for n, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"
    ]
    assert writers == ["publish"]


def test_release_gate_checks_the_tag_and_the_packages() -> None:
    verify = load("release.yml")["jobs"]["verify"]
    tag = steps(verify)[step_index(verify, "git cat-file -t")]["run"]
    assert 'test "$(git cat-file -t "refs/tags/${GITHUB_REF_NAME}")" = tag' in tag
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in tag
    assert steps(verify)[0]["with"]["fetch-depth"] == 0  # the ancestry check needs history
    build = step_index(verify, "uv build")
    assert "--build-constraints docker/build-constraints.txt --require-hashes" in str(
        steps(verify)[build]["run"]
    )
    check = step_index(verify, "docker/check_dist.py --package src/cca dist/*.whl dist/*.tar.gz")
    sdist = step_index(verify, "tar -tzf dist/*.tar.gz")
    upload = step_index(verify, "actions/upload-artifact")
    assert build < check < upload
    assert build < sdist < upload
    assert steps(verify)[upload]["with"]["if-no-files-found"] == "error"


def test_release_publishes_the_gated_files() -> None:
    publish = load("release.yml")["jobs"]["publish"]
    release = steps(publish)[step_index(publish, "softprops/action-gh-release")]["with"]
    assert release["files"] == "dist/*"
    assert release["body_path"] == "RELEASE_NOTES.md"
    assert release["fail_on_unmatched_files"] is True


def test_image_workflow_triggers_and_default_permissions() -> None:
    wf = load("image.yml")
    on = triggers(wf)
    assert "pull_request" in on
    assert on["push"] == {"branches": ["main"]}
    push_input = on["workflow_call"]["inputs"]["push"]
    assert (push_input["type"], push_input["default"]) == ("boolean", False)
    assert wf["permissions"] == {"contents": "read"}
    assert wf["env"]["IMAGE"] == smoke.IMAGE_NAME


def test_every_pull_request_builds_and_smoke_tests_both_images_without_pushing() -> None:
    jobs = load("image.yml")["jobs"]
    unit = jobs["smoke-unit"]
    runs = [str(s.get("run", "")) for s in steps(unit)]
    assert any("pytest" in run and "docker/tests" in run for run in runs), runs
    assert any("mypy --strict docker/" in run for run in runs), runs
    build = jobs["build-test"]
    assert {(m["variant"], m["target"]) for m in build["strategy"]["matrix"]["include"]} == {
        ("default", "runtime"),
        ("maia2", "runtime-maia2"),
    }
    assert "if" not in build
    built = step_index(build, "docker/build-push-action")
    options = steps(build)[built]["with"]
    assert (options["load"], options["push"], options["tags"]) == (True, False, "cca:ci")
    smoke_step = step_index(build, "python3 docker/smoke.py cca:ci")
    assert built < smoke_step
    assert "--expect-version" in steps(build)[smoke_step]["run"]


def test_publish_runs_only_for_tag_releases_after_the_tests() -> None:
    publish = load("image.yml")["jobs"]["publish"]
    condition = publish["if"].replace(" ", "")
    assert condition == "${{inputs.push&&startsWith(github.ref,'refs/tags/v')}}"
    assert sorted(publish["needs"]) == ["build-test", "smoke-unit"]
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }


def test_publish_tests_what_it_pushes_and_attests_the_pushed_digest() -> None:
    publish = load("image.yml")["jobs"]["publish"]
    order = [
        step_index(publish, "docker/metadata-action"),
        step_index(publish, "python3 docker/smoke.py cca:release"),
        step_index(publish, "docker/login-action"),
        steps(publish).index(by_id(publish, "push")),
        step_index(publish, 'docker pull "$ref"'),
        step_index(publish, "actions/attest"),
    ]
    assert order == sorted(order), order
    meta = by_id(publish, "meta")["with"]
    assert meta["images"] == "${{ env.IMAGE }}"
    assert "latest=auto" in meta["flavor"]
    assert meta["tags"].strip() == "type=pep440,pattern={{version}}"
    fresh = next(s for s in steps(publish) if s.get("with", {}).get("tags") == "cca:release")
    assert (fresh["with"]["no-cache"], fresh["with"]["load"], fresh["with"]["push"]) == (
        True,
        True,
        False,
    )
    push = by_id(publish, "push")["with"]
    assert push["push"] is True
    assert push["tags"] == "${{ steps.meta.outputs.tags }}"
    assert "labels" not in push  # the Dockerfile's labels, not metadata-action's
    for key in ("target", "platforms", "build-args", "context"):
        assert push[key] == fresh["with"][key], key  # same inputs as the tested build
    pulled = steps(publish)[step_index(publish, 'docker pull "$ref"')]
    assert pulled["env"]["DIGEST"] == "${{ steps.push.outputs.digest }}"
    assert 'ref="${IMAGE}@${DIGEST}"' in pulled["run"]
    assert 'python3 docker/smoke.py "$ref"' in pulled["run"]
    attest = steps(publish)[step_index(publish, "actions/attest")]["with"]
    assert attest == {
        "subject-name": "${{ env.IMAGE }}",
        "subject-digest": "${{ steps.push.outputs.digest }}",
        "push-to-registry": True,
        "create-storage-record": False,
    }
