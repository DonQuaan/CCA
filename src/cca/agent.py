"""C-AIME agent: the per-move loop that joins perception, affect, chaos and decision.

One call to :meth:`CAIMEAgent.choose` does, in order:

1. **Perceive** — Stockfish MultiPV at the root (engine truth ``q_opt``).
2. **Appraise** the opponent's last move against what the human model predicted:
   surprisal ``S - H`` and reward-prediction error ``δ`` -> stress / drive (Module 1).
3. **Chaos** — kick the Lorenz oscillator with those events and advance one ply (Module 2).
4. **Candidates** — engine top-K ∪ human-prior top-M moves of the agent itself.
5. **Human-aware look-ahead** — for every candidate, the opponent's human reply distribution
   (Maia-2 at the opponent's rating, flattened by the estimated opponent stress) and the
   engine value of the likely replies -> ``q_human``, trap value, entropy, sharpness.
6. **Decide** (Module 3) — safe set (risk budget), utility, piKL closed form anchored on the
   agent's own (attention-masked) human prior, deterministic sampling.
7. **Theory of mind** — how surprising the chosen move is for the opponent -> opponent stress.
8. **Timing** — human-like think time.

The agent is resumable from a UCI ``position ... moves ...`` stream: if the move list is a
continuation of what it has seen it updates incrementally, otherwise it starts a new game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import chess

from cca.chaos import make_driver
from cca.chaos.lorenz import LorenzParams
from cca.core.rng import DeterministicRng
from cca.core.types import Candidate, Clock, Decision, Knobs, MoveEval, PsychState
from cca.neuro.controller import Persona, knobs_from_state
from cca.neuro.neuromodulation import (
    NeuroParams,
    attention_radius,
    battle_centroid,
    move_attention,
    surprisal,
    time_pressure,
    update_on_opponent_move,
    update_opponent_model,
)
from cca.policy.distributions import entropy, normalize, restrict, temper
from cca.policy.exploit import reply_stats, safe_set, utility
from cca.policy.pikl import pikl_policy
from cca.timing.think_time import ThinkTimeModel, ThinkTimeParams

if TYPE_CHECKING:
    from cca.engines.base import HumanModel, SearchEngine


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """All tunables of one agent (a persona plus search breadth and seeds)."""

    persona: Persona = field(default_factory=Persona)
    neuro: NeuroParams = field(default_factory=NeuroParams)
    lorenz: LorenzParams = field(default_factory=LorenzParams)
    timing: ThinkTimeParams = field(default_factory=ThinkTimeParams)
    elo_self: int = 1900
    elo_oppo: int = 1500
    engine_multipv: int = 5
    prior_top: int = 4
    prior_mass: float = 0.9
    reply_top: int = 5
    anchor_floor: float = 0.02
    opening_plies: int = 10
    """Maia-2 was not trained on plies < 10: its prior is tempered there."""
    opening_temperature: float = 3.0
    kick_rpe: float = 4.0
    kick_surprise: float = 0.4
    chaos_driver: str = "lorenz"
    """``"lorenz"`` (owner spec) or ``"ar1"`` (matched-autocorrelation control, prediction P5)."""
    safety_bank: bool = True
    """Risk only what the opponent has given away (RWYWE-style; Ganzfried & Sandholm 2015)."""
    ood_clock: float = 30.0
    """Maia-2 never saw moves made with <= 30 s left: exploitation is damped below this."""
    ood_exploit_factor: float = 0.5
    sample: bool = True
    seed: str = "cca"


@dataclass(frozen=True, slots=True)
class _Expectation:
    """What the agent predicted about the opponent's next move."""

    fen: str
    reply_dist: dict[str, float]
    v_pred: float
    q_opt: float


