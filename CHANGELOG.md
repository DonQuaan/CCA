# Changelog

All notable changes to CCA are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see `docs/versioning.md`).

## [Unreleased]

## [0.1.0] - 2026-09-28

First research release of the C-AIME decision core, with a browser simulator (`cca play`), a
no-argument UCI launcher (`cca-uci`) and container images on ghcr.io. Every parameter is listed
in the generated `docs/reference.md`; the design decisions of this release's distribution work
are recorded in `docs/adr/0007-simulator-and-distribution.md`.

### Added
- **Module 2 — deterministic chaos:** forced Lorenz-63 oscillator (classical RK4, dt = 0.01,
  40 steps/ply), bit-reproducible across platforms (golden digests re-checked by CI on Linux
  and Windows, Python 3.11–3.13), quantised and capped event kicks,
  measured attractor statistics and Lyapunov exponent under test.
- **Module 1 — virtual neuromodulation:** latent stress (exactly discretised relaxation ODE,
  bounded clock pressure instead of the singular `1/ΔT`), reward-prediction-error drive,
  theory-of-mind opponent stress, Gaussian tunnel-vision attention mask.
- **Module 3 — decision core:** closed-form piKL policy anchored on the human prior, logit
  quantal response, human-aware 2-ply look-ahead (`q_human`, trap value, opponent entropy,
  sharpness), safe exploitation (risk budget vs the engine's best move).
- Human-like think-time model: mean-preserving log-normal noise with AR(1) memory, inverted-U
  game-phase curve, complexity and forced-move factors, clock ceilings.
- `CAIMEAgent` orchestrator (resumable from a UCI move stream), personas
  (`balanced`, `tal`, `dubov`, `solid`, `human`), strict TOML configuration (unknown keys,
  wrong types, non-integers for integer fields and NaN/Inf are errors).
- Adapters: Stockfish over UCI (WDL-based expected score, reproducible node-limited mode),
  engine-derived QRE human model; Maia-2 human model (optional extra). Stockfish is looked up
  as an explicit path, then `$CCA_STOCKFISH`, then the source checkout's `engines/`, then
  `stockfish` on `PATH`; an explicit path or `$CCA_STOCKFISH` that is not a file is an error and
  is never replaced by another binary. Maia-2 weights are read from `$CCA_WEIGHTS`, else from the
  checkout's `weights/maia2`, located from the package, not from the working directory.
- `cca` CLI (`uci`, `analyse`, `match`, `doctor`, `version`, `play`), UCI server with standard
  `UCI_Elo` / `UCI_Opponent` options, virtual-clock match harness with referee-engine error
  statistics (PGN + JSONL + summary with Wilson intervals). The `Stockfish-UCI_Elo-<elo>`
  opponent plays the strength-limited engine's own `bestmove`, not its principal variation.
  `summary.json` carries a run manifest: CCA version, the checkout's commit and dirty flag, the
  SHA-256 of the Stockfish binary and of the Maia-2 checkpoint, and the engine and human-model
  settings.
- Evidence-driven safeguards from the verification brief (`docs/research/`): risk bank
  (risk only what the opponent has given away), damped exploitation when the opponent's clock
  is in Maia-2's untrained ≤ 30 s regime, tempered prior in the first 10 plies, stress→habit
  prior sharpening, field-data risk signs (time pressure → risk-averse; `d_tilt` raises risk
  after negative surprises, a loose proxy for "after one's own mistakes"), bounded `λ_KL`.
- `chaos_driver = "ar1"`: stochastic control condition for the pre-registered Lorenz A/B test
  (P5), calibrated to the consumed signals' lag-1 autocorrelation, u0–u1 cross-correlation and
  kick response; its longer-range memory still differs from Lorenz (an IAAFT surrogate is on
  the roadmap). Lorenz signal 3 uses the z-maxima (Lorenz map) because raw `z`
  sampled per ply alternates predictably (measured lag-1 autocorrelation −0.62).
- Stockfish 17–19 material-based win-rate model (ported from `sf_19/src/uci.cpp`) for engines
  without WDL output; universal SF19 assets with pinned SHA-256 digests. A real-engine test
  checks the port against Stockfish's own WDL output (it requires a maximum error below 0.003
  and a mean error below 0.001).
