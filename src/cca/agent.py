"""C-AIME agent: the per-move loop that joins perception, affect, chaos and decision.

One call to :meth:`CAIMEAgent.choose` does, in order:

1. **Perceive** — Stockfish MultiPV at the root (engine truth ``q_opt``).
2. **Appraise** the opponent's last move against what the human model predicted:
   surprisal ``S - H`` and reward-prediction error ``δ`` -> stress / drive (Module 1).
3. **Chaos** — kick the chaos driver with those events and advance one ply (Module 2).
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

Real-time play: callers that run against a wall clock pass ``deadline`` (monotonic seconds)
and/or ``stop``; engine calls then get time slices, the look-ahead is cut short when time runs
out (remaining candidates keep ``q_human = q_opt``), and below ``fast_budget`` seconds the agent
answers in *reflex* mode with the engine's best move. Without a deadline (analysis, the
virtual-clock benchmark) search is purely node-limited and therefore reproducible.
"""

from __future__ import annotations

import math
import time
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
    import threading

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
    opening_min_pieces: int = 28
    """A position only counts as an opening if it still has this many pieces (FENs of endgames
    often carry the move number 1)."""
    opening_temperature: float = 3.0
    kick_rpe: float = 4.0
    kick_surprise: float = 0.4
    chaos_driver: str = "lorenz"
    """``"lorenz"`` (owner spec) or ``"ar1"`` (calibrated stochastic control, prediction P5)."""
    safety_bank: bool = True
    """Risk only what the opponent has given away (RWYWE-style; Ganzfried & Sandholm 2015)."""
    bank_dead_zone: float = 0.02
    """Evaluation differences below this are treated as engine noise, not as gifts."""
    ood_clock: float = 30.0
    """Maia-2 never saw moves made with <= 30 s left: exploitation is damped below this."""
    ood_exploit_factor: float = 0.5
    fast_budget: float = 0.6
    """With a deadline closer than this (seconds), answer in reflex mode (engine best move)."""
    sample: bool = True
    seed: str = "cca"


@dataclass(frozen=True, slots=True)
class _Expectation:
    """What the agent predicted about the opponent's next move."""

    fen: str
    reply_dist: dict[str, float]
    reply_scores: dict[str, float]
    v_pred: float
    q_opt: float


