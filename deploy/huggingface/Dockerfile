# The Hugging Face Space image of the public CCA demo (docs/deploy.md).
#
# TEMPLATE, not buildable as checked in: .github/workflows/deploy-space.yml (or, by hand,
# `python deploy/huggingface/space.py render VERSION DIGEST HOST OUT_DIR`) replaces the version
# and digest placeholders on the FROM line with a released image of ghcr.io/donquaan/cca and the
# SHA-256 of its manifest, and the host placeholder in CMD with the Space's own host name
# (<subdomain>.hf.space, from the Hub API), then pushes the result to the Space. A deploy uses
# this file as the deployed version's tag holds it, so the CMD below must suit the image of the
# release that ships it. Edit it in the GitHub repository, never on the Hub: every deploy
# overwrites the Space's copy.
#
# Inherited from the release image, which the release pipeline built and smoke-tested (as its
# own uid 10001): ENTRYPOINT ["/usr/bin/tini", "--", "cca"], the HEALTHCHECK on
# http://127.0.0.1:8765/healthz, EXPOSE 8765, PATH, CCA_STOCKFISH and the licence files under
# /usr/local/share/doc.
FROM ghcr.io/donquaan/cca:{{CCA_VERSION}}@{{CCA_IMAGE_DIGEST}}

# Hugging Face runs Docker Spaces as uid 1000 ("Permissions" in
# https://huggingface.co/docs/hub/spaces-sdks-docker); run as that uid here as well, so that a
# local `docker run` of this file behaves like the Space. uid 1000 is not the image's user
# (10001, home /home/cca, mode 0700), so start in "/". Everything the server runs (/opt/cca,
# /usr/local, /usr/bin/tini) is root-owned and readable and executable by any uid (a scan of the
# 0.1.0 image's layers; docs/deploy.md). `cca play` writes no files (games live in memory); HOME
# points at the world-writable /tmp only in case a library looks for a home.
USER 1000:1000
WORKDIR /
ENV HOME=/tmp

# Public mode (`cca play --help`; docs/deploy.md#security-model): per-visitor limits; the Host
# guard also answers the Space's own name (the edge forwards Host: <subdomain>.hf.space; IP
# literals, which the HEALTHCHECK uses, stay accepted); the client address is the last
# X-Forwarded-For entry, the one the Hub's edge appends (observed, not documented: see
# docs/deploy.md); framing is allowed for huggingface.co only, whose Space page shows the app in
# an iframe. --port must equal app_port in README.md.
CMD ["play", "--public", "--allowed-host", "{{CCA_SPACE_HOST}}", "--host", "0.0.0.0", "--port", "8765", "--no-browser", "--human", "qre", "--trusted-proxies", "1", "--frame-ancestor", "https://huggingface.co", "--max-sessions", "48"]
