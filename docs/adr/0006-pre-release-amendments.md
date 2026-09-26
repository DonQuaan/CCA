# ADR-0006 — Pre-release amendments to ADR-0002 and ADR-0004

* Status: accepted · 2026-09-26

## Context
ADR-0001 makes ADRs append-only. Two adversarial reviews before the first release (v0.1.0)
found factual errors and gaps in the text of two accepted ADRs; the text was corrected in place
before any release, and this record lists every change so nothing is rewritten silently.

## Amendments
* **ADR-0002** — licence facts corrected against the projects' own LICENSE files: the Cicero
  repository's *code* is MIT except `fairdiplomacy_external/` (AGPL-3.0) and only its model
  weights are CC BY-NC 4.0 (the first text called most of the repo CC BY-NC); Maia-3 is
  AGPL-3.0. Added: the one piece of third-party-derived content in v0.1 (Stockfish's win-rate
  formula and 8 fitted coefficients, re-implemented with attribution); third-party weights are
  never rehosted; the owner confirmed Apache-2.0 on 2026-09-25 after reviewing GPL-3.0.
* **ADR-0004** — "identical on every platform" weakened to "expected to be identical", now
  checked by golden digests on Linux and Windows CI; secret seeds became per game with
  commit–reveal (a single process-wide seed would make every later game replayable once one
  seed is revealed).

## Consequences
From v0.1.0 on, ADR changes follow ADR-0001 strictly: a new ADR supersedes or amends an old
one; accepted ADR text is not edited except to add an "amended by" pointer.
