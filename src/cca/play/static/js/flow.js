// cca play - game flow: the requests of a game, their ordering and the page's busy flags.
// Part of CCA (Apache-2.0). No DOM: main.js supplies the view hooks and the unit tests drive
// this module with fakes under Node.js.
//
// `ui.gen` numbers the game context (a game and its move sequence). It changes only when a new
// context is APPLIED (a new game started, a take-back done), never when a request for one is
// merely sent: a request that fails leaves the old context, and every response still pending
// for it, valid. A response is dropped only when the context changed while it was in flight,
// and the change itself clears the flags such a response would have cleared.

export function canMove(ui) {
  const s = ui.state
  return Boolean(s && !s.game_over && s.to_move === "human" && ui.review === null &&
    !ui.busy && !ui.thinking && !ui.starting && !ui.labBusy)
}

// `api`: {get(path), post(path, body)} returning promises (rejecting with {status, message,
// state}); `view`: the rendering hooks listed below; `sleep(ms)`; `now()` in milliseconds.
export function createFlow({ui, api, view, sleep, now}) {
  function newContext() {
    ui.gen++
    ui.busy = false
    ui.thinking = false
    ui.reveal = null
    ui.review = null
    ui.lab = null
  }

  async function refresh() {
    if (!ui.gameId) return
    const gen = ui.gen
    try {
      const state = await api.get(`/api/games/${ui.gameId}`)
      if (gen === ui.gen) view.applyState(state, {animate: false})
    } catch (err) {
      view.showError(err)
    }
  }

  function startSession(gameId, state) {
    newContext()
    ui.gameId = gameId
    view.remember(gameId)
    ui.decisions.clear()
    view.setOrientation(state.human_color)
    view.applyState(state, {animate: false})
    loadDecisions()
    maybeThink()
  }

  async function loadDecisions() {
    if (!ui.state.history.length) return
    const gen = ui.gen
    try {
      const res = await api.get(`/api/games/${ui.gameId}/decisions`)
      if (gen !== ui.gen) return
      for (const item of res.decisions) ui.decisions.set(item.ply, item.decision)
      view.drawBoard(false)
      view.render()
    } catch (err) {
      view.showError(err)
    }
  }

  // Resolves once the new game is shown; rejects (and changes nothing) if the server refused.
  async function newGame(body) {
    ui.starting = true
    if (ui.state) view.render()
    let res
    try {
      res = await api.post("/api/games", body)
    } catch (err) {
      ui.starting = false
      if (ui.state) view.render()
      throw err
    }
    ui.starting = false
    startSession(res.game_id, res.state)
  }

  async function humanMove(move) {
    ui.busy = true
    ui.lab = null
    view.updateInput()
    view.render()
    view.announceHuman(move)
    const gen = ui.gen
    try {
      const res = await api.post(`/api/games/${ui.gameId}/move`, {uci: move.from + move.to + (move.promotion || "")})
      if (gen !== ui.gen) return
      ui.busy = false
      view.applyState(res.state, {animate: false})
    } catch (err) {
      if (gen !== ui.gen) return
      ui.busy = false
      view.showError(err)
      if (err.state) view.applyState(err.state, {animate: false})
      else await refresh()
      if (gen !== ui.gen) return
      view.drawBoard(false)  // undo the piece the board moved locally if nothing was applied
    }
    if (ui.state.game_over) view.announceOver(ui.state)
    maybeThink()
  }

  async function maybeThink() {
    const s = ui.state
    if (!s || s.game_over || s.to_move !== "cca" || ui.thinking) return
    ui.thinking = true
    view.updateInput()
    view.render()
    const gen = ui.gen
    const ply = s.moves.length
    try {
      const res = await api.post(`/api/games/${ui.gameId}/think`, {})
      const receivedAt = now()  // before the delay: the human's clock starts after it
      if (gen !== ui.gen) return
      if (res.reveal_in_ms > 0) {
        ui.reveal = {until: now() + res.reveal_in_ms, total: res.reveal_in_ms}
        view.renderPlayers()
        await sleep(res.reveal_in_ms)
        if (gen !== ui.gen) return
      }
      ui.reveal = null
      ui.thinking = false
      if (res.move && res.decision) ui.decisions.set(ply, res.decision)
      view.applyState(res.state, {receivedAt})
      view.announceCca(res)
    } catch (err) {
      if (gen !== ui.gen) return
      ui.reveal = null
      ui.thinking = false
      view.showError(err)
      await refresh()
    }
  }

  // The position lab: one decision on the position shown. Moves wait while it runs (canMove):
  // CCA's reply would queue behind it on the one engine process. The answer is kept only for
  // the position still shown in the same game context (not after a new game, a take-back or
  // navigation); in a fair game it stays hidden (insight.js).
  async function lab() {
    const s = ui.state
    const fen = view.displayedFen()
    const gen = ui.gen
    ui.labBusy = true
    ui.lab = null
    view.updateInput()
    view.render()
    try {
      const res = await api.post("/api/analyse", {fen, persona: s.cca.persona, elo_self: s.cca.elo, elo_oppo: s.cca.opponent_elo})
      if (gen === ui.gen && view.displayedFen() === fen) ui.lab = {fen, decision: res.decision, side: res.turn}
    } catch (err) {
      view.showError(err)
    }
    ui.labBusy = false
    view.drawBoard(false)
    view.render()
  }

  async function undo() {
    ui.busy = true
    view.render()
    let res
    try {
      res = await api.post(`/api/games/${ui.gameId}/undo`, {})
    } catch (err) {
      ui.busy = false
      view.showError(err)
      view.render()
      return
    }
    newContext()
    view.applyState(res.state, {animate: true})
    maybeThink()
  }

  return {refresh, startSession, newGame, humanMove, maybeThink, lab, undo}
}
