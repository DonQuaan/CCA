"""Command-line interface: ``cca {uci,analyse,match,doctor,version}``."""

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
    from collections.abc import Sequence

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
    parser = argparse.ArgumentParser(prog="cca", description="CCA — Chaotic-Chess-Algorithm")
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
    handlers = {"analyse": cmd_analyse, "match": cmd_match, "doctor": cmd_doctor}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
