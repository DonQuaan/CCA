# The `cca play` simulator

`cca play` starts a small web server on your machine and opens a page where you play a game
against CCA and watch the numbers behind each of its moves. CCA stands for Chaotic Chess
Algorithm. The simulator is a study and research tool: the page loads nothing from other hosts,
and games live only in the server's memory on your machine.

![CCA play, English interface, dark theme](img/cca-play-dark.png)

*Figure 1. English interface, dark theme, against real Stockfish 19. You play White; CCA
(persona `balanced`, Elo 1900, QRE human model) has just answered 3.Bc4 with 3...dxe4. Two
candidates lie inside the risk budget (0.066): the engine's best move dxc4 (50.8% of CCA's
policy, dashed green arrow) and dxe4 (49.2%, blue arrow). CCA sampled dxe4, which costs 0.001
in expected score, and the sentence at the top of the panel says exactly that. The other
candidates show "-" in the CCA % column: they are outside the risk budget, although they carry
positive trap values (red).*

> **What the numbers are.** The page shows CCA's internal decision variables, not
> measurements of you. Stress, drive, opponent stress and chaos are virtual, latent control
> signals inspired by, not models of, physiology ([ADR-0005](adr/0005-honest-science-labelling.md)).
> CCA's behavioural claims (for example that its trap-seeking moves cause more human errors)
> are hypotheses with pre-registered kill criteria in [science.md](science.md).

The reasons behind the simulator's design are recorded in
[ADR-0007](adr/0007-simulator-and-distribution.md).