def is_ended(board: chess.Board) -> bool:
    """Game over *or* a draw an arbiter / auto-adjudicating GUI would end the game with."""
    return board.is_game_over(claim_draw=False) or board.is_repetition(3) or board.is_fifty_moves()


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
        self._deadline: float | None = None
        self._stop: threading.Event | None = None
        self.engine.new_game()

    @property
    def chaos_digest(self) -> str:
        """Exact fingerprint of the chaotic state (reproducibility audits)."""
        return self._osc.digest()

    def deadline_for(
        self, board: chess.Board, clock: Clock, movetime: float | None = None
    ) -> float | None:
        """Wall-clock compute deadline (``time.monotonic()`` seconds) for a timed move."""
        t = self.config.timing
        if movetime is not None:
            budget = max(0.0, movetime - 0.05)
        elif clock.my_time is not None:
            left = self._timer.horizon(board.fullmove_number, clock.moves_to_go)
            per_move = max(0.0, clock.my_time + clock.my_inc * left) / max(1.0, left)
            ceiling = min(t.max_fraction * clock.my_time, clock.my_time - t.safety_margin)
            budget = max(0.0, min(ceiling, max(per_move, self.config.fast_budget)))
        else:
            return None
        return time.monotonic() + budget

    # ------------------------------------------------------------------ main loop
    def choose(
        self,
        board: chess.Board,
        clock: Clock | None = None,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> Decision:
        """Pick a move for the side to move in ``board``.

        ``deadline`` (``time.monotonic()`` seconds) and ``stop`` bound the computation in
        real-time play; leave them ``None`` for reproducible, node-limited search.
        """
        if board.is_game_over(claim_draw=False):
            raise ValueError("position is already game over")
        cfg = self.config
        clock = clock or Clock()
        self._deadline, self._stop = deadline, stop
        trace: dict[str, float] = {}
        self._sync_history(board)
        if deadline is not None and deadline - time.monotonic() < cfg.fast_budget:
            return self._reflex(board, clock, trace)

        # 1. Perceive.
        root = self.engine.evaluate(
            board, perspective=board.turn, multipv=cfg.engine_multipv, time_limit=self._slice(4)
        )
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
        if self._in_opening(board) and cfg.opening_temperature != 1.0:
            prior_anchor = temper(prior_full, cfg.opening_temperature)
            trace["opening_tempered"] = 1.0
        root_q = self._evaluate_candidates(board, root, prior_anchor)
        knobs = self._knobs(board, clock, v_now, trace)

        # 5. Human-aware look-ahead (+ attention mask on the agent's own prior).
        candidates, lookups = self._human_lookahead(board, root_q, prior_anchor, knobs, trace)

        # 6. Decide.
        safe, policy = self._decide(candidates, knobs)
        move = self._select(policy)
        chosen = next(c for c in safe if c.uci == move)
        self._bank -= max(0.0, max(c.q_opt for c in candidates) - chosen.q_opt)  # risk taken

        # 7. Theory of mind; 8. timing.
        our_excess = self._update_opponent_model(board, clock, prior_full, move)
        think = self._think(board, clock, prior_full)

        fen_after, dist_after, scores_after = lookups[move]
        self._expect = _Expectation(
            fen=fen_after,
            reply_dist=dist_after,
            reply_scores=scores_after,
            v_pred=chosen.q_human,
            q_opt=chosen.q_opt,
        )
        self._history.append(move)
        trace.update(
            {
                "q_opt": chosen.q_opt,
                "q_human": chosen.q_human,
                "trap_value": chosen.trap_value,
                "policy_entropy": entropy(policy),
                "human_logp": -surprisal(prior_full, move),
                "engine_best": 1.0 if move == root[0].uci else 0.0,
                "n_candidates": float(len(candidates)),
                "n_safe": float(len(safe)),
                "our_surprise_excess": our_excess,
            }
        )
        return Decision(move, policy, tuple(candidates), knobs, self.state, think, trace)

    # ------------------------------------------------------------------ steps
    def _reflex(self, board: chess.Board, clock: Clock, trace: dict[str, float]) -> Decision:
        """Almost out of time: play the engine's best move, keep chaos and history in step."""
        remaining = max(0.0, (self._deadline or time.monotonic()) - time.monotonic())
        root = self.engine.evaluate(
            board, perspective=board.turn, multipv=1, time_limit=max(0.01, 0.5 * remaining)
        )
        if not root:
            raise RuntimeError("engine returned no evaluation for a non-terminal position")
        move = root[0].uci
        self._osc.advance_ply()
        self.state = replace(self.state, chaos=self._osc.signals())
        self._expect = None
        self._history.append(move)
        knobs = knobs_from_state(
            self.state,
            self.config.persona,
            elo_self=self.config.elo_self,
            elo_oppo=self.config.elo_oppo,
            current_score=root[0].q,
            baseline=self.config.neuro.c0,
        )
        cand = Candidate(move, root[0].q, root[0].q, 1.0, 0.0, 0.0)
        trace.update({"reflex": 1.0, "q_opt": root[0].q, "q_human": root[0].q, "engine_best": 1.0})
        think = self._timer.sample(
            move_number=board.fullmove_number,
            remaining=clock.my_time,
            increment=clock.my_inc,
            moves_to_go=clock.moves_to_go,
        )
        return Decision(move, {move: 1.0}, (cand,), knobs, self.state, think, trace)

    def _knobs(
        self, board: chess.Board, clock: Clock, v_now: float, trace: dict[str, float]
    ) -> Knobs:
        cfg = self.config
        own_pressure = time_pressure(
            clock.my_time,
            clock.my_inc,
            self._timer.horizon(board.fullmove_number, clock.moves_to_go),
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
        return self._regulate(knobs, clock, v_now, trace)

    def _update_opponent_model(
        self, board: chess.Board, clock: Clock, prior_full: dict[str, float], move: str
    ) -> float:
        cfg = self.config
        our_excess = surprisal(prior_full, move) - entropy(prior_full)
        opp_pressure = time_pressure(
            clock.opp_time,
            clock.opp_inc,
            self._timer.horizon(board.fullmove_number, clock.moves_to_go),
            cfg.neuro.time_ref,
        )
        self.state = update_opponent_model(
            self.state, cfg.neuro, our_surprise_excess=our_excess, opp_pressure=opp_pressure
        )
        return our_excess

    def _think(self, board: chess.Board, clock: Clock, prior_full: dict[str, float]) -> float:
        return self._timer.sample(
            move_number=board.fullmove_number,
            remaining=clock.my_time,
            increment=clock.my_inc,
            moves_to_go=clock.moves_to_go,
            prior_entropy=entropy(prior_full),
            top_prior=max(prior_full.values(), default=0.0),
            n_legal=board.legal_moves.count(),
        )

    # ------------------------------------------------------------------ helpers
    def _out_of_time(self) -> bool:
        if self._stop is not None and self._stop.is_set():
            return True
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _slice(self, calls_left: int) -> float | None:
        """Per-call time limit when running against a deadline (``None`` = nodes only)."""
        if self._deadline is None:
            return None
        return max(0.01, (self._deadline - time.monotonic()) / max(1, calls_left))

    def _in_opening(self, board: chess.Board) -> bool:
        cfg = self.config
        return board.ply() < cfg.opening_plies and len(board.piece_map()) >= cfg.opening_min_pieces

    def _evaluate_candidates(
        self, board: chess.Board, root: list[MoveEval], prior: dict[str, float]
    ) -> dict[str, MoveEval]:
        """Engine top-K plus human top-M moves, each with an engine evaluation (ordered)."""
        root_q = {e.uci: e for e in root}
        cand_moves = self._candidate_moves(root, prior)
        missing = [m for m in cand_moves if m not in root_q]
        if missing and not self._out_of_time():
            extra = self.engine.evaluate(
                board,
                perspective=board.turn,
                moves=[chess.Move.from_uci(m) for m in missing],
                multipv=len(missing),
                time_limit=self._slice(len(cand_moves) + 1),
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
            # A negative balance (risk already taken beyond what was given) shrinks the budget.
            cap = max(0.0, allowance + self._bank)
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
        last = before.pop().uci()  # always the opponent's move: it is our turn now
        exp = self._expect
        if exp is not None and exp.fen == before.fen():
            dist = exp.reply_dist
            rpe = v_now - exp.v_pred
            # Gift, measured like-for-like inside the same restricted search: how much worse
            # the reply played is for the opponent than their best evaluated reply. Replies
            # that were not evaluated only count beyond a noise dead zone.
            sc = exp.reply_scores
            if sc and last in sc:
                gift = sc[last] - min(sc.values())
            else:
                gift = max(0.0, v_now - exp.q_opt - cfg.bank_dead_zone)
            self._bank += gift
            trace["gift"] = gift
        else:
            dist = self.human.distribution(before, cfg.elo_oppo, cfg.elo_self)
            rpe = 0.0
        if dist and self._in_opening(before) and cfg.opening_temperature != 1.0:
            dist = temper(dist, cfg.opening_temperature)  # the raw opening prior is unreliable
        excess = surprisal(dist, last) - entropy(dist) if dist else 0.0
        pressure = time_pressure(
            clock.my_time,
            clock.my_inc,
            self._timer.horizon(board.fullmove_number, clock.moves_to_go),
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
        trace: dict[str, float],
    ) -> tuple[list[Candidate], dict[str, tuple[str, dict[str, float], dict[str, float]]]]:
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
        live = [u for u in cand_moves if not is_ended(afters[u])]
        batched = self.human.distributions([afters[u] for u in live], cfg.elo_oppo, cfg.elo_self)
        opp_dists = dict(zip(live, batched, strict=True))
        candidates: list[Candidate] = []
        lookups: dict[str, tuple[str, dict[str, float], dict[str, float]]] = {}
        for i, uci in enumerate(cand_moves):
            cut = self._out_of_time()
            if cut:
                trace.setdefault("lookahead_cut", float(i))  # index of the first skipped candidate
            cand, scores = self._lookahead(
                afters[uci],
                root_q[uci],
                opp_temperature=knobs.opp_temperature,
                dist={} if cut else opp_dists.get(uci, {}),
                color=board.turn,
                time_limit=self._slice(len(cand_moves) - i),
            )
            attn = move_attention(uci, centroid, sigma)
            candidates.append(replace(cand, prior=anchor[uci], attention=attn))
            lookups[uci] = (afters[uci].fen(), opp_dists.get(uci, {}), scores)
        return candidates, lookups

    def _lookahead(
        self,
        after: chess.Board,
        root_eval: MoveEval,
        *,
        opp_temperature: float,
        dist: dict[str, float],
        color: chess.Color,
        time_limit: float | None,
    ) -> tuple[Candidate, dict[str, float]]:
        """Evaluate the opponent's likely human replies in the position after a candidate."""
        cfg = self.config
        q_opt = root_eval.q
        if is_ended(after) or not dist:
            # Finished (incl. threefold / fifty-move draws the engine already scored at the
            # root) or no reply model available: no human-aware information.
            return Candidate(root_eval.uci, q_opt, q_opt, 0.0, 0.0, 0.0), {}
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
            time_limit=time_limit,
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
        return cand, scores

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
