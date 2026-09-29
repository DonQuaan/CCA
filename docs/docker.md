# Container images

CCA publishes two container images on GitHub Packages, built from the repository's
[`Dockerfile`](../Dockerfile) by [`.github/workflows/image.yml`](../.github/workflows/image.yml).
Both contain CCA and Stockfish 19, so nothing but Docker needs to be installed. By default they
start the web simulator ([simulator.md](simulator.md)), and they can also run CCA as a UCI
engine. Both images are for linux/amd64 only. The reasons behind their design, including the
decision to bundle Stockfish, are recorded in
[ADR-0007](adr/0007-simulator-and-distribution.md).

| Image and tags | Dockerfile target | Contents |
|---|---|---|
| `ghcr.io/donquaan/cca:0.2.0`, `ghcr.io/donquaan/cca:latest` | `runtime` | CCA, Stockfish 19 and the QRE human model (derived from the engine). |
| `ghcr.io/donquaan/cca:0.2.0-maia2`, `ghcr.io/donquaan/cca:latest-maia2` | `runtime-maia2` | The same, plus CPU-only PyTorch 2.8.0 and maia2 0.11 for the Maia-2 human model. The Maia-2 weights are **not** included; they are downloaded on first use. |

**Tag rules:**

- Every release `vX.Y.Z` pushes `X.Y.Z` and `X.Y.Z-maia2`.
- `latest` and `latest-maia2` move with each final release. Pre-releases (`a`, `b`, `rc`),
  dev-releases (`dev`) and post-releases (`post`) never update them.
- Pin a version, or better a digest, when you need a fixed setup.

