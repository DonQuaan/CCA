// cca play - page controller - part of CCA (Apache-2.0).
// The server is authoritative for the game; this module renders its state and sends moves.

import {api} from "./api.js"
import {t, setLanguage, language, detectLanguage, applyStatic} from "./i18n.js"
import {BoardView} from "./board.js"
import {renderCandidates, renderKnobs, renderLatent, whySentence, insightBadge, boardArrows, showInsightParts, trapMarked} from "./insight.js"
import {renderSeries} from "./charts.js"
import {remainingMs} from "./clock.js"
import {createFlow, canMove} from "./flow.js"

const $ = (id) => document.getElementById(id)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const other = (color) => (color === "white" ? "black" : "white")

// Per-viewer conveniences only; the page works the same without storage (private windows).
const prefs = {
  get(key) { try { return window.localStorage.getItem("cca-play:" + key) } catch { return null } },
  set(key, value) { try { window.localStorage.setItem("cca-play:" + key, value) } catch { /* ignore */ } }
}
const tabStore = {
  get(key) { try { return window.sessionStorage.getItem("cca-play:" + key) } catch { return null } },
  set(key, value) { try { window.sessionStorage.setItem("cca-play:" + key, value) } catch { /* ignore */ } }
}

const ui = {
  info: null,
  state: null,
  stateAt: 0,
  gameId: null,
  decisions: new Map(),  // ply -> decision JSON
  busy: false,           // a move or take-back request is in flight
  thinking: false,       // CCA is deciding (or its emulated think time runs)
  starting: false,       // a new-game request is in flight
  reveal: null,          // {until, total} while an emulated think time runs
  review: null,          // null = live; -1 = start position; k = position after ply k
  insight: true,
  lab: null,             // {fen, decision, side}
  labBusy: false,
  gen: 0,                // game context; see flow.js (late responses of an old one are dropped)
  flagAsked: false,
  inputFor: null,
  keyboard: false,       // the last human move came from the move-entry field
  resignTimer: null,
  toastTimer: null
}
let board = null
const flow = createFlow({
  ui,
  api,
  sleep,
  now: () => performance.now(),
  view: {
    applyState,
    render,
    drawBoard,
    updateInput,
    renderPlayers,
    showError,
    displayedFen,
    remember: (gameId) => tabStore.set("game", gameId),
    setOrientation: (color) => board.setOrientation(color),
    announceHuman: (move) => announce(t("you_played", {san: move.san})),
    announceCca,
    announceOver: (state) => announce(overText(state))
  }
})

// ------------------------------------------------------------------ helpers
function announce(text) {
  const box = $("announcer")
  box.textContent = ""
  setTimeout(() => { box.textContent = text }, 40)
}

function toast(text, isError = false) {
  const box = $("toast")
  box.textContent = text
  box.className = "toast" + (isError ? " error" : "")
  box.hidden = false
  clearTimeout(ui.toastTimer)
  ui.toastTimer = setTimeout(() => { box.hidden = true }, isError ? 7000 : 2500)
}

function showError(err) {
  if (err && err.status === 0) toast(t("status_offline"), true)
  else toast(t("err_generic", {message: err && err.message ? err.message : String(err)}), true)
}

function setOverlay(text) {
  $("board-overlay").hidden = !text
  $("overlay-text").textContent = text || ""
}

function moverOf(ply) {
  const s = ui.state
  const first = s.start_fen.split(" ")[1] === "b" ? "black" : "white"
  return ply % 2 === 0 ? first : other(first)
}

function displayedPly() {
  return ui.review === null ? ui.state.moves.length - 1 : ui.review
}

function displayedFen() {
  const s = ui.state
  const ply = displayedPly()
  return ply < 0 ? s.start_fen : s.moves[ply].fen
}

function latestDecision(ply) {
  for (let p = ply; p >= 0; p--) if (ui.decisions.has(p)) return {ply: p, decision: ui.decisions.get(p)}
  return null
}

