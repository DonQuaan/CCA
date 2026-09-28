"""Stockfish adapter over the UCI protocol.

Stockfish (GPL-3.0) runs as a **separate process** and is only spoken to through UCI; CCA
never links its code — only its published win-rate formula and fitted coefficients are
re-implemented below (with attribution). Evaluations are converted to expected score with the
engine's own WDL output (``UCI_ShowWDL``) when available. Otherwise the Stockfish 17+
material-based win-rate model is applied (ported from ``src/uci.cpp`` at tag ``sf_19``).
python-chess's ``Score.wdl(model="sf")`` is *not* used: it is the SF16.1 move-number model and
is mis-calibrated for the material-normalised centipawns of Stockfish 17-19.

Reproducible mode: ``threads=1`` plus a ``nodes`` limit makes Stockfish's search
deterministic for a given binary; multi-threaded or time-limited search is not.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

import chess
import chess.engine

from cca.core.types import MoveEval

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class EngineNotFoundError(FileNotFoundError):
    """Raised when no Stockfish binary can be located."""


# Where scripts/fetch_stockfish.py installs engines in a source checkout. Deliberately *not*
# the current working directory: a GUI may start `cca uci` from any folder, and running
# whatever binary happens to sit in ./engines there would be an easy way to hijack CCA.
PROJECT_ENGINES = Path(__file__).resolve().parents[3] / "engines"


def find_stockfish(explicit: str | Path | None = None) -> Path:
    """Locate Stockfish: explicit path, ``$CCA_STOCKFISH``, the project ``engines/``, ``PATH``.

    An explicit path (or ``$CCA_STOCKFISH``) that does not exist is an error — silently falling
    back to another binary would make results depend on whatever else is installed.
    """
    for label, value in (
        ("explicit path", explicit),
        ("CCA_STOCKFISH", os.environ.get("CCA_STOCKFISH")),
    ):
        if value:
            path = Path(value)
            if not path.is_file():
                raise EngineNotFoundError(f"{label} {path} is not a file")
            return path
    candidates: list[Path] = []
    engines_dir = PROJECT_ENGINES
    if engines_dir.is_dir():
        pattern = "stockfish-*.exe" if os.name == "nt" else "stockfish-*"
        candidates.extend(
            p
            for p in sorted(engines_dir.glob(f"stockfish-*/**/{pattern}"))
            if p.suffix in {"", ".exe"} and os.access(p, os.X_OK)
        )
    which = shutil.which("stockfish")
    if which:
        candidates.append(Path(which))
    for path in candidates:
        if path.is_file():
            return path
    raise EngineNotFoundError(
        "Stockfish not found: pass a path or set CCA_STOCKFISH to a Stockfish binary "
        "(in a source checkout, scripts/fetch_stockfish.py downloads the pinned one)"
    )


# Stockfish 17-19 win-rate model coefficients (src/uci.cpp, win_rate_params, tag sf_19).
_WR_A = (-142.72052667, 372.35176398, -340.71073572, 415.23490212)
_WR_B = (5.93832785, 15.61267078, -30.57816876, 69.63866711)


def sf_material(board: chess.Board) -> int:
    """Material count used by Stockfish's win-rate model (both colours, P=1 N=B=3 R=5 Q=9)."""
    return sum(
        weight * len(board.pieces(piece, color))
        for piece, weight in (
            (chess.PAWN, 1),
            (chess.KNIGHT, 3),
            (chess.BISHOP, 3),
            (chess.ROOK, 5),
            (chess.QUEEN, 9),
        )
        for color in chess.COLORS
    )


def sf19_expected_score(cp: int, material: int) -> float:
    """Expected score from a *displayed* Stockfish 17-19 centipawn score and material count.

    Displayed ``cp = 100 v / a`` so the internal value is ``v = cp a / 100``; then
    ``W = 1 / (1 + exp((a - v) / b))``, ``L = 1 / (1 + exp((a + v) / b))``.
    """
    m = min(78, max(17, material)) / 58.0
    a = ((_WR_A[0] * m + _WR_A[1]) * m + _WR_A[2]) * m + _WR_A[3]
    b = ((_WR_B[0] * m + _WR_B[1]) * m + _WR_B[2]) * m + _WR_B[3]
    v = cp * a / 100.0
    win = 1.0 / (1.0 + math.exp(min(700.0, (a - v) / b)))
    loss = 1.0 / (1.0 + math.exp(min(700.0, (a + v) / b)))
    return win + 0.5 * max(0.0, 1.0 - win - loss)