- UCI: a secret random seed per game with a published SHA-256 commitment, `go ponder` /
  `ponderhit` without touching the agent's state on guessed positions, FEN validation. Every
  `go` answers exactly one `bestmove` (the first legal move if anything fails); a rejected
  `position` is answered with `bestmove 0000`; an invalid option value is rejected and the
  previous value kept (a `StockfishPath` that is not a file keeps the working engine); a crashed
  Stockfish is restarted on the next `go`; any Maia-2 load failure falls back to QRE, and
  changing a Stockfish option (`StockfishPath`, `Threads`, `Hash`, `CCA_Nodes`) keeps a loaded
  Maia-2 model.
- UCI on Windows: the server polls its stdin pipe (`PeekNamedPipe`) instead of leaving a
  blocking read pending, which would stall loading Maia-2 (`import torch`) during `isready`
  while the GUI waits for `readyok`. A real Maia-2 handshake over a Windows pipe is a
  regression test.
- Tooling: uv, ruff, mypy `--strict`, pytest + Hypothesis, pre-commit, GitHub Actions CI
  (Linux/Windows × Python 3.11–3.13, real-Stockfish job, dependency audit), tag-gated release
  workflow, SHA-256-verified Stockfish fetcher.
- Real-time play: wall-clock deadlines and `stop` bound the computation (engine time slices,
  look-ahead cut, *reflex* mode below `fast_budget`), the human-model calls included (out of
  time, the agent uses a uniform prior); `isready` builds engines before the clock runs.
  `cca match` summaries report engine agreement, human likelihood and policy entropy.
- **`cca play`, browser simulator:** play against CCA and watch its decision signals. Board
  with click, drag and keyboard move entry (SAN or UCI; a typed promotion must name its piece)
  and a promotion dialog; clocks with increment and an optional human-like thinking delay;
  take-back in untimed games, resignation, PGN export and a position lab (what CCA would play in
  any FEN). The "CCA's thinking" panel shows the candidates (policy, human prior, `q_opt`,
  `q_human`, trap value), the knobs, the latent state and a time-series strip; switched off
  (fair game) it stays hidden until the game ends. Stress, drive, opponent stress and chaos are
  labelled as virtual control signals inspired by, not models of, physiology (ADR-0005).
  English and Vietnamese UI; light or dark theme from the system setting. A game without a
  seed gets a secret one, SHA-256-committed during the game and revealed at the end. Any Maia-2
  failure falls back to QRE, and the UI says why. Options: `docs/reference.md` (`cca play`).
- **`cca play`, server and security:** standard-library `http.server`, no build step; the page
  loads nothing from other hosts. Binds `127.0.0.1:8765` by default (`--port 0` = any free
  port); any other address prints a warning, because there is no authentication. A `Host`
  allowlist on every bind (421 otherwise) guards against DNS rebinding; a POST with a foreign
  `Origin` or `Sec-Fetch-Site: cross-site` gets 403, and its body must be `application/json` and
  at most 64 KiB (415 / 413 otherwise); a strict Content-Security-Policy whose only exception is
  `style-src-attr 'unsafe-inline'`, `nosniff`, JSON-only errors. One Stockfish process and one
  human model serve every game: a lock serialises the decisions and each decision starts with
  `ucinewgame`, so an untimed seeded game replays the same moves on the same server when the
  engine search and the human model are deterministic (checked with QRE, not with Maia-2).
  `/healthz` answers 200 at once (`"ready": false` while the engines warm up) and 503 after a
  failed engine start. Start-up errors (no Stockfish, unknown persona, unreadable config, Elo out
  of range, busy port) exit 1 with an `error:` line instead of a traceback.
- **Vendored browser files** under `cca/play/static/vendor/` (in the repository, the wheel, the
  sdist and the images): cm-chessboard 8.14.2 (MIT), chess.js 1.4.0 (BSD-2-Clause) and a
  Cburnett piece sprite built from the Wikimedia Commons originals under their BSD-3-Clause
  option. `MANIFEST.json` records the source, size and SHA-256 of all 22 files, and the tests
  re-hash them. cm-chessboard's own piece and marker sprites (CC BY-SA / CC BY-NC-SA) are not
  vendored; CCA draws its own markers.
