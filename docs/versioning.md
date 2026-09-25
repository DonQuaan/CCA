# Versioning & release process

CCA follows **[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)**, written in
**[PEP 440](https://peps.python.org/pep-0440/)** form so that the git tag, the Python package
version and the CHANGELOG entry are the same string.

## Version scheme

| Form | Meaning | Example tag |
|---|---|---|
| `0.MINOR.PATCH` | Research phase. Any `MINOR` may change the public API or the default behaviour of the agent. | `v0.1.0` |
| `MAJOR.MINOR.PATCH` (≥ 1.0.0) | Stable API. `MAJOR` = breaking, `MINOR` = backwards-compatible feature, `PATCH` = fix. | `v1.2.3` |
| `X.Y.ZaN` / `bN` / `rcN` | Pre-releases (alpha / beta / release candidate). | `v0.2.0rc1` |

Rules:

1. The single source of truth is `__version__` in `src/cca/__init__.py`.
2. **Every** release (even a one-line fix) bumps the version and creates an **annotated git
   tag** `vX.Y.Z` whose message summarises the change. `scripts/check_release.py` refuses a
   tag that does not match `__version__` or has no CHANGELOG section.
3. `1.0.0` and any later `MAJOR` bump require the maintainer's explicit approval.
4. Because CCA is a *behavioural* engine, **a change of default playing behaviour counts as a
   MINOR change** even when no function signature changes (it invalidates earlier benchmark
   numbers). Changes that keep behaviour bit-identical for a fixed seed can be PATCH.

## Research reproducibility contract

A benchmark result is only citable together with:
`cca version + git commit + Stockfish id + human-model id/version + config file + seed`.
`cca match` writes this manifest into `summary.json`. With `threads=1` and node-limited
search, a run replays bit-identically for the same manifest (GPU inference of the human model
may differ in the last bits across hardware; chaos inputs are quantised to limit the effect).

## Release checklist

1. `CHANGELOG.md`: move `[Unreleased]` items into `## [X.Y.Z] - YYYY-MM-DD`.
2. Bump `__version__`.
3. Local gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
   (+ `CCA_STOCKFISH=... uv run pytest -m engine`).
4. Independent review of the diff (bugs, maths, security, licences).
5. `git commit -m "chore(release): vX.Y.Z"` then
   `git tag -a vX.Y.Z -m "CCA vX.Y.Z — <one-line summary>"`.
6. `git push && git push origin vX.Y.Z` → the `Release` workflow re-checks the tag, runs the
   tests, builds sdist/wheel and publishes a GitHub Release with the CHANGELOG notes.
