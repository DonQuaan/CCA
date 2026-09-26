# Third-party notices

CCA's own source code is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`).
CCA **does not contain** the source code, binaries or weights of the projects below. This file
records how each one is used, so that redistributors know which obligations apply to them.
Licence identifiers were checked against each project's own LICENSE file (2026-09-25).
This is an engineering summary, not legal advice.

| Component | Licence | How CCA uses it | Obligations / rules |
|---|---|---|---|
| [Stockfish 19](https://github.com/official-stockfish/Stockfish) (tag `sf_19`) | GPL-3.0-or-later | **Separate process**, spoken to over UCI. Downloaded at install time from the official release by `scripts/fetch_stockfish.py` (SHA-256 pinned). Never committed or bundled. Its WDL win-rate formula and 8 fitted coefficients are re-implemented from `sf_19` `src/uci.cpp` in `cca.engines.stockfish` (formula and numeric data only, no source copied). | If *you* redistribute a Stockfish binary, you must ship its licence and the corresponding source (the official archive already contains both, `Copying.txt` and `src/`). Its NNUE training data comes from Leela Chess Zero under the ODbL. |
| [python-chess](https://github.com/niklasf/python-chess) (`chess` on PyPI) | GPL-3.0-or-later | **Imported in-process** by the adapter layer (`cca.engines`, `cca.uci`, `cca.bench`, `cca.agent`) and by Maia-2. | Per the FSF's interpretation, a program that imports a GPL library forms a combined work; Apache-2.0 is one-way compatible with GPL-3.0, so a distribution of CCA **together with** python-chess is possible under GPL-3.0 terms. The math core (`cca.core`, `cca.chaos`, `cca.neuro`, `cca.policy`, `cca.timing`) imports nothing from GPL code. |
| [Maia-2](https://github.com/CSSLab/maia2) (`maia2` on PyPI) | MIT, © 2024 CSSLab | Optional extra, imported in-process. | Keep the MIT notice for the code. The two checkpoints (~267 MB each, SHA-256-checked by `maia2`) are fetched from Google Drive; **no licence is published for the weights** — their terms are unknown, so do not rehost or redistribute them; let `maia2` download them from the official source. |
| [Maia-1](https://github.com/CSSLab/maia-chess) | GPL-3.0 | Not used in v0.1. Planned only as a held-out human model through Lc0 (separate process). | GPL-3.0 if redistributed. |
| Maia-3 / Chessformer ([CSSLab/maia3](https://github.com/CSSLab/maia3)) | AGPL-3.0 | Not used. | Importing it in-process would put the combined work, including network services, under the AGPL. Only a separate-process integration is compatible with ADR-0002. |
| [Leela Chess Zero](https://github.com/LeelaChessZero/lc0) | GPL-3.0 (+ §7 permission) | Not used in v0.1. Planned optional PUCT back-end over UCI (separate process). | As Stockfish. |
| [lichess-bot](https://github.com/lichess-bot-devs/lichess-bot) | AGPL-3.0-or-later | Not a dependency. It can *run* CCA as an external UCI engine (`cca uci`). | Separate programs talking UCI over pipes. AGPL §13 applies only if you modify lichess-bot and let others use it over a network. |
| [OpenSpiel](https://github.com/google-deepmind/open_spiel) | Apache-2.0 | Not a dependency; referenced for QRE / MMD algorithms. | — |
| [pymdp](https://github.com/infer-actively/pymdp) | MIT | Not a dependency; referenced for Active Inference. | — |
| [diplomacy_cicero](https://github.com/facebookresearch/diplomacy_cicero) (archived) | Code MIT, except `fairdiplomacy_external/` (AGPL-3.0); model weights CC BY-NC 4.0 | **Not used.** piKL is re-implemented from the papers (closed form), no code copied. | — |
| [Human-Chess-Bot](https://github.com/nexicturbo/Human-Chess-Bot) | Labelled MIT (licence text copied from PawnBit, © 2022 P. Iatrou); depends on GPL python-chess and bundles GPL Lc0/Maia-1 | **Not used — nothing reused.** Audit (2026-09-25): 4 commits in one day, 0 stars, no tests, no data behind its "12M games" claim; its purpose is evading anti-cheat detection, which CCA rejects. | — |

## Data

Lichess game database exports are released under CC0; they are the intended source for fitting
CCA's hand-set parameters (think-time model, QRE precision, affect dynamics).
