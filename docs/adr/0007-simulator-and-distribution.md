# ADR-0007 — Browser simulator and distribution (images, UCI launcher)

* Status: accepted · 2026-09-28 · amends [ADR-0002](0002-license-apache-2.md) (the container
  images bundle Stockfish)

## Context
v0.1.0 adds three ways to use CCA besides `cca uci` in a chess GUI: a browser simulator
(`cca play`), container images on ghcr.io and a launcher for GUIs that start an engine without
arguments. They raise licence, security, reproducibility and protocol questions that ADR-0001
to ADR-0006 do not answer. ADR-0002 says Stockfish is downloaded and never bundled by the
project; the images bundle it, so this record amends that point (ADR-0006: accepted ADRs are
changed only by a new ADR). Every parameter named here is listed in
[`docs/reference.md`](../reference.md); this record keeps the reasons.

## Decisions

### 1. Standard-library server; vendored MIT/BSD browser code only
* `cca play` is served by `http.server.ThreadingHTTPServer`: no web framework, no new Python
  dependency, no front-end build. The page is plain ES modules and CSS served from the package.
* The browser libraries are vendored, not loaded from a CDN, so the page requests nothing from
  other hosts (checked in every browser run of the release work): cm-chessboard 8.14.2 (MIT),
  chess.js 1.4.0 (BSD-2-Clause) and a Cburnett piece sprite rebuilt from the Wikimedia Commons
  originals, which are multi-licensed GFDL/BSD/GPL and used under the BSD-3-Clause option.
  `src/cca/play/static/vendor/MANIFEST.json` records the source, size and SHA-256 of every
  vendored file; tests re-hash them and scan the vendored JavaScript for network, `eval`-like
  and storage APIs.
* Rejected:
  * chessground (lichess) and chessops / lichess-pgn-viewer: GPL-3.0-or-later. Vendoring them
    would put copyleft code into CCA's Apache-2.0 source tree, which ADR-0002 rules out.
  * cm-chessboard's own sprites: `assets/pieces/standard.svg` (CC BY-SA 3.0),
    `assets/pieces/staunty.svg` (CC BY-NC-SA 4.0) and `assets/extensions/markers/markers.svg`
    (CC BY-SA 3.0 by its own header). CCA draws its own `img/markers.svg`.
  * lichess's copy of the Cburnett set, which lila's `COPYING.md` lists as GPLv2+.
* The insight panel keeps the [ADR-0005](0005-honest-science-labelling.md) labels in English
  and Vietnamese: stress, drive, opponent stress and chaos are "virtual, latent control signals
  inspired by, not models of, physiology"; tests pin the wording.

### 2. Content-Security-Policy with one exception: style attributes
Every response carries:

```text
default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:;
style-src 'self'; style-src-attr 'unsafe-inline'; object-src 'none'; base-uri 'none';
form-action 'none'; frame-ancestors 'none'
```

* `style-src-attr 'unsafe-inline'` is needed because cm-chessboard positions a dragged piece
  through its `style` attribute and the piece sprite uses `style=""` attributes. Measured in
  Chromium during implementation: with `style-src 'self'` alone the page logged 67 CSP errors,
  21 of the sprite's 51 elements were drawn with the wrong fill, and the board coordinates
  shrank from 17.5 px to 7 px.
* The exception covers style attributes only. `<style>` elements, inline scripts and `eval` stay
  blocked; `'unsafe-inline'` for all styles was rejected.
* The measurement was not repeated by the independent verification. Firefox and Safari were not
  tested: a browser without `style-src-attr` support would apply `style-src 'self'` to the
  attributes and show the defects above (expected from the CSP fallback rules, not observed).

### 3. Loopback by default; Host and Origin guards on every bind
* `cca play` binds `127.0.0.1:8765` unless told otherwise. Binding any other address prints a
  warning: the API has no authentication, so anyone who reaches the port can play and use the
  CPU.
* Every request's `Host` header is checked, on every bind (421 otherwise). A loopback bind
  answers only `localhost`, `127.0.0.1`, `[::1]` and its own address; any other bind (0.0.0.0 in
  a container, a LAN address) also answers IP literals and the machine's host name. A
  DNS-rebinding page always sends a name its author controls, while an IP-literal `Host` means
  the browser really connected to that address.
