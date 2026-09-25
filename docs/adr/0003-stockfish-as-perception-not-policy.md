# ADR-0003 — Stockfish is the perception layer, not the policy

* Status: accepted · 2026-09-25

## Context
The owner spec maps chaos onto `c_puct`, a parameter of PUCT/MCTS (Lc0). Stockfish is an
alpha-beta engine: it has no `c_puct`, and weakening it with depth caps / random pruning
produces the non-human blunder pattern the project wants to avoid.

## Decision
Stockfish only *measures* (MultiPV + WDL → expected score `q_opt`, and the value of likely
human replies). The move is chosen by CCA's own decision core: human-aware 2-ply look-ahead
(Maia-2 opponent model) + safe set + closed-form piKL anchored on the agent's human prior.
The chaotic signal modulates what exists in that core — the KL weight `λ`, exploitation `ω`,
risk budget `ε`, complexity bonus `λ_H` — instead of `c_puct`. A chaos-modulated PUCT search
(Maia-2 prior/value, Stockfish at leaves, `c_puct(t)` exactly as in the spec) is planned as an
optional search back-end.

## Consequences
Strength is bounded by Stockfish's truth and the risk budget (never a random blunder);
human-likeness comes from the anchor. The cost is ~`K + M + 1` node-limited Stockfish calls
per move.
