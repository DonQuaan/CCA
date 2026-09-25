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
  execute code; only load weights from the official Maia-2 source.
* Fair play: running CCA to assist a human in rated games is cheating on every chess site.
  Online play must use an account flagged as a bot (e.g. a Lichess BOT account).