Contents: [Quick start](#quick-start) · [What is in the images](#what-is-in-the-images) ·
[Configuration](#configuration) · [Licences and GPL source](#licences-and-gpl-source) ·
[Verifying provenance](#verifying-provenance) · [Building locally](#building-locally) ·
[Smoke test](#smoke-test) · [Known limits](#known-limits)

## Quick start

### Web simulator

```bash
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/donquaan/cca:0.2.0
```

Then open <http://127.0.0.1:8765/> or <http://localhost:8765/>.

- The image's command is
  `cca play --host 0.0.0.0 --port 8765 --no-browser --human qre`. Inside the container the
  server has to listen on all interfaces, so its log shows the warning that it may be
  reachable from other machines. That warning is expected.
- `-p 127.0.0.1:8765:8765` publishes the port on your machine's loopback address only. Keep it
  that way: `cca play` has no authentication (see
  [the security model](simulator.md#security-model)).
- The engines warm up in the background. The page says "Warming up the engines..." until they
  are ready, and the container reports `healthy` once they are.
- `docker stop` sends SIGTERM. tini, the image's init process, forwards it, and `cca play` stops
  cleanly together with Stockfish.

To change the simulator's settings, give the whole command after the image name. It replaces
the default command, so repeat `--host 0.0.0.0 --port 8765 --no-browser`:

```bash
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/donquaan/cca:0.2.0 \
  play --host 0.0.0.0 --port 8765 --no-browser --human qre --persona tal --elo-self 2000
```

Every `cca play` flag is listed in [simulator.md](simulator.md#flags).

### UCI engine

```bash
docker run -i --rm --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

- `-i` keeps standard input open, and the GUI speaks UCI over it.
- **Never add `-t`.** With piped input Docker refuses to start and prints "cannot attach stdin to
  a TTY-enabled container because stdin is not a terminal".
- `--no-healthcheck`: the image's health check probes the web server, which `uci` does not
  start.
- The default image has no Maia-2. With the default `CCA_HumanModel` (`maia2`), CCA falls back
  to `qre`. Set `CCA_HumanModel` to `qre` to skip the attempt.

A quick check that the engine answers:

```bash
printf 'uci\nisready\nquit\n' | docker run -i --rm --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

It prints `id name CCA 0.2.0`, `id author`, the option list and `uciok`. In the default image
an `info string maia2 unavailable (...); falling back to QRE human model` line then comes
before `readyok`. Its hint to run `uv sync --extra maia2` does not apply inside the image: use
the `-maia2` image, or set `CCA_HumanModel` to `qre`. The UCI options are listed in
[reference.md](reference.md#4-uci-options).

**From a chess GUI.** A GUI starts one program for an engine, and some GUIs cannot pass it any
arguments. Put the command in a wrapper script and register the script as the engine. The
wrapper does not forward arguments: `cca uci` accepts none, so arguments a GUI appends would
make it exit. Pull the image once beforehand and use `--pull=never`, so that the GUI's
handshake never waits for a download.

```sh
#!/bin/sh
exec docker run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

```bat
@echo off
docker run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

These wrappers have **not** been tested end to end with a GUI. Docker must be running, and
`docker` must be on the GUI's `PATH`. A UCI `quit`, or the end of input, ends `cca uci` and
with it the container. If a GUI kills the `docker` process instead, the container may keep running; check
with `docker ps`. Without Docker, the installed package provides the launcher `cca-uci`,
which needs no arguments. Setup steps for individual GUIs and for lichess-bot are in
[gui-integration.md](gui-integration.md).

### Maia-2 variant

```bash
docker run --rm -p 127.0.0.1:8765:8765 -v cca-data:/data ghcr.io/donquaan/cca:0.2.0-maia2
```

- **First start:**
  - `maia2` downloads the `rapid` checkpoint, about 280 MB (267 MiB), from its official source into
    `/data/maia2`. Its SHA-256 is checked against the pin in
    [reference.md](reference.md#22-maia-2-checkpoints).
  - Meanwhile the page shows "Warming up the engines...". This is why this image's health check
    allows a 600 s start period.
  - If Maia-2 cannot be loaded, `cca play` falls back to QRE, shows the reason on the page and
    still becomes ready.
- **The volume.** The named volume `cca-data` keeps the weights for later containers.
  - A new named volume is filled from the image's `/data`, which belongs to the image's user,
    uid 10001.
  - Without `-v`, Docker creates an anonymous volume for `/data`, and `--rm` removes it when the
    container exits. The next run then downloads the weights again.
- **CPU only.** The image contains the CPU build of PyTorch, and its command passes
  `--device cpu`. It cannot use a GPU.
- **UCI with Maia-2:**

  ```bash
  docker run -i --rm --no-healthcheck -v cca-data:/data ghcr.io/donquaan/cca:0.2.0-maia2 uci
  ```

  `CCA_HumanModel` defaults to `maia2` here. The default `CCA_Device` (`gpu`) uses the CPU when
  CUDA is not available, which is always the case in this image.
- **Weights you already have.** Mount their folder read-only on `/data/maia2`, as
  `docker/smoke.py` does:

  ```bash
  docker run --rm -p 127.0.0.1:8765:8765 \
    --mount type=bind,source=/path/to/weights,target=/data/maia2,readonly \
    ghcr.io/donquaan/cca:0.2.0-maia2
  ```

  - The folder must hold `rapid_model.pt` (or `blitz_model.pt` for `--maia2-type blitz`) with
    the pinned SHA-256, readable by uid 10001.
  - A file with any other hash is refused.
  - No licence is published for the Maia-2 weights: do not rehost them (see
    [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)).

### Other commands

The entry point is `cca`, so any subcommand works. Start commands that do not run the web
server with `--no-healthcheck`:

```bash
docker run --rm --no-healthcheck ghcr.io/donquaan/cca:0.2.0 version
docker run --rm --no-healthcheck ghcr.io/donquaan/cca:0.2.0 doctor
docker run --rm --no-healthcheck ghcr.io/donquaan/cca:0.2.0 \
  analyse "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1" --human qre
```

`doctor` reports `engine ok: Stockfish 19 (/usr/local/bin/stockfish)`.

## What is in the images

Both images are built on `python:3.12-slim`, pinned by digest. They contain no pip, uv,
compiler or git.

| Path | Content |
|---|---|
| `/usr/local/bin/stockfish` | Stockfish 19: the official `sf_19` Linux x86-64 "universal" binary, from the release archive pinned by SHA-256 in [`engines/stockfish.lock.json`](../engines/stockfish.lock.json). |
| `/usr/local/share/doc/stockfish/` | Stockfish's licence, complete source and NNUE network (see [Licences and GPL source](#licences-and-gpl-source)). |
| `/usr/bin/tini` | tini 0.19.0, the init process (PID 1): it forwards signals and reaps child processes. Upstream release binary, pinned by SHA-256. |
| `/usr/local/share/doc/tini/LICENSE` | tini's MIT licence. |
| `/usr/local/share/doc/cca/` | CCA's `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md`. |
| `/opt/cca/` | A Python virtual environment without pip: the `cca-chess` wheel and its locked dependencies (python-chess), installed with hash checking and precompiled to bytecode. In the `-maia2` image it also holds CPU-only torch 2.8.0, maia2 0.11 and their locked dependencies, with no CUDA packages. |
| `/data/` (`-maia2` only) | A volume; the Maia-2 weights go to `/data/maia2`. |
| `/home/cca` | Home and working directory of the user `cca`. |

The image configuration is the same for both, except where noted:

| Setting | Value |
|---|---|
| Entry point | `/usr/bin/tini -- cca` |
| Command | `play --host 0.0.0.0 --port 8765 --no-browser --human qre`. The `-maia2` image uses `play --host 0.0.0.0 --port 8765 --no-browser --human maia2 --device cpu`. |
| User | `10001:10001` (`cca`, login shell `/usr/sbin/nologin`) |
| Port | `8765/tcp` (`EXPOSE`) |
| Labels | `org.opencontainers.image.title`, `description`, `source` and `url` (both `https://github.com/DonQuaan/CCA`), `licenses` (`Apache-2.0 AND GPL-3.0-or-later`), `version`, `revision` (the commit). |

These sizes are approximate, uncompressed, and were measured on a pre-release build of the
Dockerfile; the published images may differ:

- default image: about 341 MB, of which Stockfish's binary is about 103 MB and its source,
  documents and network about 100 MB;
- `-maia2` image: about 1.38 GB.

The compressed download sizes were not measured.

## Configuration

### Ports

The simulator listens on port 8765 inside the container. Publish it on the loopback address,
for example `-p 127.0.0.1:8765:8765`, or `-p 127.0.0.1:9000:8765` for another host port.

The simulator's DNS-rebinding guard stays on inside the container. It answers requests whose
`Host` is `localhost`, an IP address, or the container's own host name. Any other name gets
**421**: a Compose service name such as `http://cca:8765`, or the public name of a reverse
proxy. A proxy must forward `Host` as `localhost` or an IP address.

### Environment variables

| Variable | Value in the image | Effect |
|---|---|---|
| `CCA_STOCKFISH` | `/usr/local/bin/stockfish` | The Stockfish binary CCA uses. If you override it, the path must exist inside the container; CCA does not fall back to another binary. |
| `CCA_WEIGHTS` | `/data/maia2` (`-maia2` only) | Folder of the Maia-2 checkpoints. |
| `PATH` | `/opt/cca/bin:$PATH` | Puts the virtual environment first. |
| `PYTHONDONTWRITEBYTECODE` | `1` | No `.pyc` files are written at run time. |
| `PYTHONUNBUFFERED` | `1` | Log lines appear immediately. |

`CCA_STOCKFISH` and `CCA_WEIGHTS` are the only environment variables CCA itself reads (see
[reference.md](reference.md#11-environment-variables)).

### Health check

The health check runs Python's standard library inside the container and requests
`http://127.0.0.1:8765/healthz`. It ignores proxy variables. The container is healthy when the
answer is HTTP 200 with `"status": "ok"` and `"ready": true`, that is, when the engines are
built and a game can start. After a failed engine start, `/healthz` answers 503 and the
container turns unhealthy.

| Image | Interval | Timeout | Start period | Start interval | Retries |
|---|---|---|---|---|---|
| default | 30 s | 5 s | 30 s | 2 s | 3 |
| `-maia2` | 30 s | 5 s | 600 s | 5 s | 3 |

```bash
docker inspect --format '{{.State.Health.Status}}' <container>
```

Start containers that do not run the web server (`uci`, `analyse`, `doctor`, `version`) with
`--no-healthcheck`.

### User and files

The images run as the unprivileged user `cca` (uid and gid 10001), never as root. A directory
you bind-mount must be readable by uid 10001, and writable too if CCA should write into it. The
`/data` volume of the `-maia2` image is created with that owner.

## Licences and GPL source

The images combine several licences: CCA (Apache-2.0), Stockfish 19 (GPL-3.0-or-later),
python-chess (GPL-3.0-or-later), tini (MIT), and in the `-maia2` image Maia-2's code (MIT) and
PyTorch (BSD-3-Clause), on top of the Debian packages and CPython of the base image.
[NOTICE](../NOTICE) and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) state what this
means for redistributors; they are an engineering summary, not legal advice. The same files are
in the image under `/usr/local/share/doc/cca/`.

Stockfish's licence and the corresponding source travel with the binary, in
`/usr/local/share/doc/stockfish/`:

- everything in the official `sf_19` release archive except the binary itself, including
  `Copying.txt` (the GPL-3.0 text), `AUTHORS`, `README.md`, the complete `src/` directory with
  its `Makefile`, and `scripts/`;
- the NNUE network, which exists only inside the binary. The build writes it out with
  Stockfish's `export_net` command as `nn-1a298aa575a0.nnue`, and checks that the name matches
  the first 12 hex digits of the file's SHA-256.

The smoke test fails if `Copying.txt`, `AUTHORS`, `src/Makefile`, `src/evaluate.h` or tini's
licence is missing, or if the NNUE file is absent. python-chess's licence is in its
`*.dist-info` directory under `/opt/cca/lib/python3.12/site-packages/`.

To copy the Stockfish documents and source out of the image:

```bash
docker run --rm --no-healthcheck --entrypoint ls ghcr.io/donquaan/cca:0.2.0 /usr/local/share/doc/stockfish
id=$(docker create ghcr.io/donquaan/cca:0.2.0)
docker cp "$id":/usr/local/share/doc/stockfish ./stockfish-doc
docker rm "$id"
```

## Verifying provenance

Images are pushed only by the release workflow
([`.github/workflows/release.yml`](../.github/workflows/release.yml)), for an annotated `v*`
tag on `main`, after the full CI and the release gate pass. For each variant, the publish job
in `image.yml` runs these steps in order:

1. Build without cache.
2. Run the smoke test on the build.
3. Push it.
4. Pull the pushed digest back and run the smoke test on it.
5. Sign a build-provenance attestation for that digest with `actions/attest`, and push the
   attestation to the registry.

Check an image with the GitHub CLI:

```bash
gh attestation verify oci://ghcr.io/donquaan/cca:0.2.0 -R DonQuaan/CCA
gh attestation verify oci://ghcr.io/donquaan/cca:0.2.0-maia2 -R DonQuaan/CCA
```

The command succeeds only if the image's digest carries an attestation signed for a GitHub
Actions run in the repository `DonQuaan/CCA`.

The labels name the version and the commit:

```bash
docker inspect --format '{{json .Config.Labels}}' ghcr.io/donquaan/cca:0.2.0
```

To pin an image by digest, pull `ghcr.io/donquaan/cca@sha256:<digest>`.

## Building locally

You need Docker with BuildKit (`docker buildx`) and network access. The build downloads:

- the base images, pinned by digest;
- Stockfish and tini from GitHub, pinned by SHA-256;
- the Python packages from PyPI, with hash checking;
- for the `-maia2` target, the CPU torch wheel from download-r2.pytorch.org (listed on the
  download.pytorch.org index), pinned by SHA-256.

From the repository root, as in the Dockerfile's header:

```bash
docker buildx build --target runtime --build-arg CCA_VERSION=0.2.0 -t cca:local .
docker buildx build --target runtime-maia2 --build-arg CCA_VERSION=0.2.0 -t cca:maia2 .
```

If your buildx builder uses the `docker-container` driver, add `--load` so that the image
lands in your local image store.

| Build argument | Required | Meaning |
|---|---|---|
| `CCA_VERSION` | yes | Must equal `__version__` in `src/cca/__init__.py`. The build stops if it is missing or does not match the wheel it builds. It becomes the `version` label. |
| `VCS_REF` | no | Becomes the `org.opencontainers.image.revision` label, for example `--build-arg VCS_REF=$(git rev-parse HEAD)`. |

**Stages:**

| Stage | What it does |
|---|---|
| `uv`, `base` | The pinned uv image and the pinned `python:3.12-slim` base. |
| `stockfish` | Downloads the pinned Stockfish release, exports its network and checks the binary. |
| `tini` | Downloads the pinned tini binary and its licence. |
| `build` | Builds the wheel and the locked requirement sets. |
| `venv`, `venv-maia2` | Install the virtual environments. |
| `runtime`, `runtime-maia2` | The two images. |

The build context is an allowlist ([`.dockerignore`](../.dockerignore)). `.git`, virtual
environments, `weights/`, the downloaded engines, `runs/` and private files never enter it;
only the files the Dockerfile copies do.

**The build stops when:**

- the Stockfish archive's SHA-256 differs from the lock;
- `export_net` fails, or the exported network's name does not match its hash;
- the binary does not identify as `Stockfish 19`, or does not answer `readyok`;
- tini's hash or version is wrong;
- the build backend (hash-pinned in `docker/build-constraints.txt`) does not match its hashes;
- `CCA_VERSION` does not match the wheel;
- the wheel leaves out any file under `src/cca` (checked by `docker/check_dist.py`);
- `uv.lock` is stale for `pyproject.toml` (`uv export --locked`);
- `docker/torch-cpu.txt` pins a torch version other than the one in `uv.lock`;
- a CUDA package is left in the `-maia2` requirement set;
- `uv pip check` fails, or torch in the `-maia2` environment is not the CPU build.

Only linux/amd64 is supported: the Stockfish asset, tini and the torch wheel are x86-64 pins.

## Smoke test

[`docker/smoke.py`](../docker/smoke.py) runs the checks CI runs before and after pushing. It
needs only Python 3.11 or later and the `docker` CLI, and is run from the repository root:

```bash
python docker/smoke.py cca:local --expect-version 0.2.0
python docker/smoke.py cca:maia2 --variant maia2 --expect-version 0.2.0
python docker/smoke.py cca:maia2 --variant maia2 --maia2-weights weights/maia2
python docker/smoke.py ghcr.io/donquaan/cca:0.2.0
```

| Option | Meaning |
|---|---|
| `IMAGE` | Image reference, for example `cca:local` or `ghcr.io/donquaan/cca@sha256:...`. |
| `--variant {default,maia2}` | Which image's configuration to expect (default: `default`). |
| `--expect-version V` | Required `org.opencontainers.image.version` label. |
| `--expect-revision SHA` | Required `org.opencontainers.image.revision` label. |
| `--port N` | Host port on 127.0.0.1 for the web check (default: a free port Docker picks). |
| `--health-timeout S` | Seconds to wait for `healthy` (default 240). |
| `--maia2-weights DIR` | A folder with Maia-2 checkpoints, mounted read-only on `/data/maia2` (`maia2` variant only). |

| Check | What it verifies |
|---|---|
| config | linux/amd64; user, entry point, command, port, environment, the exec-form health check on `/healthz`, the labels, and a start period of at least 300 s for `-maia2`. |
| version | `cca version` prints the version the image is labelled with. |
| doctor | `cca doctor` starts the bundled Stockfish 19. |
| filesystem | Inside the image: uid and gid 10001; the licence and source files are readable; the NNUE file is present; the notices name the image; no build tools and no pip; no CUDA packages. For `-maia2` it also checks that maia2 is installed, that torch is the CPU build, and (without `--maia2-weights`) that `/data/maia2` is writable. For the default image, that neither torch nor maia2 is installed. |
| uci | `cca uci` answers `uciok` and `readyok`. |
| analyse | `cca analyse` makes one decision from the start position and returns a legal-looking move from its policy. |
| web | The container turns healthy and `/healthz` answers 200 `ok` and ready. The page then loads the way a browser loads it: every script, module, stylesheet, image and link it references answers 200 with a usable MIME type. Every vendored file is also served with the SHA-256 listed in `MANIFEST.json`. |

- Every check runs even after an earlier one failed. The exit status is 1 if any failed.
- Every container the test starts is removed together with its anonymous volumes.
- Without `--maia2-weights`, the `maia2` variant runs its UCI, analyse and web checks with the
  QRE model. CI therefore never downloads the checkpoint; torch and maia2 are still imported
  and inspected.
- With `--maia2-weights`, the image's own Maia-2 command line runs.
- On Windows, some ports cannot be used:
  `netsh interface ipv4 show excludedportrange protocol=tcp` lists them. Omitting `--port`
  lets Docker choose a usable one.

The smoke runner's own tests run with `uv run pytest docker/tests`.

## Known limits

- **linux/amd64 only.** There is no arm64 image.
- **Maia-2 runs on the CPU only** in the `-maia2` image.
- **The first Maia-2 start downloads about 280 MB (267 MiB)** from the official source (the `maia2`
  package fetches it from Google Drive). Keep `/data` on a named volume so that this happens
  once.
- **Package visibility is set by the owner.** GitHub Packages makes a package private when it
  is first published, and the repository owner decides when it becomes public. Until then,
  anonymous `docker pull` is refused with `denied`.
- **No authentication in `cca play`.** Publish its port on `127.0.0.1` only; see
  [the security model](simulator.md#security-model).
- **Games are kept in memory.** Restarting or replacing the container loses them.
- **The health check uses `--start-interval`.** The Dockerfile reference lists that option as
  requiring Docker Engine 25.0 or later. Behaviour on older engines has not been tested.
- **GUI wrappers are untested.** Running the UCI image from a chess GUI through a wrapper, and
  what happens when a GUI force-kills `docker`, have not been tested end to end.
