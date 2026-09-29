# CCA in chess GUIs, lichess-bot and Docker

CCA (Chaotic Chess Algorithm) is a UCI engine, so any program that can add a UCI engine can
run it. This page covers four things: which command to give the program; how to set CCA up in
seven GUIs and in lichess-bot; how to run it from the container image; and what to check when
something goes wrong. Every option and CLI argument is defined in the generated
[technical reference](reference.md). This page links to it rather than repeating it.

**How far each part was tested.** Every step marked **UNVERIFIED** rests only on third-party
reports.

| Part | Status |
|---|---|
| `cca-uci` and `cca uci`: handshake, options, moves, error messages | Run for this page on Windows 11, from a wheel install and from a source checkout |
| lichess-bot's engine layer (python-chess 1.11.2) | Run: the same calls lichess-bot makes, without connecting to Lichess |
| Cute Chess, Arena, En Croissant, BanksiaGUI, Scid vs. PC, ChessBase/Fritz | Not run. The steps come from each GUI's documentation or source code |
| Docker wrappers | Not run: no Docker engine was available while this page was written. The commands follow the [`Dockerfile`](../Dockerfile) and Docker's documentation |
| The launcher on Linux and macOS | Not run locally. CI's Linux job has a test that runs the generated launcher over real pipes |

## Contents