def info_to_q(
    info: Mapping[str, Any], perspective: chess.Color, board: chess.Board
) -> float | None:
    """Expected score of ``perspective`` from one UCI ``info`` dictionary at root ``board``."""
    wdl = info.get("wdl")
    if wdl is not None:
        return float(wdl.pov(perspective).expectation())
    score = info.get("score")
    if score is None:
        return None
    pov = score.pov(perspective)
    mate = pov.mate()
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    cp = pov.score()
    if cp is None:  # pragma: no cover - python-chess always gives cp or mate
        return None
    return sf19_expected_score(cp, sf_material(board))


def _quit_engine(engine: chess.engine.SimpleEngine) -> None:
    with contextlib.suppress(chess.engine.EngineError, OSError, RuntimeError):
        engine.quit()
    with contextlib.suppress(Exception):
        engine.close()


class StockfishEngine:
    """:class:`cca.engines.base.SearchEngine` backed by a Stockfish process."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        threads: int = 1,
        hash_mb: int = 256,
        nodes: int | None = 200_000,
        depth: int | None = None,
        movetime: float | None = None,
        options: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        if nodes is None and depth is None and movetime is None:
            raise ValueError("give at least one search limit (nodes, depth or movetime)")
        self.path = find_stockfish(path)
        # popen_uci takes an argument list (no shell): the path is never shell-interpreted.
        self._engine = chess.engine.SimpleEngine.popen_uci([str(self.path)])
        # Close the process even if the owner forgets (exceptions, abandoned objects, exit):
        # an unclosed python-chess engine keeps a background thread alive and blocks shutdown.
        self._finalizer = weakref.finalize(self, _quit_engine, self._engine)
        config: dict[str, str | int | bool] = {"Threads": threads, "Hash": hash_mb}
        if "UCI_ShowWDL" in self._engine.options:
            config["UCI_ShowWDL"] = True
        if options:
            config.update(options)
        self._engine.configure(config)
        self._limit = chess.engine.Limit(nodes=nodes, depth=depth, time=movetime)
        self._game = object()

    @property
    def name(self) -> str:
        """Engine id reported over UCI (e.g. ``Stockfish 19``)."""
        return str(self._engine.id.get("name", "unknown"))

    def new_game(self) -> None:
        """Start a new game key so python-chess sends ``ucinewgame``."""
        self._game = object()

    def evaluate(
        self,
        board: chess.Board,
        *,
        perspective: chess.Color,
        moves: Sequence[chess.Move] | None = None,
        multipv: int = 1,
        time_limit: float | None = None,
    ) -> list[MoveEval]:
        """Evaluate root moves (see :class:`cca.engines.base.SearchEngine`)."""
        n_legal = board.legal_moves.count()
        if n_legal == 0:
            return []
        root = list(dict.fromkeys(moves)) if moves else None
        width = max(1, min(multipv, len(root) if root else n_legal))
        infos = self._engine.analyse(
            board, self._call_limit(time_limit), multipv=width, root_moves=root, game=self._game
        )
        out: list[MoveEval] = []
        for info in infos:
            pv = info.get("pv")
            if not pv:
                continue
            q = info_to_q(info, perspective, board)
            if q is None:
                continue
            out.append(
                MoveEval(
                    uci=pv[0].uci(),
                    q=q,
                    depth=int(info.get("depth", 0)),
                    pv=tuple(m.uci() for m in pv),
                )
            )
        out.sort(key=lambda e: e.q, reverse=True)
        return out

    def bestmove(self, board: chess.Board, time_limit: float | None = None) -> str:
        """The engine's own ``bestmove`` (honours ``UCI_LimitStrength`` / ``Skill Level``).

        Unlike :meth:`evaluate`, whose PV lines always show the search's best line, this is
        the move a strength-limited Stockfish actually plays. Stockfish seeds its skill picker
        from the clock, so strength-limited play is not reproducible.
        """
        result = self._engine.play(board, self._call_limit(time_limit), game=self._game)
        if result.move is None:
            raise RuntimeError("engine returned no bestmove")
        return result.move.uci()

    def _call_limit(self, time_limit: float | None) -> chess.engine.Limit:
        if time_limit is None:
            return self._limit
        own = self._limit.time
        return chess.engine.Limit(
            nodes=self._limit.nodes,
            depth=self._limit.depth,
            time=time_limit if own is None else min(own, time_limit),
        )

    def close(self) -> None:
        """Quit the engine process (idempotent)."""
        self._finalizer()

    def __enter__(self) -> StockfishEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