// ------------------------------------------------------------------ drawing
function drawBoard(animate = true) {
  const s = ui.state
  if (!s) return
  const ply = displayedPly()
  let lastMove = null
  if (ply >= 0) {
    const m = s.moves[ply]
    lastMove = {from: m.uci.slice(0, 2), to: m.uci.slice(2, 4)}
  }
  board.show(displayedFen(), {lastMove, arrows: boardArrows(ui, ply), animate})
  ui.inputFor = null
  updateInput()
}

function updateInput() {
  const want = canMove(ui) ? ui.state.human_color : null
  if (want === ui.inputFor) return
  ui.inputFor = want
  if (want) board.enable(want)
  else board.disable()
}

// `receivedAt`: when the response carrying `state` arrived (its clocks are relative to that).
function applyState(state, {animate = true, receivedAt = performance.now()} = {}) {
  ui.state = state
  ui.stateAt = receivedAt
  ui.flagAsked = false
  if (ui.review !== null && ui.review >= state.moves.length - 1) ui.review = null
  for (const ply of [...ui.decisions.keys()]) if (ply >= state.moves.length) ui.decisions.delete(ply)
  drawBoard(animate)
  render()
}

// ------------------------------------------------------------------ rendering
function render() {
  if (!ui.state) return
  renderPlayers()
  renderClocks()
  renderStatus()
  renderMoves()
  renderControls()
  renderInsight()
}

function renderPlayers() {
  const s = ui.state
  const bottom = board.orientation
  for (const [id, color] of [["player-bottom", bottom], ["player-top", other(bottom)]]) {
    const root = $(id)
    const human = color === s.human_color
    root.querySelector(".player-swatch").className = "player-swatch " + color
    root.querySelector(".player-name").textContent = human ? t("you") : `CCA · ${s.cca.persona} · ${s.cca.elo}`
    root.querySelector(".player-meta").textContent = human
      ? t("you_meta", {elo: s.cca.opponent_elo})
      : t("cca_meta", {model: s.cca.human_model === "maia2" ? "Maia-2" : "QRE"})
    root.querySelector(".thinking").hidden = human || !ui.thinking
    root.querySelector(".clock").hidden = !s.clocks
    const bar = root.querySelector(".think-bar")
    const showBar = !human && Boolean(ui.reveal)
    if (showBar && bar.hidden) {
      const fill = bar.querySelector("span")
      bar.classList.remove("run")
      fill.style.animationDuration = ui.reveal.total + "ms"
      void fill.offsetWidth  // restart the CSS animation
      bar.classList.add("run")
    }
    bar.hidden = !showBar
  }
}

function clockMs(color) {
  return remainingMs(ui.state, color, ui.stateAt, performance.now())
}

