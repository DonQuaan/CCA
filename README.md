# CCA — Chaotic-Chess-Algorithm

**A research layer that makes a superhuman engine play like a human who is hard to read —
and who plays for the mistakes *you* are likely to make.**

CCA sits on top of **Stockfish 19** (spoken to over UCI, never embedded) and **Maia-2**
(a neural model of which move a human of a given rating plays). Its decision core,
**C-AIME** (*Chaotic Active-Inference MCTS Engine*), turns their outputs into moves that are

* **sound** — never more than a bounded, budgeted expected-score sacrifice below Stockfish's best;
* **human-like** — anchored on Maia-2 at the agent's own rating (KL-regularised, piKL);
* **opponent-aware** — it looks one reply ahead with Maia-2 at the *opponent's* rating and
  prefers moves after which that opponent is likely to go wrong ("trap value");
* **variable but reproducible** — latent stress / drive variables and a forced Lorenz
  attractor move its knobs from move to move; a node-limited game replays exactly from its seed.

> ⚠️ **Research status (v0.1.0).** The architecture is implemented and tested; its
> *behavioural* claims are **hypotheses** with pre-registered kill criteria
> (`docs/science.md`). The "neuro" mechanisms are labelled for what they are: latent control
> variables *inspired by*, not models of, human physiology.

## Why not just weaken an engine?

Depth caps and random noise make engines play perfectly for ten moves and then blunder in a
way no human would. CCA keeps the engine's truth as a *constraint* and moves the humanity into
the *decision*: which of the sound moves would a human of this strength play, and which of them
poses this particular opponent the hardest practical problems?

## Architecture in one picture

```
board, clock, ratings ─► Stockfish 19 (MultiPV + WDL) ─► q_opt   (engine truth)
                     ├─► Maia-2 @ my Elo            ─► anchor τ (how a human would play)
                     └─► Maia-2 @ opponent Elo      ─► q_human, trap value, opponent entropy
opponent's last move ─► surprise, reward-prediction error ─► stress / drive ─┐
                                              Lorenz-63 attractor (RK4) ─────┤─► knobs λ, ω, ε, …
safe set (ε, risk bank) ─► U = (1−ω)·q_opt + ω·q_human + λ_H·H  ─► π ∝ τ·exp(U/λ) ─► move + think time
```

Details: [`docs/architecture.md`](docs/architecture.md) · science and honest labels:
[`docs/science.md`](docs/science.md) · decisions: [`docs/adr/`](docs/adr/).

## Install

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/DonQuaan/CCA.git && cd CCA
uv sync                                   # core + dev tools
python scripts/fetch_stockfish.py         # Stockfish 19, SHA-256 pinned (≈ 81 MB on Windows)
uv run cca doctor                         # checks Python, engine, optional GPU
```

**Maia-2 (optional, recommended).** `maia2` 0.11 supports **Python 3.10–3.12** only, and the
PyPI `torch` wheel for Windows is CPU-only. On a CUDA machine:

```bash
uv venv --python 3.12
uv pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu128
uv sync --extra maia2                     # first use downloads the 267 MB rapid checkpoint
```

Without Maia-2, `--human qre` uses an engine-derived logit-QRE human model (a baseline with
unfitted parameters — it cannot represent systematic human blind spots).

## Use

```bash
# one decision, as JSON (move, policy, knobs, latent state, candidates, diagnostics)
uv run cca analyse "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3" --human qre --persona tal

# benchmark on a virtual clock against a simulated human opponent (writes PGN + JSONL + summary
# with score, error profiles, engine agreement, human likelihood and policy entropy);
# add --human qre if Maia-2 is not installed (the CLI also falls back with a warning)
uv run cca match --persona tal --opponent human --elo-oppo 1500 --games 10 --out runs/tal-vs-1500

# run as a UCI engine (Arena, cutechess-cli, lichess-bot, ...)
uv run cca uci
```

Personas: `balanced`, `tal`, `dubov`, `solid`, `human` (hand-set; the Tal/Dubov names describe
public reputations and are **not fitted** to those players). UCI options include `UCI_Elo`
(the human rating the agent imitates), the standard `UCI_Opponent`, `CCA_Persona`,
`CCA_HumanModel`, `CCA_Seed` and `CCA_EmulateThinkTime`.

## Reproducibility

`--threads 1` + a node limit + a fixed `--seed` replays a game exactly on the same machine;
`cca match` stores the manifest (versions, commit, engine id, every CLI argument, config, seed,
and whether the run is reproducible) in `summary.json`. The chaos integrator and RNG are pinned
by golden-value tests that CI runs on Linux and Windows. Real-time play (a UCI clock, `stop`)
is *not* reproducible by design: it trades determinism for never losing on time.

For online play leave `CCA_Seed` empty: the UCI server draws a fresh secret seed per game,
prints its full SHA-256 commitment before the first move and reveals the seed when the game
ends (`ucinewgame` / `quit`), so opponents cannot replay the agent's variability during the
game but anyone can audit it afterwards.

## Fair play and ethics

CCA may play online **only** from an account declared as a bot (e.g. a Lichess BOT account).
Using it to assist a human player is cheating. Games against people for research are
human-subjects research: inform, obtain consent and ethics review. CCA is never tuned to evade
anti-cheat detection. See [`docs/science.md#3-ethics-and-platform-rules`](docs/science.md).

## Development

```bash
uv run pytest -m "not slow"     # fast suite (no binaries needed: a fake UCI engine is included)
uv run pytest                   # + numerical checks (Lyapunov exponent, attractor statistics)
CCA_STOCKFISH=path/to/stockfish uv run pytest -m engine
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Versioning follows SemVer / PEP 440 with an annotated git tag per release
([`docs/versioning.md`](docs/versioning.md)); see [`CHANGELOG.md`](CHANGELOG.md) and
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## License and credit

CCA is © 2026 **Nguyễn Vũ Đông Quân (DonQuaan)** and licensed under the
[Apache License 2.0](LICENSE): use, modify and redistribute freely, keep the
[`NOTICE`](NOTICE) and credit the author. Third-party components (Stockfish, python-chess,
Maia-2, …) keep their own licences — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
If you use CCA in research, please cite it ([`CITATION.cff`](CITATION.cff)) and the works it
builds on (`docs/science.md`).
