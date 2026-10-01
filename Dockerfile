# CCA container images, published as ghcr.io/donquaan/cca by .github/workflows/image.yml.
#
#   target runtime        tags <version>, latest              CCA + Stockfish 19 + QRE human model
#   target runtime-maia2  tags <version>-maia2, latest-maia2  + CPU-only torch 2.8.0 + maia2 0.11
#
# Build (BuildKit, linux/amd64 only: the Stockfish asset, tini and the torch wheel are x86-64 pins):
#   docker buildx build --target runtime --build-arg CCA_VERSION=<version> -t cca:local .
#   docker buildx build --target runtime-maia2 --build-arg CCA_VERSION=<version> -t cca:maia2 .
# CCA_VERSION must equal __version__ in src/cca/__init__.py (the build stops otherwise); the
# optional VCS_REF becomes the org.opencontainers.image.revision label.
# Smoke test: python docker/smoke.py cca:local [--variant maia2]
#
# Run:
#   web simulator  docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/donquaan/cca:<version>
#   UCI engine     docker run -i --rm --no-healthcheck ghcr.io/donquaan/cca:<version> uci
#   Maia-2         docker run --rm -p 127.0.0.1:8765:8765 -v cca-data:/data ghcr.io/donquaan/cca:<version>-maia2
#
# No "# syntax=" line on purpose: the builder's bundled Dockerfile frontend is used, so no
# unpinned frontend image is pulled. Base images are pinned by digest and written literally in
# FROM so that tooling (e.g. Dependabot's docker ecosystem) can bump them.

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# ------------------------------------------------------------------ Stockfish 19 (GPL-3.0-or-later)
FROM base AS stockfish
WORKDIR /src
COPY scripts/fetch_stockfish.py scripts/
COPY engines/stockfish.lock.json engines/
# Aborts (and deletes the download) unless its SHA-256 equals the entry in the committed lock.
RUN python scripts/fetch_stockfish.py --os linux --arch x86-64
# Licence and complete corresponding source travel with the binary: the official archive holds
# Copying.txt, AUTHORS, src/ and scripts/. The NNUE network exists only inside the binary, so
# export_net writes it out; Stockfish names a net after the first 12 hex digits of its SHA-256.
RUN d=engines/stockfish-linux-x86-64-universal/stockfish; \
    install -D -m 0755 "$d/stockfish-linux-x86-64-universal" /out/bin/stockfish; \
    mkdir -p /out/doc; \
    cp -R "$d/." /out/doc/; \
    rm /out/doc/stockfish-linux-x86-64-universal; \
    cd /out/doc; \
    log=$(printf 'export_net\nquit\n' | /out/bin/stockfish); \
    grep -q '^Network saved successfully' <<<"$log"; \
    shopt -s nullglob; \
    nets=(nn-*.nnue); \
    test "${#nets[@]}" -ge 1; \
    for f in "${nets[@]}"; do test "nn-$(sha256sum "$f" | cut -c1-12).nnue" = "$f"; done; \
    chmod -R u=rwX,go=rX /out; \
    out=$(printf 'uci\nisready\nquit\n' | /out/bin/stockfish); \
    grep -qx 'id name Stockfish 19' <<<"$out"; \
    grep -qx 'readyok' <<<"$out"

# ------------------------------------------------------------------ tini 0.19.0 (MIT)
# The upstream release binary (linked against glibc, as Debian's tini is), pinned by SHA-256: the
# runtime stage needs no apt index, and a Debian rebuild of the package cannot break the build.
# tini-amd64's pin equals the release asset tini-amd64.sha256sum, and the binary's detached
# signature (tini-amd64.asc) was verified against the key named in tini's README,
# 595E85A6B1B4779EA4DAAEC70B588DFF0527A9B7. The release has no LICENSE asset: LICENSE's pin is the
# SHA-256 of LICENSE at tag v0.19.0. Both pins were checked on 2026-09-28.
FROM base AS tini
WORKDIR /src
COPY docker/fetch_pinned.py docker/
RUN fetch=(python docker/fetch_pinned.py); \
    "${fetch[@]}" --mode 755 https://github.com/krallin/tini/releases/download/v0.19.0/tini-amd64 \
      93dcc18adc78c65a028a84799ecf8ad40c936fdfc5f2a57b1acda5a8117fa82c /out/bin/tini; \
    "${fetch[@]}" https://raw.githubusercontent.com/krallin/tini/v0.19.0/LICENSE \
      e5f46bca81266bdd511cf08018d66866870531794569c04f9b45f50dd23c28b0 /out/doc/LICENSE; \
    test "$(/out/bin/tini --version)" = "tini version 0.19.0 - git.de40ad0"

