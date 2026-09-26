# Security Policy

## Supported versions

Only the latest `0.y` minor release receives fixes while CCA is in the 0.x research phase.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on the CCA repository. You should get an answer
within 7 days. Include a reproduction and the affected version.

## Scope and design notes

* CCA starts third-party engines as child processes with an **argument list** (never through a
  shell), so engine paths from options or config files cannot inject shell commands.
* `scripts/fetch_stockfish.py` only downloads from GitHub, verifies the asset's SHA-256
  against GitHub's published digest, pins it in `engines/stockfish.lock.json`, and refuses
  archives containing path-traversal entries.
* Neural-network weights are third-party artefacts. Loading untrusted PyTorch checkpoints can
  execute code — torch 2.8 (capped by maia2 0.11) is affected by CVE-2026-24747 in its
  `weights_only` unpickler. CCA therefore pins the SHA-256 of the official Maia-2 checkpoints
  and refuses to load any other file (`cca.engines.maia2_human.PINNED_SHA256`).
* Dependency advisories: `scripts/audit_deps.py` audits every locked pin (all platforms,
  extras and groups). Accepted advisories are listed there with a reason tied to how CCA uses
  the package and an expiry date after which the audit fails until they are re-assessed.
* Fair play: running CCA to assist a human in rated games is cheating on every chess site.
  Online play must use an account flagged as a bot (e.g. a Lichess BOT account).
