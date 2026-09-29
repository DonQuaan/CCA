"""Command-line interface: ``cca {uci,analyse,match,play,doctor,version}``."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cca import __version__

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cca.agent import AgentConfig
    from cca.engines.base import HumanModel, SearchEngine


def _force_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _agent_config(args: argparse.Namespace) -> AgentConfig:
    from cca.agent import AgentConfig
    from cca.config import load_agent_config, load_persona

    cfg = load_agent_config(args.config) if args.config else AgentConfig()
    changes: dict[str, Any] = {
        "elo_self": args.elo_self,
        "elo_oppo": args.elo_oppo,
        "seed": args.seed,
    }
    if args.persona:
        changes["persona"] = load_persona(args.persona)
    if args.argmax:
        changes["sample"] = False
    return dataclasses.replace(cfg, **changes)


def _engine(args: argparse.Namespace, nodes: int | None = None) -> SearchEngine:
    from cca.engines.stockfish import StockfishEngine

    return StockfishEngine(
        args.stockfish, threads=args.threads, hash_mb=args.hash, nodes=nodes or args.nodes
    )


def _human(args: argparse.Namespace, engine: SearchEngine) -> HumanModel:
    if args.human == "maia2":
        try:
            from cca.engines.maia2_human import Maia2HumanModel

            return Maia2HumanModel(model_type=args.maia2_type, device=args.device)
        except ImportError as exc:
            print(f"warning: {exc}; falling back to --human qre", file=sys.stderr)
            args.human = "qre"  # recorded as such in the run manifest
    from cca.engines.qre_human import QREHumanModel

    return QREHumanModel(engine)


def _play_human(
    args: argparse.Namespace, engine: SearchEngine
) -> tuple[HumanModel, str, str | None]:
    """Human model for ``cca play``: ``(model, kind, reason Maia-2 was not used)``.

    Unlike :func:`_human`, *any* Maia-2 failure (missing extra, CUDA, checkpoint, download)
    falls back to QRE: an interactive app should start, and it reports why it fell back.
    """
    reason: str | None = None
    if args.human == "maia2":
        try:
            from cca.engines.maia2_human import Maia2HumanModel

            maia = Maia2HumanModel(model_type=args.maia2_type, device=args.device)
        except Exception as exc:  # any failure -> QRE, reported in /api/info and the UI
            reason = f"{type(exc).__name__}: {exc}"
            print(f"warning: Maia-2 unavailable ({reason}); using QRE", file=sys.stderr)
        else:
            return maia, "maia2", None
    from cca.engines.qre_human import QREHumanModel

    return QREHumanModel(engine), "qre", reason


def _bounded_int(lo: int, hi: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return value

    return parse


def _bounded_float(lo: float, hi: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
        if not lo <= value <= hi:  # (also refuses nan)
            raise argparse.ArgumentTypeError(f"must be between {lo:g} and {hi:g}")
        return value

    return parse


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stockfish", help="path to Stockfish (default: auto-detect)")
    p.add_argument("--threads", type=int, default=1, help="Stockfish threads (1 = reproducible)")
    p.add_argument("--hash", type=int, default=256, help="Stockfish hash MB")
    p.add_argument("--nodes", type=int, default=200_000, help="Stockfish nodes per evaluation")
    p.add_argument("--human", choices=["maia2", "qre"], default="maia2")
    p.add_argument("--maia2-type", choices=["rapid", "blitz"], default="rapid")
    p.add_argument("--device", choices=["gpu", "cpu"], default="gpu")
    p.add_argument("--persona", help="shipped persona name or persona .toml path")
    p.add_argument("--config", type=Path, help="full agent config .toml")
    p.add_argument("--elo-self", type=int, default=1900)
    p.add_argument("--elo-oppo", type=int, default=1500)
    p.add_argument("--seed", default="cca")
    p.add_argument("--argmax", action="store_true", help="deterministic argmax instead of sampling")


def cmd_analyse(args: argparse.Namespace) -> int:
    """Run one decision on a FEN and print it as JSON."""
    import chess

    from cca.agent import CAIMEAgent

    board = chess.Board(args.fen)
    engine = _engine(args)
    try:
        agent = CAIMEAgent(engine, _human(args, engine), _agent_config(args), game_id="analyse")
        d = agent.choose(board)
        out = {
            "move": d.move,
            "policy": dict(sorted(d.policy.items(), key=lambda kv: -kv[1])),
            "knobs": dataclasses.asdict(d.knobs),
            "state": dataclasses.asdict(d.state),
            "think_time": d.think_time,
            "trace": dict(d.trace),
            "candidates": [dataclasses.asdict(c) for c in d.candidates],
            "chaos_digest": agent.chaos_digest,
        }
        print(json.dumps(out, indent=2, ensure_ascii=True))
    finally:
        engine.close()
    return 0


def _git(repo: Path, *args: str) -> str | None:
    import subprocess

    try:
        # Argument list (no shell); args are literals from this module, repo a resolved path.
        return subprocess.run(  # noqa: S603 - fixed argument list, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH, like any VCS tool
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _sha256(path: object) -> str | None:
    import hashlib

    if not isinstance(path, (str, Path)) or not Path(path).is_file():
        return None
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def code_provenance() -> dict[str, object]:
    """Commit and dirty flag of the checkout the *running package* comes from (not the cwd)."""
    pkg = Path(__file__).resolve().parent
    top = _git(pkg, "rev-parse", "--show-toplevel")
    if top is None or not pkg.is_relative_to(Path(top).resolve()):
        return {"git_commit": "unknown", "git_dirty": None}
    status = _git(Path(top), "status", "--porcelain", "--untracked-files=no")
    return {
        "git_commit": _git(Path(top), "rev-parse", "HEAD") or "unknown",
        "git_dirty": bool(status),
    }


def run_manifest(
    args: argparse.Namespace, engine: SearchEngine, human: HumanModel, cfg: AgentConfig
) -> dict[str, object]:
    """Everything needed to reproduce a benchmark run (code, binaries, weights, settings)."""
    human_version = "n/a"
    if importlib.util.find_spec("maia2"):
        from importlib.metadata import PackageNotFoundError, version

        try:
            human_version = version("maia2")
        except PackageNotFoundError:
            human_version = "unknown"
    return {
        "cca_version": __version__,
        **code_provenance(),
        "engine_sha256": _sha256(getattr(engine, "path", None)),
        "maia2_checkpoint_sha256": _sha256(getattr(human, "checkpoint", None)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "engine": getattr(engine, "name", type(engine).__name__),
        "engine_threads": args.threads,
        "engine_nodes": args.nodes,
        "human_model": type(human).__name__,
        "maia2_version": human_version,
        "maia2_device": getattr(human, "device", None),
        "arguments": {
            k: (str(v) if isinstance(v, Path) else v) for k, v in sorted(vars(args).items())
        },
        "config": dataclasses.asdict(cfg),
        "reproducible": args.threads == 1 and args.opponent != "stockfish",
    }


def cmd_match(args: argparse.Namespace) -> int:
    """Play CCA against an opponent and write PGN + metrics."""
    from cca.agent import CAIMEAgent
    from cca.bench.match import (
        CCAPlayer,
        EnginePlayer,
        HumanModelPlayer,
        Player,
        play_game,
        write_results,
    )
    from cca.engines.stockfish import StockfishEngine

    engine = _engine(args)
    referee = _engine(args, nodes=args.referee_nodes)
    opp_engine: StockfishEngine | None = None
    try:
        human = _human(args, engine)
        cfg = _agent_config(args)
        cca = CCAPlayer(CAIMEAgent(engine, human, cfg))
        opponent: Player
        if args.opponent == "stockfish":
            # Stockfish 19 accepts UCI_Elo in [1320, 3190] (CCRL-blitz-anchored, not human Elo).
            sf_elo = min(3190, max(1320, args.elo_oppo))
            opp_engine = StockfishEngine(
                args.stockfish,
                nodes=args.nodes,
                options={"UCI_LimitStrength": True, "UCI_Elo": sf_elo},
            )
            opponent = EnginePlayer(opp_engine, name=f"Stockfish-UCI_Elo-{sf_elo}")
        else:
            opponent = HumanModelPlayer(
                human, args.elo_oppo, args.elo_self, seed=f"{args.seed}-opp"
            )
        records = []
        for g in range(args.games):
            white, black = (cca, opponent) if g % 2 == 0 else (opponent, cca)
            rec = play_game(
                white,
                black,
                f"{args.seed}-{g}",
                base_time=args.base,
                increment=args.inc,
                referee=referee,
                max_plies=args.max_plies,
            )
            records.append(rec)
            print(f"game {g + 1}/{args.games}: {rec.white} vs {rec.black}", end=" ")
            print(f"{rec.result} ({rec.termination})")
        summary = write_results(
            records, cca.name, Path(args.out), run_manifest(args, engine, human, cfg)
        )
        print(summary.read_text(encoding="utf-8"))
    finally:
        engine.close()
        referee.close()
        if opp_engine is not None:
            opp_engine.close()
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    """Serve the browser simulator until interrupted (engines warm up in the background)."""
    from cca.engines.stockfish import EngineNotFoundError, find_stockfish
    from cca.play.app import Engines, PlayApp, PlaySettings
    from cca.play.limits import resolve_limits
    from cca.play.server import (
        LOG_HOPS_NEEDS_PUBLIC,
        PEER_CAP_NEEDS_A_BOUND,
        PEER_CAP_NEEDS_DIRECT,
        PUBLIC_NEEDS_A_HOST_NAME,
        PUBLIC_NEEDS_A_PUBLIC_HOST,
        ServerOptions,
        is_loopback,
        run,
    )

    if args.public and is_loopback(args.host):
        print(f"error: {PUBLIC_NEEDS_A_PUBLIC_HOST}", file=sys.stderr)
        return 1
    if args.public and not args.allowed_host:
        print(f"error: {PUBLIC_NEEDS_A_HOST_NAME}", file=sys.stderr)
        return 1
    if args.log_forwarded_hops and not args.public:
        print(f"error: {LOG_HOPS_NEEDS_PUBLIC}", file=sys.stderr)
        return 1
    if args.max_connections_per_peer is not None:
        problem = None
        if args.trusted_proxies:
            problem = PEER_CAP_NEEDS_DIRECT
        elif not args.public and args.max_connections is None:
            problem = PEER_CAP_NEEDS_A_BOUND
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 1
    options = ServerOptions(
        public=args.public,
        trusted_proxies=args.trusted_proxies,
        frame_ancestors=tuple(dict.fromkeys(args.frame_ancestor or ())),
        allowed_hosts=tuple(dict.fromkeys(args.allowed_host or ())),
        log_forwarded_hops=args.log_forwarded_hops,
    )
    limits = resolve_limits(
        public=args.public,
        max_sessions_per_client=args.max_sessions_per_client,
        decisions_per_minute=args.decisions_per_minute,
        max_queue=args.max_queue,
        session_idle_minutes=args.session_idle_minutes,
        max_connections=args.max_connections,
        max_think_seconds=args.max_think_seconds,
        reads_per_minute=args.reads_per_minute,
        max_connections_per_peer=args.max_connections_per_peer,
    )

    def build() -> Engines:
        engine = _engine(args)
        try:
            human, kind, reason = _play_human(args, engine)
        except BaseException:
            engine.close()  # never leak a Stockfish process
            raise
        return Engines(engine, human, kind, reason)

    try:  # fail fast, before a port is opened
        find_stockfish(args.stockfish)
        settings = PlaySettings(
            base_config=_agent_config(args),
            requested_human=args.human,
            nodes=args.nodes,
            threads=args.threads,
            hash_mb=args.hash,
            max_sessions=args.max_sessions,
            limits=limits,
        )
        app = PlayApp(build, settings)  # also checks the Elo defaults against the API ranges
    # ConfigError and a TOML syntax error are ValueErrors; an unreadable --config is an OSError.
    except (EngineNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return run(
        app, host=args.host, port=args.port, open_browser=not args.no_browser, options=options
    )


def _checked(kind: str) -> Callable[[str], str]:
    """An argparse type for a public-mode value: ``origin`` (an exact https origin) or ``host``.

    A bad value is reported by argparse with the validator's own message.
    """

    def parse(text: str) -> str:
        from cca.play.server import parse_allowed_host, parse_frame_ancestor

        check = parse_frame_ancestor if kind == "origin" else parse_allowed_host
        try:
            return check(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    return parse


def _add_public(p: argparse.ArgumentParser) -> None:
    """The public-mode flags of ``cca play`` (all off unless given or implied by --public)."""
    p.add_argument(
        "--public",
        action="store_true",
        help="public demo mode (e.g. on Hugging Face Spaces): needs a non-loopback --host and "
        "--allowed-host; turns the limits below on with the defaults in brackets and logs one "
        "line per request (no addresses)",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        type=_checked("host"),
        metavar="NAME",
        help="public host name answered besides IP addresses and localhost, e.g. "
        "owner-space.hf.space (repeatable)",
    )
    p.add_argument(
        "--trusted-proxies",
        type=_bounded_int(0, 8),
        default=0,
        metavar="N",
        help="reverse proxies in front: the client is the X-Forwarded-For entry N hops from the "
        "right (0: the header is ignored; 1 on Hugging Face Spaces; --log-forwarded-hops "
        "counts them on other platforms)",
    )
    p.add_argument(
        "--log-forwarded-hops",
        action="store_true",
        help="with --public: add to each request-log line the NUMBER of X-Forwarded-For "
        "entries (xff=N; never an address), to calibrate --trusted-proxies after a deploy "
        "behind a platform that does not document its proxies: use the smallest N seen on "
        "the page's own requests",
    )
    p.add_argument(
        "--frame-ancestor",
        action="append",
        type=_checked("origin"),
        metavar="ORIGIN",
        help="https origin allowed to show the page in a frame, e.g. https://huggingface.co "
        "(repeatable; default: no framing)",
    )
    p.add_argument(
        "--max-sessions-per-client",
        type=_bounded_int(1, 1024),
        metavar="N",
        help="games one client keeps; a new one replaces its least recently used [public: 3]",
    )
    p.add_argument(
        "--decisions-per-minute",
        type=_bounded_int(1, 600),
        metavar="N",
        help="CCA decisions (moves and position-lab analyses) per client and minute; more get "
        "429 [public: 20]",
    )
    p.add_argument(
        "--max-queue",
        type=_bounded_int(0, 1024),
        metavar="N",
        help="requests that may wait for the engine; more get 503 (busy) [public: 6]",
    )
    p.add_argument(
        "--session-idle-minutes",
        type=_bounded_int(1, 7 * 24 * 60),
        metavar="N",
        help="games unused this long are dropped [public: 30]",
    )
    p.add_argument(
        "--max-connections",
        type=_bounded_int(1, 1024),
        metavar="N",
        help="connections handled at once, one thread each; more get 503 (busy). Also caps "
        "API requests in progress per client (8) and requests waiting for a busy game (2), "
        "and gives request heads 10 s [public: 64]",
    )
    p.add_argument(
        "--max-connections-per-peer",
        type=_bounded_int(1, 1024),
        metavar="N",
        help="with --trusted-proxies 0 on a server that clients reach directly (no proxy in "
        "front): connections one address (an IPv6 /64) may have open at once, whatever they "
        "are doing; more get 503. Needs --max-connections or --public. Never behind a proxy, "
        "even an untrusted one (every connection then has the proxy's address): there the "
        "platform's edge is expected to buffer requests [public: off]",
    )
    p.add_argument(
        "--max-think-seconds",
        type=_bounded_float(1.0, 3600.0),
        metavar="S",
        help="wall-clock cap of each CCA decision (moves and position-lab analyses) once it "
        "has the engine: the search is shortened to meet it (not reproducible then) "
        "[public: 20]",
    )
    p.add_argument(
        "--reads-per-minute",
        type=_bounded_int(1, 6000),
        metavar="N",
        help="reads of a game's decisions or PGN per client and minute; more get 429 [public: 60]",
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report the environment; exit 0 if Stockfish is usable."""
    print(f"cca {__version__} | python {platform.python_version()} | {platform.platform()}")
    for mod in ("chess", "maia2", "torch"):
        print(f"  {mod:<6} {'ok' if importlib.util.find_spec(mod) else 'missing'}")
    if importlib.util.find_spec("torch"):
        import torch

        cuda = torch.cuda.is_available()
        print(f"  cuda   {'ok: ' + torch.cuda.get_device_name(0) if cuda else 'not available'}")
    try:
        from cca.engines.stockfish import StockfishEngine

        with StockfishEngine(args.stockfish, nodes=1000) as sf:
            print(f"  engine ok: {sf.name} ({sf.path})")
    except (OSError, RuntimeError) as exc:
        print(f"  engine FAIL: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser (exposed for tests)."""
    parser = argparse.ArgumentParser(
        prog="cca", description="CCA — human-like chess engine layer (C-AIME)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("uci", help="run as a UCI engine on stdin/stdout")
    sub.add_parser("version", help="print version")
    p_doc = sub.add_parser("doctor", help="check the environment")
    p_doc.add_argument("--stockfish")
    p_an = sub.add_parser("analyse", help="one decision on a FEN, as JSON")
    p_an.add_argument("fen")
    _add_common(p_an)
    p_m = sub.add_parser("match", help="play a benchmark match")
    _add_common(p_m)
    p_m.add_argument("--opponent", choices=["human", "stockfish"], default="human")
    p_m.add_argument("--games", type=int, default=2)
    p_m.add_argument("--base", type=float, default=300.0, help="virtual clock base seconds")
    p_m.add_argument("--inc", type=float, default=2.0, help="virtual clock increment seconds")
    p_m.add_argument("--referee-nodes", type=int, default=1_000_000)
    p_m.add_argument("--max-plies", type=int, default=300, help="adjudicate a draw after N plies")
    p_m.add_argument("--out", default="runs/latest")
    p_play = sub.add_parser(
        "play",
        help="play against CCA in the browser and watch its decision signals",
        epilog="Games started without a seed get a fresh secret seed (SHA-256 commitment shown "
        "during the game, seed revealed at the end); --seed seeds the position lab.",
    )
    p_play.add_argument("--host", default="127.0.0.1", help="address to bind (default: loopback)")
    p_play.add_argument(
        "--port", type=_bounded_int(0, 65535), default=8765, help="TCP port (0 = any free port)"
    )
    p_play.add_argument("--no-browser", action="store_true", help="do not open a web browser")
    p_play.add_argument(
        "--max-sessions",
        type=_bounded_int(1, 1024),
        default=16,
        help="games kept in memory (least recently used ones are dropped)",
    )
    _add_public(p_play)
    _add_common(p_play)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    _force_utf8()
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "uci":
        from cca.uci.protocol import main as uci_main

        uci_main()
        return 0
    handlers = {
        "analyse": cmd_analyse,
        "match": cmd_match,
        "play": cmd_play,
        "doctor": cmd_doctor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
