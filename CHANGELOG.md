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

[Unreleased]: https://github.com/DonQuaan/CCA/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DonQuaan/CCA/releases/tag/v0.1.0
