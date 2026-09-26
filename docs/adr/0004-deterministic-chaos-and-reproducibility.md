# ADR-0004 — Deterministic chaos that replays bit-identically

* Status: accepted · 2026-09-25

## Decision
* Lorenz-63 integrated in **pure Python floats** with classical RK4 in a fixed operation
  order (IEEE-754 binary64 `+ - *` are correctly rounded and CPython does not fuse them), so
  the trajectory is expected to be identical on every platform. Golden digests pinned in
  `tests/unit/test_review_regressions.py` are checked by CI on Linux and Windows × 3.11–3.13.
* All randomness flows through `DeterministicRng(seed, stream...)` (SHA-256-derived seeds,
  `random.Random.random()` only, Box–Muller for normals).
* External inputs (surprise, reward-prediction error) are **quantised to 1e-3 and capped**
  before they kick the ODE, so last-ulp differences in neural-network inference cannot fork
  the trajectory except at a quantisation boundary.
* Signals per ply (40 RK4 steps): `u0 = tanh(x/σx)` (lobe = persistent mood),
  `u1 = tanh(y/σy)`, and `u2` from the **last local maximum of z** (Lorenz map), *not* from
  `z` itself. Measured: raw `z` sampled per ply has lag-1 autocorrelation **−0.62** and flips
  sign on **~80 %** of plies — a near-alternating pattern an opponent could learn (the owner
  spec maps exactly this `z(t)` onto `c_puct`). The z-maximum series has lag-1 **+0.50**,
  flip rate **0.22**, and decorrelates within ~4 plies. A regression test pins this.
* Burn-in is 3000 steps (an adversarial KS check showed 1000 is not enough to reach the
  invariant measure).
* For online play the seed must be **secret** (anyone who knows seed + moves can replay the
  modulation). With no explicit seed the UCI server draws a fresh 128-bit secret **per game**,
  prints its full SHA-256 commitment before the first move and reveals it when the game ends
  (commit–reveal), so a revealed seed never helps against later games. Research runs set an
  explicit seed.

## Consequences
With Stockfish `Threads=1` and a node limit, a whole game replays from its manifest.
Multi-threaded search or time limits break exact replay (documented).