- **UCI for GUIs and bots:** `cca-uci`, a console script that is exactly `cca uci` and ignores
  its command-line arguments (lichess-bot appends its engine options as `--key=value`), for GUIs
  that cannot pass arguments. New options: `Move Overhead` (spin, default 10, 0–5000 ms,
  subtracted from every clock and `movetime` deadline but never below 10 ms from now, so by
  default every such deadline is 10 ms earlier), `Ponder` (check; signals `go ponder` /
  `ponderhit` support; `bestmove` carries no ponder move) and `UCI_LimitStrength` (check,
  default `true`, unlike the UCI spec's default, see ADR-0007; `false` ignores `UCI_Elo` and
  imitates 2600, the top of its range). `go depth` / `nodes` / `mate` / `searchmoves` are
  accepted but not applied (the budget is `CCA_Nodes` plus the clock), and CCA says so in one
  `info string` before its `bestmove`; an unknown `setoption` name is answered with
  `info string ignoring unknown option <name>`. The placeholders `<auto>`, `<empty>` and
  `<secret>` are accepted only by the string options that advertise them (`StockfishPath`,
  `UCI_Opponent`, `CCA_Seed`). `UCI_OPTION_HELP` describes every advertised option in one line
  (table in `docs/reference.md`). Option names are case-sensitive.