function formatClock(ms) {
  const total = Math.max(0, ms)
  if (total < 10000) return "0:0" + (Math.floor(total / 100) / 10).toFixed(1)
  const secs = Math.floor(total / 1000)
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = String(secs % 60).padStart(2, "0")
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`
}

function renderClocks() {
  const s = ui.state
  if (!s) return
  for (const [id, color] of [["player-bottom", board.orientation], ["player-top", other(board.orientation)]]) {
    const root = $(id)
    if (!ui.thinking || color === s.human_color) {
      root.querySelector(".thinking-text").textContent = t("thinking")
    } else {
      const left = ui.reveal ? Math.max(0, ui.reveal.until - performance.now()) : 0
      root.querySelector(".thinking-text").textContent = ui.reveal ? t("replying_in", {s: (left / 1000).toFixed(1)}) : t("thinking")
    }
    if (!s.clocks) continue
    const ms = clockMs(color)
    const clock = root.querySelector(".clock")
    root.querySelector(".clock-time").textContent = formatClock(ms)
    const running = s.clocks.running === color && !s.game_over
    clock.classList.toggle("running", running)
    clock.classList.toggle("low", ms < 10000)
    if (running && ms <= 0 && !ui.flagAsked && !ui.thinking && !ui.busy) {
      ui.flagAsked = true
      setTimeout(flow.refresh, 150)  // the server decides: it flags lazily on the next request
    }
  }
}

function overText(s) {
  switch (s.reason) {
    case "checkmate": return t(s.winner === "human" ? "over_checkmate_win" : "over_checkmate_loss")
    case "stalemate": return t("over_stalemate")
    case "threefold": return t("over_threefold")
    case "fifty-move": return t("over_fifty")
    case "insufficient-material": return t("over_insufficient")
    case "resignation": return t("over_resigned")
    case "time":
      if (s.winner === "human") return t("over_time_win")
      return s.winner === "cca" ? t("over_time_loss") : t("over_time_draw")
    default: return s.result
  }
}

function renderStatus() {
  const s = ui.state
  const status = $("status")
  let text
  if (s.game_over) text = overText(s)
  else if (ui.thinking) text = t("status_thinking")
  else if (ui.busy || ui.starting) text = t("status_waiting")
  else if (s.to_move === "human") text = t("status_your_move", {color: t(s.human_color + "_l")})
  else text = t("status_cca_move")
  status.textContent = text
  status.className = "status" + (s.game_over ? " over" : "")
  const note = $("seed-note")
  note.textContent = s.game_over && s.cca.seed && s.cca.seed_commitment
    ? t("seed_revealed", {seed: s.cca.seed, commit: s.cca.seed_commitment.slice(0, 12)})
    : ""
  const banner = $("review-banner")
  banner.hidden = ui.review === null
  if (ui.review !== null) {
    $("review-text").textContent = ui.review < 0 ? t("review_start") : t("review_text", {n: moveLabel(ui.review)})
  }
}

function moveLabel(ply) {
  const s = ui.state
  const startNo = parseInt(s.start_fen.split(" ")[5], 10) || 1
  const offset = s.start_fen.split(" ")[1] === "b" ? 1 : 0
  const no = startNo + Math.floor((ply + offset) / 2)
  return `${no}${(ply + offset) % 2 === 0 ? "." : "..."} ${s.moves[ply].san}`
}

function renderMoves() {
  const s = ui.state
  const box = $("moves")
  const heading = $("moves-heading")
  box.replaceChildren(heading)
  if (!s.moves.length) {
    const empty = document.createElement("p")
    empty.className = "empty"
    empty.textContent = t("no_moves")
    box.appendChild(empty)
    return
  }
  const startNo = parseInt(s.start_fen.split(" ")[5], 10) || 1
  const blackFirst = s.start_fen.split(" ")[1] === "b"
  const traps = new Map(s.history.map((h) => [h.ply, h.trap_value]))
  const current = displayedPly()
  const cell = (ply) => {
    const b = document.createElement("button")
    b.type = "button"
    b.className = "mv"
    b.textContent = s.moves[ply].san
    b.setAttribute("aria-label", moveLabel(ply))
    if (ply === current) b.classList.add("current")
    if (trapMarked(ui, traps.get(ply))) b.classList.add("cca-trap")
    b.addEventListener("click", () => setReview(ply))
    return b
  }
  const num = (n) => {
    const e = document.createElement("span")
    e.className = "num"
    e.textContent = n + "."
    return e
  }
  let ply = 0
  let no = startNo
  if (blackFirst) {
    const dots = document.createElement("span")
    dots.className = "mv"
    dots.textContent = "…"
    box.append(num(no++), dots, cell(0))
    ply = 1
  }
  for (; ply < s.moves.length; ply += 2) {
    box.append(num(no++), cell(ply))
    if (ply + 1 < s.moves.length) box.appendChild(cell(ply + 1))
    else box.appendChild(document.createElement("span"))
  }
  if (s.game_over) {
    const res = document.createElement("p")
    res.className = "result"
    res.textContent = s.result
    box.appendChild(res)
  }
  const active = box.querySelector(".mv.current")
  if (active && ui.review === null) box.scrollTop = box.scrollHeight
  else if (active) active.scrollIntoView({block: "nearest"})
}

function renderControls() {
  const s = ui.state
  const live = !s.game_over
  const idle = !ui.busy && !ui.thinking && !ui.starting
  $("undo-btn").disabled = !s.can_undo || !idle
  $("resign-btn").disabled = !live || !idle
  const at = displayedPly()
  $("nav-first").disabled = at <= -1
  $("nav-prev").disabled = at <= -1
  $("nav-next").disabled = ui.review === null
  $("nav-last").disabled = ui.review === null
  const entry = canMove(ui)
  $("kbd-move").disabled = !entry
  $("kbd-form").querySelector("button").disabled = !entry
  // Disabling the field while CCA thinks dropped its focus: give it back once it is enabled.
  if (entry && ui.keyboard && document.activeElement === document.body) $("kbd-move").focus()
  $("lab-btn").disabled = ui.labBusy || ui.thinking
}

function renderInsight() {
  const s = ui.state
  const visible = showInsightParts({note: $("insight-hidden"), body: $("insight-body"), legend: $("arrow-legend")}, ui)
  const badge = $("insight-badge")
  const key = insightBadge(ui)
  badge.textContent = key === "badge_review" ? t(key, {n: moveLabel(ui.review)}) : t(key)
  badge.classList.toggle("live", visible && !ui.lab)
  if (!visible) return
  const found = ui.lab ? {ply: null, decision: ui.lab.decision} : latestDecision(displayedPly())
  $("lab-banner").hidden = !ui.lab
  if (ui.lab) $("lab-text").textContent = t("lab_banner", {side: t(ui.lab.side + "_l")})
  $("insight-empty").hidden = Boolean(found)
  $("insight-empty").textContent = ui.labBusy ? t("lab_busy") : t("insight_empty")
  if (ui.labBusy) $("insight-empty").hidden = false
  $("insight-content").hidden = !found
  if (found) {
    const d = found.decision
    $("why").textContent = whySentence(d)
    renderCandidates($("cand-table").querySelector("tbody"), d)
    renderKnobs($("knobs"), d)
    renderLatent($("latent"), d)
  }
  const selected = found && found.ply !== null ? found.ply : null
  renderSeries($("series"), s.history, {selectedPly: selected, onSelect: (ply) => setReview(ply)})
}

function renderModelNote() {
  const hm = ui.info ? ui.info.human_model : null
  let text = ""
  if (hm && hm.fallback_reason) text = t("model_fallback", {reason: hm.fallback_reason})
  else if (hm && hm.kind === "qre") text = t("model_qre")
  $("model-note").textContent = text
}

function renderAbout() {
  const info = ui.info
  if (!info) return
  const model = info.human_model.kind === "maia2" ? "Maia-2" : info.human_model.kind === "qre" ? "QRE" : "-"
  $("about-runtime").textContent = t("about_runtime", {
    version: info.version,
    engine: info.engine.name || "-",
    nodes: info.engine.nodes,
    model,
    chaos: {lorenz: "Lorenz", ar1: "AR(1)"}[info.chaos_driver] || info.chaos_driver || "-"
  })
}

// ------------------------------------------------------------------ game flow (see flow.js)
function onHumanMove(move) {
  if (!canMove(ui)) {
    drawBoard(false)
    return
  }
  if (document.activeElement !== $("kbd-move")) ui.keyboard = false
  flow.humanMove(move)
}

function announceCca(res) {
  if (res.move) {
    let text = t("cca_played", {san: res.move.san})
    if (res.state.check) text += " " + t("check")
    if (res.state.game_over) text += " " + overText(res.state)
    announce(text)
  } else if (res.state.game_over) {
    announce(overText(res.state))
  }
}

function setReview(ply) {
  if (!ui.state) return
  const n = ui.state.moves.length
  let target = ply
  if (target !== null && target >= n - 1) target = null
  if (target !== null && target < -1) target = -1
  ui.review = target
  ui.lab = null
  drawBoard(true)
  render()
}

async function resign() {
  const btn = $("resign-btn")
  if (!btn.classList.contains("confirming")) {
    btn.classList.add("confirming")
    btn.textContent = t("resign_confirm")
    clearTimeout(ui.resignTimer)
    ui.resignTimer = setTimeout(() => {
      btn.classList.remove("confirming")
      btn.textContent = t("resign")
    }, 3500)
    return
  }
  clearTimeout(ui.resignTimer)
  btn.classList.remove("confirming")
  btn.textContent = t("resign")
  try {
    const res = await api.post(`/api/games/${ui.gameId}/resign`, {})
    applyState(res.state, {animate: false})
    announce(overText(res.state))
  } catch (err) {
    showError(err)
  }
}

async function copyText(text, title) {
  try {
    await navigator.clipboard.writeText(text)
    toast(t("copied"))
  } catch {
    $("text-title").textContent = title
    $("text-area").value = text
    $("text-dialog").showModal()
    $("text-area").select()
  }
}

async function copyPgn() {
  try {
    const text = await api.get(`/api/games/${ui.gameId}/pgn`)
    await copyText(text, t("pgn_title"))
  } catch (err) {
    showError(err)
  }
}

// ------------------------------------------------------------------ new-game dialog
function tcLabel(tc) {
  return tc ? `${tc.base_s / 60}+${tc.inc_s}` : t("untimed")
}

function fillDialog(focusFen = false) {
  const info = ui.info
  let saved = {}
  try { saved = JSON.parse(prefs.get("newgame") || "{}") || {} } catch { saved = {} }
  const d = {...info.defaults, ...saved}
  const form = $("new-form")
  for (const input of form.querySelectorAll("input[name=color]")) input.checked = input.value === (d.human_color || "white")
  const select = $("persona-select")
  select.replaceChildren()
  for (const p of info.personas) {
    const opt = document.createElement("option")
    opt.value = p.name
    opt.textContent = p.name
    select.appendChild(opt)
  }
  select.value = info.personas.some((p) => p.name === d.persona) ? d.persona : info.defaults.persona
  updatePersonaDesc()
  for (const [id, key, range] of [["elo-self", "elo_self", info.ranges.elo_self], ["elo-oppo", "elo_oppo", info.ranges.elo_oppo]]) {
    const input = $(id)
    input.min = range[0]
    input.max = range[1]
    input.value = d[key]
    $(id + "-out").textContent = input.value
  }
  const group = $("tc-group")
  for (const old of group.querySelectorAll("label")) old.remove()
  const want = JSON.stringify(d.time_control ?? null)
  info.time_controls.forEach((tc, i) => {
    const label = document.createElement("label")
    const input = document.createElement("input")
    input.type = "radio"
    input.name = "tc"
    input.value = String(i)
    input.checked = JSON.stringify(tc) === want
    const span = document.createElement("span")
    span.textContent = tcLabel(tc)
    label.append(input, span)
    group.appendChild(label)
  })
  if (!group.querySelector("input:checked")) group.querySelector("input").checked = true
  $("emulate").checked = Boolean(d.emulate_think_time)
  $("seed").value = ""
  $("start-fen").value = ""
  $("new-error").textContent = ""
  $("new-dialog").showModal()
  if (focusFen) $("start-fen").focus()
}

function updatePersonaDesc() {
  const name = $("persona-select").value
  const p = ui.info.personas.find((x) => x.name === name)
  $("persona-desc").textContent = p ? p.description : ""
}

async function submitNewGame(event) {
  event.preventDefault()
  if (ui.starting) return  // one new-game request at a time
  const form = $("new-form")
  const tcIndex = Number(form.querySelector("input[name=tc]:checked").value)
  const body = {
    human_color: form.querySelector("input[name=color]:checked").value,
    persona: $("persona-select").value,
    elo_self: Number($("elo-self").value),
    elo_oppo: Number($("elo-oppo").value),
    time_control: ui.info.time_controls[tcIndex],
    emulate_think_time: $("emulate").checked
  }
  prefs.set("newgame", JSON.stringify(body))
  const seed = $("seed").value.trim()
  const fen = $("start-fen").value.trim()
  if (seed) body.seed = seed
  if (fen) body.fen = fen
  try {
    await flow.newGame(body)
    $("new-dialog").close()
  } catch (err) {
    $("new-error").textContent = err && err.message ? err.message : String(err)
  }
}

// ------------------------------------------------------------------ wiring
function onKeyMove(event) {
  event.preventDefault()
  const input = $("kbd-move")
  $("kbd-error").textContent = ""
  if (!canMove(ui)) {
    $("kbd-error").textContent = t("err_not_now")
    return
  }
  ui.keyboard = true  // give the focus back to the field once it is the human's turn again
  const problem = board.enter(input.value)  // an illegal move, or a promotion without its piece
  if (problem) $("kbd-error").textContent = t(problem.key, problem.params)
  else input.value = ""
}

function onKeyNav(event) {
  const tag = event.target && event.target.tagName
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || document.querySelector("dialog[open]") || !ui.state) return
  const at = displayedPly()
  if (event.key === "ArrowLeft") setReview(at - 1)
  else if (event.key === "ArrowRight") setReview(at + 1)
  else if (event.key === "Home") setReview(-1)
  else if (event.key === "End") setReview(null)
  else return
  event.preventDefault()
}

function applyLanguage() {
  applyStatic()
  $("lang-btn").textContent = language() === "vi" ? "EN" : "VI"
  $("lang-btn").setAttribute("aria-label", language() === "vi" ? "English" : "Tiếng Việt")
  $("resign-btn").classList.remove("confirming")
  renderModelNote()
  renderAbout()
  render()
}

function wire() {
  $("insight-toggle").addEventListener("change", (e) => {
    ui.insight = e.target.checked
    prefs.set("insight", ui.insight ? "1" : "0")
    if (!ui.insight) ui.lab = null
    drawBoard(false)
    render()
  })
  $("lang-btn").addEventListener("click", () => {
    setLanguage(language() === "vi" ? "en" : "vi")
    prefs.set("lang", language())
    applyLanguage()
  })
  $("about-btn").addEventListener("click", () => $("about-dialog").showModal())
  $("about-close").addEventListener("click", () => $("about-dialog").close())
  $("text-close").addEventListener("click", () => $("text-dialog").close())
  $("new-btn").addEventListener("click", () => fillDialog(false))
  $("loadfen-btn").addEventListener("click", () => fillDialog(true))
  $("new-cancel").addEventListener("click", () => $("new-dialog").close())
  $("new-form").addEventListener("submit", submitNewGame)
  $("persona-select").addEventListener("change", updatePersonaDesc)
  for (const id of ["elo-self", "elo-oppo"]) $(id).addEventListener("input", () => { $(id + "-out").textContent = $(id).value })
  $("undo-btn").addEventListener("click", flow.undo)
  $("resign-btn").addEventListener("click", resign)
  $("flip-btn").addEventListener("click", () => {
    board.setOrientation(other(board.orientation))
    render()
  })
  $("pgn-btn").addEventListener("click", copyPgn)
  $("fen-btn").addEventListener("click", () => copyText(displayedFen(), t("fen_title")))
  $("kbd-form").addEventListener("submit", onKeyMove)
  $("nav-first").addEventListener("click", () => setReview(-1))
  $("nav-prev").addEventListener("click", () => setReview(displayedPly() - 1))
  $("nav-next").addEventListener("click", () => setReview(displayedPly() + 1))
  $("nav-last").addEventListener("click", () => setReview(null))
  $("live-btn").addEventListener("click", () => setReview(null))
  $("lab-btn").addEventListener("click", flow.lab)
  $("lab-close").addEventListener("click", () => {
    ui.lab = null
    drawBoard(false)
    render()
  })
  document.addEventListener("keydown", onKeyNav)
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !ui.thinking && !ui.busy && !ui.starting) flow.refresh()
  })
  setInterval(renderClocks, 100)
}

async function waitReady() {
  for (;;) {
    try {
      ui.info = await api.get("/api/info")
      renderModelNote()
      renderAbout()
      if (ui.info.error) setOverlay(t("status_engine_error", {error: ui.info.error}))
      else if (ui.info.ready) {
        setOverlay(null)
        return
      } else setOverlay(t("status_warming"))
    } catch {
      setOverlay(t("status_offline"))
    }
    await sleep(ui.info && ui.info.error ? 3000 : 400)
  }
}

async function boot() {
  setLanguage(detectLanguage(prefs.get("lang")))
  ui.insight = prefs.get("insight") !== "0"
  $("insight-toggle").checked = ui.insight
  board = new BoardView($("board"), onHumanMove)
  wire()
  applyLanguage()
  setOverlay(t("status_connecting"))
  await waitReady()
  const saved = tabStore.get("game")
  if (saved) {
    try {
      const state = await api.get(`/api/games/${saved}`)
      flow.startSession(saved, state)
      return
    } catch {
      // unknown or evicted game: start a fresh one
    }
  }
  try {
    await flow.newGame({})
  } catch (err) {
    showError(err)
  }
}

boot()