Contents: [Starting the simulator](#starting-the-simulator) ·
[Reading the page](#reading-the-page) · [Fair-game toggle](#fair-game-toggle) ·
[Time controls and clocks](#time-controls-and-clocks) ·
[Seeds and commit-reveal](#seeds-and-commit-reveal) · [Position lab](#position-lab) ·
[Entering moves](#entering-moves) · [Languages and themes](#languages-and-themes) ·
[Other features](#other-features) · [Local HTTP API](#local-http-api) ·
[Security model](#security-model) · [Public mode](#public-mode) ·
[Vendored browser files and licences](#vendored-browser-files-and-licences) ·
[Limits](#limits)

## Starting the simulator

### Requirements

- CCA installed with Python 3.11 or later (see the [README](../README.md)). To run it in a
  container instead, see [docker.md](docker.md).
- **Stockfish 19.** The Python packages do not contain it. CCA uses the first match of:
  1. `--stockfish PATH`;
  2. the `CCA_STOCKFISH` environment variable;
  3. `engines/stockfish-*/` in the source checkout CCA runs from, which is where
     `python scripts/fetch_stockfish.py` puts the official release (SHA-256 pinned in
     [`engines/stockfish.lock.json`](../engines/stockfish.lock.json));
  4. `stockfish` on `PATH`.

  A path given in 1 or 2 that is not a file is an error; CCA does not fall back to another
  binary.
- **Maia-2 (optional).** Install the `maia2` extra (maia2 0.11 supports Python 3.10 to 3.12,
  so 3.11 or 3.12 for CCA). On first use it downloads its checkpoint (about 280 MB (267 MiB)) into
  `$CCA_WEIGHTS`, by default `weights/maia2` in the source checkout. Without Maia-2, start
  with `--human qre`. The default `--human maia2` still works but falls back to the QRE model
  and the page says why.

### Commands

```bash
cca play                          # serves http://127.0.0.1:8765/ and opens your browser
cca play --human qre              # without Maia-2
cca play --port 0 --no-browser    # any free port; the URL is printed
```

In a source checkout, run the same commands through `uv run` (for example `uv run cca play`).

The server prints one line, for example `CCA simulator: http://127.0.0.1:8765/`, and opens
that URL unless you pass `--no-browser`. Stockfish (and Maia-2) are built in a background
thread. The page shows "Warming up the engines..." until they are ready, and `/healthz`
answers from the first moment. Stop the server with Ctrl+C or SIGTERM (for example
`docker stop`): it prints `cca play: stopped` and closes Stockfish.

`cca play` exits with status 1 and an `error: ...` line, before it opens a port, when:

- Stockfish cannot be found;
- the persona or the `--config` file cannot be loaded;
- `--elo-self` or `--elo-oppo` is outside the range the page offers (800-2600 and 400-3000);
- `--public` is given with a loopback `--host` or without `--allowed-host`,
  `--log-forwarded-hops` without `--public`, or `--max-connections-per-peer` with a
  `--trusted-proxies` other than 0 or without `--max-connections` and `--public` (see
  [Public mode](#public-mode));
- the address cannot be bound (port in use, address not on this machine).

Invalid argument values, such as `--port 70000` or `--max-sessions 0`, are rejected by the
argument parser with exit status 2.

### Flags

Flags of `cca play` only:

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Address to bind. Anything other than a loopback address prints a warning (see [Security model](#security-model)). |
| `--port PORT` | `8765` | TCP port, 0-65535; `0` picks any free port. |
| `--no-browser` | off | Do not open a web browser. |
| `--max-sessions N` | `16` | Games kept in memory, 1-1024; the least recently used game is dropped first. |

The flags of a public deployment (`--public`, `--allowed-host`, `--trusted-proxies`,
`--frame-ancestor` and the limits) are described under [Public mode](#public-mode). Without
them every limit is off and `cca play` behaves as this guide describes.

Flags shared with `cca analyse` and `cca match`. In `cca play` they are fixed for the life of
the server and are the defaults of every new game:

| Flag | Default | Meaning in `cca play` |
|---|---|---|
| `--stockfish PATH` | auto-detect | Stockfish binary (discovery order above). |
| `--threads N` | `1` | Stockfish threads. `1` keeps node-limited search reproducible. |
| `--hash MB` | `256` | Stockfish hash table size. |
| `--nodes N` | `200000` | Stockfish nodes per evaluation. |
| `--human {maia2,qre}` | `maia2` | Human move model. Any Maia-2 failure falls back to `qre`, and the reason is shown on the page and in `/api/info`. |
| `--maia2-type {rapid,blitz}` | `rapid` | Maia-2 checkpoint. |
| `--device {gpu,cpu}` | `gpu` | Maia-2 device; `gpu` uses the CPU when CUDA is not available. |
| `--persona NAME_OR_PATH` | `balanced` | Default persona. A shipped name appears under that name; a `.toml` path appears as `cli` on the page. |
| `--config FILE` | none | Full agent configuration (TOML). |
| `--elo-self N` | `1900` | Default rating of the human player CCA imitates. |
| `--elo-oppo N` | `1500` | Default rating CCA assumes for you. |
| `--seed S` | `cca` | Seed of the [position lab](#position-lab) only; every game gets its own seed. |
| `--argmax` | off | Play the most probable move instead of sampling from the policy. |

Types, choices and the exact help texts are generated from the parser in
[reference.md](reference.md#cca-play); the shipped personas are listed in
[reference.md](reference.md#8-shipped-personas).

## Reading the page

On a wide screen the page has three columns: the board, the **Game** card and the
**CCA's thinking** panel. On a narrow screen they stack.

### Board and player strips

- **CCA's strip:** "CCA · *persona* · *Elo*" and "*QRE* / *Maia-2* human model".
- **Your strip:** "You" and "*Elo* (CCA's model of you)". This Elo is the rating CCA assumes
  for you when it predicts your replies. It is a setting, not a measurement.
- **Clocks** appear in timed games. While CCA decides, its strip shows "thinking...". While an
  emulated thinking delay runs, it shows "replies in *N* s" with a progress bar.
- **Highlights:** the squares of the last move, the king in check, and legal-move dots once
  you pick up a piece.
- **Arrows** (hidden in a fair game, see [Fair-game toggle](#fair-game-toggle)):

  | Arrow | Meaning |
  |---|---|
  | Blue, solid | CCA's policy: up to four of its most probable moves with probability of at least 2%. Darker means more probable, in four shades: at least 50%, at least 25%, at least 10%, and below 10%. |
  | Green, dashed | The engine's best move: the candidate with the highest "q engine". Drawn only when CCA played a different move. |

  The arrows belong to the CCA decision that produced the position shown (CCA's last move, or
  the move you are reviewing). They are drawn on the position after that move, so they show
  what CCA weighed, not what it threatens next. While a [position lab](#position-lab) answer
  is open, they show that answer instead.

### Game card

- **Status line:** "Your move (white).", "CCA is thinking...", or the result.
- **Move list:** click a move to review the position after it (a banner offers "Back to
  live"). A ▲ after a CCA move marks a trap value of at least 0.03 (hidden in a fair game).
  The four buttons below the list, and the keys Left, Right, Home and End, move through the
  game.
- **Move entry field:** see [Entering moves](#entering-moves).
- **Buttons:**
  - New game;
  - Take back (see [take-back rules](#take-backs));
  - Resign, which asks for a second click ("Confirm resign") within 3.5 seconds;
  - Flip board;
  - Copy PGN;
  - Copy FEN (of the position shown);
  - Load FEN, which opens the New game dialog with the FEN field focused.
- **New game dialog:**
  - Play as: White, Black or Random.
  - CCA persona, with the persona's own description.
  - CCA Elo (800-2600) and "Your Elo (CCA's model of you)" (400-3000), in steps of 50.
  - Time control.
  - Human-like thinking delay.
  - Seed (optional).
  - Start position (FEN, optional).

  The dialog remembers your last choices in the browser, except the seed and the FEN.

### CCA's thinking

The badge in the panel's corner says what the panel shows:

| Badge | Meaning |
|---|---|
| live | CCA's latest decision, during the game. |
| move *n* | The decision behind the CCA move you are reviewing. |
| position lab | The answer of the [position lab](#position-lab). |
| hidden | Fair game: nothing is shown until the game ends. |
| revealed | Fair game after the game ended: everything is shown. |

#### Why sentence

The sentence at the top is built by fixed rules in
[`static/js/insight.js`](../src/cca/play/static/js/insight.js) from the numbers of this one
decision. It summarises those numbers; it is not a separate explanation produced by the model.
It uses these terms:

- **Engine best:** the candidate with the highest q engine.
- **Cost:** q engine of the engine's best move minus q engine of the played move, never below
  zero.
- **Risk budget:** the `risk_budget` knob.

The first rule that applies picks the sentence:

| Condition | Sentence (English) |
|---|---|
| Reflex mode (almost out of time) | "Almost out of time: CCA played the engine's best move ... without its full analysis (reflex mode)." |
| Played the engine's best move, while a more probable policy move existed | "CCA sampled ... (... of its policy; top was ... at ...): it samples from its policy, so less likely moves are sometimes played." |
| Played the engine's best move, trap value above 0.02 | "CCA played ..., the engine's best move, which also sets you problems: trap value ..." |
| Played the engine's best move, otherwise | "CCA played ..., also the engine's best move (expected score ...)." |
| Played another move, while a more probable policy move existed | "CCA sampled ... instead of the engine's best ...; the ... cost is within its risk budget ..." |
| Its trap value is above the engine move's and above 0.01 | "CCA chose ... over the engine's best ...: its trap value ... is worth the ... expected-score sacrifice, within the risk budget ..." |
| Its human prior exceeds the engine move's | "... it is the more human-like move (prior ... vs ...) and costs only ... expected score, within the risk budget ..." |
| It leaves you more replies (higher Opp. H) | "... it leaves you more plausible replies (... vs ... nats) for a ... expected-score cost, within the risk budget ..." |
| Otherwise | the "sampled" sentence above |

Percentages are rounded to one decimal, and to whole numbers below 10% and from 99.5% up, so
two close probabilities can print alike.

#### Candidates table

The rows are CCA's candidate moves: the engine's top moves (`engine_multipv`, 5 by default)
plus up to `prior_top` (4 by default) of the human model's most likely moves, without
duplicates (see `AgentConfig` in [reference.md](reference.md#agentconfig)). In reflex mode
there is only one row. Rows are sorted by "CCA %" (moves without one last), then by q engine.
All scores are expected scores from CCA's side, between 0 and 1: a win counts 1 and a draw
0.5.

| Column | Decision field | Meaning |
|---|---|---|
| Move | `san` | The move. The tag **played** marks the move CCA played; **engine** marks the engine's best move. |
| CCA % | `policy` | The final probability CCA sampled its move from. "-" means the move is outside the policy: it costs more than the risk budget allows. |
| Human % | `prior` | CCA's anchor: how likely a human of CCA's rating is to play the move, according to the human model. It is restricted to the candidates, renormalised, mixed with a small uniform floor (`anchor_floor`), and tempered during the opening (`opening_plies`, `opening_temperature`), where Maia-2 was not trained. |
| q engine | `q_opt` | CCA's expected score after the move if you reply perfectly (Stockfish). |
| q human | `q_human` | CCA's expected score if you reply like a human of your rating (the human model at "your Elo"). |
| Trap | `trap_value` | `q_human - q_opt`: how much CCA expects to gain from your likely mistakes. Values above +0.005 are shown in bold red, values below -0.005 greyed. |
| Opp. H | `opp_entropy` | Entropy, in nats, of the human model's distribution of your replies: how many plausible choices you face. |

The decision JSON also carries `sharpness` and `attention` for each candidate; the table does
not show them. All fields are defined in [reference.md](reference.md#candidate).

#### Decision knobs

The knobs are derived for every move from the latent state, the persona, the ratings and the
clock ([reference.md](reference.md#knobs)).

| Knob | Meaning |
|---|---|
| `kl_weight` | piKL temperature: large means imitate the human prior. |
| `exploit` | Weight of the human-aware value against the worst-case (engine) value. |
| `entropy_bonus` | Bonus per nat of your decision entropy. |
| `risk_budget` | Largest expected-score sacrifice allowed against the engine's best move. |
| `tunnel` | Tunnel vision: exponent on the attention weights (0 = none). |
| `habit` | Stress-induced pull toward the most intuitive move. |
| `opp_temperature` | Flattening of CCA's model of you when you seem stressed (1 = none). |
| risk bank | What you have given away minus the risk CCA has already taken. It caps the risk budget. |

#### Latent state

| Row | Meaning |
|---|---|
| `stress` | Latent arousal. Virtual; inspired by stress research, not a physiological model. |
| `drive` | Leaky integrator of evaluation surprises (reward-prediction errors); positive means things went better than CCA expected. |
| `opp_stress` | CCA's internal proxy for pressure on you, driven by how surprising its own moves are under the human model and by your clock (theory-of-mind variable; virtual, not a measurement of you). |
| `u0`, `u1`, `u2` | Signals in (-1, 1) from the configured chaos driver: a deterministic Lorenz attractor by default, or its AR(1) control condition when a config file selects `chaos_driver = "ar1"`. They are a reproducible source of variability, not a model of temperament. |
| `think_time` | The human-like think time CCA wanted, in seconds (hand-set model, not fitted). |

See [reference.md](reference.md#psychstate) for the fields and [science.md](science.md) for
what each mechanism is and is not.

#### Over the game

One point per CCA move. Click a point to review that move.

| Row | Content |
|---|---|
| Expected score | q engine (solid) and q human (dashed) of the move CCA played, with a reference line at 0.5. |
| Stress | `stress`. |
| Drive | `drive`, symmetric around 0. |
| Opp. stress | `opp_stress`. |
| Chaos u0-u2 | The three chaos signals. |
| Think time | Bars of the wanted think time; the tooltip adds the compute time. |

Each row starts from a default range and grows to fit the data. A vertical line and a gap in
the curves mark a restart of CCA's latent state after a take-back (see
[Take-backs](#take-backs)).

Under the charts the page repeats the honest label for the latent signals. With the QRE model
it also names the human model in use and, after a fallback, gives the reason Maia-2 was not
loaded.

## Fair-game toggle

The switch **Show CCA's thinking** in the top bar has two states:

- **On** (default): the panel, the board arrows and the ▲ trap marks are shown live.
- **Off** (fair game): all of them, and the arrow legend, are hidden until the game ends, then
  revealed. The badge reads "hidden", then "revealed".

The choice is remembered in the browser.

The toggle works in the page only. The local API always returns CCA's full decisions (the
`/think` response and `/api/games/<id>/decisions`), so the toggle keeps you from seeing them by
accident; it does not lock them away. The server itself withholds two things until the game
ends: the per-move diagnostics in the PGN and a secret seed.

## Time controls and clocks

The New game dialog offers Untimed (the default), 3+2, 5+3, 10+5 and 15+10 (minutes, plus an
increment in seconds per move). Through the API any base time from 1 to 10800 seconds and any
increment from 0 to 180 seconds is accepted.

### Clock rules

- **The server keeps the clocks.** The page only displays them from the server's state and
  cannot change them. The server has no background timer: it checks the clocks on every
  request, and the page asks it as soon as a displayed clock reaches zero.
- **Fischer increment:** the increment is added after every move.
- The clock of the side to move runs from the moment its position appears.
- **CCA is charged the wall time since its clock started.** That includes time spent waiting
  for the shared engine (see [Limits](#limits)). CCA's compute deadline is derived from the
  time it has left once it has the engine. With less than `fast_budget` (0.6 s by default)
  available it plays in reflex mode: the engine's best move without its full analysis.
- **Human-like thinking delay** (the dialog's checkbox, `emulate_think_time` in the API):
  - CCA is charged the larger of the wall time and the think time its human-like model
    sampled.
  - The page shows CCA's move only after that delay (`reveal_in_ms`), and your clock starts at
    that moment (`starts_in_ms`).
  - A move you send more than 50 ms (`REVEAL_GRACE_S`) before the reveal is refused with 409,
    so answering early buys no time. A move within those 50 ms is charged from the reveal.

  Without the delay, CCA's move appears as soon as it is computed and CCA is charged the wall
  time only.

### Flag rules

- A side whose clock reaches zero loses on time.
- It is a draw instead if the other side does not have enough material to mate.
- CCA can lose on time while it waits for the engine; it then makes no decision.
- CCA also loses on time when its decision, or the emulated think time, takes longer than the
  time it has left; the move is then not played.

### Game ends

Checkmate, stalemate, insufficient material, the fifty-move rule and threefold repetition end
the game automatically, as an arbiter or an auto-adjudicating GUI would; nobody has to claim
the draw. Resignation and loss on time are the other endings. The API reports them as
`checkmate`, `stalemate`, `insufficient-material`, `fifty-move`, `threefold`, `resignation`
and `time`.

### Take-backs

- **Untimed games only.** In a timed game the button is disabled and the API answers 409.
- Not after a resignation or a loss on time. After a checkmate, a stalemate or a draw by rule,
  a take-back is allowed.
- It takes back your last move and CCA's reply to it, if there was one.
- If one of CCA's own moves was taken back, CCA's latent state (stress, drive, chaos) restarts
  on its next move, and the charts mark the restart. If only a game-ending move of yours was
  taken back, CCA never saw it and simply continues.

## Seeds and commit-reveal

A seed fixes CCA's own random draws.

- **No seed given** (the default): the server draws a secret random seed, 32 hexadecimal
  characters.
  - From the moment the game is created, before the first move, the game state
    (`cca.seed_commitment`) and the PGN (`CCASeedCommitment` tag, see Copy PGN) carry its
    SHA-256 commitment: the hex SHA-256 of the seed's UTF-8 bytes.
  - When the game ends, the seed is revealed: in the game state (`cca.seed`), in the PGN
    (`CCASeed` tag), and on the Game card together with the first 12 hex digits of the
    commitment.
  - The reveal lets you check that the seed was fixed before the first move:

    ```bash
    printf '%s' 'THE-SEED' | sha256sum
    python -c "import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" THE-SEED
    ```

- **Seed chosen by you:** 1 to 64 characters from `A-Z a-z 0-9 . _ : + -`, with no spaces. It
  is public from the start: there is no commitment, and it appears in the state and in the PGN
  at once.
- **Replays:** replaying an untimed game on the same server, with the same seed, settings and
  moves, repeats CCA's decisions when the engine search and the human model are deterministic.
  That means one engine thread (`--threads 1`). Every decision also starts from a cleared
  engine (`ucinewgame`), so other games on the server do not change the result. Timed games
  also depend on the clock, through the deadlines and the time-sliced search, and so does a
  decision under the [decision time cap](#decision-time-cap) of public mode. This was checked
  with the QRE model; with Maia-2 it also depends on Maia-2's inference being deterministic on
  your device.
- `--seed` on the command line seeds only the position lab.

The PGN's CCA tags are `CCAPersona`, `CCAHumanModel`, `CCASeedCommitment` (secret seeds only),
`CCASeed` (chosen seeds, or secret ones after the end) and `CCAReason` (how the game ended).

## Position lab

**Ask CCA about the displayed position** runs one CCA decision on the position shown, live or
reviewed.

- It plays for the side to move in that position, which can be your side.
- It uses the game's persona and Elo settings and the server's `--seed`. It has no clock and
  uses the full node budget.
- It starts from a fresh agent: no game history and the starting latent state. Its answer can
  therefore differ from what CCA plays in the game in the same position.
- The answer appears in the panel with the badge "position lab", a banner saying it was not
  played in the game, and its own arrows. **Close** returns to the game's decision.
- While the lab runs, move entry is off. The lab and the game share the one engine, so CCA's
  reply would wait behind it and, in a timed game, the wait would count against CCA's clock.
  Your own clock keeps running.
- The answer is dropped if you move through the game, take back, or start a new game before it
  arrives. In a fair game it stays hidden until the game ends.
- Finished positions are refused.

The API equivalent is [`POST /api/analyse`](#post-apianalyse).

## Entering moves

- **Mouse or touch:** click the piece and then the target square, or drag it. Legal targets
  show dots. A promotion opens a dialog on the board; cancelling it puts the pawn back.
- **Keyboard:** the field "Type a move (SAN or UCI)" accepts:
  - SAN, such as `e4`, `Nf3`, `exd5`, `O-O` or `e8=Q`;
  - UCI, such as `e2e4` or `e7e8q`, in either case (`E2E4` works).

  Trailing `+`, `#` and annotations such as `!?` are ignored.
- **A promotion must name the piece:**
  - accepted: `axb8=Q`, `axb8=r`, `a8=N`, `a8n`, `a7b8q`;
  - refused: `a8`, `axb8`, `a7a8`, `a7-a8`.

  A refused promotion shows "Name the promotion piece, for example axb8=Q+ or a7b8q (Q, R, B
  or N)." and sends nothing. CCA never guesses a promotion piece for you. A piece letter on a
  move that is not a promotion (`e2e4q`) is refused as illegal.
- Anything else that is not a legal move shows "Not a legal move here: ...".
- **When moves are accepted:** only when it is your turn, the game is shown live (not in
  review), and nothing else is pending (your previous move, CCA's decision, a new game, a lab
  request).
- **Accessibility:**
  - A "Skip to move entry" link.
  - Keyboard review with Left, Right, Home and End when the focus is not in a text field.
  - The field gets the focus back after a typed move.
  - Moves and results are announced through a live region. This was not tested with a screen
    reader.

## Languages and themes

- **Languages:** English and Vietnamese. The page starts in Vietnamese when the browser's
  language starts with `vi`, and in English otherwise. The **VI** / **EN** button switches
  language, and the choice is remembered in the browser.
  - Every string of the page is in
    [`static/js/i18n.js`](../src/cca/play/static/js/i18n.js).
  - Error messages from the server and the persona descriptions are English only.
- **Theme:** light or dark, following the operating system or browser setting
  (`prefers-color-scheme`). The page has no theme switch of its own.

![CCA play, Vietnamese interface, light theme](img/cca-play-vi-light.png)

*Figure 2. Vietnamese interface, light theme, after 4.d4 g6 5.Nc3 Nxc3. Nxc3 is again the only
candidate inside the risk budget and the engine's best move. The sentence says so and gives
its expected score, 0.810. The charts have one point per CCA move.*

## Other features

- **Reload:** each browser tab remembers its game. A reload restores it, including CCA's
  recorded decisions. If the server has restarted or dropped the game, the page starts a new
  one.
- **Copy PGN:**
  - Headers: Event "CCA play (local)", the players (your side as "Human", CCA with its version,
    persona and Elo), both Elo values, `TimeControl` (for example `300+3`, or `-`),
    `Termination`, and the CCA tags above. The `Date` is the UTC date the game started.
  - Once the game has ended, each CCA move carries a comment of the form
    `{ cca think=<seconds>s q_opt=<q engine> trap=<trap value> stress=<stress> }`. A value the
    decision did not produce is left out, and reflex moves are marked `reflex`, as in this
    comment from a real 1+0 game: `{ cca think=0.2s q_opt=0.529 stress=0.20 reflex }`.
  - If the browser refuses clipboard access, a dialog shows the text so you can copy it by
    hand.
- **About:**
  - The server's runtime: CCA version, engine, nodes per evaluation, human model and chaos
    driver.
  - A glossary of every signal.
  - The fair-play note: using an engine to help a human in rated games is cheating on every
    platform.
  - The credits, with links to the licence files.

## Local HTTP API

The page talks to the server through a small JSON API, which scripts on the same machine can
use too. The source is [`src/cca/play/server.py`](../src/cca/play/server.py) (routing and
guards), [`app.py`](../src/cca/play/app.py) (validation) and
[`game.py`](../src/cca/play/game.py) (game rules and clocks).

### Conventions

- **Base URL:** the one the server printed, for example `http://127.0.0.1:8765`. The `Host`
  header must name the server as described under [Security model](#security-model); clients
  such as curl do this by default.
- **POST bodies:**
  - a JSON object, sent with `Content-Type: application/json` and a `Content-Length` header
    (no chunked encoding);
  - at most 65536 bytes;
  - an empty body counts as `{}`.

  Unknown fields are rejected. JSON must be strict: `NaN` and `Infinity` are refused. Integer
  fields must be JSON integers, and `true` is not an integer.
- **Responses:**
  - JSON, except the PGN (`text/plain; charset=utf-8`) and the page's static files;
  - non-finite numbers are sent as `null`;
  - `<`, `>` and `&` are written as the escapes `\u003c`, `\u003e` and `\u0026` (the same
    JSON value), so no response contains markup;
  - API responses carry `Cache-Control: no-store`.
- **Errors:** `{"error": "<message>"}`. When a 409 concerns the game (it is over, or CCA's move
  is still being revealed), the body also carries `"state"`. A refusal by one of the
  [public-mode limits](#limits-and-refusals) also carries `"limit"`, the name of the limit, and
  a `Retry-After` header in whole seconds.

### Endpoints

| Method | Path | Request body | Success | Endpoint-specific errors |
|---|---|---|---|---|
| GET | `/` or `/index.html` | none | 200, the page | |
| GET | `/static/<path>` | none | 200, a file of the page | 404 |
| GET | `/healthz` | none | 200 `{"status": "ok", "version", "ready"}` | 503 `{"status": "error", "version", "ready": false, "error"}` once the engine failed |
| GET | `/api/info` | none | 200, see [below](#get-apiinfo) | |
| POST | `/api/games` | [new-game fields](#post-apigames) | 201 `{"game_id", "state"}` | 400, 503 |
| GET | `/api/games/<id>` | none | 200, the [game state](#game-state) | 404 |
| POST | `/api/games/<id>/move` | `{"uci": "e2e4"}` | 200 `{"state"}` | 400 illegal or malformed move, or not your turn; 404; 409 game over, or CCA's move still being revealed |
| POST | `/api/games/<id>/think` | `{}` (content ignored) | 200, see [below](#post-apigamesidthink) | 404; 409 game over, or it is your turn; 503 |
| POST | `/api/games/<id>/undo` | `{}` (content ignored) | 200 `{"state"}` | 400 no move of yours to take back; 404; 409 timed game, or the game ended by resignation or on time |
| POST | `/api/games/<id>/resign` | `{}` (content ignored) | 200 `{"state"}` | 404; 409 game over |
| GET | `/api/games/<id>/pgn` | none | 200, PGN text | 404 |
| GET | `/api/games/<id>/decisions` | none | 200 `{"decisions": [{"ply", "decision"}]}`: every CCA decision of the game, by ply | 404 |
| POST | `/api/analyse` | [analyse fields](#post-apianalyse) | 200, see [below](#post-apianalyse) | 400, 503 |

A game id is 32 URL-safe characters. An unknown or evicted id gives 404 ("no such game
(evicted, or the server restarted)"). `/think` makes CCA decide and play for the side to move;
the page calls it whenever it is CCA's turn, and an API client must call it itself.

Errors any request can get:

| Status | Cause |
|---|---|
| 400 | Invalid JSON, a body that is not a JSON object, a field that fails validation, a truncated body, an HTTP/0.9 request. |
| 403 | `Origin` differs from the server's own origin, or `Sec-Fetch-Site: cross-site` (POST). |
| 404 | Unknown path or file. |
| 405 | Wrong method for a known path; the `Allow` header names the right one. |
| 408 | The request body did not arrive in time (with `--max-connections`: within 10 seconds in all). |
| 409 | With `--max-connections`: two requests already wait for this game (`"limit": "game_busy"`, see [Public mode](#limits-and-refusals)). |
| 411 | No `Content-Length`, or chunked transfer encoding (POST). |
| 413 | Body larger than 65536 bytes. |
| 415 | `Content-Type` is not `application/json` (POST). |
| 421 | `Host` header not accepted (DNS-rebinding guard). |
| 429 | Only with limits on (public mode): this client asks too often or has too many requests at once ([Limits and refusals](#limits-and-refusals)). |
| 500 | Internal error; the details go to the server's standard error, not to the client. |
| 501 | Unsupported method (for example `PUT`, `OPTIONS`, `HEAD`). |
| 503 | The engines are still warming up, or an engine error occurred. With limits on, also: the server is busy, all game slots are in use, or the server is shutting down ([Limits and refusals](#limits-and-refusals)). |

### `GET /api/info`

| Field | Content |
|---|---|
| `version` | CCA version. |
| `ready`, `error` | Whether the engines are built; the start-up error, or `null`. |
| `engine` | `{"name", "nodes", "threads", "hash_mb"}`. |
| `human_model` | `{"requested", "kind", "fallback_reason"}`: `kind` is `"maia2"` or `"qre"`. |
| `personas` | `[{"name", "description"}]`: the shipped personas, plus `cli` first when the server's default persona is not an unchanged shipped one (a persona file, or a `--config` that changes it). |
| `defaults` | `human_color` (`"white"`), `persona`, `elo_self`, `elo_oppo`, `time_control` (`null`), `emulate_think_time` (`false`). |
| `ranges` | `elo_self` `[800, 2600]`, `elo_oppo` `[400, 3000]`, `base_s` `[1.0, 10800.0]`, `inc_s` `[0.0, 180.0]`, `fen_length` 100, `seed_length` 64. |
| `time_controls` | The dialog's choices: `null` (untimed) and `{"base_s", "inc_s"}` for 180+2, 300+3, 600+5, 900+10. |
| `max_sessions` | `--max-sessions`. |
| `sample` | `false` with `--argmax`. |
| `chaos_driver` | `"lorenz"` or `"ar1"`. |
| `limits` | Only when public mode or a limit is on: `public`, `max_sessions_per_client`, `decisions_per_minute`, `max_queue`, `session_idle_minutes`, `max_connections`, `max_think_seconds`, `reads_per_minute`, `max_connections_per_peer` (`null` = no limit) and `waiting`, the requests waiting for the engine right now. Absent otherwise. |

### `POST /api/games`

| Field | Values | Default |
|---|---|---|
| `human_color` | `"white"`, `"black"` or `"random"` | `"white"` |
| `persona` | a name from `/api/info` `personas` (shipped names only, never a file path) | the server's default persona |
| `elo_self` | integer, 800-2600 | `--elo-self` |
| `elo_oppo` | integer, 400-3000 | `--elo-oppo` |
| `fen` | start position: at most 100 characters, a legal position that is not already finished | the standard start |
| `seed` | 1-64 characters from `A-Z a-z 0-9 . _ : + -` | a secret seed |
| `time_control` | `null` (untimed) or `{"base_s": 1-10800, "inc_s": 0-180}` (`inc_s` defaults to 0) | `null` |
| `emulate_think_time` | `true` or `false` | `false` |

If the FEN puts CCA's side to move, call `/think` next.

### Game state

Every game endpoint returns this object as `state`.

| Field | Content |
|---|---|
| `id`, `start_fen`, `fen` | Game id, start position, current position. |
| `turn`, `human_color` | `"white"` or `"black"`. |
| `to_move` | `"human"`, `"cca"`, or `null` once the game is over. |
| `moves` | `[{"uci", "san", "fen"}]`, from the start position. |
| `last_move` | `{"uci", "san", "from", "to"}` or `null`. |
| `check`, `check_square` | Whether the side to move is in check, and its king's square. |
| `game_over`, `result`, `reason`, `winner` | `result` is `"1-0"`, `"0-1"`, `"1/2-1/2"` or `"*"`; `reason` is one of the [endings](#game-ends) or `null`; `winner` is `"human"`, `"cca"` or `null`. |
| `cca` | `{"persona", "elo", "opponent_elo", "human_model", "seed_commitment", "seed"}`: `seed` is `null` while a secret seed is still hidden, and `seed_commitment` is `null` for a chosen seed. |
| `time_control`, `emulate_think_time` | As created. |
| `clocks` | `null` when untimed, else `{"white_ms", "black_ms", "running", "starts_in_ms", "inc_ms"}`. `running` is the colour whose clock runs (or `null`); `starts_in_ms` is the time until it starts (the reveal delay). |
| `can_undo` | Whether a take-back is possible now. |
| `history` | One entry per CCA move: `ply`, `move_number`, `uci`, `san`, `q_opt`, `q_human`, `trap_value`, `stress`, `drive`, `opp_stress`, `chaos`, `think_time`, `compute_s`, `risk_budget`, `reflex`, `restart`. |

### `POST /api/games/<id>/think`

| Field | Content |
|---|---|
| `move` | `{"uci", "san"}` of the move CCA played, or `null` if CCA lost on time. |
| `decision` | The [decision](#decision-object); `null` if CCA's time ran out while it waited for the engine. |
| `compute_s` | Wall time of the decision, in seconds. |
| `reveal_in_ms` | How long the page waits before showing the move (emulated delay), else 0. |
| `state` | The game state after the move. |

### `POST /api/analyse`

This is the position lab. It runs one decision without a game: nothing is stored and no clock
runs.

| Request field | Values | Default |
|---|---|---|
| `fen` (required) | as for `/api/games` | |
| `persona`, `elo_self`, `elo_oppo` | as for `/api/games` | the server's defaults |
| `seed` | as for `/api/games` | `--seed` |

The response is `{"fen", "turn", "persona", "seed", "decision", "compute_s"}`.

### Decision object

| Field | Content |
|---|---|
| `move` | `{"uci", "san"}`. |
| `policy` | `[{"uci", "san", "p"}]`, most probable first. |
| `candidates` | One object per candidate: `uci`, `san`, `q_opt`, `q_human`, `prior`, `opp_entropy`, `sharpness`, `attention`, `trap_value`, and `policy` (`null` when outside the policy). |
| `knobs` | `kl_weight`, `exploit`, `entropy_bonus`, `risk_budget`, `tunnel`, `opp_temperature`, `habit`. |
| `state` | `stress`, `drive`, `opp_stress`, `chaos` (three numbers). |
| `think_time` | Seconds, from the human-like think-time model. |
| `trace` | Diagnostics as numbers, for example `q_opt`, `q_human`, `trap_value`, `risk_bank`, `rpe`, `time_pressure`, `policy_entropy`, `reflex`. Which keys appear depends on the decision. |

The dataclasses behind these fields are documented in [reference.md](reference.md#6-data-model).

### Example with curl

```bash
BASE=http://127.0.0.1:8765
curl -s $BASE/healthz
GAME=$(curl -s -X POST -H 'Content-Type: application/json' -d '{"human_color":"white"}' \
  $BASE/api/games | python -c "import json, sys; print(json.load(sys.stdin)['game_id'])")
curl -s -X POST -H 'Content-Type: application/json' -d '{"uci":"e2e4"}' $BASE/api/games/$GAME/move
curl -s -X POST -H 'Content-Type: application/json' -d '{}' $BASE/api/games/$GAME/think
curl -s $BASE/api/games/$GAME/pgn
```

Without `-H 'Content-Type: application/json'`, curl sends `-d` data as a form, and the server
answers 415.

## Security model

`cca play` has no authentication and no TLS. It is meant to be used from the machine it runs
on. This section describes that local use; [Public mode](#public-mode) lists what changes for a
public demo behind a reverse proxy.

- **Loopback by default.** It binds `127.0.0.1`. Any other `--host` prints this warning to
  standard error:

  ```text
  WARNING: cca play is listening on <address>, which other machines may reach.
  WARNING: there is no authentication: anyone who can reach this port can play, read
  WARNING: every game and keep your CPU busy. ...
  ```

  Bind another address only inside a container whose port is published on `127.0.0.1` (see
  [docker.md](docker.md)), or for a public demo in [Public mode](#public-mode), which prints a
  notice instead of this warning.
- **Host check (DNS-rebinding guard).** Every request's `Host` header must name the server,
  with any port; otherwise the answer is 421.
  - On a loopback bind: only `localhost`, `127.0.0.1`, `[::1]` and the bound address itself.
  - On any other bind (`0.0.0.0` in a container, a LAN address): `localhost`, any IP address,
    and the machine's host name.

  A hostile web page that points its own DNS name at your machine always sends that name, so it
  is refused. A reverse proxy in front of `cca play` must forward `Host` as `localhost`, an IP
  address, or a name given with `--allowed-host` ([Public mode](#host-origin-and-framing)).
- **Cross-site requests are refused (POST).**
  - An `Origin` header other than `http://<Host>` or `https://<Host>` gives 403, and so does
    `Sec-Fetch-Site: cross-site`.
  - Only `application/json` is accepted (415 otherwise), so neither an HTML form nor any other
    "simple" cross-site request can reach the API.
- **Bounded input:**
  - bodies up to 65536 bytes (413 above), with a `Content-Length` (411 otherwise);
  - a 30-second socket timeout per connection;
  - strict JSON;
  - unknown fields rejected;
  - FENs up to 100 characters;
  - seeds limited to a safe character set, because they are written into PGN tags;
  - personas limited to the shipped names, never file paths.
- **Static files** come only from the package's own `static` directory. Paths are checked
  segment by segment, and only a whitelist of file types is served (`.html`, `.js`, `.mjs`,
  `.css`, `.svg`, `.json`, `.txt`, and `LICENSE` files as plain text), each with an explicit
  MIME type.
- **Response headers:** every response carries these headers, and the `Server` header does not
  reveal the Python version:
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; connect-src 'self';
    img-src 'self' data:; style-src 'self'; style-src-attr 'unsafe-inline'; object-src
    'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. Its one relaxation,
    `style-src-attr 'unsafe-inline'`, allows style attributes: the board library positions a
    dragged piece with one, and the piece sprite uses them. `<style>` elements stay
    forbidden.
  - `X-Content-Type-Options: nosniff`.
  - `Referrer-Policy: no-referrer`.
  - `X-Frame-Options: DENY`. With `--frame-ancestor` this header is left out and
    `frame-ancestors` lists the given origins ([Public mode](#host-origin-and-framing)).
  - `Cross-Origin-Opener-Policy: same-origin`.
  - `Cross-Origin-Resource-Policy: same-origin`.
- **Errors are JSON, never HTML,** and client input is never echoed into markup. Tracebacks go
  to the server's standard error only.
- **No external requests.** The page loads only files from the server itself, and
  `connect-src 'self'` enforces this. The vendored JavaScript was audited: its only network
  call is a same-origin request for the piece sprite. The server makes no requests of its own
  either, except that the `maia2` package downloads its weights on first use when they are
  missing.
- **Game ids** are random (`secrets.token_urlsafe(24)`) and never listed. Anyone who can reach
  the port can still create games, read `/api/info` and use CPU time.

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).

## Public mode

`cca play --public` runs the simulator as a public demo on a hosting service, behind a reverse
proxy that terminates HTTPS. It keeps everything described above and adds limits per visitor, a
cap on the time of each decision, a waiting line in front of the engine that serves visitors in
turn, settings for the proxy, and a request log without addresses. The deploy pipelines and the
hosts they target are described in [deploy.md](deploy.md).

> **Status (2026-09-29).** Public mode ships in v0.2.0 (see the [CHANGELOG](../CHANGELOG.md)).
> It is covered by the unit tests in
> [`tests/unit/test_play_public.py`](../tests/unit/test_play_public.py); its first public host
> is the Render demo described in [deploy.md](deploy.md).

### Starting a public server

```bash
cca play --public --host 0.0.0.0 --port 8765 --no-browser --human qre \
  --allowed-host demo.example.org --trusted-proxies 1
```

`demo.example.org` stands for the public host name visitors type; it is a placeholder, not a
deployment.

- `--public` needs a non-loopback `--host` and at least one `--allowed-host`,
  `--log-forwarded-hops` needs `--public`, and `--max-connections-per-peer` needs
  `--trusted-proxies 0` and either `--max-connections` or `--public` (which sets it). Otherwise
  `cca play` exits with status 1 before it opens a port, with one of these lines:

  ```text
  error: --public needs a non-loopback --host (e.g. --host 0.0.0.0 in a container): a loopback address cannot be reached from the Internet
  error: --public needs --allowed-host NAME, the public host name visitors use (e.g. owner-space.hf.space): requests naming another host are refused (DNS-rebinding guard)
  error: --log-forwarded-hops needs --public: only public mode writes a request log
  error: --max-connections-per-peer needs --trusted-proxies 0: behind a proxy every connection has the proxy's address, so it would cap the whole site
  error: --max-connections-per-peer needs --max-connections (or --public, which sets it): only a server that bounds its connections counts them per address
  ```

- Bind a non-loopback address only where a proxy of the hosting platform stands in front of
  the port (a container on a hosting service). On your own machine the
  [Security model](#security-model) above still applies.
- Instead of the exposure warning, the server prints a notice to standard error, each line
  starting with `cca play:`. It lists the address, the host names answered, every limit in
  force (including `--nodes`, and the times after which an unplayed game may go to a
  newcomer), the decision time cap, the fixed per-client bounds, with `--max-connections` a
  "Connections per address:" line (the `--max-connections-per-peer` cap, or why there is none),
  how the client address is found (with a warning when `X-Forwarded-For` is ignored), which
  origins may frame the page, and that one log line per request follows.

### Public-mode flags

| Flag | Without `--public` | With `--public` | Meaning |
|---|---|---|---|
| `--public` | off | | Public mode: requires a non-loopback `--host` and `--allowed-host`, turns on the limits below with the defaults of this column, prints the start-up notice and writes the [request log](#privacy). |
| `--allowed-host NAME` | none | required | Public host name answered in addition to the names of the bind ([Host check](#host-origin-and-framing)), for example the name the hosting service gives the demo. Repeatable; lower-cased; no scheme, port, path or wildcard. |
| `--trusted-proxies N` | `0` | `0` | Reverse proxies in front, 0-8: the client is the `X-Forwarded-For` entry N hops from the right. `0` ignores the header. See [Client address](#client-address). |
| `--log-forwarded-hops` | not allowed | off | Each request-log line also gives `xff=N`, the number of `X-Forwarded-For` entries the request carried (a number, never an address), to find the right `--trusted-proxies` ([Operating](#operating-a-public-instance)). |
| `--frame-ancestor ORIGIN` | none | none | Exact `https://host[:port]` origin allowed to show the page in a frame. Repeatable. Without it no site may frame the page. |
| `--max-sessions-per-client N` | no limit | `3` | Games one client keeps, 1-1024; a new game replaces the client's least recently used one ([Game slots](#game-slots)). |
| `--decisions-per-minute N` | no limit | `20` | CCA decisions (moves and position-lab analyses) per client and minute, 1-600. |
| `--max-queue N` | no limit | `6` | Requests that may wait for the engine while it works, 0-1024; one more gets 503. Waiting clients take turns ([The engine's waiting line](#the-engines-waiting-line)). |
| `--session-idle-minutes N` | no limit | `30` | Games unused this long are dropped, 1-10080 (one week). |
| `--max-connections N` | no limit | `64` | Connections handled at once, one thread each, 1-1024; one more gets 503. It also switches on the bounds on request heads, bodies and waiters ([Threads](#threads-and-request-bodies)). |
| `--max-connections-per-peer N` | no limit | no limit | Connections one address (an IPv6 /64 as one) may have open at once, whatever they are doing, 1-1024; one more gets 503. Only with `--trusted-proxies 0` and `--max-connections` (or `--public`), on a server that clients reach directly; never behind a proxy, even one not trusted: every connection then has the proxy's address, and the cap would hold for the whole site ([Threads](#threads-and-request-bodies)). |
| `--max-think-seconds S` | no limit | `20` | Wall-clock cap of each CCA decision (moves and position-lab analyses), 1-3600 seconds, counted once the decision has the engine ([Decision time cap](#decision-time-cap)). |
| `--reads-per-minute N` | no limit | `60` | Reads of a game's decisions or PGN per client and minute, 1-6000. |

- A value given on the command line replaces the public default.
- Except `--log-forwarded-hops`, these flags also work without `--public`: each limit flag turns
  its own limit on (and `/api/info` then shows `limits`); `--max-connections-per-peer` then
  needs `--max-connections` as well. Only `--public` requires `--allowed-host`, replaces the
  exposure warning by the notice and writes the request log.
- `--max-sessions` (default 16) still caps the games of all clients together, and `--nodes`
  still sets the engine budget of every search; `--public` changes neither.
- An invalid value is refused by the argument parser (exit status 2) with the reason, for
  example `'http://x.org' is not an exact https origin such as https://huggingface.co
  (https://host[:port]: no path, query, user or wildcard)`.
- Fixed bounds (constants in the code, not flags):
  - one client holds at most 2 places in the engine's waiting line
    (`EngineGate.PER_CLIENT_WAITING`), and the line remembers the last turn of at most 256
    clients (`EngineGate.TURN_MEMORY`);
  - with `--max-sessions-per-client`, on a full table, a game in which nothing was played may
    be taken for a newcomer's game after 900 seconds if it is the human's move and the human
    has moved in it (`PlayApp.RECLAIM_THINKING_S`), after 300 seconds otherwise
    (`PlayApp.RECLAIM_UNPLAYED_S`); reading it does not count
    ([Game slots](#game-slots)); the reasons of the last 256 games taken this way are kept for
    their 404 (`PlayApp.TAKEN_REMEMBERED`);
  - with `--max-connections`: a request head within 10 seconds (`PlayHandler.HEAD_S`); when all
    places are taken, a place given up by a connection without a head after 0.5 seconds
    (`PlayServer.HEAD_GRACE_S`), or after 0.05 seconds by one of an address holding more than
    its fair share (`PlayServer.FLOOD_GRACE_S`); up to a quarter more threads, at least 2,
    while connections that gave up their place close (`PlayServer.eviction_slack`), half of
    them for an address holding its fair share or more (`PlayServer.peer_slack`); at most 8 API
    requests in progress per client (`PlayServer.API_REQUESTS_PER_CLIENT`); at most 2 requests
    waiting for one busy game (`PlayApp.GAME_WAITERS`); 10 seconds for a request body
    (`PlayHandler.BODY_S`); at most 2 seconds (`PlayHandler.DRAIN_S`) and 2 places per client
    (`PlayServer.DRAINS_PER_CLIENT`) to read away the body of a refused request; and 2 seconds
    of `Retry-After` for a connection refused at once (`PlayServer.FULL_RETRY_AFTER_S`);
  - each rate table (decisions, reads) remembers at most 4096 clients (`MAX_TRACKED_CLIENTS`).

The generated flag table is in [reference.md](reference.md#cca-play).

### Limits and refusals

A request refused by a limit gets a JSON error that names the limit (`"limit"`) and a
`Retry-After` header in whole seconds, which is a hint, not a promise. "A client" is the key
described under [Client address](#client-address). The numbers in the messages below are
examples.

| Status | `limit` | When | Message | `Retry-After` |
|---|---|---|---|---|
| 429 | `decisions_per_minute` | The client asked for more CCA decisions (`/think` and `/api/analyse`) than `--decisions-per-minute` allows. Each client has a bucket of that many tokens, refilled evenly over a minute, and each decision takes one. | `slow down: at most 20 CCA decisions per minute; try again in 3 s` | Until one token is back. |
| 429 | `reads_per_minute` | The client read a game's decisions (`/decisions`) or PGN (`/pgn`), the responses that grow with the game, more often than `--reads-per-minute` allows (a bucket as above). | `slow down: at most 60 reads of a game's decisions or PGN per minute; try again in 1 s` | Until one token is back. |
| 429 | `requests_at_once` | The client already holds 2 places in the engine's waiting line, or, with `--max-connections`, has 8 API requests in progress. | `too many requests at once: wait for your previous ones; try again in 4 s` | The engine line's estimate (below); 1 s for the 8-request bound. |
| 503 | `max_queue` | `--max-queue` requests already wait for the engine (with `0`: the engine is busy). | `busy: CCA is thinking for other players (6 waiting); try again in 21 s` | Estimate: the moving average of recent engine hold times (2 s before the first) times the requests waiting plus one, 1-300 s. |
| 503 | `max_connections` | `--max-connections` connections are already being handled and none of them gives its place up ([Threads](#threads-and-request-bodies)). | `busy: 64 connections are open; try again in 2 s` | 2 s. |
| 503 | `max_connections_per_peer` | With `--max-connections-per-peer`: the connection's address already has that many connections counted, whatever they are doing. Checked first, before any place is given up. | `too many connections from your address: at most 4 at once; try again in 2 s` | 2 s. |
| 503 | `max_sessions` | A new game finds all `--max-sessions` slots taken and no game may be dropped for it ([Game slots](#game-slots)). | `all 12 game slots are in use; try again in 180 s` | Until the first game may be reclaimed (300 or 900 s after it was last played, by the rules of [Game slots](#game-slots); never a game with a `/think` in progress) or the least recently used game expires (`--session-idle-minutes`), whichever comes first; 60 s without either rule; 1-3600 s. |
| 409 | `game_busy` | With `--max-connections`: 2 requests already wait for this game while a `/think` holds it. | `this game is busy with another request (CCA is thinking); try again in 4 s` | The engine line's estimate. |

- **Decision tokens.** A decision the server could not make (refused by the engine line with
  429 or 503, or an engine error, 503) gives its token back. A 409, such as `/think` when it is
  your turn or `game_busy`, still costs one. Creating a game costs none.
- **Other answers of public mode:** 408 when a request body does not arrive within 10 seconds
  (with `--max-connections`); 404 for a game that is gone, and, while the server shuts down,
  503 `cca play is shutting down; try again in 5 s` (no `limit`) for each request that waits
  for the engine or asks for it later (with `--max-queue`). The 404 says why the game is gone:
  - for a game taken for someone else's new game ([Game slots](#game-slots)), one of

    ```text
    no such game any more: every game slot was in use and this game went to a newcomer, as it had ended
    no such game any more: every game slot was in use and this game went to a newcomer, as its player had 3 games, the most one player may keep
    no such game any more: every game slot was in use and this game went to a newcomer, as nothing had been played in it for 5 minutes
    ```

    The last says 15 minutes for a game on the move of a player who has moved in it; it
    starts `CCA had not played in it` instead for a game in which the player has moved but CCA
    has not decided yet (rule 3), and adds `, nor in the older game of its player's it
    replaced,` before `for` for a game that kept the time of the game it replaced (rule 4).
    The reason is remembered for the last 256 games taken this way;
  - otherwise, with `--session-idle-minutes`,
    `no such game (unused for 30 minutes, replaced by a newer game, or the server restarted)`,
    also for a game its own client's new game replaced; without it,
    `no such game (evicted, or the server restarted)`.
- **Engine budget.** No request can raise the engine's search budget: the API has no field that
  reaches it, and every engine call keeps the `--nodes` limit.

#### The engine's waiting line

A CCA move, a position-lab analysis and the creation of a game all need the one engine. With
`--max-queue`, the requests that wait for it form one line, served by client in turn: each time
the engine is taken, the next turn goes to the waiting client whose last turn is the oldest (a
client never served comes first; arrival order breaks ties). A client that keeps its two places
filled therefore gets one decision in turn with every other waiting client, and a newcomer waits
at most for the decision running and the turn already given. A request counts as waiting as
soon as it would have to wait, also in a burst on a free engine. Without `--max-queue` the
engine is a plain lock, as in local use.

#### Decision time cap

With `--max-think-seconds` S (20 in public mode), each CCA decision, a move or a position-lab
analysis, gets a wall-clock deadline S seconds after it takes the engine; the time it waited in
line does not count. In a timed game the earlier of this deadline and the clock's own applies.
CCA meets it as it meets a clock ([Clock rules](#clock-rules)): each engine call gets a share of
the time left in addition to the node limit, the look-ahead is cut short when time runs out, and
with less than `fast_budget` (0.6 s by default) available it plays in reflex mode.

- When a share of time ends before the node limit is reached, the decision depends on timing: it
  is not reproducible, and a replay with the same seed can differ
  ([Seeds and commit-reveal](#seeds-and-commit-reveal)). Local use has no cap.
- When the server shuts down with any limit on, a running decision is asked to stop early; with
  `--max-queue` (on in public mode), the requests still waiting for the engine are also refused
  at once (503).

### Game slots

With `--max-sessions-per-client` *q*, a new game of a client frees its slot by these rules:

1. A client that already has *q* games loses its own least recently used one.
2. While all `--max-sessions` slots are taken, the game dropped is, in this order: the client's
   own least recently used game; then, of the other games, never one with a `/think` in
   progress (waiting for the engine or deciding): the least recently used finished game of
   anyone; the least recently used game of a client that holds *q* games (its whole share); the
   game of anyone in which nothing was played for 900 seconds (`PlayApp.RECLAIM_THINKING_S`) if
   it is the human's move and the human has moved in it, for 300 seconds
   (`PlayApp.RECLAIM_UNPLAYED_S`) otherwise, the one unplayed the longest first.
3. A game is *played* when it is created (but see rule 4), each time CCA makes a decision in it,
   and, once CCA has made a decision in it, each time its player makes a move that takes the
   game further than it had ever been. A player's move before CCA's first decision in the game
   (a first move, which costs no engine time; the page asks for CCA's reply at once, and that
   decision counts), a move played again after a take-back, take-backs themselves,
   resignations, reads of the game (its state, PGN or decisions) and refused requests do not
   count.
4. A new game that replaces a game of its own client (rule 1, or the first choice of rule 2)
   keeps that game's time: it may be taken when that game could have been, or when a game
   created now could be if that is sooner (at once if that game could be taken already). So
   starting new games, and making a first move in each, never keeps a slot; only play that
   costs engine time does.
5. If no game qualifies, the new game is refused with 503 (`max_sessions`). The check runs
   before the new game's agent is built, so a refusal costs no engine time.

So, on a full table, an unfinished game of a client below its share is taken for someone
else's new game only when nothing was played in it for 15 minutes while it is its player's
move (once the player has moved in it), or for 5 minutes before the player's first move or
while CCA's move is not being made: not asked for, or asked for and refused by a limit (the
page asks again by itself, but only a decision made counts; while a request for it waits for
the engine or decides, the game is not taken). Before CCA's first decision in a game, its 5
minutes run from its creation (or from the time rule 4 gave it), whatever the player does.
The 404 then gives the reason
([Limits and refusals](#limits-and-refusals), [A dropped game](#what-a-visitor-sees)).

These rules count games by client key. Visitors who share a key (behind a proxy with
`--trusted-proxies 0`, or with N too small: [Client address](#client-address)) are one client
to them: together they hold at most *q* games, and once they hold *q*, rule 1 lets a new game
of any of them drop the least recently used of their games at once, with free slots and while
it is in play ([Shared quotas](#residual-risks)).

With `--session-idle-minutes`, a game that no request has touched for that long is dropped.
For this rule every request for a game touches it, reading its state included. Without a
per-client limit, the least recently used game is dropped, as in local use.

### What a visitor sees

The page words each refusal in the visitor's language (the strings are in
[`static/js/i18n.js`](../src/cca/play/static/js/i18n.js)). A wait is shown in whole seconds
below two minutes, else in minutes rounded up.

| Refusal | Message on the page (English) |
|---|---|
| `decisions_per_minute`, or any 429 without a known limit | "Slow down a little: this public demo allows a limited number of CCA moves per minute. Try again in *wait*." |
| `reads_per_minute` | "Slow down a little: this public demo limits how often a game's moves and CCA's reasons are downloaded. Try again in *wait*." |
| `requests_at_once` | "You already have requests waiting for CCA. Try again in *wait*, once they are done." |
| `max_queue`, `max_connections`, or a 503 with `Retry-After` and no known limit | "The CCA server is busy with other players' games. Try again in *wait*." |
| `max_connections_per_peer` | "Too many connections from your network to this public demo (other tabs or devices?). Try again in *wait*." |
| `max_sessions` | "No free game slot: all games this public demo can hold are in use. Try again in *wait*." |
| `game_busy` | "This game is still busy with an earlier request (CCA is thinking, perhaps in another tab). Try again in *wait*." |

- **CCA's move is asked for again by itself.** When the request for CCA's move is refused by a
  limit (a `limit` and a `Retry-After`), the page waits and asks again:
  - each wait is the server's `Retry-After`, bounded to 1-60 seconds;
  - it keeps asking for up to 15 minutes after the first refusal in a row (a wait that would
    end later is not started);
  - meanwhile the status line reads "CCA's move is waiting for the busy server; the page asks
    again shortly.", and a message says "CCA cannot move yet: this public demo is busy or
    limits CCA moves per minute. The page asks again by itself in *wait*.";
  - a new game or a take-back cancels the wait.
- **Timed games.** CCA's clock keeps running while its move waits, as it does while CCA waits
  for the engine ([Clock rules](#clock-rules)). A long wait can make CCA lose on time.
- **"Ask CCA to move".** When CCA is to move and nothing is asking for its move (15 minutes of
  refusals have passed, or the request failed for another reason: an engine error, a server
  shutting down, a lost connection), the Game card shows "CCA's move did not arrive." with a
  button **Ask CCA to move**, and the page's live region announces it once, without moving the
  focus. The button asks once more; a new refusal by a limit starts a new 15-minute period of
  automatic retries.
- **Nothing else is retried by itself.** A refused move of yours is shown as a message and the
  piece goes back; a refused take-back, resignation, position-lab analysis or PGN copy is shown
  as a message. The New game dialog shows the sentence of the table instead of the server's
  message.
- **The first game.** When the page opens without a game to resume and the new game is refused
  by a limit with a `Retry-After` of at most 60 seconds (a busy engine, say, or a full table
  whose next slot frees soon), the page tries again by itself for up to 15 minutes, and the
  status line gives the refusal's sentence from the table followed by "No game is open yet:
  the page tries again by itself in *wait*." A wait over 60 seconds (a full table often gives
  one) or another failure ends the status line with "No game is open: "New game" starts one."
  instead.
- **A dropped game.** A game dropped by idle expiry, by the rules of [Game slots](#game-slots)
  or by a restart answers 404. A reload then starts a new game, as in local use.

### Behind a reverse proxy

`cca play` has no TLS and no authentication in public mode either: the proxy of the hosting
platform terminates HTTPS, and anyone who reaches the page can play. Everything under
[Security model](#security-model) still holds, with the changes below.

#### Client address

The per-client limits count requests under a key derived from the client's address:

- **`--trusted-proxies 0`** (the default): the TCP peer. `X-Forwarded-For` is ignored. Behind a
  proxy the peer is the proxy, so every visitor who comes through the same proxy address shares
  one set of quotas, games included ([Shared quotas](#residual-risks)); the start-up notice
  says so. This over-limits, but no visitor can choose its own key.
- **`--trusted-proxies N`**: the `X-Forwarded-For` entry N hops from the right, all header lines
  read in order as one list. Each proxy appends the address it received the request from, so
  only the N rightmost entries were written by the trusted proxies; entries further left come
  from the client and may be forged.
  - Set N to exactly the number of proxies that append to the header. Too small: the key is a
    proxy's address, and visitors share quotas. Too large: a visitor chooses its own key by
    sending the header, and escapes its limits.
  - When the header is missing, has fewer than N entries, or that entry is not a bare IP
    address (a port, `unknown`, a name), the TCP peer is used.
- `X-Real-IP`, `Forwarded` and `X-Forwarded-Host` are never read.
- An IPv6 client is counted by its /64 block, the block a subscriber usually gets, so rotating
  addresses inside it does not multiply a quota. An IPv4-mapped IPv6 address counts as its IPv4
  address.

#### Host, origin and framing

- **Host check.** The `--allowed-host` names are answered in addition to the names of a
  non-loopback bind: `localhost`, any IP address and the machine's host name. Any other `Host`
  gets 421, so the DNS-rebinding guard stays on. A platform whose health checks send the public
  name as `Host` needs that name as an `--allowed-host`, or its checks get 421.
- **POST origin check.** Besides `http://<Host>` and `https://<Host>`, the origin
  `https://<name>` of every `--allowed-host` is accepted, for proxies that rewrite `Host`.
  `Sec-Fetch-Site: cross-site` still gets 403, and bodies must still be `application/json`.
- **Framing.** Without `--frame-ancestor` the page cannot be framed: CSP `frame-ancestors 'none'`
  and `X-Frame-Options: DENY`. With it, the CSP says `frame-ancestors` followed by the given
  origins, and `X-Frame-Options` is left out, because its `ALLOW-FROM` form is obsolete and
  `DENY` would still block the frame. The rest of the Content-Security-Policy is unchanged.

#### Threads and request bodies

With `--max-connections` (on in public mode) the server bounds its threads:

- at most that many connections are handled at once, one thread each. A connection that gave
  up its place (below) keeps its thread until it notices, within 0.5 seconds, so briefly up to
  a quarter more threads run (at least 2; 16 at the default 64). A newcomer whose address holds
  its fair share of the places or more (the bound divided by the number of addresses holding
  places, its own counted) may use only half of that extra (8 at the default 64): addresses
  that flood a full server, one or a few, churn their own places and leave the rest of the
  extra to the others;
- a connection's request head (request line and headers) must arrive within 10 seconds of the
  connection's start, however slowly it trickles in; otherwise the connection is closed without
  an answer;
- when every place is taken, a connection that has waited more than 0.5 seconds for its head
  gives its place to the newcomer (it is closed within 0.5 seconds), so idle or trickling
  connections cannot keep others out. Failing that, for a newcomer from another address, the
  address holding the most places gives up its oldest connection that has waited 0.05 seconds
  for its head, with nothing arrived that its thread has yet to read (so a head a proxy sends
  at once is never cut off), if that address holds more than its fair share (the newcomer's
  address counted among those holding places) and at least 2 places more than the newcomer's
  address. So addresses that replace their connections faster than they grow stale cannot keep
  everyone else out. Without such a connection, or while the extra threads above are taken by
  connections closing, the newcomer is answered 503 (`max_connections`) at once by the
  accepting thread; one helper thread keeps the refused connection open for up to 2 seconds
  and discards what the client still sends, so that the client can read the 503;
- with `--max-connections-per-peer` N (off unless given, also in public mode; only with
  `--trusted-proxies 0`), one address (an IPv6 /64 as one) has at most N connections counted at
  once, whatever they are doing: a head on its way, a body, or an answer it reads slowly (each
  write waits at most 30 seconds, `PlayHandler.timeout`). Connections that gave up their place
  and have yet to close count too. One more is answered 503 (`max_connections_per_peer`) at
  once, in the same way. Only a connection still waiting for its head gives its place to a
  newcomer, so this cap is what keeps one address from holding every thread past its heads. It
  is for a server that clients reach directly: behind a proxy every connection has the proxy's
  address, trusted or not, and the cap would hold for the whole site;
- the route and the method are checked before any body is read (404 and 405 at once);
- one client has at most 8 API requests in progress (one more: 429), and each body must arrive
  within 10 seconds (408 otherwise);
- the unread body of a refused request is read away for at most 2 seconds, within the client's
  own places; beyond them only what has already arrived is discarded;
- at most 2 requests wait for a game that a `/think` holds (one more: 409 `game_busy`);
- the server speaks HTTP/1.0: one request per connection, so no connection idles between
  requests.

#### Residual risks

- **Game slots.** Clients below their share keep the games they keep playing, so the table
  can be held in two ways; newcomers then get 503 (`max_sessions`) for as long as it lasts:
  - **by playing:** `--max-sessions` / (*q* - 1) addresses, each below its share with *q* - 1
    games (6 addresses for the Render Blueprint's 12 slots at the default *q* = 3; an IPv6 /56
    holds 256 /64 blocks), play each game: a CCA decision, then within 900 seconds a move of
    their own, then within 300 seconds the next CCA decision. One decision and one move per
    game every 1,200 seconds (300 + 900) are enough. That costs engine time, which other
    visitors wait for in the engine's line, and a small part of each address's
    `--decisions-per-minute` budget; the moves cost nothing, and reading the games holds
    nothing;
  - **without engine time:** `--max-sessions` + 1 addresses each start a game as soon as one
    may be reclaimed, each from an address whose own game has just gone, so that the new game
    counts as played and is kept 300 seconds: one new game per slot every 300 seconds keeps
    most newcomers out. Restarting one's own games, or making a first move in them, holds
    nothing (rules 3 and 4 of [Game slots](#game-slots)).

  The defence is a table larger than an attacker can fill: `--max-sessions` above the number
  of addresses it can use without engine time, or *q* - 1 times that with engine time.
- **Connections.** One address, or a few, cannot keep others out with idle or trickling
  connections ([Threads](#threads-and-request-bodies)). Many addresses, each holding about its
  fair share of the places (a few dozen at the default 64, with a place or two each), can, and
  so can one client behind a proxy or port forwarder that passes idle connections through
  (everyone there shares its address). Against those only a front that buffers request heads
  helps: a hosting platform's edge or a reverse proxy. Only a connection still waiting for its
  head gives its place up, so without `--max-connections-per-peer` nothing keeps one address
  from holding places past its heads, for example by reading the answers slowly (each write may
  wait up to 30 seconds); that cap is only for a server that clients reach directly.
- **Creating games** has no rate limit of its own: it costs no decision token. It is bounded by
  `--max-sessions-per-client` and waits in the engine line.
- **Shared quotas.** With `--trusted-proxies 0` behind a proxy, or N too small, the key is a
  proxy's address, and every visitor who comes through that address is one client (possibly
  all visitors). The Render Blueprint ships this way until the client address is
  [calibrated](deploy.md#calibrating-the-client-address). Every per-client bound then holds for
  all of them together:
  - **games:** at most `--max-sessions-per-client` games (3 in public mode) for all of them,
    whatever `--max-sessions` is. Once they hold that many, a new game of any of them drops
    the least recently used of their games at once (rule 1 of [Game slots](#game-slots)), with
    free slots and while it is in play, so the protection of a game in play (5 or 15 minutes
    without play, rule 2) does not apply; the visitor whose game it was gets 404
    ([A dropped game](#what-a-visitor-sees));
  - **decisions and reads:** one `--decisions-per-minute` and one `--reads-per-minute` budget
    for all of them, so one busy visitor can use up the decisions of everyone;
  - **the engine's line:** 2 waiting places for all of them (besides the decision running),
    served in arrival order rather than in turn; one more request while both are taken gets
    429 `requests_at_once`, whose message ("You already have requests waiting for CCA") speaks
    of requests that may be another visitor's;
  - **requests in progress:** at most 8 API requests for all of them (with
    `--max-connections`, on in public mode; 429 beyond);
  - **connections:** `--max-connections-per-peer`, if it were set behind such a proxy, would
    hold for all of them together, which is why it is only for a server that clients reach
    directly.
- **Game ids are bearer secrets.** Anyone who knows a game's id can read and play it. Ids are
  random, never listed and never logged.
- **The proxy.** How the platform's proxy buffers slow clients, and which headers it adds (for
  example CORS headers, which `cca play` never sends itself), is outside `cca play`. What was
  observed on Hugging Face Spaces is in [deploy.md](deploy.md).
- **Load.** Public mode was not load-tested ([Limits](#limits)).

### Privacy

- **No address is written anywhere.** The client key (an IPv4 address, an IPv6 /64 prefix, or
  the TCP peer when neither applies) lives only in the server's memory:
  - in the two rate tables (decisions, reads), as a token count and a time per client, for at
    most 4096 clients each; when a table grows beyond that, it is pruned to 3072 clients:
    clients whose budget is full again go first, then the least recently active ones;
  - in the engine's waiting line: the places of waiting clients, and the last turn of at most
    256 clients;
  - as the owner of each game, for as long as the game is kept;
  - in the counters of requests in progress, until each request ends;
  - with `--max-connections`, as the TCP peer (an IPv6 /64 prefix, or the IPv4 address) of each
    connection counted, until the connection ends. Behind a proxy that is the proxy's
    address.

  Nothing of it is written to disk, and a restart forgets all of it. No response contains it;
  `/api/info` shows only the limits and the number of requests waiting.
- **Request log.** Public mode writes one line per request to standard error: UTC time, method,
  route template, status and duration, for example
  `2026-09-29T08:15:02Z POST /api/games/:id/think 200 2731ms`. The line has no address, no game
  id (`:id` stands for it), no query string, no header and no body. `-` stands for a value that
  does not exist (a connection refused by `--max-connections` or `--max-connections-per-peer` is
  logged as `- - 503 -ms`), and
  ` aborted` marks a request whose client went away before its answer. With
  `--log-forwarded-hops` the line ends with `xff=N`, the number of `X-Forwarded-For` entries,
  never an entry (`xff=-` when no request head was read).
- **Other output.** The standard library's own access log, which includes the address, is
  never written, in any mode. In public mode the report of an unexpected error leaves out the
  client address too.
- **Games** (moves, seeds, decisions) are kept in memory like every game and are gone after a
  restart.
- **In the browser**, the page keeps the game id per tab and the New game dialog's choices, as
  in local use. The server sets no cookies.
- **Outside `cca play`:** the hosting platform and its proxies see every visitor's address and
  may keep logs of their own. `cca play` does not control them; the platform's own policy
  applies.

### Operating a public instance

- **Run one instance.** Games, quotas and the engine's waiting line live in the memory of one
  process. Do not run several instances or replicas behind a load balancer: a game created on
  one instance is unknown (404) to the others, and each instance would count its own limits.
- **Restarts lose every game.** A redeploy, a restart or a host that stops idle instances (as
  free tiers do) drops all games in progress; the page then starts a new game. A shutdown
  (SIGTERM, as a platform sends it) refuses the waiting requests at once and stops a running
  decision early.
- **`--human qre` on a small host.** Maia-2 needs PyTorch (the `-maia2` image) and downloads
  its checkpoint (about 280 MB) on first use; on a host without persistent disk it does so again
  after every restart.
- **Speed.** One engine serves every visitor in turn, and each decision costs several engine
  searches of `--nodes` nodes. On the development machine a decision at the default 200,000
  nodes took a few seconds ([Limits](#limits)); a smaller host is slower, by an amount this guide
  does not measure. `--max-think-seconds` bounds each decision's search time and `--max-queue`
  the length of the line. A lower `--nodes` or cap makes decisions faster but measures `q_opt`
  more coarsely, which changes CCA's play: a demo's decisions are not comparable with research
  runs made with other settings.
- **Memory.** `--max-sessions` caps the games of all visitors together, and `--hash` sets the
  engine's table size. The memory one game needs was not measured.
- **Finding `--trusted-proxies`.** When the platform does not document how many proxies append
  to `X-Forwarded-For`, start with `--trusted-proxies 0 --log-forwarded-hops`, use the page, and
  read the `xff=N` of the page's own requests (`/api/...`) in the log, not of `/healthz` (health
  checks may come from inside the platform). The smallest N seen is the number of proxies: set
  `--trusted-proxies` to it and restart. The start-up notice's "Client address" line shows the
  setting in force. Until then visitors share their limits, games included
  ([Shared quotas](#residual-risks)): calibrate before announcing the demo.
- **Health checks.** `/healthz` answers a `Host` that is an IP address, `localhost` or an
  `--allowed-host`. When all `--max-connections` connections are taken, a health check can get
  the 503 too; allow for that in the platform's health-check settings, or raise the limit.
- **Watching.** The request log shows whether the limits bite: many 429 and 503 lines mean
  visitors are being refused. If the demo is overwhelmed, stop or pause it; only games in
  progress are lost.

## Vendored browser files and licences

The page's third-party files are copied into
[`src/cca/play/static/vendor/`](../src/cca/play/static/vendor/) and served from there. Nothing
is loaded from a CDN.

| Package | Version | Licence | Used for |
|---|---|---|---|
| [cm-chessboard](https://github.com/shaack/cm-chessboard) | 8.14.2 | MIT, © 2017 Stefan Haack | Board, move input, markers, arrows and the promotion dialog. |
| [chess.js](https://github.com/jhlywa/chess.js) | 1.4.0 | BSD-2-Clause, © 2025 Jeff Hlywa | Move legality and SAN in the browser (the server stays authoritative). |
| Cburnett chess pieces ([Wikimedia Commons](https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces/Standard_transparent)) | Commons revisions downloaded 2026-09-27 | BSD-3-Clause, one option of the author's GFDL/BSD/GPL multi-licence; Colin M.L. Burnett and Commons contributors | Piece sprite, assembled from the 12 Commons files. |

- **Not vendored:** cm-chessboard's own piece sprites (CC BY-SA 3.0 / CC BY-NC-SA 4.0) and its
  markers sprite (CC BY-SA 3.0). CCA draws its own markers and icons, which are Apache-2.0
  like the rest of the interface.
- [`MANIFEST.json`](../src/cca/play/static/vendor/MANIFEST.json) lists every vendored file with
  its source URL, size, SHA-256 and licence. The tests re-hash every file against it and fail on
  any unlisted file.
- Each package's licence file ships next to it, and the page's About panel credits all three
  with links.
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) covers the rest of CCA's third-party
  components, including Stockfish and Maia-2.

## Limits

- **Sessions:** at most `--max-sessions` games (default 16) are kept, in memory only. The least
  recently used game is dropped first, and a restart loses every game.
- **One shared engine, decisions in series:**
  - One Stockfish process and one human model serve every game and the position lab.
  - A global lock serialises every decision, so a decision waits for any other one running.
  - In a timed game that wait counts against CCA's clock.
  - Every decision starts with `ucinewgame` so that replays do not depend on other games. This
    was measured to cost about 25 ms per decision at 256 MB hash and 1 thread.
- **Decision time:** on the development machine, a decision at the default 200,000 nodes, one
  thread and the QRE model took a few seconds (2.7 to 5.9 s of wall time in the build
  measurements). This is an observation, not a benchmark.
- **Browsers:** tested in a Chromium-based browser only. Whether Firefox and Safari honour
  `style-src-attr` was not checked. Screen readers were not tested.
- **Load:** many simultaneous sessions were not load-tested. The concurrency tests ran up to
  ten parallel requests.
- **Exposure:** without public mode, the server is not built for exposure beyond your own
  machine (see [Security model](#security-model)). A public demo needs
  [Public mode](#public-mode), whose own known limits are listed under
  [Residual risks](#residual-risks).
