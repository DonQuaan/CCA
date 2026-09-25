"""CCA — Chaotic-Chess-Algorithm.

A research layer that sits on top of a strong search engine (Stockfish, run as a separate
UCI process) and a human move-prediction model (Maia-2) and turns their outputs into moves
that are human-like, hard to predict, and aimed at the opponent's likely mistakes.

The decision core is C-AIME (Chaotic Active-Inference MCTS Engine), see ``docs/architecture.md``.

Copyright 2026 Nguyễn Vũ Đông Quân (DonQuaan). Licensed under the Apache License 2.0.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
