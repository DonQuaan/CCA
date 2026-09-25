"""A tiny UCI engine used to test the real Stockfish adapter without a Stockfish binary.

It speaks enough UCI for python-chess's ``analyse``: MultiPV, ``searchmoves``, ``wdl``.
Scores are material after the move (centipawns), deterministic. Set ``FAKE_UCI_NO_WDL=1`` to
omit the ``wdl`` field (exercises the score -> WDL fallback).
"""

from __future__ import annotations

import os
import sys

import chess

VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 310,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        VALUES[p.piece_type] * (1 if p.color == color else -1) for p in board.piece_map().values()
    )


def main() -> None:  # noqa: PLR0912, PLR0915 - a flat protocol loop reads best
    board = chess.Board()
    multipv = 1
    show_wdl = os.environ.get("FAKE_UCI_NO_WDL") != "1"
    for raw in sys.stdin:
        tokens = raw.split()
        if not tokens:
            continue
        cmd = tokens[0]
        if cmd == "uci":
            print("id name FakeFish 1")
            print("id author tests")
            print("option name Threads type spin default 1 min 1 max 8")
            print("option name Hash type spin default 16 min 1 max 1024")
            print("option name MultiPV type spin default 1 min 1 max 256")
            if show_wdl:
                print("option name UCI_ShowWDL type check default false")
            print("uciok")
        elif cmd == "isready":
            print("readyok")
        elif cmd == "setoption" and "MultiPV" in tokens:
            multipv = int(tokens[-1])
        elif cmd == "position":
            if tokens[1] == "startpos":
                board = chess.Board()
                rest = tokens[2:]
            else:
                idx = tokens.index("moves") if "moves" in tokens else len(tokens)
                board = chess.Board(" ".join(tokens[2:idx]))
                rest = tokens[idx:]
            if rest and rest[0] == "moves":
                for u in rest[1:]:
                    board.push_uci(u)
        elif cmd == "go":
            pool = list(board.legal_moves)
            if "searchmoves" in tokens:
                i = tokens.index("searchmoves") + 1
                allowed = set()
                while i < len(tokens) and tokens[i] not in {
                    "nodes",
                    "depth",
                    "movetime",
                    "wtime",
                    "btime",
                }:
                    allowed.add(tokens[i])
                    i += 1
                pool = [m for m in pool if m.uci() in allowed]
            scored = []
            for m in pool:
                after = board.copy(stack=False)
                after.push(m)
                if after.is_checkmate():
                    scored.append((100000, m, after))
                    continue
                scored.append((material(after, board.turn), m, after))
            scored.sort(key=lambda t: (-t[0], t[1].uci()))
            for rank, (cp, m, after) in enumerate(scored[:multipv], start=1):
                reply = next(iter(after.legal_moves), None)
                pv = m.uci() + (f" {reply.uci()}" if reply else "")
                score = "mate 1" if cp == 100000 else f"cp {cp}"
                wdl = ""
                if show_wdl:
                    w = 1000 if cp == 100000 else max(0, min(1000, 500 + cp // 2))
                    l_ = 1000 - w if cp == 100000 else max(0, min(1000 - w, 500 - cp // 2))
                    wdl = f" wdl {w} {1000 - w - l_} {l_}"
                print(f"info depth 3 multipv {rank} score {score}{wdl} nodes 100 pv {pv}")
            best = scored[0][1].uci() if scored else "0000"
            print(f"bestmove {best}")
        elif cmd == "quit":
            break
        sys.stdout.flush()


if __name__ == "__main__":
    main()