1. [Before you start](#1-before-you-start)
2. [Which command to use](#2-which-command-to-use)
3. [Options and engine output](#3-options-and-engine-output)
4. [Cute Chess (GUI and cutechess-cli)](#4-cute-chess)
5. [Arena](#5-arena)
6. [En Croissant](#6-en-croissant)
7. [BanksiaGUI](#7-banksiagui)
8. [Scid vs. PC](#8-scid-vs-pc)
9. [ChessBase and Fritz](#9-chessbase-and-fritz)
10. [lichess-bot](#10-lichess-bot)
11. [Docker](#11-docker)
12. [The browser board instead of a GUI](#12-the-browser-board-instead-of-a-gui)
13. [Troubleshooting](#13-troubleshooting)
14. [How this page was checked](#14-how-this-page-was-checked)

## 1. Before you start

### 1.1 Install CCA

Download `cca_chess-0.2.0-py3-none-any.whl` from the
[v0.2.0 release](https://github.com/DonQuaan/CCA/releases/tag/v0.2.0). Install it into its own
virtual environment. CCA needs Python 3.11 or newer. `C:\cca\venv` and `~/cca/venv` below are
example paths.

Windows (Command Prompt):

```bat
python -m venv C:\cca\venv
C:\cca\venv\Scripts\python -m pip install cca_chess-0.2.0-py3-none-any.whl
```

Linux and macOS:

```sh
python3 -m venv ~/cca/venv
~/cca/venv/bin/python -m pip install cca_chess-0.2.0-py3-none-any.whl
```

With uv, `uv tool install ./cca_chess-0.2.0-py3-none-any.whl` installs the `cca` and `cca-uci`
executables into uv's tool folder instead ([section 2](#2-which-command-to-use) shows where that
is). A source checkout set up with `uv sync` has both launchers in `.venv` (see the
[README](../README.md)).

**Maia-2 is optional.** Without it, CCA uses the QRE human model and says so in an
`info string`. The `maia2` extra supports Python 3.10 to 3.12 only, so on Python 3.13 it
installs nothing. On Windows the PyPI `torch` wheel is CPU-only. The [README](../README.md)
gives the tested recipe with the CUDA wheel.

### 1.2 Install Stockfish 19

A pip or uv install does **not** include Stockfish; only the container images do
([section 11](#11-docker)). Download Stockfish 19 for your platform. The pinned release assets
and their SHA-256 are listed in [reference §2.1](reference.md#21-stockfish-release-assets).
Then tell CCA where it is.

CCA looks for Stockfish in this order (`find_stockfish` in `src/cca/engines/stockfish.py`):

| # | Source | If it is wrong |
|---|---|---|
| 1 | UCI option `StockfishPath`, set in the GUI | A value that is not a file is rejected, and the engine already in use is kept |
| 2 | Environment variable `CCA_STOCKFISH` | An error. CCA does not fall back to the next source |
| 3 | `<checkout>/engines/stockfish-*/…/stockfish-*` (`.exe` on Windows) | Used only by a source checkout, where [`scripts/fetch_stockfish.py`](../scripts/fetch_stockfish.py) puts it. CCA never searches the current directory |
| 4 | `stockfish` on `PATH` | |

**Set `StockfishPath` in the GUI.** That is the most reliable choice. A GUI started from the
desktop does not see variables you set in a terminal. On Windows, a user variable set with
`setx CCA_STOCKFISH "C:\path\to\stockfish.exe"` reaches programs started after it, so restart
the GUI afterwards.

To check, run `cca doctor` (add `--stockfish <path>` to test an explicit path). It prints
`engine ok: Stockfish 19 (<path>)`, or `engine FAIL: ...` and exits with code 1.

## 2. Which command to use

CCA has two entry points to the same UCI engine:

| Entry point | Arguments | Use it when |
|---|---|---|
| **`cca-uci`** | None. Any arguments are ignored | The program takes only a path to an executable: Cute Chess GUI, En Croissant, ChessBase/Fritz, BanksiaGUI, lichess-bot. It also works everywhere else |
| `cca uci` | `uci` | The program has an arguments field: Arena, Scid vs. PC, cutechess-cli (`arg=uci`) |

`cca` without `uci` prints its usage text and exits with code 2, so the GUI reports that the
engine failed to start.

Where to find the launchers:

| Install | `cca-uci` | `cca` |
|---|---|---|
| venv, Windows | `<venv>\Scripts\cca-uci.exe` | `<venv>\Scripts\cca.exe` |
| venv, Linux/macOS | `<venv>/bin/cca-uci` | `<venv>/bin/cca` |
| Source checkout (`uv sync`) | `<checkout>\.venv\Scripts\cca-uci.exe` (Linux/macOS: `<checkout>/.venv/bin/cca-uci`) | Same folder |
| `uv tool install` | The folder printed by `uv tool dir --bin` | Same folder |

If you set nothing, uv puts tool executables in the first of these that applies
([uv storage docs](https://docs.astral.sh/uv/reference/storage/)):

- `$XDG_BIN_HOME`, then `$XDG_DATA_HOME/../bin`, then `$HOME/.local/bin`.
- On Windows: `%XDG_BIN_HOME%`, `%XDG_DATA_HOME%\..\bin`, `%USERPROFILE%\.local\bin`.

`UV_TOOL_BIN_DIR` overrides this, so ask `uv tool dir --bin` rather than guessing. To print a
launcher's full path from an activated environment, run `where cca-uci` (Windows) or
`command -v cca-uci` (Linux/macOS).

**A launcher only works in its own environment.** A pip- or uv-generated `cca-uci` stores the
absolute path of its environment's Python. So if you move or rename the venv, recreate it
instead of pointing the GUI at the moved copy. Arguments are ignored on purpose, so lichess-bot
can append its `--key=value` `engine_options` without breaking the launch.

### 2.1 Check the handshake

Before you open a GUI, run the handshake in a terminal:

| Shell | Command |
|---|---|
| Command Prompt | `(echo uci& echo isready& echo quit) \| "C:\cca\venv\Scripts\cca-uci.exe"` |
| PowerShell | `'uci','isready','quit' \| & 'C:\cca\venv\Scripts\cca-uci.exe'` |
| Linux/macOS | `printf 'uci\nisready\nquit\n' \| ~/cca/venv/bin/cca-uci` |

This is the exact output of the repository's `.venv\Scripts\cca-uci.exe` (CCA 0.1.0, Windows 11,
2026-09-28, without Maia-2 installed). A wheel install advertises the same 16 options.

```text
id name CCA 0.1.0
id author Nguyen Vu Dong Quan (DonQuaan)
option name StockfishPath type string default <auto>
option name Threads type spin default 1 min 1 max 256
option name Hash type spin default 256 min 16 max 65536
option name CCA_Nodes type spin default 200000 min 1000 max 100000000
option name UCI_Elo type spin default 1900 min 800 max 2600
option name CCA_OpponentElo type spin default 1500 min 400 max 3000
option name Move Overhead type spin default 10 min 0 max 5000
option name UCI_Opponent type string default <empty>
option name CCA_Persona type combo default balanced var balanced var dubov var human var solid var tal
option name CCA_HumanModel type combo default maia2 var maia2 var qre
option name CCA_Maia2Type type combo default rapid var rapid var blitz
option name CCA_Device type combo default gpu var gpu var cpu
option name CCA_Seed type string default <secret>
option name CCA_EmulateThinkTime type check default false
option name Ponder type check default false
option name UCI_LimitStrength type check default true
uciok
info string maia2 unavailable (Maia-2 is not installed: use Python 3.10-3.12 and `uv sync --extra maia2`); falling back to QRE human model
readyok
```

The `uci` answer comes at once: Stockfish and the human model are built only at `isready` or at
the first `go`. So a GUI that probes the engine while you add it is not slowed down by Maia-2.
If Stockfish cannot be found, `isready` still answers `readyok`, but only after a line like
`info string error: EngineNotFoundError: Stockfish not found: ...`
(see [13.1](#131-engine-not-found)).

## 3. Options and engine output

The full definitions, with types, ranges and help text, are in
[reference §4](reference.md#4-uci-options). These are the options you are most likely to change
in a GUI:

| Option | Default | Notes for GUI use |
|---|---|---|
| `StockfishPath` | `<auto>` | Set it if CCA cannot find Stockfish ([1.2](#12-install-stockfish-19)). Leave it alone in Docker |
| `CCA_HumanModel` | `maia2` | Choose `qre` if Maia-2 is not installed or starts too slowly ([13.3](#133-slow-start-with-maia-2)). A missing Maia-2 falls back to `qre` anyway |
| `CCA_Device` | `gpu` | `gpu` uses the CPU when CUDA is unavailable |
| `UCI_Elo`, `UCI_LimitStrength` | `1900`, `true` | The rating of the human player CCA imitates. The `UCI_LimitStrength` default departs from the UCI spec, see [13.7](#137-uci_limitstrength-and-uci_elo) |
| `CCA_OpponentElo` | `1500` | The opponent's rating (yours, when you play CCA). Needed when the GUI sends no rating in `UCI_Opponent` (Cute Chess, Scid vs. PC) |
| `Move Overhead` | `10` | Milliseconds taken off every deadline. Raise it if CCA loses on time ([13.5](#135-losses-on-time)) |
| `CCA_Persona` | `balanced` | `balanced`, `dubov`, `human`, `solid`, `tal`. They are hand-set; the Tal and Dubov names describe public reputations, not fitted players ([reference §8](reference.md#8-shipped-personas)) |
| `CCA_Seed` | `<secret>` | Leave it at `<secret>` for games against people (see the seed lines below) |
| `CCA_EmulateThinkTime` | `false` | `true` waits for the sampled human-like think time, but never past the move deadline |
| `Ponder` | `false` | Only signals that CCA handles `go ponder` and `ponderhit`. See the pondering note below |

- **Option names are case-sensitive; combo and check values are not.** `setoption name move overhead value 100`
  is answered with `info string ignoring unknown option move overhead`. A combo or check value
  such as `TAL` or `TRUE` is accepted.
- **Out-of-range numbers are clamped; other invalid values are rejected.** `UCI_Elo 5000`
  becomes 2600 (the top of its range) without a message. A value that is not a number, not one
  of a combo's choices or not `true`/`false` is rejected and the old value is kept, for example
  `info string error: option UCI_Elo needs an integer, got 'abc'`.
- **Pondering never starts in practice.** CCA's `bestmove` carries no ponder move, and both
  python-chess (used by lichess-bot) and Cute Chess ponder only on the ponder move that
  `bestmove` names. Leaving pondering on in a GUI does no harm.

What CCA writes back:

- **One report line per move:**
  `info string cca q_opt=… q_human=… trap=… stress=… drive=… opp_stress=… lam=… omega=… eps=… think=…s`.
  `stress` and `opp_stress` are dimensionless latent arousal variables, inspired by stress
  physiology but not models of it. `drive` is a leaky integrator of evaluation surprises, an
  analogue of a reward-prediction error. They are virtual control variables of the decision
  core, and their behavioural effects are hypotheses with kill criteria. See
  [science](science.md) and [ADR-0005](adr/0005-honest-science-labelling.md).
- **Seed lines.** With `CCA_Seed` left at `<secret>`, CCA draws a fresh seed
  for each game and prints `info string cca game <n> seed commitment sha256:<digest>` before its
  first move. It prints `... seed reveal <seed>` at the next `ucinewgame` or `quit`.
- **No `info depth`, `score` or `pv`.** Evaluation bars, score graphs and PV panes stay empty.
  GUI rules that need an engine score, such as score-based adjudication or resign and draw rules,
  get nothing from CCA. CCA is built to play games, not to analyse positions.

## 4. Cute Chess

[Cute Chess](https://github.com/cutechess/cutechess). The latest release at the time of writing
is 1.5.1. The steps below come from the 1.5.1 source (not run).

### GUI

The **Command** field is used as the program path; Cute Chess never splits it into a program and
arguments. On Windows the whole field is quoted if it contains a space; on Linux it goes to Qt
as a program name. So `cca.exe uci` does not work there. Use `cca-uci`, which needs no arguments.

1. **Tools → Settings →** the **Engines** tab **→ Add**.
2. **Name:** `CCA`. **Command:** use **Browse…** to pick the `cca-uci` launcher
   ([section 2](#2-which-command-to-use)). **Working Directory:** any folder you can write to.
   **Protocol:** `uci`.
3. On the **Advanced** tab, click **Detect**. The options appear; set `StockfishPath` and any
   others you need.
4. Click **OK**. To play a game, use **Game → New…** and set one side to **CPU** with CCA. For
   engine matches, use **Tournament → New…**.

Notes:

- Cute Chess sends `UCI_Opponent` with the rating `none`, so CCA uses `CCA_OpponentElo`: set it
  to your rating.
- If the first start with Maia-2 times out, raise **Scale Timeouts** on the Basic tab. The
  dialog's own tooltip says to change it only if necessary.

### cutechess-cli

`cmd=`, `arg=`, `proto=`, `option.<name>=` and the other flags below are documented in the
[cutechess-cli man page](https://raw.githubusercontent.com/cutechess/cutechess/master/docs/cutechess-cli.6.txt).
In the examples below, CCA plays Stockfish 19 limited to 1900. Stockfish 19's own `UCI_Elo`
range is 1320 to 3190.

Windows (Command Prompt). Quote the whole `key=value` token when a path contains spaces:

```bat
cutechess-cli ^
  -engine name=CCA "cmd=C:\cca\venv\Scripts\cca-uci.exe" proto=uci ^
    "option.StockfishPath=C:\engines\stockfish\stockfish-windows-x86-64-universal.exe" ^
    option.CCA_Persona=balanced option.CCA_OpponentElo=1900 restart=off ^
  -engine name=SF1900 "cmd=C:\engines\stockfish\stockfish-windows-x86-64-universal.exe" proto=uci ^
    option.UCI_LimitStrength=true option.UCI_Elo=1900 ^
  -each tc=60+0.6 -games 2 -rounds 10 -repeat -recover -pgnout cca.pgn
```

Linux/macOS:

```sh
cutechess-cli \
  -engine name=CCA cmd="$HOME/cca/venv/bin/cca-uci" proto=uci \
    option.CCA_Persona=balanced option.CCA_OpponentElo=1900 restart=off \
  -engine name=SF1900 cmd=stockfish proto=uci option.UCI_LimitStrength=true option.UCI_Elo=1900 \
  -each tc=60+0.6 -games 2 -rounds 10 -repeat -recover -pgnout cca.pgn
```

- `cmd=<venv>/bin/cca arg=uci` does the same thing as the `cca-uci` launcher.
- `restart=off` keeps the CCA process between games, so Maia-2 is loaded only once.
- `tscale=<factor>` scales cutechess-cli's engine timeouts, which helps with a slow first start.
- `timemargin=<ms>` lets engines go over the time limit. Raise CCA's `Move Overhead` first.
- `st=<seconds>` (fixed time per move) is applied. `depth=` and `nodes=` are not
  ([13.6](#136-depth-nodes-and-mate-limits-have-no-effect)).
- With `-concurrency` above 1, each CCA process starts its own Stockfish and its own Maia-2.

## 5. Arena

[Arena](http://www.playwitharena.de/): 3.5.1 for Windows, and a 3.10 beta for 64-bit Linux. The
steps and labels come from third-party guides; Arena's own help file was not read
(**UNVERIFIED**).

1. **Engines → Install New Engine…** and pick the `cca-uci` launcher. When Arena asks for the
   type, choose **UCI**.
2. Go to **Engines → Manage…** (F11), open the **Details** tab and select CCA. Set
   **Type: UCI** explicitly: with Auto, one published Arena log shows it sending `xboard` before
   `uci`. Leave **Command Line Parameters** empty; if you installed `cca.exe` instead, enter
   `uci` there. Click **Apply**.
3. Click **Start this engine right now!**, then set options with
   **Engines → Engine 1 → Configure**.

## 6. En Croissant

[En Croissant](https://github.com/franciscoBSalgueiro/en-croissant). The latest release at the
time of writing is v0.15.1. The following was read from its source; none of it was run:

- En Croissant starts the engine with no arguments and uses the binary's folder as the working
  directory.
- On Windows the file picker lists only `.exe` files.
- It launches the engine as soon as you pick it, to read `id name` and the options.

1. In the sidebar, click **Engines**, then **Add New**.
2. In the **Add Engine** dialog, open the **Local** tab.
3. Under **Binary file**, select `cca-uci.exe` on Windows or `<venv>/bin/cca-uci` on
   Linux/macOS. Do not select `cca.exe`: without `uci` it exits with a usage error.
4. The **Name** is detected automatically. Click **Add**. Options are on the engine's settings
   page, which also has **Edit JSON**.

CCA sends no evaluation, so the analysis panel shows none ([section 3](#3-options-and-engine-output)).

## 7. BanksiaGUI

The steps follow [BanksiaGUI's online help](https://banksiagui.com/help/engines.html) (not run):

1. **Engines → Manage Engines… → Add**, then browse to the `cca-uci` launcher.
2. Optionally give it a name, then click **OK**. BanksiaGUI reads the UCI options itself; change
   them with **Configure**.

Notes:

- The current help does not describe command-line arguments, so use `cca-uci`. Whether
  BanksiaGUI accepts a `.bat` file (for the Docker wrapper) is **UNVERIFIED**.
- Its changelog lists support for `UCI_Elo` and `UCI_LimitStrength`
  ([13.7](#137-uci_limitstrength-and-uci_elo)).
- The help's **Search depth** setting does not limit CCA
  ([13.6](#136-depth-nodes-and-mate-limits-have-no-effect)). Use time controls.
- BanksiaGUI has a built-in Lichess bot client. The rules in [10.1](#101-rules-first) apply to it
  unchanged.

## 8. Scid vs. PC

The current version is 4.27. The steps follow the official
[analysis engine documentation](https://scidvspc.sourceforge.net/doc/Analysis.htm) (not run):

1. Open **Tools → Analysis Engines** and add a new engine. The names of the button and the
   dialog come from a third-party guide.
2. **Name:** `CCA`. **Command:** the full path to `cca-uci` with **Parameters** left empty, or
   `cca` with **Parameters** set to `uci`. A third-party guide recommends forward slashes in the
   path. **Directory:** a folder the engine may write to. Protocol: **UCI**.
3. Click **OK**, then start the engine.

Notes:

- Its documentation says Scid vs. PC generally ignores options named `UCI_*`. So `UCI_Elo` stays
  at its default; set `CCA_OpponentElo` yourself.
- Analysis uses `go infinite`. CCA makes one decision, prints its report line, and sends
  `bestmove` when Scid sends `stop`. It shows no evaluation.
- The documented `set ::uci::goCommand {go movetime 10000}` is applied by CCA.
  `{go depth 20}` is not.

## 9. ChessBase and Fritz

The steps follow ChessBase's official help (not run):
[Fritz 19](https://help.chessbase.com/Fritz/19/Eng/000528.htm) and
[ChessBase 18](https://help.chessbase.com/CBase/18/Eng/000528.htm).

1. In Fritz 19, use **Engines → Create UCI Engine**; in ChessBase 18, **Home → UCI Engine**.
2. Click **Browse** and select `cca-uci.exe`. Name and author are filled in from the engine
   (`CCA 0.1.0`, `Nguyen Vu Dong Quan (DonQuaan)`). Click **OK**.
3. Change options with the **Parameters** button. If you save changed parameters under a new
   name, that name must contain the original name, `CCA 0.1.0`.

Notes:

- **UNVERIFIED:** whether ChessBase accepts an `.exe` launcher generated by pip or uv. No
  ChessBase product was available for testing. Third-party reports say ChessBase needs a real
  `.exe`, rejects `.bat` files, and in one case (ChessBase 16) rejected another tool's launcher
  executable.
- No arguments field is documented, so `cca.exe uci` is not an option.
- The Docker wrapper ([section 11](#11-docker)) is a `.bat`, so it is not usable here either.

## 10. lichess-bot

[lichess-bot](https://github.com/lichess-bot-devs/lichess-bot) connects a UCI engine to a
Lichess BOT account. It drives the engine through python-chess, which is the layer tested for
this page. Its version file names lichess-bot 2026.8.9.2, with Python 3.11 as the minimum.

### 10.1 Rules first

- **CCA may play on Lichess only through a BOT account.** Lichess's
  [Fair Play rules](https://lichess.org/page/fair-play) forbid engine assistance in games played
  from a normal account, including Board API games. Engine play is allowed through the Bot API.
  Using CCA to help a human account is cheating, on Lichess and on every other platform
  ([science §3](science.md#3-ethics-and-platform-rules)).
- **Use a fresh account.** A BOT account must not have played any game before the upgrade. The
  upgrade is irreversible: afterwards the account can only play as a bot
  ([Lichess Bot API](https://lichess.org/api#tag/Bot)).
- **Bot restrictions** (Lichess API documentation):
  - challenge games only, with no pools and no tournaments;
  - no UltraBullet (¼+0);
  - the [Terms of Service](https://lichess.org/terms-of-service) apply, in particular the rules
    on sandbagging, constant aborting and boosting.

  Lichess advises bot developers to play casual games while testing.
- **Games against people are research on people.** If you collect or publish data from them,
  inform your opponents and get consent and an ethics review first. Nothing in CCA may be tuned
  to evade anti-cheat detection ([science §3](science.md#3-ethics-and-platform-rules)).

### 10.2 Configure the engine

1. Install lichess-bot as its [wiki](https://github.com/lichess-bot-devs/lichess-bot/wiki)
   describes, and copy `config.yml.default` to `config.yml`.
2. Create an API token with the `bot:play` scope for the fresh account. Put it in `token:` or in
   the `LICHESS_BOT_TOKEN` environment variable.
3. Install CCA and Stockfish ([section 1](#1-before-you-start)) and note the folder that holds
   `cca-uci`.
4. Replace the `engine:` section of `config.yml` with the one below. Keys you leave out get
   lichess-bot's defaults, and those keep books, online moves and tablebases off.

```yaml
engine:
  dir: "C:/cca/venv/Scripts/"        # folder that holds the launcher; Linux/macOS: "/home/<you>/cca/venv/bin/"
  name: "cca-uci.exe"                # Linux/macOS: "cca-uci"
  working_dir: ""                    # blank: the directory lichess-bot runs in
  protocol: "uci"
  ponder: false                      # CCA sends no ponder move, so pondering never starts anyway
  debug: false                       # true logs all start-up communication with the engine
  polyglot:
    enabled: false                   # a book would play the opening instead of CCA
  draw_or_resign:
    resign_enabled: false            # CCA reports no score, so score-based rules have nothing to use
    offer_draw_enabled: false
  online_moves:
    online_egtb:
      enabled: false
      move_quality: "best"           # "suggest" restricts the engine with searchmoves, which CCA ignores
  lichess_bot_tbs:
    syzygy:
      enabled: false
      move_quality: "best"
    gaviota:
      enabled: false
      move_quality: "best"
  uci_options:                       # only options CCA advertises (section 2.1)
    Move Overhead: 100               # milliseconds; raise it if the bot loses on time
    Threads: 1                       # Stockfish threads
    Hash: 256                        # Stockfish hash, MB
    StockfishPath: "C:/engines/stockfish/stockfish-windows-x86-64-universal.exe"
    CCA_HumanModel: "maia2"          # "qre" if Maia-2 is not installed
    CCA_Device: "gpu"                # falls back to the CPU without CUDA
    CCA_Persona: "balanced"
    UCI_LimitStrength: true
    UCI_Elo: 1900                    # rating of the human player CCA imitates
  silence_stderr: false
```

Before using this section, check these points:

- **Delete `SyzygyPath` and `UCI_ShowWDL`**, which `config.yml.default` sets under
  `uci_options`. CCA does not advertise them, and python-chess refuses to set them, for example
  `EngineError: engine does not support option SyzygyPath`. lichess-bot then stops the engine.
- **Never put `Ponder` in `uci_options`.** python-chess manages it and raises
  `cannot set Ponder which is automatically managed`. Pondering is controlled by
  `engine.ponder` only.
- **Do not set `UCI_Opponent`.** lichess-bot sends the opponent's title, rating and name itself,
  and CCA uses that rating instead of `CCA_OpponentElo`.
- **Leave `engine_options` empty.** It becomes `--key=value` arguments, which `cca-uci`
  ignores.
- **Leave `go_commands` commented out.** `depth` and `nodes` there are not applied;
  `movetime` is.
- **Keep `challenge.variants` at `standard`.** CCA does not support Chess960
  ([13.9](#139-chess960-and-variants)).
- **Paths in YAML.** Use forward slashes, as above; Windows accepts them. Inside double quotes,
  a backslash starts an escape sequence, so `"C:\engines"` would not mean what it says.

This `uci_options` block was run through python-chess against the `cca-uci.exe` of a wheel
install: it configured cleanly and CCA played a move. lichess-bot's own `move_overhead` key
(at the top level of `config.yml`) is lichess-bot's time reserve. It is separate from CCA's
`Move Overhead`.

### 10.3 Upgrade and run

```sh
python lichess-bot.py -u     # upgrade the account to BOT: irreversible (10.1)
python lichess-bot.py        # run; -v for verbose output, -l <file> to log, --config <file> for another config
```

lichess-bot starts a new engine process for every game. It gives the engine 60 seconds to answer
the handshake. With Maia-2, each game therefore pays the start-up cost described in
[13.3](#133-slow-start-with-maia-2). python-chess keeps only the last `info string` of each move,
which is CCA's report line. So lichess-bot never shows the "does not apply go …" note
([13.6](#136-depth-nodes-and-mate-limits-have-no-effect)); only the raw engine traffic contains
it.

## 11. Docker

| Image | Contents |
|---|---|
| `ghcr.io/donquaan/cca:0.2.0` (also `latest`) | CCA, Stockfish 19 and the QRE human model |
| `ghcr.io/donquaan/cca:0.2.0-maia2` (also `latest-maia2`) | The same, plus CPU-only torch and Maia-2. The Maia-2 weights are not in the image: they are downloaded on first use into `/data/maia2` and checked against a pinned SHA-256 |

- Both images are built for **linux/amd64 only**.
- The images set `CCA_STOCKFISH=/usr/local/bin/stockfish`. Stockfish's licence and source are
  in `/usr/local/share/doc/stockfish`.
- The entrypoint is `tini -- cca`, and the default command starts the web simulator
  ([section 12](#12-the-browser-board-instead-of-a-gui)). Pass `uci` to get the engine.
- The default image has no Maia-2. With the default `CCA_HumanModel` (`maia2`), CCA falls back
  to `qre` and says so; set `CCA_HumanModel` to `qre` to skip the attempt.
- Everything else about the images (contents, licences, volumes, provenance) is in
  [docker.md](docker.md).

Pull the image first, and check where it came from (needs the GitHub CLI):

```sh
docker pull ghcr.io/donquaan/cca:0.2.0
gh attestation verify oci://ghcr.io/donquaan/cca:0.2.0 -R DonQuaan/CCA
printf 'uci\nisready\nquit\n' | docker run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

These flags matter when a GUI starts the container
([`docker run` reference](https://docs.docker.com/reference/cli/docker/container/run/)):

| Flag | Why |
|---|---|
| `-i` | Keeps stdin open. That is the UCI channel |
| never `-t` | With stdin coming from a pipe, as it does from a GUI, Docker refuses to start the container: `cannot attach stdin to a TTY-enabled container because stdin is not a terminal` |
| `--pull=never` | A GUI handshake never waits for a download; a missing image fails at once. So pull the image before you start the GUI |
| `--no-healthcheck` | The image's health check probes the web simulator on port 8765, which `uci` does not start |
| `--rm` | Removes the container when CCA exits. CCA exits on `quit` or when stdin closes |

- **Do not set `StockfishPath` from the GUI.** A host path does not exist inside the container.
  CCA rejects it and keeps the bundled Stockfish.
- **There is no GPU in the images.** The Maia-2 image ships CPU-only torch, so `CCA_Device=gpu`
  runs on the CPU, and `--gpus` does not help. For Maia-2 on a GPU, use a local install.

### Wrapper scripts

Most GUIs start a single executable, so wrap the `docker run` command in a script.

Windows, `cca-docker.bat`:

```bat
@echo off
docker run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

`@echo off` is required. Without it, `cmd` echoes the command line to stdout before the engine
starts, and the GUI reads that output as garbage.

Linux/macOS, `cca-docker.sh` (then run `chmod +x cca-docker.sh`):

```sh
#!/bin/sh
exec docker run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci
```

A GUI may start with a different `PATH` than your terminal. If the engine does not start,
replace `docker` with the absolute path that `command -v docker` prints (on Windows,
`where docker`). The Docker daemon must be running (on Windows and macOS: Docker Desktop).

**Maia-2 image.** Add a named volume so the weights are downloaded only once, and use the
`-maia2` tag:

```sh
exec docker run -i --rm --pull=never --no-healthcheck -v cca-data:/data ghcr.io/donquaan/cca:0.2.0-maia2 uci
```

Do the first download in a terminal rather than during a GUI handshake. `isready` loads Maia-2,
which fetches the checkpoint into the volume (the rapid checkpoint is 279,704,570 bytes, about
267 MiB or 280 MB):

```sh
printf 'uci\nisready\nquit\n' | docker run -i --rm --pull=never --no-healthcheck -v cca-data:/data ghcr.io/donquaan/cca:0.2.0-maia2 uci
```

On Windows (Command Prompt), `(echo uci& echo isready& echo quit) | docker run ...` does the
same.

### Which programs can use the Docker wrapper

None of these were run with Docker.

| Program | Docker wrapper |
|---|---|
| lichess-bot | `dir`: the wrapper's folder; `name`: `cca-docker.bat` or `cca-docker.sh`. On Linux/macOS the file must be executable. The `--key=value` arguments lichess-bot appends are dropped by the wrapper |
| cutechess-cli, Cute Chess GUI | `cmd=` or **Command**: the wrapper. On Windows, a `.bat` started this way worked through python-chess in earlier tests, but was not tried in Cute Chess (**UNVERIFIED**) |
| Arena | The `.bat`. Third-party logs show Arena starting `.bat` engines (**UNVERIFIED**) |
| Scid vs. PC | Either the wrapper, or **Command** `docker` with **Parameters** `run -i --rm --pull=never --no-healthcheck ghcr.io/donquaan/cca:0.2.0 uci` |
| En Croissant | Linux/macOS: the `.sh`. Windows: not possible, because the file picker accepts only `.exe` |
| BanksiaGUI | **UNVERIFIED** |
| ChessBase/Fritz | Not usable: third-party reports say `.bat` engines are not accepted |

If a GUI kills the engine abruptly, the `docker` client may exit while the container keeps
running (**UNVERIFIED**). To check, run `docker ps --filter ancestor=ghcr.io/donquaan/cca:0.2.0`,
and stop any leftover container with `docker stop <id>`.

## 12. The browser board instead of a GUI

`cca play` serves CCA's own board and decision-signal view on `127.0.0.1:8765` and opens a
browser, unless you pass `--no-browser`. The page is explained in [simulator.md](simulator.md),
and all arguments are in [reference: `cca play`](reference.md#cca-play). From the image, the
default command does the same on port 8765:

```sh
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/donquaan/cca:0.2.0
docker run --rm -p 127.0.0.1:8765:8765 -v cca-data:/data ghcr.io/donquaan/cca:0.2.0-maia2
```

Then open `http://127.0.0.1:8765`. Mapping the port to `127.0.0.1` keeps the simulator off
your network.

![CCA's browser board in English with the dark theme: you play White, CCA plays Black](img/cca-play-dark.png)

The same view in Vietnamese with the light theme: [`img/cca-play-vi-light.png`](img/cca-play-vi-light.png).

## 13. Troubleshooting

### 13.1 Engine not found

**Symptom:** every move comes back at once and is simply the first legal move (from the
starting position, `g1h3`). The engine log shows:

```text
info string error: EngineNotFoundError: Stockfish not found: pass a path, set CCA_STOCKFISH, or run scripts/fetch_stockfish.py
```

CCA always answers `go` with exactly one `bestmove`. When it cannot think, it falls back to the
first legal move.

**Fix:** set `StockfishPath` in the GUI, or set `CCA_STOCKFISH` where the GUI can see it
([1.2](#12-install-stockfish-19)), and check with `cca doctor`. A `StockfishPath` that is not a
file is answered with `info string error: StockfishPath '<value>' is not a file; keeping the current engine`.

### 13.2 The engine does not start

| Symptom | Cause and fix |
|---|---|
| The GUI says the engine exited or sent no `uciok` | It was started as `cca` without `uci` (exit code 2). Point it at `cca-uci`, or add the argument `uci` |
| The launcher fails after the venv was moved or renamed | The launcher stores the absolute path of its Python ([section 2](#2-which-command-to-use)). Recreate the venv |
| lichess-bot: `EngineError: engine does not support option ...` | An option CCA does not advertise is in `uci_options`: delete `SyzygyPath` and `UCI_ShowWDL` ([10.2](#102-configure-the-engine)) |
| lichess-bot: `cannot set Ponder which is automatically managed` | Remove `Ponder` from `uci_options` and use `engine.ponder` |
| lichess-bot: the engine file "doesn't have execute (x) permission" | Run `chmod +x` on the wrapper script. pip and uv create their launchers with execute permission |

### 13.3 Slow start with Maia-2

The `uci` answer is always immediate. The time goes into the first `isready`, which builds
Stockfish and the human model. These times were measured for this page from `uci` to `readyok`,
on one Windows 11 machine (Intel i9-14900HX, RTX 4060 Laptop GPU), with the rapid checkpoint
already on disk:

| Human model | Time to `readyok` |
|---|---|
| `qre` | 1.5 s |
| `maia2` on the CPU (`CCA_Device=cpu`) | 8.8 s and 8.9 s (two runs) |
| `maia2` on CUDA (`CCA_Device=gpu`) | 17.1 s on the first run, 8.2 s on the second |

Your times will differ. The first use of Maia-2 also downloads the checkpoint, which takes much
longer.

- **Warm up once in a terminal** with the handshake from [2.1](#21-check-the-handshake). It
  downloads the weights, and later starts only load them. For a pip install, set `CCA_WEIGHTS`
  to a folder you choose, as a user environment variable (like `CCA_STOCKFISH` in
  [1.2](#12-install-stockfish-19)). The terminal and the GUI then use the same folder
  ([reference §11](reference.md#11-environment-variables)).
- **Give the GUI more time:** Cute Chess's **Scale Timeouts**, or cutechess-cli's `tscale=`.
  Also keep the process between games: `restart=off` in cutechess-cli.
- **If it is still too slow**, set `CCA_HumanModel=qre`. That is a different human model (an
  engine-derived baseline), not a faster Maia-2.

### 13.4 Unsupported or ignored options

| What you see | Meaning |
|---|---|
| `info string ignoring unknown option <name>` | CCA does not have this option, or the name's case is wrong (names are case-sensitive). Nothing changed |
| `info string error: option <name> ...` | The value was invalid (not a number, not one of the choices, not `true`/`false`, or a `StockfishPath` that is not a file). The previous value is kept. Numbers outside a spin option's range are clamped silently instead |
| python-chess or lichess-bot: `engine does not support option <name>` | python-chess refuses options that CCA does not advertise, before CCA sees them ([10.2](#102-configure-the-engine)) |
| The GUI does not show `UCI_Opponent`, or ignores `UCI_*` options | Cute Chess hides the `UCI_*` options except `UCI_LimitStrength` and `UCI_Elo`, and Scid vs. PC generally ignores them. Use `CCA_OpponentElo` for the opponent's rating |

### 13.5 Losses on time

- **Raise `Move Overhead`** (0 to 5000 ms, default 10). It is subtracted from every clock and
  `movetime` deadline, to absorb GUI, process and network lag. lichess-bot's default config uses
  100.
- **For `go movetime`**, CCA keeps 50 ms plus `Move Overhead` in reserve. In the release tests,
  `movetime` 1000 with `Move Overhead` 100 was answered in 0.86 s.
- **On a clock**, CCA spends at most a quarter of its remaining time on one move and keeps a
  0.5 s safety margin. Below 0.6 s it answers in reflex mode with Stockfish's best move
  ([reference §5](reference.md#5-configuration): `max_fraction`, `safety_margin`,
  `fast_budget`).
- **The deadline includes engine start-up** when the GUI sends no `isready` before the first
  `go`.
- **`CCA_EmulateThinkTime=true` never waits past the deadline**, so it is not the cause of a
  time loss.
- **In cutechess-cli**, `timemargin=` tolerates small overruns. Treat it as a last resort.

### 13.6 Depth, nodes and mate limits have no effect

CCA accepts `go depth`, `go nodes`, `go mate` and `go searchmoves`, but does not apply them. It
sends one line and then searches normally, bounded by the clock or `movetime` and by the
`CCA_Nodes` budget per Stockfish evaluation:

```text
info string cca does not apply go depth: its search budget is CCA_Nodes plus the clock; searching normally
```

So GUI modes such as fixed depth, fixed nodes or "search depth" do not limit CCA. Use time-based
modes (a clock or a fixed time per move), and change `CCA_Nodes` for a different budget per
evaluation. `go infinite` (analysis) makes one decision and waits for `stop` before `bestmove`.
In lichess-bot, `move_quality: "suggest"` sends `searchmoves`, which CCA ignores.

### 13.7 UCI_LimitStrength and UCI_Elo

- `UCI_LimitStrength=true` is the default. CCA then imitates a human player rated `UCI_Elo`
  (800 to 2600, default 1900), which is its human-model anchor.
- `UCI_LimitStrength=false` makes CCA ignore `UCI_Elo` and use 2600, the top of the range.
- **This departs from the UCI spec** ([UCI protocol](https://backscattering.de/chess/uci/)).
  The spec says `UCI_LimitStrength` should default to false. CCA defaults to true so that
  `UCI_Elo` applies without an extra step. A GUI that sends `UCI_LimitStrength false` explicitly
  switches CCA to 2600. Check the engine log if the level looks wrong.
- **`UCI_Elo` is the rating of the imitated human, not a measured strength.** No calibration of
  CCA's playing strength against `UCI_Elo` has been published ([science](science.md) lists what
  CCA claims).
- With Maia-2, ratings below 1100 or from 2000 up are out of distribution, because its rating
  bins saturate ([science §1](science.md#1-what-each-c-aime-mechanism-is--and-is-not)).
- The opponent's rating comes from `UCI_Opponent` when the GUI sends one (lichess-bot does), and
  from `CCA_OpponentElo` otherwise.

### 13.8 Docker problems

| Symptom | Fix |
|---|---|
| `cannot attach stdin to a TTY-enabled container because stdin is not a terminal` | Remove `-t`: use `-i` only |
| `docker pull` answers `denied` | The image is pulled anonymously from GitHub Packages, which works only while the package is public. Try again later, or build locally with the commands at the top of the [`Dockerfile`](../Dockerfile) |
| The engine fails at once with `--pull=never` | The image is not on this machine. Run `docker pull` first |
| The first Maia-2 start times out in the GUI | Do the first download in a terminal ([section 11](#11-docker)) |
| Containers are left over after the GUI closed | `docker ps --filter ancestor=<image>`, then `docker stop <id>` |

### 13.9 Chess960 and variants

CCA does not advertise `UCI_Chess960` and plays standard chess only. python-chess (and therefore
lichess-bot) refuses a Chess960 game with `engine does not support UCI_Chess960`. Other GUIs
should not offer CCA for variants.

## 14. How this page was checked

Everything below was run on 2026-09-28 on Windows 11, except the items under "Read, not run".
Nothing was run inside a GUI.

- **UCI handshake.** Run through the repository's `.venv\Scripts\cca-uci.exe`. It was also run
  through the `cca-uci.exe` of three fresh installs of a wheel built from this source: `pip` in a
  `python -m venv`, `uv pip`, and `uv tool install`. All four advertised the same 16 option lines.
- **Stockfish handling.** Checked without Stockfish (the `EngineNotFoundError` fallback) and with
  `StockfishPath` set through `setoption`, including a path with forward slashes.
- **Commands.** `cca --help`, `cca uci --help`, `cca play --help`, `cca doctor` (with and without
  Stockfish), and bare `cca` (exit code 2). The pre-warm one-liners were run in Command Prompt
  and in PowerShell.
- **Engine behaviour.** The option messages in [13.4](#134-unsupported-or-ignored-options), the
  `go depth` note, and the Maia-2 start-up times in [13.3](#133-slow-start-with-maia-2).
- **python-chess 1.11.2, as lichess-bot uses it:**
  - `popen_uci([cca-uci.exe, "--Threads=4"])`;
  - the refusal of `SyzygyPath`, `UCI_ShowWDL` and `Ponder`;
  - the `uci_options` block from [10.2](#102-configure-the-engine);
  - `send_opponent_information`;
  - `play(..., ponder=True)` (no ponder move came back);
  - `Limit(depth=5)`;
  - refusal of a Chess960 board.
- **Read, not run:**
  - the GUI steps, from each project's documentation or source (Cute Chess 1.5.1 source,
    En Croissant source and UI strings, BanksiaGUI help, Scid vs. PC and ChessBase help);
  - lichess-bot's `config.yml.default`, `lib/config.py` and `lib/engine_wrapper.py`;
  - the Lichess Fair Play page and Bot API documentation;
  - the Docker CLI flags, from `docker run --help` (Docker 29.8.0) and Docker's documentation.