class CAIMEAgent:
    """Chaotic Active-Inference agent over a :class:`SearchEngine` and a :class:`HumanModel`."""

    def __init__(
        self,
        engine: SearchEngine,
        human: HumanModel,
        config: AgentConfig | None = None,
        game_id: str = "game-0",
    ) -> None:
        self.engine = engine
        self.human = human
        self.config = config or AgentConfig()
        self.new_game(game_id)

    # ------------------------------------------------------------------ lifecycle
    def new_game(self, game_id: str) -> None:
        """Reset all per-game state (affect, chaos, timing memory, history)."""
        cfg = self.config
        self.game_id = game_id
        self.state = PsychState(stress=cfg.neuro.c0, opp_stress=cfg.neuro.c0)
        self._osc = make_driver(cfg.chaos_driver, cfg.seed, game_id, lorenz=cfg.lorenz)
        self.state = replace(self.state, chaos=self._osc.signals())
        self._timer = ThinkTimeModel(cfg.timing, cfg.seed, game_id)
        self._history: list[str] = []
        self._expect: _Expectation | None = None
        self._ply_rng_counter = 0
        self._bank = 0.0
        self.engine.new_game()

    @property
    def chaos_digest(self) -> str:
        """Exact fingerprint of the chaotic state (reproducibility audits)."""
        return self._osc.digest()

    # ------------------------------------------------------------------ main loop
    def choose(self, board: chess.Board, clock: Clock | None = None) -> Decision:
        """Pick a move for the side to move in ``board``."""
        if board.is_game_over(claim_draw=False):
            raise ValueError("position is already game over")
        cfg = self.config
        clock = clock or Clock()
        color = board.turn
        trace: dict[str, float] = {}

        self._sync_history(board)

        # 1. Perceive.
        root = self.engine.evaluate(board, perspective=color, multipv=cfg.engine_multipv)
        if not root:
            raise RuntimeError("engine returned no evaluation for a non-terminal position")
        v_now = root[0].q
        trace["v_now"] = v_now

        # 2. Appraise the opponent's last move; 3. drive the chaos oscillator.
        kick = self._appraise(board, clock, v_now, trace)
        self._osc.advance_ply(kick)
        self.state = replace(self.state, chaos=self._osc.signals())

        # 4. Candidate set (the anchor is tempered where the human model is out of distribution).
        prior_full = self.human.distribution(board, cfg.elo_self, cfg.elo_oppo)
        prior_anchor = prior_full
        if board.ply() < cfg.opening_plies and cfg.opening_temperature != 1.0:
            prior_anchor = temper(prior_full, cfg.opening_temperature)
            trace["opening_tempered"] = 1.0
        root_q = self._evaluate_candidates(board, root, prior_anchor)

        own_pressure = time_pressure(
            clock.my_time,
            clock.my_inc,
            self._timer.moves_left(board.fullmove_number),
            cfg.neuro.time_ref,
        )
        knobs = knobs_from_state(
            self.state,
            cfg.persona,
            elo_self=cfg.elo_self,
            elo_oppo=cfg.elo_oppo,
            current_score=v_now,
            baseline=cfg.neuro.c0,
            pressure=own_pressure,
        )
        knobs = self._regulate(knobs, clock, v_now, trace)

        # 5. Human-aware look-ahead (+ attention mask on the agent's own prior).
        candidates, reply_dists = self._human_lookahead(board, root_q, prior_anchor, knobs)

        # 6. Decide.
        safe, policy = self._decide(candidates, knobs)
        move = self._select(policy)
        chosen = next(c for c in safe if c.uci == move)
        best_q = max(c.q_opt for c in candidates)
        self._bank -= max(0.0, best_q - chosen.q_opt)  # objective risk actually taken

        # 7. Theory of mind: our move's surprise for the opponent.
        our_excess = surprisal(prior_full, move) - entropy(prior_full)
        opp_pressure = time_pressure(
            clock.opp_time,
            clock.opp_inc,
            self._timer.moves_left(board.fullmove_number),
            cfg.neuro.time_ref,
        )
        self.state = update_opponent_model(
            self.state, cfg.neuro, our_surprise_excess=our_excess, opp_pressure=opp_pressure
        )

        # 8. Timing.
        think = self._timer.sample(
            move_number=board.fullmove_number,
            remaining=clock.my_time,
            increment=clock.my_inc,
            moves_to_go=clock.moves_to_go,
            prior_entropy=entropy(prior_full),
            top_prior=max(prior_full.values(), default=0.0),
            n_legal=board.legal_moves.count(),
        )

        fen_after, dist_after = reply_dists[move]
        self._expect = _Expectation(
            fen=fen_after, reply_dist=dist_after, v_pred=chosen.q_human, q_opt=chosen.q_opt
        )
        self._history.append(move)
        trace.update(
            {
                "q_opt": chosen.q_opt,
                "q_human": chosen.q_human,
                "trap_value": chosen.trap_value,
                "policy_entropy": entropy(policy),
                "n_candidates": float(len(candidates)),
                "n_safe": float(len(safe)),
                "our_surprise_excess": our_excess,
            }
        )
        return Decision(
            move=move,
            policy=policy,
            candidates=tuple(candidates),
            knobs=knobs,
            state=self.state,
            think_time=think,
            trace=trace,
        )

    # ------------------------------------------------------------------ helpers
    def _evaluate_candidates(
        self, board: chess.Board, root: list[MoveEval], prior: dict[str, float]
    ) -> dict[str, MoveEval]:
        """Engine top-K plus human top-M moves, each with an engine evaluation (ordered)."""
        root_q = {e.uci: e for e in root}
        cand_moves = self._candidate_moves(root, prior)
        missing = [m for m in cand_moves if m not in root_q]
        if missing:
            extra = self.engine.evaluate(
                board,
                perspective=board.turn,
                moves=[chess.Move.from_uci(m) for m in missing],
                multipv=len(missing),
            )
            root_q.update({e.uci: e for e in extra})
        return {m: root_q[m] for m in cand_moves if m in root_q}

    @staticmethod
    def _decide(
        candidates: list[Candidate], knobs: Knobs
    ) -> tuple[list[Candidate], dict[str, float]]:
        """Safe set, masked/sharpened anchor, utility and the closed-form piKL policy."""
        safe = safe_set(candidates, knobs.risk_budget)
        masked = {c.uci: c.prior * (c.attention**knobs.tunnel) for c in safe}
        anchor_safe = normalize(masked)
        if knobs.habit > 0.0:
            anchor_safe = temper(anchor_safe, 1.0 / (1.0 + knobs.habit))
        util = {c.uci: utility(c, knobs) for c in safe}
        return safe, pikl_policy(anchor_safe, util, knobs.kl_weight)

    def _regulate(self, knobs: Knobs, clock: Clock, v_now: float, trace: dict[str, float]) -> Knobs:
        """Apply the safety bank and the out-of-distribution guard to this move's knobs."""
        cfg = self.config
        if cfg.safety_bank:
            losing = min(1.0, max(0.0, (0.5 - v_now) * 2.0))
            allowance = cfg.persona.eps0 * (1.0 + cfg.persona.l_eps * losing)
            cap = allowance + max(0.0, self._bank)
            knobs = replace(knobs, risk_budget=min(knobs.risk_budget, cap))
            trace["risk_bank"] = self._bank
        if clock.opp_time is not None and clock.opp_time <= cfg.ood_clock:
            knobs = replace(knobs, exploit=knobs.exploit * cfg.ood_exploit_factor)
            trace["opp_clock_ood"] = 1.0
        return knobs

    def _sync_history(self, board: chess.Board) -> None:
        """Keep incremental state if ``board`` continues the game we know, else reset."""
        moves = [m.uci() for m in board.move_stack]
        known = self._history
        continues = moves[: len(known)] == known and len(moves) - len(known) <= 1
        if not continues:
            self.new_game(f"{self.game_id}+{len(moves)}")
            self._history = moves[:-1] if moves else []
            self._expect = None
        if len(moves) > len(self._history):
            self._history.append(moves[-1])

    def _appraise(
        self, board: chess.Board, clock: Clock, v_now: float, trace: dict[str, float]
    ) -> tuple[float, float, float]:
        cfg = self.config
        if not board.move_stack:
            return (0.0, 0.0, 0.0)
        before = board.copy(stack=True)
        last = before.pop()  # always the opponent's move: it is our turn now
        if self._expect is not None and self._expect.fen == before.fen():
            dist = self._expect.reply_dist
            rpe = v_now - self._expect.v_pred
            # Gift = how much better than its best reply the opponent let us be (can be < 0
            # through engine noise); it funds later objective risk-taking.
            self._bank += v_now - self._expect.q_opt
        else:
            dist = self.human.distribution(before, cfg.elo_oppo, cfg.elo_self)
            rpe = 0.0
        excess = surprisal(dist, last.uci()) - entropy(dist) if dist else 0.0
        pressure = time_pressure(
            clock.my_time,
            clock.my_inc,
            self._timer.moves_left(board.fullmove_number),
            cfg.neuro.time_ref,
        )
        self.state = update_on_opponent_move(
            self.state, cfg.neuro, surprise_excess=excess, rpe=rpe, pressure=pressure
        )
        trace.update({"opp_surprise_excess": excess, "rpe": rpe, "time_pressure": pressure})
        return (cfg.kick_rpe * rpe, cfg.kick_surprise * excess, 0.0)

    def _candidate_moves(self, root: list[MoveEval], prior: dict[str, float]) -> list[str]:
        cfg = self.config
        moves = [e.uci for e in root]
        mass = 0.0
        for uci, p in sorted(prior.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.prior_top]:
            if mass >= cfg.prior_mass:
                break
            moves.append(uci)
            mass += p
        return list(dict.fromkeys(moves))

    def _human_lookahead(
        self,
        board: chess.Board,
        root_q: dict[str, MoveEval],
        prior_anchor: dict[str, float],
        knobs: Knobs,
    ) -> tuple[list[Candidate], dict[str, tuple[str, dict[str, float]]]]:
        """Candidates with human-aware statistics; opponent models are queried in one batch."""
        cfg = self.config
        cand_moves = list(root_q)
        centroid = battle_centroid(self._history[-4:])
        sigma = attention_radius(self.state.stress, threshold=cfg.persona.tau)
        anchor = restrict(prior_anchor, cand_moves, floor=cfg.anchor_floor)
        # Keep the move stack: the engine must see repetitions in the child positions.
        afters = {uci: board.copy() for uci in cand_moves}
        for uci, child in afters.items():
            child.push_uci(uci)
        live = [u for u in cand_moves if not afters[u].is_game_over(claim_draw=False)]
        batched = self.human.distributions([afters[u] for u in live], cfg.elo_oppo, cfg.elo_self)
        opp_dists = dict(zip(live, batched, strict=True))
        candidates: list[Candidate] = []
        reply_dists: dict[str, tuple[str, dict[str, float]]] = {}
        for uci in cand_moves:
            cand, fen_after, dist = self._lookahead(
                afters[uci], root_q[uci], knobs.opp_temperature, opp_dists.get(uci, {}), board.turn
            )
            attn = move_attention(uci, centroid, sigma)
            candidates.append(replace(cand, prior=anchor[uci], attention=attn))
            reply_dists[uci] = (fen_after, dist)
        return candidates, reply_dists

    def _lookahead(
        self,
        after: chess.Board,
        root_eval: MoveEval,
        opp_temperature: float,
        dist: dict[str, float],
        color: chess.Color,
    ) -> tuple[Candidate, str, dict[str, float]]:
        """Evaluate the opponent's likely human replies in the position after a candidate."""
        cfg = self.config
        fen_after = after.fen()
        q_opt = root_eval.q
        if after.is_game_over(claim_draw=False) or not dist:
            stats = reply_stats({}, {}, q_opt)
            return (
                Candidate(root_eval.uci, q_opt, stats.q_human, 0.0, 0.0, 0.0),
                fen_after,
                {},
            )
        replies = [
            u for u, _ in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.reply_top]
        ]
        if len(root_eval.pv) > 1:
            replies.append(root_eval.pv[1])  # always include the engine's refutation
        replies = [u for u in dict.fromkeys(replies) if chess.Move.from_uci(u) in after.legal_moves]
        evals = self.engine.evaluate(
            after,
            perspective=color,
            moves=[chess.Move.from_uci(u) for u in replies],
            multipv=len(replies),
        )
        scores = {e.uci: e.q for e in evals}
        if scores:
            q_opt = min(q_opt, *scores.values())  # a deeper refutation found here wins
        stats = reply_stats(dist, scores, q_opt, opp_temperature)
        cand = Candidate(
            uci=root_eval.uci,
            q_opt=q_opt,
            q_human=stats.q_human,
            prior=0.0,
            opp_entropy=stats.entropy,
            sharpness=stats.sharpness,
        )
        return cand, fen_after, dist

    def _select(self, policy: dict[str, float]) -> str:
        if not self.config.sample:
            return max(sorted(policy), key=lambda k: policy[k])
        self._ply_rng_counter += 1
        rng = DeterministicRng(
            "move", self.config.seed, self.game_id, len(self._history), self._ply_rng_counter
        )
        return rng.choice(policy)


def expected_score_to_cp(q: float) -> float:
    """Rough inverse of a logistic win-rate curve (for human-readable logs only)."""
    q = min(1.0 - 1e-6, max(1e-6, q))
    return 400.0 * math.log10(q / (1.0 - q))