* A POST with a foreign `Origin` or `Sec-Fetch-Site: cross-site` gets 403; a content type other
  than `application/json` gets 415, so no HTML form or other "simple" cross-site request reaches
  the API; a body above 64 KiB gets 413. Static files come only from the package's `static`
  directory, through strict path segments and a whitelist of extensions with explicit MIME
  types. Errors are JSON and never echo input into markup.
* Consequences: a reverse proxy must forward `Host` as `localhost` or an IP address (a compose
  service name gets 421); in Docker the server binds 0.0.0.0 inside the container, so the port is
  published on the host's loopback only (`-p 127.0.0.1:8765:8765`), and the `HEALTHCHECK` probes
  `127.0.0.1`.

### 4. One shared engine, a decision lock, `ucinewgame` before every decision
* The engines are built once, in a background thread, so `/healthz` answers at once
  (`"ready": false` until they are built). One Stockfish process and one human model serve
  every game and the position lab; each game owns its `CAIMEAgent` (latent state, chaos,
  think-time memory). Sessions stay in memory, at most `--max-sessions` (default 16), least
  recently used first out.
* A global lock serialises every engine call (decisions, lab analyses, game creation):
  python-chess cancels a running command when another one starts on the same engine, so without
  the lock concurrent games would cut each other's searches short. CCA's clock and deadline are
  read inside the lock, so waiting for another game does not shrink the deadline (the wait is
  still charged to CCA's clock); the page blocks moves while the lab runs.
* Each decision starts with `ucinewgame`: with a shared hash table, a decision would otherwise
  depend on whatever other games and the lab searched before it. Measured cost: about 25 ms per
  decision (25–35 ms against 1 ms without, 256 MB hash, 1 thread). Checked live with the QRE
  model: the same seed, played alone and interleaved with another game's decision and a lab
  analysis, gave identical decisions. The replay claim is limited to untimed games with a
  deterministic engine search and human model; Maia-2's determinism was not checked, and timed
  games also depend on the clock.

### 5. The images are a combined GPL distribution and carry the corresponding source
* The images hold CCA (Apache-2.0) together with python-chess (GPL-3.0-or-later, imported
  in-process) and the Stockfish 19 binary (GPL-3.0-or-later). ADR-0002 already allows such a
  bundle under GPL-3.0 terms, so the images are labelled
  `org.opencontainers.image.licenses="Apache-2.0 AND GPL-3.0-or-later"` and the source travels
  in the same image as the binaries:
  * `/usr/local/share/doc/stockfish/`: the official `sf_19` archive's `Copying.txt`, `AUTHORS`,
    `README.md`, complete `src/` (with its `Makefile`) and `scripts/`, plus the NNUE network,
    which exists only inside the binary and is written out with `export_net` (the build fails
    unless the file name equals the first 12 hex digits of its SHA-256);
  * python-chess is installed from its SHA-256-pinned PyPI sdist; it is pure Python, so the
    installed modules are its source.
* tini (MIT) and, in the `-maia2` image, Maia-2's code (MIT) and CPU-only PyTorch (BSD-3-Clause)
  keep their licence files. CCA's `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` are in
  `/usr/local/share/doc/cca/`, and the smoke test fails if the notices do not name the image.
* Amendment of ADR-0002: "downloaded, never committed or bundled" still holds for the repository
  and the Python packages, not for the images.
* Supply chain: base images pinned by digest (Dependabot's `docker` ecosystem bumps them);
  Stockfish checked against `engines/stockfish.lock.json`; tini and its licence, and the torch
  CPU wheel, pinned by SHA-256; requirements exported from `uv.lock` with `--locked` and
  installed with `--require-hashes`; hash-pinned build backend; no pip, uv or compiler at run
  time; non-root user (uid 10001). linux/amd64 only, because the Stockfish asset, tini and the
  torch wheel are x86-64 pins. BuildKit's own provenance is off; each pushed digest gets one
  signed build-provenance attestation from `actions/attest` (no storage record: those need an
  organisation-owned repository).

### 6. Maia-2 weights are never bundled
* No licence is published for the Maia-2 checkpoints and third-party weights are never rehosted
  (ADR-0002, `THIRD_PARTY_NOTICES.md`), so no image contains them; `.dockerignore` is an
  allowlist, so `weights/` never enters a build context.
* The `-maia2` image sets `CCA_WEIGHTS=/data/maia2` and declares `/data` a volume. On first use
  `maia2` downloads the checkpoint from its official source and checks its SHA-256; a checkpoint
  already on the volume is checked against CCA's pinned SHA-256 before torch loads it. The
  health check allows a 600 s start period for that first download. CI never downloads it: the
  smoke test runs the `-maia2` image's UCI, analyse and web checks with `--human qre` unless a
  weights directory is mounted read-only.
* The default image has no Maia-2, so its command passes `--human qre` (the CLI default,
  `maia2`, would only fall back to QRE with a warning in the page).

### 7. Actions pinned to releases that run on Node 24
* GitHub's changelog
  ([2025-09-19](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/))
  made Node 24 the runner default from 2026-06-16 and removed Node 20 on 2026-09-23. The five
  actions pinned before this release all declared `runs.using: node20`.
* Every action is now pinned to the full commit SHA of a release whose `action.yml` declares
  `node24` (read at each SHA), with the exact version in a comment; Dependabot bumps them.
* Behaviour changes accepted with the major bumps: `download-artifact` v8 fails on a digest
  mismatch; `setup-uv` v10's `enable-cache: auto` turns the uv cache off for release and
  tag-push runs; the READMEs of `checkout` v7 and `download-artifact` v8 require Actions Runner
  2.327.1 or later.

### 8. `cca-uci` for GUIs that start an engine without arguments
* `cca` without a subcommand exits 2 with a usage error, and several hosts cannot pass one:
  Cute Chess's engine dialog and saved configuration have no arguments field (v1.5.1 source), En
  Croissant starts the selected binary at once to read its options and its Windows picker shows
  only `.exe` files, ChessBase/Fritz are reported by third parties to accept no engine
  parameters, and lichess-bot passes only `--key=value` options.
* Decision: a second console script, `cca-uci = cca.uci.protocol:console_main`, which is exactly
  `cca uci` (same UTF-8 stream set-up and exit code). pip and uv generate a real launcher for it
  (`cca-uci.exe` on Windows). It ignores its command-line arguments, so lichess-bot's appended
  options do no harm.
* Rejected as the main route: `.bat`/`.sh` wrappers (not selectable in En Croissant's Windows
  picker; Microsoft documents that a batch file needs the command interpreter, so starting one
  directly through `CreateProcess` works only as observed behaviour) and lichess-bot's
  `interpreter` trick. A wrapper stays necessary only where the launcher
  cannot help, such as a GUI that runs the engine in Docker
  (`docker run -i --rm --no-healthcheck ghcr.io/donquaan/cca:0.1.0 uci`, with `-i` and no `-t`).

### 9. `UCI_LimitStrength` defaults to `true`
* The UCI protocol description says this option should default to false; CCA advertises
  `default true` (Stockfish 19 advertises false).
* In Stockfish, `UCI_Elo` limits strength and applies only when `UCI_LimitStrength` is on. In
  CCA, `UCI_Elo` is the rating of the human player the agent imitates: the anchor of the human
  prior in every decision ([ADR-0003](0003-stockfish-as-perception-not-policy.md)), and it was
  always in force before this option existed. A false default would have changed the default
  playing behaviour, a MINOR change under [`docs/versioning.md`](../versioning.md) rule 4 that
  invalidates earlier benchmark numbers, and a user who sets only `UCI_Elo` would see it ignored.
* `false` only sets the imitated rating to 2600, the top of the `UCI_Elo` range. It does not make
  CCA play Stockfish's moves: the decision core still anchors on the human prior. The option's
  help text and `docs/reference.md` state this.

## Consequences
* `cca play` is a local HTTP service without authentication; binds other than loopback are for
  trusted networks only. Its reproducibility costs about 25 ms per decision, and one server makes
  one decision at a time for all its games.
* Obtained as an image, CCA is a GPL-3.0 combined distribution; the repository and the Python
  packages stay Apache-2.0 with python-chess as a dependency.
* If Docker is unavailable locally, the first `Image` workflow run on GitHub is the first build
  and smoke test of the images ([`docs/versioning.md`](../versioning.md), checklist step 5); for
  v0.1.0 that run is the images' acceptance test.
* Not verified in the release work: the legal adequacy of the image notices (the Debian base
  image's GPL and LGPL packages; the licence label names neither tini, PyTorch nor the base
  image), left to the owner's licence review; that every later Commons revision of the Cburnett
  pieces carries the BSD option (Commons policy suggests it); `style-src-attr` in Firefox and
  Safari; screen readers; any real GUI or lichess-bot run (the launcher was tested through
  python-chess 1.11.2, lichess-bot's engine layer, with Stockfish 19, on Windows only); the
  first Maia-2 download inside a container.
