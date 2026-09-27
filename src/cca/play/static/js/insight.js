// Insight panel: candidates, knobs, latent state and a plain-language "why" - part of CCA (Apache-2.0).
// All text goes through textContent; numbers come from the server's decision JSON.

import {t} from "./i18n.js"
import {ARROW} from "./board.js"

const KNOBS = ["kl_weight", "exploit", "entropy_bonus", "risk_budget", "tunnel", "habit", "opp_temperature"]
const MAX_ARROWS = 4
const TRAP_MARK = 0.03  // trap value from which CCA's move is marked in the move list

export const num = (v, digits = 3) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : "-")
export const pct = (v) => (typeof v === "number" && Number.isFinite(v) ? (100 * v).toFixed(v >= 0.995 || v < 0.1 ? 0 : 1) + "%" : "")
const signed = (v, digits = 3) => (typeof v === "number" && Number.isFinite(v) ? (v >= 0 ? "+" : "") + v.toFixed(digits) : "-")

function node(tag, cls = "", text = "") {
  const e = document.createElement(tag)
  if (cls) e.className = cls
  if (text) e.textContent = text
  return e
}

export function engineBest(decision) {
  let best = null
  for (const c of decision.candidates) {
    if (typeof c.q_opt !== "number") continue
    if (!best || c.q_opt > best.q_opt) best = c
  }
  return best
}

function chosenCandidate(decision) {
  return decision.candidates.find((c) => c.uci === decision.move.uci) || null
}

// Fair game: with "show CCA's thinking" off, nothing of CCA's reasoning (panel, arrows, trap
// marks) is shown until the game ends. `ui` is main.js's page state.
export function insightVisible(ui) {
  return Boolean(ui.insight || (ui.state && ui.state.game_over))
}

// Shows the panel's parts ({note, body, legend}: elements with `hidden`) for the fair-game
// switch - the "hidden until the game ends" note, or the panel body and the arrow legend -
// and returns whether CCA's thinking is visible.
export function showInsightParts(parts, ui) {
  const visible = insightVisible(ui)
  parts.note.hidden = visible
  parts.body.hidden = !visible
  parts.legend.hidden = !visible
  return visible
}

// Whether the move list marks CCA's move of this trap value (never in a fair game).
export function trapMarked(ui, trap) {
  return insightVisible(ui) && typeof trap === "number" && trap >= TRAP_MARK
}

// The i18n key of the panel's badge ("badge_review" takes the reviewed move as {n}).
export function insightBadge(ui) {
  if (!insightVisible(ui)) return "badge_hidden"
  if (ui.lab) return "badge_lab"
  if (ui.state.game_over && !ui.insight) return "badge_revealed"
  if (ui.review !== null && ui.review >= 0) return "badge_review"
  return "badge_live"
}

// The arrows drawn on the board at `ply` (-1 = start position): those of the position lab's
// answer while it is open, else those of CCA's decision at that ply; none in a fair game.
export function boardArrows(ui, ply) {
  if (!insightVisible(ui)) return []
  if (ui.lab) return decisionArrows(ui.lab.decision)
  const decision = ply >= 0 ? ui.decisions.get(ply) : undefined
  return decision ? decisionArrows(decision) : []
}

// Arrows for the top policy moves (opacity by probability) and the engine's best move.
export function decisionArrows(decision) {
  const arrows = []
  const best = engineBest(decision)
  const seen = new Set()
  if (best && best.uci !== decision.move.uci) {
    arrows.push({type: ARROW.engine, from: best.uci.slice(0, 2), to: best.uci.slice(2, 4)})
    seen.add(best.uci.slice(0, 4))
  }
  for (const entry of decision.policy.slice(0, MAX_ARROWS)) {
    const key = entry.uci.slice(0, 4)
    if (entry.p < 0.02 || seen.has(key)) continue
    seen.add(key)
    const type = entry.p >= 0.5 ? ARROW.p1 : entry.p >= 0.25 ? ARROW.p2 : entry.p >= 0.1 ? ARROW.p3 : ARROW.p4
    arrows.push({type, from: entry.uci.slice(0, 2), to: entry.uci.slice(2, 4)})
  }
  return arrows
}

