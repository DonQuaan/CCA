---
title: CCA
emoji: ♟️
colorFrom: gray
colorTo: green
sdk: docker
app_port: 8765
pinned: false
license: apache-2.0
short_description: Human-like, hard-to-predict chess on Stockfish 19
---

<!-- Rendered by .github/workflows/deploy-space.yml from deploy/huggingface/README.md in
https://github.com/DonQuaan/CCA. Edit it there, not here: every deploy replaces this file. -->

# CCA: play chess against a human-like engine

This Space runs **CCA {{CCA_VERSION}}**
([source](https://github.com/DonQuaan/CCA/tree/v{{CCA_VERSION}})), a research layer on top of
Stockfish 19 that aims to play human-like, hard-to-predict chess and to steer games towards the
mistakes its opponent is likely to make. The page is `cca play`, CCA's browser simulator: play a
game and watch CCA's candidate moves, decision knobs and latent state change move by move
([guide](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/docs/simulator.md)).

## What to expect

- **Research, not a product.** CCA's behavioural claims (human-likeness, unpredictability,
  trap-setting against people) are hypotheses under test, not established results
  ([science notes](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/docs/science.md)).
- **Human move model: QRE.** The demo predicts human moves with CCA's built-in quantal-response
  (QRE) model. The Maia-2 neural model is not part of it: this Space runs the default image,
  which has no Maia-2.
- **Shared free CPU.** The Space runs on Hugging Face's free CPU tier, shared by every visitor, so
  CCA thinks slowly when the Space is busy, and each visitor has limits on games and requests.
  After 48 hours without visitors the Space sleeps; the next visit wakes it, which takes a while.
- **Games live in memory only.** Nothing is saved on the server: a restart, a new deploy or a
  sleep ends every game in progress. CCA has no accounts and sets no cookies; the page keeps its
  own settings (language, the thinking panel, your last new-game choices) in your browser's
  local storage.

## Fair play

Never use this demo, or CCA anywhere, to help a person during a game against other people, rated
or casual: that is cheating. CCA may play online only from a declared bot account, and games
meant to exploit real people's mistakes are human-subjects research that needs their informed
consent and an ethics review
([responsible use](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/README.md#responsible-use)).

## Licences and source

- CCA's own code is under the Apache License 2.0
  ([LICENSE](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/LICENSE),
  [NOTICE](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/NOTICE)); this card's `license`
  field names that licence only.
- The container image is a combined distribution: it also holds Stockfish 19 and python-chess
  (both GPL-3.0-or-later), tini (MIT) and the python:3.12-slim base image, as
  [THIRD_PARTY_NOTICES.md](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/THIRD_PARTY_NOTICES.md)
  lists. Stockfish's licence and complete corresponding source are inside the image, under
  `/usr/local/share/doc/stockfish`.
- Image: `ghcr.io/donquaan/cca:{{CCA_VERSION}}`, pinned by digest in this Space's `Dockerfile`.
  Release notes: [v{{CCA_VERSION}}](https://github.com/DonQuaan/CCA/releases/tag/v{{CCA_VERSION}}).

Report security problems privately, as
[SECURITY.md](https://github.com/DonQuaan/CCA/blob/v{{CCA_VERSION}}/SECURITY.md) describes, not in
the Space's discussions.
