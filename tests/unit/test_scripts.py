"""Behavioural tests for the enforcement scripts (release gate, verified fetch, dependency audit).

A guard without a test of its own is a fake guard: each test below fails if the guard is removed.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"cca_script_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- check_release.py
@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    mod = _load("check_release")
    (tmp_path / "src" / "cca").mkdir(parents=True)
    (tmp_path / "src" / "cca" / "__init__.py").write_text('__version__ = "1.2.3"\n', "utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-01-01\n### Fixed\n- a thing\n\n"
        "## [1.2.2] - 2025-12-01\n- older\n",
        "utf-8",
    )
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _run(mod: ModuleType, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["check_release.py", *argv])
    return int(mod.main())


def test_release_gate_accepts_a_matching_tag(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(release, monkeypatch, "v1.2.3", "--notes") == 0
    notes = capsys.readouterr().out
    assert "a thing" in notes
    assert "older" not in notes  # only this version's section


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        ("v1.2.4", "!= package version"),
        ("1.2.3", "is not vMAJOR"),
        ("v01.2.3", "is not vMAJOR"),
        ("v1.2.3-beta", "is not vMAJOR"),
    ],
)
def test_release_gate_rejects_bad_tags(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch, tag: str, message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        _run(release, monkeypatch, tag)


def test_release_gate_requires_a_changelog_section(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", "utf-8")
    with pytest.raises(SystemExit, match=r"no '## \[1.2.3\]' section"):
        _run(release, monkeypatch, "v1.2.3")


# --- fetch_stockfish.py
@pytest.fixture
def fetch() -> ModuleType:
    return _load("fetch_stockfish")


def test_fetch_refuses_non_github_urls(fetch: ModuleType) -> None:
    with pytest.raises(SystemExit, match="refusing non-GitHub URL"):
        fetch._request("https://evil.example/stockfish.zip")
    with pytest.raises(SystemExit, match="refusing non-GitHub URL"):
        fetch._request("http://github.com/x")  # plain HTTP


def test_fetch_sends_the_token_only_to_the_api(
    fetch: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    api = fetch._request("https://api.github.com/repos/x")
    dl = fetch._request("https://github.com/official-stockfish/Stockfish/releases/download/a/b")
    assert api.get_header("Authorization") == "Bearer t0ken"
    assert dl.get_header("Authorization") is None  # downloads redirect to other hosts


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _serve(monkeypatch: pytest.MonkeyPatch, fetch: ModuleType, payload: bytes) -> None:
    def urlopen(req: Any, timeout: float = 0.0) -> _Resp:
        del req, timeout
        return _Resp(payload)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)


def test_download_with_a_wrong_digest_leaves_no_file(
    fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib

    _serve(monkeypatch, fetch, b"tampered binary")
    target = tmp_path / "sf.zip"
    good = hashlib.sha256(b"official binary").hexdigest()
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        fetch.download_verified("https://github.com/o/r/releases/download/t/sf.zip", target, good)
    assert not target.exists()
    _serve(monkeypatch, fetch, b"official binary")
    fetch.download_verified("https://github.com/o/r/releases/download/t/sf.zip", target, good)
    assert target.read_bytes() == b"official binary"


def test_unpinned_asset_without_a_published_digest_is_refused(
    fetch: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert fetch.expected_digest("sf_19", "a.zip", {"sf_19/a.zip": "abc"}) == "abc"
    monkeypatch.setattr(fetch, "api_assets", lambda tag: [{"name": "b.zip", "digest": None}])
    with pytest.raises(SystemExit, match="refusing an unverified binary"):
        fetch.expected_digest("sf_19", "b.zip", {})
    with pytest.raises(SystemExit, match="has no asset"):
        fetch.expected_digest("sf_19", "c.zip", {})


def test_zip_slip_is_refused(
    fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engines = tmp_path / "engines"
    engines.mkdir()
    monkeypatch.setattr(fetch, "ENGINES", engines)
    archive = tmp_path / "stockfish-evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("stockfish/ok.txt", "ok")
        zf.writestr("../../escaped.txt", "pwned")
    with pytest.raises(SystemExit, match="unsafe path in archive"):
        fetch.extract(archive)
    assert not (tmp_path / "escaped.txt").exists()
    assert not any(engines.rglob("ok.txt"))  # nothing extracted before the check


# --- audit_deps.py
@pytest.fixture
def audit() -> ModuleType:
    return _load("audit_deps")


def test_expired_advisory_list_fails_before_auditing(
    audit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_subprocess(*a: object, **k: object) -> None:
        raise AssertionError("an expired allowlist must fail before anything is audited")

    monkeypatch.setattr(audit, "REVIEW_BY", date(2000, 1, 1))
    monkeypatch.setattr(audit.subprocess, "run", no_subprocess)
    assert audit.main() == 1


def test_every_accepted_advisory_carries_a_reason(audit: ModuleType) -> None:
    assert audit.ACCEPTED
    assert all(len(reason) > 20 for reason in audit.ACCEPTED.values())
    assert date(2026, 9, 26) < audit.REVIEW_BY


def test_marker_forks_never_share_an_audit_file(audit: ModuleType) -> None:
    req = audit.Requirement
    pins = [
        req("torch==2.8.0 ; python_version < '3.13'"),
        req("torch==2.9.1 ; python_version >= '3.13'"),
        req("numpy==2.3.0"),
        req("torch==2.8.0 ; sys_platform == 'win32'"),  # same pin again: no new file
        req("numpy==2.2.0 ; python_version < '3.12'"),
    ]
    files = audit.split_forks(pins)
    assert files == [
        {"torch": "torch==2.8.0", "numpy": "numpy==2.3.0"},
        {"torch": "torch==2.9.1", "numpy": "numpy==2.2.0"},
    ]
    covered = {pin for f in files for pin in f.values()}
    assert covered == {f"{r.name}{r.specifier}" for r in pins}  # every locked pin is audited