// One sentence, generated only from the numbers of this decision.
export function whySentence(decision) {
  const move = decision.move.san
  const trace = decision.trace || {}
  if (trace.reflex) return t("why_reflex", {move})
  const chosen = chosenCandidate(decision)
  const best = engineBest(decision)
  const eps = num(decision.knobs.risk_budget)
  if (!chosen || !best) return t("why_best", {move, q: num(trace.q_opt)})
  const top = decision.policy[0]
  const pChosen = decision.policy.find((e) => e.uci === decision.move.uci)
  const sampledLower = top && pChosen && top.uci !== decision.move.uci && top.p > pChosen.p + 1e-9
  if (best.uci === chosen.uci) {
    if (sampledLower) return t("why_sampled_best", {move, p: pct(pChosen.p), top: top.san, tp: pct(top.p)})
    if (chosen.trap_value > 0.02) return t("why_best_trap", {move, trap: signed(chosen.trap_value)})
    return t("why_best", {move, q: num(chosen.q_opt)})
  }
  const cost = num(Math.max(0, best.q_opt - chosen.q_opt))
  if (sampledLower) {
    return t("why_sampled", {move, p: pct(pChosen.p), top: top.san, tp: pct(top.p), best: best.san, cost, eps})
  }
  if (chosen.trap_value > best.trap_value && chosen.trap_value > 0.01) {
    return t("why_trap", {move, best: best.san, trap: signed(chosen.trap_value), cost, eps})
  }
  if (chosen.prior > best.prior) {
    return t("why_human", {move, best: best.san, p1: pct(chosen.prior), p2: pct(best.prior), cost, eps})
  }
  if (chosen.opp_entropy > best.opp_entropy) {
    return t("why_entropy", {move, best: best.san, h1: num(chosen.opp_entropy, 2), h2: num(best.opp_entropy, 2), cost, eps})
  }
  return t("why_sampled", {move, p: pct(pChosen ? pChosen.p : null), top: top ? top.san : "-", tp: pct(top ? top.p : null), best: best.san, cost, eps})
}

function bar(value, cls) {
  const wrap = node("span", "pbar")
  const track = node("span", "pbar-track")
  const fill = node("span", "pbar-fill " + cls)
  fill.style.width = Math.max(0, Math.min(100, 100 * (value || 0))).toFixed(1) + "%"
  track.appendChild(fill)
  wrap.append(node("span", "", pct(value)), track)
  return wrap
}

export function renderCandidates(tbody, decision) {
  tbody.replaceChildren()
  const best = engineBest(decision)
  const rows = [...decision.candidates].sort((a, b) => (b.policy ?? -1) - (a.policy ?? -1) || b.q_opt - a.q_opt)
  for (const c of rows) {
    const tr = document.createElement("tr")
    if (c.uci === decision.move.uci) tr.className = "chosen"
    const first = document.createElement("td")
    first.appendChild(node("span", "san", c.san))
    if (c.uci === decision.move.uci) first.appendChild(node("span", "tag tag-chosen", t("tag_chosen")))
    if (best && c.uci === best.uci) first.appendChild(node("span", "tag tag-best", t("tag_best")))
    const policy = document.createElement("td")
    if (typeof c.policy === "number") policy.appendChild(bar(c.policy, ""))
    else policy.appendChild(node("span", "na", "-"))
    const prior = document.createElement("td")
    prior.appendChild(bar(c.prior, "prior"))
    const trap = node("td", c.trap_value > 0.005 ? "trap-pos" : c.trap_value < -0.005 ? "trap-neg" : "", signed(c.trap_value))
    tr.append(first, policy, prior, node("td", "", num(c.q_opt)), node("td", "", num(c.q_human)), trap, node("td", "", num(c.opp_entropy, 2)))
    tbody.appendChild(tr)
  }
}

function kv(dl, key, value, hint) {
  const dt = node("dt", "", key)
  if (hint) dt.title = hint
  dl.append(dt, node("dd", "", value))
}

export function renderKnobs(dl, decision) {
  dl.replaceChildren()
  for (const k of KNOBS) kv(dl, k, num(decision.knobs[k]), t("knob_" + k))
  const bank = decision.trace ? decision.trace.risk_bank : undefined
  kv(dl, t("risk_bank"), typeof bank === "number" ? signed(bank) : "-", t("knob_risk_bank"))
}

export function renderLatent(dl, decision) {
  dl.replaceChildren()
  const s = decision.state
  kv(dl, "stress", num(s.stress), t("lat_stress"))
  kv(dl, "drive", signed(s.drive), t("lat_drive"))
  kv(dl, "opp_stress", num(s.opp_stress), t("lat_opp_stress"))
  const chaos = s.chaos || []
  for (let i = 0; i < 3; i++) kv(dl, "u" + i, signed(chaos[i], 2), t("lat_u" + i))
  kv(dl, "think_time", num(decision.think_time, 1) + " s", t("lat_think"))
}
