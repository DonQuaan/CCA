"""Tests for docker/check_dist.py against small hand-made wheels and sdists."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import check_dist
import pytest

PACKAGE_FILES = {
    "cca/__init__.py": b"__version__ = '0.1.0'\n",
    "cca/py.typed": b"",
    "cca/play/static/index.html": b"<!doctype html>",
    "cca/play/static/vendor/chess.js/.gitignore": b"!dist/\n",
    "cca/play/static/vendor/chess.js/dist/esm/chess.js": b"export class Chess {}\n",
}
CHESS_JS = "cca/play/static/vendor/chess.js/dist/esm/chess.js"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A src-layout project with generated files that must not count; cwd is its root."""
    for rel, body in PACKAGE_FILES.items():
        path = tmp_path / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (tmp_path / "src/cca/__pycache__").mkdir()
    (tmp_path / "src/cca/__pycache__/__init__.cpython-312.pyc").write_bytes(b"\0")
    # importlib writes bytecode to "<name>.pyc.<id>" first, then renames it; an interrupted
    # write leaves that file behind: not a .pyc suffix, but still no package file.
    (tmp_path / "src/cca/__pycache__/__init__.cpython-312.pyc.2130706433").write_bytes(b"\0")
    (tmp_path / "src/cca/stray.pyc").write_bytes(b"\0")
    (tmp_path / "src/cca/empty-dir").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("cca/", b"")  # a directory entry
        for rel, body in files.items():
            zf.writestr(rel, body)
        zf.writestr("cca_chess-0.1.0.dist-info/RECORD", b"")
    return path


def sdist(path: Path, files: dict[str, bytes], top: str = "cca_chess-0.1.0") -> Path:
    with tarfile.open(path, "w:gz") as tf:
        extra = {"PKG-INFO": b"", "tests/test_x.py": b"", "docs/cca/notes.md": b""}
        members = {f"src/{rel}": body for rel, body in files.items()} | extra
        for rel, body in members.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        folder = tarfile.TarInfo(f"{top}/src/cca")
        folder.type = tarfile.DIRTYPE
        tf.addfile(folder)
    return path


def without(*names: str) -> dict[str, bytes]:
    return {rel: body for rel, body in PACKAGE_FILES.items() if rel not in names}


def run(*args: str | Path) -> tuple[int, str]:
    out = io.StringIO()
    code = check_dist.check([Path(a) for a in args], Path("src/cca"), out)
    return code, out.getvalue()


def test_source_files_skip_bytecode_but_keep_dotfiles(project: Path) -> None:
    assert check_dist.source_files(Path("src/cca")) == set(PACKAGE_FILES)


def test_complete_wheel_and_sdist_pass(project: Path) -> None:
    code, out = run(
        wheel(project / "cca_chess-0.1.0-py3-none-any.whl", PACKAGE_FILES),
        sdist(project / "cca_chess-0.1.0.tar.gz", PACKAGE_FILES),
    )
    assert code == 0, out
    assert out.splitlines() == [
        "ok   cca_chess-0.1.0-py3-none-any.whl: all 5 files under src/cca",
        "ok   cca_chess-0.1.0.tar.gz: all 5 files under src/cca",
    ]


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_a_file_left_out_is_reported(project: Path, kind: str) -> None:
    dist = (
        wheel(project / "x-0.1.0-py3-none-any.whl", without(CHESS_JS))
        if kind == "wheel"
        else sdist(project / "x-0.1.0.tar.gz", without(CHESS_JS))
    )
    code, out = run(dist)
    assert code == 1
    assert out.splitlines() == [
        f"FAIL {dist.name} leaves out 1 of the 5 files under src/cca "
        "(an ignore rule in .gitignore, or the build config):",
        f"  {CHESS_JS}",
    ]


def test_every_distribution_is_checked_even_after_a_failure(project: Path) -> None:
    code, out = run(
        wheel(project / "a-1-py3-none-any.whl", without("cca/py.typed")),
        wheel(project / "b-1-py3-none-any.whl", PACKAGE_FILES),
    )
    assert code == 1
    assert out.splitlines()[-1] == "ok   b-1-py3-none-any.whl: all 5 files under src/cca"


def test_sdist_paths_are_matched_below_the_top_directory_only(project: Path) -> None:
    # the package's files at the wrong depth do not count
    misplaced = {f"src/{rel}": body for rel, body in PACKAGE_FILES.items()}
    code, out = run(sdist(project / "x-1.tar.gz", misplaced, top="x-1/extra"))
    assert code == 1
    assert "leaves out 5 of the 5 files" in out


def test_flat_layout_package(project: Path) -> None:
    (project / "cca").mkdir()
    (project / "cca/__init__.py").write_bytes(b"")
    with tarfile.open(project / "flat-1.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo("flat-1/cca/__init__.py")
        tf.addfile(info, io.BytesIO(b""))
    out = io.StringIO()
    assert check_dist.check([Path("flat-1.tar.gz")], Path("cca"), out) == 0, out.getvalue()


def test_long_lists_are_cut(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_dist, "MAX_LISTED", 2)
    code, out = run(wheel(project / "x-1-py3-none-any.whl", {}))
    assert code == 1
    assert out.splitlines()[-1] == "  ... and 3 more"
    assert len(out.splitlines()) == 4


@pytest.mark.parametrize(
    ("name", "body", "problem"),
    [
        ("x-1-py3-none-any.whl", b"not a zip", "not a readable wheel"),
        ("x-1.tar.gz", b"not a tarball", "not a readable sdist"),
        ("x-1.zip", b"", "neither a wheel (*.whl) nor an sdist (*.tar.gz)"),
    ],
)
def test_unreadable_or_unknown_files_fail(
    project: Path, name: str, body: bytes, problem: str
) -> None:
    (project / name).write_bytes(body)
    code, out = run(project / name)
    assert code == 1
    assert out.startswith("FAIL ")
    assert problem in out


def test_missing_distribution_fails(project: Path) -> None:
    code, out = run(project / "gone-1-py3-none-any.whl")
    assert code == 1
    assert "not a readable wheel" in out


def test_empty_or_wrong_package_is_a_usage_error(project: Path) -> None:
    out = io.StringIO()
    assert check_dist.check([Path("x.whl")], Path("src/nothing"), out) == 2
    assert "no files under src/nothing: wrong --package?" in out.getvalue()


def test_main(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    whl = wheel(project / "cca_chess-0.1.0-py3-none-any.whl", PACKAGE_FILES)
    assert check_dist.main([str(whl)]) == 0  # --package defaults to src/cca
    assert "all 5 files under src/cca" in capsys.readouterr().out
    assert check_dist.main(["--package", "src/cca", str(whl)]) == 0
    with pytest.raises(SystemExit) as info:
        check_dist.main(["--package", str(project / "src/cca"), str(whl)])
    assert info.value.code == 2
    with pytest.raises(SystemExit):
        check_dist.main([])
