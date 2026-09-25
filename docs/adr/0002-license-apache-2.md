# ADR-0002 — Apache-2.0 for CCA's own code; third parties at arm's length

* Status: accepted · 2026-09-25

## Context
The owner wants CCA to be 100 % open: anyone may modify and reuse it, provided they credit
the author. Dependencies have mixed licences: Stockfish, Lc0, python-chess and Maia-1 are
GPL-3.0, lichess-bot is AGPL-3.0, Maia-2 and pymdp are MIT, OpenSpiel is Apache-2.0, and the
Cicero repository is mostly CC BY-NC 4.0 (non-commercial).

## Decision
* CCA's own files are **Apache-2.0** (permissive, explicit attribution through `NOTICE`,
  explicit patent grant, "state your changes" clause).
* Stockfish / Lc0 run as **separate processes over UCI** and are **downloaded, never
  committed or bundled** by the project.
* python-chess is imported (it is the adapter layer's chess library). Apache-2.0 is
  one-way compatible with GPL-3.0, so a combined distribution is possible under GPL-3.0
  terms; the math core (`chaos`, `neuro`, `policy`, `timing`, `core`) imports nothing from
  GPL code so it can be reused on its own under Apache-2.0.
* Code from non-commercial or copyleft projects is **never copied**; algorithms (piKL, QRE,
  Active Inference) are re-implemented from the papers.

## Consequences
Anyone redistributing a *bundle* that contains python-chess or Stockfish binaries must also
honour GPL-3.0 for those parts (documented in `THIRD_PARTY_NOTICES.md`). If the owner prefers
a single copyleft licence for everything, switching CCA to GPL-3.0-or-later is compatible
with all current dependencies.
