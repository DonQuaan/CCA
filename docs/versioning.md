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

## What a release publishes

| Artefact | Where | Produced by |
|---|---|---|
| Annotated tag `vX.Y.Z` | the GitHub repository | the maintainer (checklist step 7) |
| Wheel `cca_chess-X.Y.Z-py3-none-any.whl` and sdist `cca_chess-X.Y.Z.tar.gz` | GitHub Release `vX.Y.Z`, with the CHANGELOG section as its notes | `release.yml`, job `publish` |
| Image `ghcr.io/donquaan/cca:X.Y.Z`, plus `latest` | GitHub Packages (ghcr.io), linux/amd64 | `release.yml`, job `image` (calls `image.yml`) |
| Image `ghcr.io/donquaan/cca:X.Y.Z-maia2`, plus `latest-maia2` | as above | as above |
| Build-provenance attestation of each image digest | the registry and the repository's attestations | `image.yml`, `actions/attest` |

Pre-releases (`a`, `b`, `rc`) get no `latest` / `latest-maia2` tags, and their GitHub Release
is marked as a pre-release. The image contents and the reasons behind them are recorded in
[ADR-0007](adr/0007-simulator-and-distribution.md).

## Release checklist

1. `CHANGELOG.md`: move `[Unreleased]` items into `## [X.Y.Z] - YYYY-MM-DD`.
2. Bump `__version__`.
3. Regenerate the technical reference. The version appears in [`reference.md`](reference.md)
   (package table and the UCI `id name` line), so `tests/unit/test_reference.py` fails until the
   file is regenerated:

   ```bash
   uv run python scripts/gen_reference.py
   uv run python scripts/gen_reference.py --check
   ```

4. Local gate (on Windows, give `CCA_STOCKFISH` a Windows path, e.g. from `cygpath -w`):

   ```bash
   uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
   CCA_STOCKFISH=/path/to/stockfish uv run pytest -m engine
   uv run mypy --strict docker/*.py docker/tests/*.py
   uv run pytest -p no:cacheprovider docker/tests
   ```

   The last two lines are the `smoke-unit` job of `.github/workflows/image.yml` (the image smoke
   runner, `docker/check_dist.py`, `docker/fetch_pinned.py`, and checks of the Dockerfile and
   the workflows); `uv run pytest` alone does not collect `docker/tests`. Optionally, build the
   packages exactly as the release gate does and check that nothing under `src/cca` is missing:

   ```bash
   uv build --build-constraints docker/build-constraints.txt --require-hashes
   uv run python docker/check_dist.py --package src/cca dist/*.whl dist/*.tar.gz
   ```

5. Images, if Docker (BuildKit) is available locally:

   ```bash
   docker buildx build --check --target runtime --build-arg CCA_VERSION=X.Y.Z .
   docker buildx build --check --target runtime-maia2 --build-arg CCA_VERSION=X.Y.Z .
   docker buildx build --target runtime --build-arg CCA_VERSION=X.Y.Z \
     --build-arg VCS_REF="$(git rev-parse HEAD)" --load -t cca:local .
   docker buildx build --target runtime-maia2 --build-arg CCA_VERSION=X.Y.Z \
     --build-arg VCS_REF="$(git rev-parse HEAD)" --load -t cca:local-maia2 .
   python docker/smoke.py cca:local --expect-version X.Y.Z
   python docker/smoke.py cca:local-maia2 --variant maia2 --expect-version X.Y.Z
   ```

   `CCA_VERSION` must equal `__version__` (the build stops otherwise). Without `--port`, the
   smoke runner lets Docker pick a free host port. **If local Docker is unavailable, the first
   `Image` workflow run on GitHub (step 8) is the first time the images are built and
   smoke-tested**; treat that run as the image acceptance test and do not push the tag before it
   is green.
6. Independent review of the diff (bugs, maths, security, licences).
7. `git commit -m "chore(release): vX.Y.Z"` then
   `git tag -a vX.Y.Z -m "CCA vX.Y.Z — <one-line summary>"`. A tag that was created on an
   earlier commit and never pushed must be recreated on the release commit (`git tag -d vX.Y.Z`,
   then tag again); a pushed tag is never moved.
8. `git push`, then wait for **green `CI` and `Image` runs on `main`**. On `main` (and on pull
   requests) `image.yml` runs `smoke-unit` and `build-test`: `build-test` builds both targets
   (`runtime`, `runtime-maia2`) with the GitHub Actions cache and runs `docker/smoke.py` on each;
   it never pushes.
9. `git push origin vX.Y.Z` → the `Release` workflow runs:
   - `ci`: the full CI again (Linux/Windows × 3.11–3.13, hygiene hooks, real Stockfish,
     dependency audit);
   - `verify`: the tag must be **annotated** and on a commit of `main`; version and CHANGELOG
     re-checked by `scripts/check_release.py`; sdist and wheel built with the hash-pinned build
     backend; `docker/check_dist.py` on both; the sdist must contain no local tool directories,
     engines, weights or private files;
   - `image`: calls `image.yml` with `push: true` (honoured only for `v*` tag refs). For each
     variant: build again without cache, smoke-test, push to ghcr.io, pull the pushed digest and
     smoke-test it, then attest its build provenance;
   - `publish`: needs `verify` **and** `image`; creates the GitHub Release with the CHANGELOG
     notes and attaches the wheel and the sdist.
10. **Once, after the first image push:** make the package public. A GitHub Packages package
    owned by a personal account is private when it is first published, and only public
    container packages can be pulled anonymously. On the package's page (`cca`, under the
    owner's *Packages*): **Package settings** → *Danger Zone* → **Change visibility** →
    **Public**. This cannot be undone.
11. Verify what was published:

    ```bash
    gh attestation verify oci://ghcr.io/donquaan/cca:X.Y.Z -R DonQuaan/CCA
    gh attestation verify oci://ghcr.io/donquaan/cca:X.Y.Z-maia2 -R DonQuaan/CCA
    docker run --rm --no-healthcheck ghcr.io/donquaan/cca:X.Y.Z version
    ```

    The GitHub Release must list the wheel and the sdist.

## Notes on v0.1.0

- An early local annotated tag `v0.1.0` was made on `3c0bbed`, before the simulator, the UCI
  launcher and the images existed. It was never pushed, was deleted, and was created again on
  the release commit (step 7).
- Docker Desktop was not usable on the maintainer's machine while the images were written, so
  the final Dockerfile was first built and smoke-tested by the `Image` workflow on GitHub
  (step 5): both targets were green on `main` before the tag was pushed.