# ------------------------------------------------------------------ wheel + locked requirement sets
FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE .gitignore ./
COPY docker/build-constraints.txt docker/torch-cpu.txt docker/check_dist.py docker/
COPY src/ src/
ARG CCA_VERSION
# The build backend is hash-pinned too (docker/build-constraints.txt). hatchling picks the wheel's
# files with the root .gitignore; docker/check_dist.py stops the build if that leaves out any file
# under src/cca (an unanchored `dist/` rule, for one, drops static/vendor/chess.js/dist/esm/).
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -z "${CCA_VERSION:-}" ]; then \
      echo "pass --build-arg CCA_VERSION=<__version__ from src/cca/__init__.py>" >&2; exit 1; \
    fi; \
    uv build --wheel --out-dir /dist --build-constraints docker/build-constraints.txt --require-hashes; \
    wheel="/dist/cca_chess-${CCA_VERSION}-py3-none-any.whl"; \
    if [ ! -f "$wheel" ]; then \
      echo "CCA_VERSION=${CCA_VERSION} does not match the built wheel: $(ls /dist)" >&2; exit 1; \
    fi; \
    python docker/check_dist.py --package src/cca "$wheel"
# --locked: stop if uv.lock is stale for pyproject.toml. On linux x86_64, PyPI's torch pulls the
# CUDA stack (nvidia-* and triton); those and torch itself are left out of the maia2 set, and
# torch comes from docker/torch-cpu.txt, which must pin the same version as uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    common=(--quiet --locked --no-dev --no-emit-project --format requirements.txt --no-header); \
    uv export "${common[@]}" --output-file /dist/req-base.txt; \
    maia=$(uv export "${common[@]}" --extra maia2 --no-hashes --no-annotate); \
    locked_torch=$(sed -nE 's/^torch==([^ ;]+).*/\1/p' <<<"$maia"); \
    if [ -z "$locked_torch" ] || ! grep -q "/torch-${locked_torch}%2Bcpu-" docker/torch-cpu.txt; then \
      echo "docker/torch-cpu.txt does not pin torch ${locked_torch:-?}+cpu (the uv.lock version)" >&2; exit 1; \
    fi; \
    skip=(--no-emit-package torch); \
    for p in $(sed -nE 's/^(nvidia-[A-Za-z0-9._-]+|triton)==.*/\1/p' <<<"$maia"); do \
      skip+=(--no-emit-package "$p"); \
    done; \
    uv export "${common[@]}" --extra maia2 "${skip[@]}" --output-file /dist/req-maia2.txt; \
    if grep -Eiq '^(torch|triton|nvidia-)' /dist/req-maia2.txt; then \
      echo "CUDA packages left in the maia2 requirement set" >&2; exit 1; \
    fi

# ------------------------------------------------------------------ virtual environments
# Hash-checking installs (every requirement pinned with its lock hash). uv rather than pip:
# python-chess 1.11.2 exists only as an sdist, and uv also checks the hashes of the build
# dependencies listed in docker/build-constraints.txt, which pip cannot do. The project wheel
# (built above, so it has no published hash) goes in with --no-deps. Bytecode is compiled once,
# content-hash based. No pip, uv or compiler ends up in the runtime images.
FROM build AS venv
RUN --mount=type=cache,target=/root/.cache/uv \
    python -m venv --without-pip /opt/cca; \
    install=(uv pip install --python /opt/cca/bin/python --build-constraints docker/build-constraints.txt); \
    "${install[@]}" --require-hashes -r /dist/req-base.txt; \
    "${install[@]}" --no-deps /dist/cca_chess-*.whl; \
    uv pip check --python /opt/cca/bin/python; \
    /opt/cca/bin/python -m compileall -q -j 0 --invalidation-mode unchecked-hash /opt/cca/lib

FROM venv AS venv-maia2
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/cca/bin/python --build-constraints docker/build-constraints.txt \
      --require-hashes -r docker/torch-cpu.txt -r /dist/req-maia2.txt; \
    uv pip check --python /opt/cca/bin/python; \
    /opt/cca/bin/python -c "import maia2, torch; assert torch.version.cuda is None and torch.__version__.endswith('+cpu'), torch.__version__"; \
    /opt/cca/bin/python -m compileall -q -j 0 --invalidation-mode unchecked-hash /opt/cca/lib

