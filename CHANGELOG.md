# Changelog

All notable changes to CCA are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see `docs/versioning.md`).

## [Unreleased]

## [0.1.0] - 2026-09-25

First research release of the C-AIME decision core.

### Added
- **Module 2 — deterministic chaos:** forced Lorenz-63 oscillator (classical RK4, dt = 0.01,
  40 steps/ply), bit-reproducible across platforms, quantised and capped event kicks,
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
  (`balanced`, `tal`, `dubov`, `solid`, `human`), strict TOML configuration.
- Adapters: Stockfish over UCI (WDL-based expected score, reproducible node-limited mode),
  engine-derived QRE human model; Maia-2 human model (optional extra).
- `cca` CLI (`uci`, `analyse`, `match`, `doctor`, `version`), UCI server with standard
  `UCI_Elo` / `UCI_Opponent` options, virtual-clock match harness with referee-engine error
  statistics (PGN + JSONL + summary with Wilson intervals).
- Evidence-driven safeguards from the verification brief (`docs/research/`): risk bank
  (risk only what the opponent has given away), damped exploitation when the opponent's clock
  is in Maia-2's untrained ≤ 30 s regime, tempered prior in the first 10 plies, stress→habit
  prior sharpening, field-data risk signs (time pressure → risk-averse, tilt after mistakes),
  bounded `λ_KL`.
- `chaos_driver = "ar1"`: matched-autocorrelation control condition for the pre-registered
  Lorenz A/B test (P5). Lorenz signal 3 uses the z-maxima (Lorenz map) because raw `z`
  sampled per ply alternates predictably (measured lag-1 autocorrelation −0.62).
- Stockfish 17–19 material-based win-rate model (ported from `sf_19/src/uci.cpp`) for engines
  without WDL output; universal SF19 assets with pinned SHA-256 digests.
- UCI: secret random seed with a published SHA-256 commitment, `go ponder` / `ponderhit`
  without touching the agent's state on guessed positions, FEN validation.
- Tooling: uv, ruff, mypy `--strict`, pytest + Hypothesis, pre-commit, GitHub Actions CI
  (Linux/Windows × Python 3.11–3.13, real-Stockfish job, dependency audit), tag-gated release
  workflow, SHA-256-verified Stockfish fetcher.
- Real-time play: wall-clock deadlines and `stop` bound the computation (engine time slices,
  look-ahead cut, *reflex* mode below `fast_budget`); `isready` builds engines before the clock
  runs. `cca match` summaries report engine agreement, human likelihood and policy entropy.

### Fixed (pre-release adversarial review: 7 lenses, 55 verified findings)
- **Benchmark:** the "Stockfish-UCI_Elo" opponent played full-strength PV moves; it now plays
  the engine's own strength-limited `bestmove`.
- **Agent:** clock pressure ignored UCI `movestogo`; draws by threefold repetition / fifty-move
  rule were scored as if play continued; the risk bank was credited with evaluation noise and a
  negative balance never restricted risk; the opening guard mis-detected endgame FENs and did not
  temper the opponent's surprise; child positions lost their move stack (repetitions invisible).
- **Opponent model:** theory-of-mind flattening lowered `q_human` (mass moved to the
  unevaluated tail); truncated reply distributions were treated optimistically.
- **Chaos control:** the AR(1) condition now matches the *consumed* Lorenz signals (lag-1,
  u0–u1 cross-correlation, kick response) and a test keeps it calibrated.
- **UCI:** every `go` now answers exactly one `bestmove` (also when engines fail to start);
  invalid option values are rejected without side effects; the echoed `<secret>` default no
  longer becomes a public seed; per-game commit–reveal seeds; dead Stockfish processes are
  restarted; no process leaks.
- **Security / supply chain:** explicit engine paths are never silently replaced; engines are
  only auto-discovered in the project's own `engines/`; the GitHub token is sent only to the API;
  TOML rejects NaN/Inf and floats for integers; CI audits every extra and group, runs the
  pre-commit hygiene hooks, pins actions to SHAs; releases only from `main` after the full gate.
- **Docs:** Maia-2 weights licence marked unknown (do not rehost); Cicero licence corrected in
  ADR-0002; overclaiming labels removed; cross-platform reproducibility is now tested (golden
  values) instead of asserted.

[Unreleased]: https://github.com/DonQuaan/CCA/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DonQuaan/CCA/releases/tag/v0.1.0