- **Container images** (GitHub Packages, linux/amd64 only): `ghcr.io/donquaan/cca:<version>`
  (+ `latest`) holds CCA, Stockfish 19 and the QRE human model and runs `cca play` on port
  8765; `ghcr.io/donquaan/cca:<version>-maia2` (+ `latest-maia2`) adds Maia-2 0.11 on CPU-only
  PyTorch 2.8.0 and downloads the Maia-2 weights on first use into a `/data` volume (no image
  contains them). Non-root user, tini as init, `HEALTHCHECK` on `/healthz`; base images pinned
  by digest; Stockfish, tini and the torch wheel pinned by SHA-256; Python requirements and the
  build backend hash-checked (`uv.lock`, `docker/build-constraints.txt`); no pip, uv or
  compiler in the runtime images. Stockfish's licence, complete source and exported NNUE
  network ship in `/usr/local/share/doc/stockfish/`. `docker/smoke.py` tests each image
  (config and labels, `version`, `doctor`, files and licences, UCI, `analyse`, and the web page
  loaded the way a browser loads it, with every vendored file's SHA-256) before the push and
  again on the pushed digest, which then gets a signed build-provenance attestation
  (`gh attestation verify oci://ghcr.io/donquaan/cca:0.1.0 -R DonQuaan/CCA`).
- **Technical reference** `docs/reference.md`, generated from the code by
  `scripts/gen_reference.py`: package metadata, pinned artefacts with their SHA-256, every CLI
  flag, every UCI option, the configuration file layout and every configuration and data-model
  field, the shipped personas, the model equations' docstrings, numeric constants, environment
  variables and the dependency-audit exceptions. `tests/unit/test_reference.py` fails when the
  file no longer matches the code (`gen_reference.py --check` prints the diff) and checks key
  rows independently of the generator.
- **Release pipeline:** `release.yml` runs `ci` → `verify` → `image` → `publish`; the GitHub
  Release is published only after the gate and both images passed. The gate builds the sdist
  and wheel with the hash-pinned build backend, checks with `docker/check_dist.py` that both
  hold every file under `src/cca`, rejects an sdist that contains local tool directories,
  engines, weights or private files, and the upload and the release fail if a file is missing.
  `image.yml` builds and smoke-tests both images on pull requests and on `main` without pushing;
  for a `v*` tag it rebuilds them without cache, tests them, pushes, tests the pulled digest and
  attests it. Every action is pinned to the commit SHA of a release that runs on Node 24
  (GitHub removed Node 20 from its runners on 2026-09-23); CI checkouts keep no credentials;
  Dependabot also watches the Docker base images.
- **Notices:** `NOTICE` and `THIRD_PARTY_NOTICES.md` separate the source and the Python
  packages (whose only third-party files are the vendored browser files) from the container
  images, a combined distribution: Stockfish 19, python-chess, tini and the python:3.12-slim
  base, plus Maia-2's code and PyTorch in the `-maia2` images, never the Maia-2 weights. New
  rows for PyTorch, tini, the base image and the vendored web assets. No licence is published
  for the Maia-2 weights, so CCA never rehosts them.

### Security
- The dependency audit (`scripts/audit_deps.py`, run by CI, also inside the Release workflow)
  checks every pin in `uv.lock`, across all extras and dependency groups. It accepts eight
  advisories, all in torch 2.8.0, which only the optional `maia2` extra installs (maia2 0.11
  requires torch < 2.9). Each carries a written reason, and the list expires on 2027-03-31:
  after that date the audit fails until every entry is re-assessed.
- CVE-2026-24747 (torch's `weights_only` unpickler) is mitigated: CCA checks the pinned SHA-256
  of the official Maia-2 checkpoint before torch loads it. The other seven accepted advisories
  concern torch APIs that neither maia2 0.11.0 nor CCA calls (checked by grep).
- `scripts/fetch_stockfish.py` sends `GITHUB_TOKEN`, when set, only to `https://api.github.com/`,
  never to the download hosts the release assets redirect to.

### Development notes (pre-release)

Bugs found and fixed by the maintainer's reviews while this first release was prepared. None of
them was in a published release; the resulting behaviour is described under *Added*.

<details>
<summary>Pre-release fixes</summary>

- **Packaging:** the root `.gitignore` rule `dist/` also matched
  `src/cca/play/static/vendor/chess.js/dist/`, so hatchling left `chess.js` out of the wheel
  and the sdist. `/build/` and `/dist/` are now anchored to the repository root, and
  `docker/check_dist.py` stops the image build and the release on any missing file. The
  unanchored sdist include patterns in `pyproject.toml` also matched inside local tool
  directories (`.venv-maia2`, `.uv-python`, `.pre-commit-cache`); they are anchored too.
- **Windows deadlock:** a blocking read pending on the stdin pipe stalled `import torch` (and
  CUDA's lazy DLL loads) during the `isready` warm-up until more input arrived, and a GUI waiting
  for `readyok` sends none. Measured: more than 90 s (hang) with the blocking reader, 1.4 s with
  the polling reader, the same as with no reader.
- **UCI:** `setoption name Move Overhead value <empty>` was accepted, after which every timed
  `go` answered with the fallback move. Also fixed: a `go` did not always answer exactly one
  `bestmove` (also when the engines failed to start); invalid option values had side effects;
  the echoed `<secret>` default became a public seed; commit–reveal did not follow what was
  actually committed; a rejected `position` answered a move for the previous board; the
  deadline left out engine (re)build time; `CCA_EmulateThinkTime` subtracted compute time
  twice; changing a Stockfish option threw away the loaded Maia-2 model; `serve()` did not
  always shut the engines down; dead Stockfish processes were not restarted; processes leaked.
- **Time control:** a history resync inside `choose()` erased the deadline and `stop`, and the
  human-model calls (each a full engine search with the QRE model) ignored the deadline.
- **Agent:** clock pressure ignored UCI `movestogo`; draws by threefold repetition or the
  fifty-move rule were scored as if play continued; the risk bank was credited with evaluation
  noise and a negative balance never restricted risk; the opening guard mis-detected endgame FENs
  and did not temper the opponent's surprise; child positions lost their move stack, so
  repetitions were invisible.
- **Opponent model:** theory-of-mind flattening lowered `q_human` (mass moved to the
  unevaluated tail); truncated reply distributions were treated optimistically.
- **Chaos control:** the AR(1) condition was re-calibrated to the Lorenz signals the agent
  actually consumes, and a test keeps it calibrated.
- **Benchmark:** the `Stockfish-UCI_Elo` opponent played full-strength principal-variation
  moves instead of the engine's strength-limited `bestmove`.
- **Security and supply chain:** the dependency audit failed on every run and skipped pins with
  environment markers; explicit engine paths could be silently replaced; Stockfish
  auto-discovery searched the caller's working directory; the GitHub token could reach hosts
  other than the API; TOML accepted NaN/Inf and floats for integers. CI now also runs the
  pre-commit hygiene hooks, actions are pinned to commit SHAs, and releases need an annotated
  tag on `main` and the full CI.
- **Reproducibility:** Maia-2 weights were resolved against the caller's working directory; run
  manifests lacked the commit, the dirty flag and the binary and checkpoint digests.
- **Tests:** each regression test was checked by reverting its fix and seeing it fail; a second
  mutation round found three untested guards (risk debit, `λ_KL` clamp, win-rate coefficients),
  now covered. The enforcement scripts (release gate, verified Stockfish fetch, dependency audit)
  have behavioural tests. Cross-platform reproducibility is tested with golden values instead of
  asserted.
- **Docs:** the Maia-2 weights licence is marked unknown (do not rehost); the Cicero licence was
  corrected in ADR-0002; overclaiming labels were removed.

</details>

[Unreleased]: https://github.com/DonQuaan/CCA/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DonQuaan/CCA/releases/tag/v0.1.0
