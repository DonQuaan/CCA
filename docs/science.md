# Scientific basis, honest labels and falsifiable predictions

This page is the curated version of the adversarially verified research brief
(`docs/research/2026-09-25-verification-brief.md`: 8 research dimensions, each re-checked by
an independent refuting verifier, then merged). Every citation below was opened and checked.
**If a claim is not on this page, CCA does not make it.**

## 1. What each C-AIME mechanism is — and is not

| Mechanism (code) | Status | Required label in docs and talks |
|---|---|---|
| Human prior / opponent model (`engines.maia2_human`) | **Empirically grounded.** Maia predicts human moves by rating (McIlroy-Young et al. 2020; Tang et al. 2024). | Out of distribution for plies < 10, for moves made with ≤ 30 s on the clock (never trained on), and for ratings < 1100 or ≥ 2000 (bins saturate). |
| piKL anchor (`policy.pikl`) | **Grounded.** KL-regularised search yields play that is both strong and human-like (Jacob et al. 2022). | CCA uses the canonical closed form `π ∝ τ·exp(U/λ)`. The owner spec's objective (KL weight fixed to 1 plus `λ·H(s')`) is *not* piKL; `H(s')` is a CCA design term with no literature support, kept small and testable (P7). |
| Human-aware look-ahead, trap value (`policy.exploit`) | **Plausible, untested on humans.** Closest evidence: 2-ply "expector" agents against Maia bots in collaborative play (Hamade et al. 2024). | A hypothesis about exploiting human reply distributions; no controlled human study exists yet (P8). |
| Safe exploitation, risk bank (`safe_set`, `agent._regulate`) | **Principle from game theory** (Ganzfried & Sandholm 2015; Johanson et al. 2007). | A *heuristic analogue*: chess has no known game value and Stockfish's evaluation is not a payoff, so the formal safety guarantees do not transfer. |
| Latent stress, "virtual cortisol" (`neuro.neuromodulation`) | **Metaphor.** Cortisol responds to uncontrollable / social-evaluative stressors over tens of minutes (Dickerson & Kemeny 2004), not per move; fast arousal in chess shows up in heart rate / HRV. | "A dimensionless latent *arousal* variable updated each ply, inspired by — not a model of — stress physiology." |
| Surprise driver `S − H` | **Grounded definition** (surprisal, `−ln p`; Smith & Levy 2013). Note: Itti & Baldi's *Bayesian* surprise is a different quantity (a KL divergence). | Behavioural effect on humans is unmeasured (P1). |
| Drive / "virtual dopamine" (reward-prediction error) | **Only the RPE role is solid** (Schultz, Dayan & Montague 1997). "Pleasure"/risk claims are not. | "A leaky integrator of evaluation surprises (RPE)." |
| Tunnel vision: spatial mask (`move_attention`) | **Phenomenon real, form invented.** Einstellung in chess (Bilalić, McLeod & Gobet 2008), cue-utilisation (Easterbrook 1959) — but no evidence for a *spatial Gaussian*, and acute stress has also *improved* set-shifting (Gabrys et al. 2019). | "An engineering heuristic inspired by …; direction and form are hypotheses" (P4). |
| Stress → habitual choice (`knobs.habit`) | **Grounded direction** (stress shifts control from goal-directed to habitual; Schwabe & Wolf 2009). | Prediction P4. |
| Risk signs (`c_clock`, `d_tilt`) | **Field data:** less clock time → more risk-averse moves; more risk after one's own mistakes (Carow & Witzig 2025, FIDE World Cups; ML risk proxy). | The owner spec assumed the opposite for time pressure; CCA follows the data (P3). `d_tilt` reacts to *negative surprises* (worse-than-predicted outcomes), only a loose proxy for "own mistakes". |
| Deterministic chaos (`chaos.lorenz`) | **No evidence** that Lorenz dynamics model emotion or any player's temperament; the best-known Lorenz emotion model (Losada) was formally withdrawn (2013). | "A reproducible, structured source of variability. It gives no extra unpredictability to an observer over a keyed PRNG unless P5 shows otherwise." Tal / Dubov personas are *inspired by public reputation, not fitted*. |
| Free Energy Principle / Active Inference | **Framework, not evidence**; falsifiability is contested. | Named as inspiration only; no mechanism in v0.1 claims to implement it. |
| Working memory "7 ± 2" | **Outdated** — about 4 (Cowan 2001); chess experts use few but large chunks. | Not used as a parameter. |
| Think-time model (`timing`) | **Form grounded** (right-skewed RT distributions; chess move-time structure: Sigman et al. 2010; time pressure and decision quality: Sunde, Zegners & Strittmatter, PNAS 2026). **Parameters hand-set.** | "Placeholder parameters, to be fitted on CC0 Lichess clocks" (P9). |

## 2. Pre-registered, falsifiable predictions

Each mechanism survives only if it passes its test; otherwise it is removed or relabelled as
pure design. Engine A/B tests use SPRT; human data use mixed-effects models (moves are
clustered within players). No post-hoc tuning on the test set.

| ID | Mechanism | Test | Kill criterion |
|---|---|---|---|
| P1 | Surprise → arousal | Regress humans' reply think time and win-probability loss on the Maia surprisal of the preceding opponent move (controls: eval, policy entropy, top-2 gap, clock, ratings, player effects), CC0 Lichess data. | 95 % CI of the surprisal coefficient includes 0 at adequate power. |
| P2 | Arousal ODE | CCA with the ODE must predict human moves (NLL, top-1) better than Maia alone after high-surprise moves and at low clock; ablations: ODE off, ODE series shuffled across games, "just weaker" Maia. | No gain over every ablation, or fitted half-life → 0. |
| P3 | Risk signs | Low clock → risk-averse; recent negative swing → risk-seeking, in human data and in CCA (`c_clock`; `d_tilt` fires on negative surprises, which include the opponent playing unexpectedly well — test both "own mistake" and "opponent surprise" swings separately). | CCA cannot reproduce both signs, or `d_tilt` only tracks opponent surprises → split the signal. |
| P4 | Narrowing (mask, habit) | Under low clock, realised choice entropy relative to Maia's policy entropy falls; CCA with the mechanism must match this curve better than without. | No improvement in fit. |
| P5 | Lorenz chaos | Same Elo: (A) Lorenz vs (B) phase-randomised surrogate (roadmap) vs (C) `chaos_driver="ar1"` (calibrated to the consumed signals' lag-1 autocorrelation, u0–u1 cross-correlation and kick response; a test keeps it calibrated). Metric: opponent-side predictability of CCA's moves (`subject_engine_agreement`, NLL) and expected score. | A not significantly better than B and C → default to the control. |
| P6 | Named styles | A stylometry model must place CCA-"tal" games closer to Tal than baseline Maia at equal rating. | Otherwise withdraw the style claim. |
| P7 | `λ_H·H(s')` term | At matched soundness (rate of Stockfish expected-score drops > 0.1), with vs without the term: opponent error against Maia-k (1100–1900), then on a BOT account. | No gain → delete the term. |
| P8 | Human-aware look-ahead | Versus baselines (plain Maia, Stockfish `UCI_LimitStrength`, Lc0 WDL-contempt, a 2-ply expector) at matched soundness. | No advantage. |
| P9 | Think-time model | Held-out NLL, per-phase distribution distances, lag-1…5 autocorrelation, share of clock used by move 40. | Worse than Allie's reported fit, or flagged more often than humans. |

**Circularity warning.** Evaluating a Maia-exploiting agent against Maia opponents measures
how well it exploits *that model*. Every claim about humans needs games against people
(declared BOT account, consent, ethics review) or a held-out human model.

## 3. Ethics and platform rules

* Only a declared **Lichess BOT account** may play with engine assistance (fresh account, the
  upgrade is irreversible, challenges only, no pools/tournaments, no sandbagging). Using CCA to
  help a human account is cheating on every platform. Chess.com rules were not checked.
* Games against people "to exploit cognitive weaknesses" are **human-subjects research**:
  inform opponents, obtain consent and an IRB-style review before collecting or publishing
  data. Offline analysis of CC0 Lichess data avoids this.
* Nothing in CCA may be tuned to evade anti-cheat detection.

## 4. Verified bibliography

Human-move models and search
- McIlroy-Young, R., Sen, S., Kleinberg, J., Anderson, A. (2020). Aligning Superhuman AI with Human Behavior: Chess as a Model System. *KDD '20* (26th ACM SIGKDD), 1677–1687. doi:10.1145/3394486.3403219 · arXiv:2006.01855
- Tang, Z., Jiao, D., McIlroy-Young, R., Kleinberg, J., Sen, S., Anderson, A. (2024). Maia-2: A Unified Model for Human-AI Alignment in Chess. *NeurIPS 37*, 20919–20944. arXiv:2409.20553
- Monroe, D., Eilender, G., Chalmers, P., Tang, Z., Anderson, A. (2026). Chessformer: A Unified Architecture for Chess Modeling (Maia-3). *ICLR 2026*. arXiv:2605.19091
- Zhang, Y., Jacob, A. P., Lai, V., Fried, D., Ippolito, D. (2025). Human-Aligned Chess With a Bit of Search (Allie). *ICLR 2025*. arXiv:2410.03893
- Hamade, K., McIlroy-Young, R., Sen, S., Kleinberg, J., Anderson, A. (2024). Designing Skill-Compatible AI: Methodologies and Frameworks in Chess. *ICLR 2024*. arXiv:2405.05066
- McIlroy-Young, R., Wang, R., Sen, S., Kleinberg, J., Anderson, A. (2022). Learning Models of Individual Behavior in Chess. *KDD '22*, 1253–1263. arXiv:2008.10086
- Ruoss, A., et al. (2024). Amortized Planning with Large-Scale Transformers: A Case Study on Chess. *NeurIPS 37*. arXiv:2402.04494

KL-regularised search, equilibria, exploitation
- Jacob, A. P., Wu, D. J., Farina, G., Lerer, A., Hu, H., Bakhtin, A., Andreas, J., Brown, N. (2022). Modeling Strong and Human-Like Gameplay with KL-Regularized Search (piKL). *ICML 2022*, PMLR 162:9695–9728. arXiv:2112.07544
- Grill, J.-B., Altché, F., Tang, Y., Hubert, T., Valko, M., Antonoglou, I., Munos, R. (2020). Monte-Carlo Tree Search as Regularized Policy Optimization. *ICML 2020*, PMLR 119:3769–3778. arXiv:2007.12509
- Bakhtin, A., et al. (2023). Mastering the Game of No-Press Diplomacy via Human-Regularized Reinforcement Learning and Planning (DiL-piKL). *ICLR 2023*. arXiv:2210.05492
- Meta FAIR Diplomacy Team, Bakhtin, A., et al. (2022). Human-level play in the game of Diplomacy by combining language models with strategic reasoning (Cicero). *Science* 378(6624):1067–1074. doi:10.1126/science.ade9097
- McKelvey, R. D., Palfrey, T. R. (1995). Quantal Response Equilibria for Normal Form Games. *Games and Economic Behavior* 10(1):6–38. doi:10.1006/game.1995.1023
- Sokota, S., D'Orazio, R., Kolter, J. Z., Loizou, N., Lanctot, M., Mitliagkas, I., Brown, N., Kroer, C. (2023). A Unified Approach to Reinforcement Learning, Quantal Response Equilibria, and Two-Player Zero-Sum Games (MMD). *ICLR 2023*. arXiv:2206.05825
- Ganzfried, S., Sandholm, T. (2015). Safe Opponent Exploitation. *ACM TEAC* 3(2), Art. 8. doi:10.1145/2716322
- Johanson, M., Zinkevich, M., Bowling, M. (2007). Computing Robust Counter-Strategies. *NIPS 20* (2007).
- Regan, K. W., Haworth, G. McC. (2011). Intrinsic Chess Ratings. *AAAI-11*, 834–839. doi:10.1609/aaai.v25i1.7951 (a one-sided stochastic choice model, not a QRE)

Human error, time and difficulty
- Anderson, A., Kleinberg, J., Mullainathan, S. (2016). Assessing Human Error Against a Benchmark of Perfection. *KDD '16*, 705–714. doi:10.1145/2939672.2939803 · arXiv:1606.04956 (tablebase positions: difficulty dominates skill and time)
- Sunde, U., Zegners, D., Strittmatter, A. (2026). Speed and quality of complex strategic decisions. *PNAS* 123(20):e2531472123. doi:10.1073/pnas.2531472123 (working paper: arXiv:2201.10808)
- Sigman, M., Etchemendy, P., Fernández Slezak, D., Cecchi, G. A. (2010). Response time distributions in rapid chess: a large-scale decision making experiment. *Frontiers in Neuroscience* 4:60. doi:10.3389/fnins.2010.00060
- Carow, J., Witzig, N. M. (2025). Time pressure and strategic risk-taking in professional chess. *J. Economic Behavior & Organization* 238:107218. doi:10.1016/j.jebo.2025.107218

Cognition, stress, neuroscience
- Bilalić, M., McLeod, P., Gobet, F. (2008). Why good thoughts block better ones: The mechanism of the pernicious Einstellung (set) effect. *Cognition* 108(3):652–661. doi:10.1016/j.cognition.2008.05.005
- Gobet, F., Simon, H. A. (1996). Templates in chess memory. *Cognitive Psychology* 31(1):1–40. doi:10.1006/cogp.1996.0011
- Cowan, N. (2001). The magical number 4 in short-term memory. *Behavioral and Brain Sciences* 24(1):87–114. doi:10.1017/S0140525X01003922
- Schultz, W., Dayan, P., Montague, P. R. (1997). A neural substrate of prediction and reward. *Science* 275(5306):1593–1599. doi:10.1126/science.275.5306.1593
- Schwabe, L., Wolf, O. T. (2009). Stress prompts habit behavior in humans. *J. Neuroscience* 29(22):7191–7198. doi:10.1523/JNEUROSCI.0979-09.2009
- Easterbrook, J. A. (1959). *Psychological Review* 66(3):183–201. doi:10.1037/h0047707
- Gabrys, R. L., et al. (2019). *Stress* 22(2):182–189. doi:10.1080/10253890.2018.1494152 (acute stress improved set-shifting)
- Smith, N. J., Levy, R. (2013). *Cognition* 128(3):302–319. doi:10.1016/j.cognition.2013.02.013 (surprisal)
- Dickerson, S. S., Kemeny, M. E. (2004). Acute stressors and cortisol responses: a theoretical integration and synthesis of laboratory research. *Psychological Bulletin* 130(3):355–391. doi:10.1037/0033-2909.130.3.355
- Friston, K. (2010). The free-energy principle: a unified brain theory? *Nature Reviews Neuroscience* 11(2):127–138. doi:10.1038/nrn2787
- Da Costa, L., Parr, T., Sajid, N., Veselic, S., Neacsu, V., Friston, K. (2020). Active inference on discrete state-spaces: A synthesis. *J. Mathematical Psychology* 99:102447. arXiv:2001.07203
- Heins, C., et al. (2022). pymdp: A Python library for active inference in discrete state spaces. *JOSS* 7(73):4098. doi:10.21105/joss.04098

Chaos
- Lorenz, E. N. (1963). Deterministic Nonperiodic Flow. *J. Atmospheric Sciences* 20(2):130–141. doi:10.1175/1520-0469(1963)020<0130:DNF>2.0.CO;2

**Do not cite** (verified wrong): "Maia-2 … arXiv:2406.07548" — that identifier is an image-tokenization
paper (Zhao, Xiong & Krähenbühl 2024); the "12 million games" and "21.26 % premoves" timing claims
propagated by Human-Chess-Bot (no data behind them).
