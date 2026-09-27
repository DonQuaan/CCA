"""Strict JSON for ``cca play``: no NaN/Infinity in or out, and decisions as plain data."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING

import chess

if TYPE_CHECKING:
    from cca.core.types import Decision


def jsonable(value: object) -> object:
    """Recursively convert to JSON types; non-finite floats become ``None``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


_HTML_SAFE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


def dumps(value: object) -> bytes:
    r"""Strict JSON bytes (``allow_nan=False`` after replacing non-finite floats).

    ``<``, ``>`` and ``&`` are written as ``\u`` escapes (same JSON value), so no response
    body ever contains markup, even when it quotes client input in an error message.
    """
    text = json.dumps(jsonable(value), allow_nan=False, ensure_ascii=True, separators=(",", ":"))
    return text.translate(_HTML_SAFE).encode("ascii")


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def loads_object(raw: bytes) -> dict[str, object]:
    """Parse a request body that must be a JSON object (empty body = ``{}``).

    Raises ``ValueError`` for malformed JSON and ``TypeError`` for a non-object value.
    """
    text = raw.decode("utf-8")
    if not text.strip():
        return {}
    value = json.loads(text, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise TypeError("the request body must be a JSON object")
    return value


def san(board: chess.Board, uci: str) -> str:
    """SAN of ``uci`` in ``board``; the UCI string itself if it is not a legal move there."""
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return uci
    return board.san(move) if move in board.legal_moves else uci


def decision_to_json(board: chess.Board, decision: Decision) -> dict[str, object]:
    """Every field of a :class:`~cca.core.types.Decision`; ``board`` is the position *before*.

    Candidates carry all :class:`~cca.core.types.Candidate` fields plus ``trap_value``, their
    SAN and their probability in the final policy (``None`` = outside the policy support,
    i.e. not in the risk-budget safe set).
    """
    policy = sorted(decision.policy.items(), key=lambda kv: (-kv[1], kv[0]))
    candidates: list[dict[str, object]] = []
    for cand in decision.candidates:
        row: dict[str, object] = dataclasses.asdict(cand)
        row["trap_value"] = cand.trap_value
        row["san"] = san(board, cand.uci)
        row["policy"] = decision.policy.get(cand.uci)
        candidates.append(row)
    return {
        "move": {"uci": decision.move, "san": san(board, decision.move)},
        "policy": [{"uci": u, "san": san(board, u), "p": p} for u, p in policy],
        "candidates": candidates,
        "knobs": dataclasses.asdict(decision.knobs),
        "state": dataclasses.asdict(decision.state),
        "think_time": decision.think_time,
        "trace": dict(decision.trace),
    }
