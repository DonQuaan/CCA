"""Maia-2 human move model (Tang et al., NeurIPS 2024; MIT licence), optional extra.

Why not ``maia2.inference.inference_each``: it returns probabilities *rounded to 4 decimals*,
so any move below 5e-5 becomes exactly 0 and its Shannon surprisal becomes infinite. CCA
needs full-precision log-probabilities (surprise, KL anchor), so this adapter replicates
Maia-2's preprocessing and calls ``MAIA2Model.forward`` directly, then applies a masked
log-softmax over legal moves itself.

Known limits of Maia-2 that CCA compensates for elsewhere (see docs/science.md):

* no clock input, and no training data for the first 10 plies or for moves made with
  <= 30 s on the clock -> the agent tempers the prior in the opening and models time pressure
  itself;
* one position, no history -> no notion of repetition (Stockfish's value handles it);
* Elo enters as 11 bins of width 100 that saturate below 1100 and at 2000+;
* the value head is White-perspective and uncalibrated -> CCA never uses it as ``Q``.

Installation: ``maia2`` 0.11 requires Python < 3.13 and ``torch`` 2.8; on Windows the PyPI
torch wheel is CPU-only, so install the CUDA wheel from the PyTorch index (see README).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import chess

if TYPE_CHECKING:
    from collections.abc import Sequence


def _mirror_uci(uci: str) -> str:
    move = chess.Move.from_uci(uci)
    return chess.Move(
        chess.square_mirror(move.from_square),
        chess.square_mirror(move.to_square),
        move.promotion,
    ).uci()


class Maia2HumanModel:
    """:class:`cca.engines.base.HumanModel` backed by a Maia-2 checkpoint."""

    def __init__(
        self,
        model_type: str = "rapid",
        device: str = "gpu",
        save_root: str | Path = "weights/maia2",
        batch_size: int = 64,
    ) -> None:
        try:
            import torch
            from maia2 import inference, model
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "Maia-2 is not installed: use Python 3.10-3.12 and `uv sync --extra maia2`"
            ) from exc
        if model_type not in {"rapid", "blitz"}:
            raise ValueError("model_type must be 'rapid' or 'blitz'")
        self._torch = torch
        self._inference = inference
        want_cuda = device in {"gpu", "cuda"} and torch.cuda.is_available()
        self.device = "cuda" if want_cuda else "cpu"
        # from_pretrained prints progress to stdout, which would corrupt a UCI stream.
        with contextlib.redirect_stdout(sys.stderr):
            self._model = model.from_pretrained(
                type=model_type, device=self.device, save_root=str(save_root)
            )
        self._model.eval()
        self._all_moves, self._elo_dict, self._reverse = inference.prepare()
        self.batch_size = batch_size
        self.model_type = model_type

    # ------------------------------------------------------------------ protocol
    def distribution(self, board: chess.Board, elo_self: int, elo_oppo: int) -> dict[str, float]:
        """Human move distribution for the side to move (full precision, legal moves)."""
        return self.distributions([board], elo_self, elo_oppo)[0]

    def distributions(
        self, boards: Sequence[chess.Board], elo_self: int, elo_oppo: int
    ) -> list[dict[str, float]]:
        """Batched inference; boards without legal moves map to ``{}``."""
        out: list[dict[str, float]] = [{} for _ in boards]
        live = [i for i, b in enumerate(boards) if not b.is_game_over(claim_draw=False)]
        for start in range(0, len(live), self.batch_size):
            chunk = live[start : start + self.batch_size]
            for i, dist in zip(
                chunk, self._infer([boards[i] for i in chunk], elo_self, elo_oppo), strict=True
            ):
                out[i] = dist
        return out

    # ------------------------------------------------------------------ internals
    def _infer(
        self, boards: Sequence[chess.Board], elo_self: int, elo_oppo: int
    ) -> list[dict[str, float]]:
        torch = self._torch
        tensors: list[Any] = []
        selfs: list[int] = []
        oppos: list[int] = []
        masks: list[Any] = []
        for b in boards:
            board_input, e_self, e_oppo, legal = self._inference.preprocessing(
                b.fen(), elo_self, elo_oppo, self._elo_dict, self._all_moves
            )
            tensors.append(board_input)
            selfs.append(int(e_self))
            oppos.append(int(e_oppo))
            masks.append(legal)
        with torch.no_grad():
            x = torch.stack(tensors).to(self.device)
            es = torch.tensor(selfs, dtype=torch.long, device=self.device)
            eo = torch.tensor(oppos, dtype=torch.long, device=self.device)
            logits, _side_info, _value = self._model(x, es, eo)
            mask = torch.stack(masks).to(self.device) > 0
            logp = logits.float().masked_fill(~mask, float("-inf")).log_softmax(dim=-1)
            probs = logp.exp().cpu()
        results: list[dict[str, float]] = []
        for row, b, m in zip(probs, boards, masks, strict=True):
            idx = m.nonzero(as_tuple=True)[0].tolist()
            dist: dict[str, float] = {}
            for j in idx:
                uci = self._reverse[j]
                if b.turn == chess.BLACK:  # Maia-2 sees Black positions mirrored
                    uci = _mirror_uci(uci)
                dist[uci] = float(row[j])
            total = sum(dist.values())
            results.append({k: v / total for k, v in dist.items()} if total > 0 else dist)
        return results