# ------------------------------------------------------------------ default image
# No apt here: everything comes from pinned stages above. The base image's own pip (not used at
# run time: /opt/cca has none) is removed, together with its pip3/pip3.12 scripts and the
# pip -> pip3 symlink the base image adds.
FROM base AS runtime
RUN python -m pip uninstall --yes pip; \
    rm -f /usr/local/bin/pip; \
    for f in pip pip3 pip3.12; do \
      if [ -e "/usr/local/bin/$f" ] || [ -L "/usr/local/bin/$f" ]; then \
        echo "/usr/local/bin/$f is still present" >&2; exit 1; \
      fi; \
    done; \
    python -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("pip") is not None)'; \
    groupadd --gid 10001 cca; \
    useradd --uid 10001 --gid 10001 --no-log-init --create-home --shell /usr/sbin/nologin cca
COPY --from=tini /out/bin/tini /usr/bin/tini
COPY --from=tini /out/doc/ /usr/local/share/doc/tini/
COPY --from=stockfish /out/bin/stockfish /usr/local/bin/stockfish
COPY --from=stockfish /out/doc/ /usr/local/share/doc/stockfish/
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/local/share/doc/cca/
COPY --from=venv /opt/cca /opt/cca
ENV PATH=/opt/cca/bin:$PATH \
    CCA_STOCKFISH=/usr/local/bin/stockfish \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ARG CCA_VERSION
ARG VCS_REF=
# The only labels: CI passes version and revision as build-args and uses docker/metadata-action
# for tags only (see .github/workflows/image.yml).
LABEL org.opencontainers.image.title="CCA" \
      org.opencontainers.image.description="CCA: human-like, hard-to-predict chess on top of Stockfish 19. Web simulator (cca play) and UCI engine (cca uci); human move model: QRE. Stockfish's licence and source are in /usr/local/share/doc/stockfish." \
      org.opencontainers.image.source="https://github.com/DonQuaan/CCA" \
      org.opencontainers.image.url="https://github.com/DonQuaan/CCA" \
      org.opencontainers.image.licenses="Apache-2.0 AND GPL-3.0-or-later" \
      org.opencontainers.image.version="${CCA_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
USER 10001:10001
WORKDIR /home/cca
EXPOSE 8765
# The slim base has no curl/wget: probe with the standard library, straight to the loopback
# (ProxyHandler({}): an http_proxy passed into the container must not turn it unhealthy). `cca play`
# answers /healthz at once, with "ready": false until its engines are built (in a background
# thread), so healthy means ready to play; a payload without "ready" counts as ready. Containers
# that do not run the web server (`uci`, `analyse`, `doctor`, ...) should be started with
# --no-healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["/usr/local/bin/python", "-c", "import json, sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8765/healthz', timeout=4); d = json.load(r); sys.exit(0 if r.status == 200 and d.get('status') == 'ok' and d.get('ready', True) is True else 1)"]
ENTRYPOINT ["/usr/bin/tini", "--", "cca"]
# --human qre: this image has no Maia-2, and cca play's default (maia2) would only fall back to QRE
# with a warning in the UI.
CMD ["play", "--host", "0.0.0.0", "--port", "8765", "--no-browser", "--human", "qre"]

# ------------------------------------------------------------------ Maia-2 image (CPU torch)
FROM runtime AS runtime-maia2
USER root
COPY --from=venv-maia2 /opt/cca /opt/cca
# The Maia-2 weights are NOT in the image (no licence is published for them): maia2 downloads
# them from their official source on first use into $CCA_WEIGHTS and checks their SHA-256.
# Mount a volume on /data to keep them across containers.
RUN install -d -o 10001 -g 10001 -m 0755 /data /data/maia2
ENV CCA_WEIGHTS=/data/maia2
VOLUME ["/data"]
LABEL org.opencontainers.image.description="CCA with the Maia-2 human move model on CPU-only torch. The Maia-2 weights are not included: they are downloaded on first use into /data/maia2 (mount a volume there) and SHA-256-checked. Stockfish's licence and source are in /usr/local/share/doc/stockfish."
USER 10001:10001
# Same probe as above (healthy = engines ready). The web server answers at once, but on a first
# start the engines are ready only after maia2 has downloaded the 267 MB checkpoint into
# /data/maia2, hence the long start period. If Maia-2 cannot be loaded, cca play falls back to
# QRE, reports why in the UI, and still becomes ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --start-interval=5s --retries=3 \
  CMD ["/usr/local/bin/python", "-c", "import json, sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8765/healthz', timeout=4); d = json.load(r); sys.exit(0 if r.status == 200 and d.get('status') == 'ok' and d.get('ready', True) is True else 1)"]
CMD ["play", "--host", "0.0.0.0", "--port", "8765", "--no-browser", "--human", "maia2", "--device", "cpu"]
