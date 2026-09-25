# Contributing to CCA

Thanks for your interest! CCA is a research project: correctness and honesty come first.

## Ground rules

1. **No fabricated science.** Every claim in code comments or docs that cites research must
   point to a real, checkable source (DOI / arXiv / official repo). Hand-set parameters are
   labelled as such. Mechanisms inspired by neuroscience are labelled as *inspired by*, not
   *models of*, physiology (see `docs/science.md`).
2. **Licences.** Never paste code from GPL, AGPL or non-commercial (CC BY-NC) projects into
   `src/`. Stockfish / Lc0 are used as separate UCI processes; algorithms from papers are
   re-implemented from the maths. See `THIRD_PARTY_NOTICES.md`.
3. **Determinism.** Randomness goes through `cca.core.rng.DeterministicRng`; the chaos
   integrator uses only `+ - *` on Python floats in a fixed order. Do not introduce
   `random.random()`, `numpy.random` or time-dependent behaviour in the decision path.

## Development

```bash
uv sync                                   # create .venv with dev tools
uv run pre-commit install -t pre-commit -t pre-push
uv run pytest -m "not slow"               # fast suite
uv run pytest                             # everything that does not need binaries
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Engine tests need a real Stockfish: `python scripts/fetch_stockfish.py`, then
`CCA_STOCKFISH=<path> uv run pytest -m engine`.

## Commits and pull requests

* [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
  `feat(policy): ...`, `fix(chaos): ...`, `docs: ...`, `test: ...`, `refactor: ...`,
  `perf: ...`, `build: ...`, `ci: ...`, `chore: ...`. Breaking changes: `feat!:` plus a
  `BREAKING CHANGE:` footer.
* Every PR updates `CHANGELOG.md` under `## [Unreleased]`.
* Every behaviour change comes with a test; numerical changes come with a property test or a
  measured tolerance.
* CI must be green on Linux and Windows, Python 3.11 - 3.13.

## Releases

See `docs/versioning.md`.
