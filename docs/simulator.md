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
[Security model](#security-model) ·
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
  also depend on the clock, through the deadlines and the time-sliced search. This was checked
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
  is still being revealed), the body also carries `"state"`.

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
| 408 | The request body did not arrive in time. |
| 411 | No `Content-Length`, or chunked transfer encoding (POST). |
| 413 | Body larger than 65536 bytes. |
| 415 | `Content-Type` is not `application/json` (POST). |
| 421 | `Host` header not accepted (DNS-rebinding guard). |
| 500 | Internal error; the details go to the server's standard error, not to the client. |
| 501 | Unsupported method (for example `PUT`, `OPTIONS`, `HEAD`). |
| 503 | The engines are still warming up, or an engine error occurred. |

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
on.

- **Loopback by default.** It binds `127.0.0.1`. Any other `--host` prints this warning to
  standard error:

  ```text
  WARNING: cca play is listening on <address>, which other machines may reach.
  WARNING: there is no authentication: anyone who can reach this port can play, read
  WARNING: every game and keep your CPU busy. ...
  ```

  Bind another address only inside a container whose port is published on `127.0.0.1` (see
  [docker.md](docker.md)).
- **Host check (DNS-rebinding guard).** Every request's `Host` header must name the server,
  with any port; otherwise the answer is 421.
  - On a loopback bind: only `localhost`, `127.0.0.1`, `[::1]` and the bound address itself.
  - On any other bind (`0.0.0.0` in a container, a LAN address): `localhost`, any IP address,
    and the machine's host name.

  A hostile web page that points its own DNS name at your machine always sends that name, so it
  is refused. A reverse proxy in front of `cca play` must forward `Host` as `localhost` or an
  IP address.
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
  - `X-Frame-Options: DENY`.
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
- **Exposure:** the server is not built for exposure beyond your own machine (see
  [Security model](#security-model)).
