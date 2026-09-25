"""Module 1: virtual neuromodulation (latent stress / drive, tunnel vision, theory of mind)."""

from cca.neuro.controller import Persona, knobs_from_state
from cca.neuro.neuromodulation import (
    NeuroParams,
    attention_radius,
    battle_centroid,
    move_attention,
    relax,
    surprisal,
    time_pressure,
    update_on_opponent_move,
    update_opponent_model,
)

__all__ = [
    "NeuroParams",
    "Persona",
    "attention_radius",
    "battle_centroid",
    "knobs_from_state",
    "move_attention",
    "relax",
    "surprisal",
    "time_pressure",
    "update_on_opponent_move",
    "update_opponent_model",
]
