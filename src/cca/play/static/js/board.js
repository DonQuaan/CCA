// Board view: cm-chessboard for drawing and input, chess.js for legality - part of CCA (Apache-2.0).
// chess.js (and the server) are the source of truth; board animations are fire-and-forget:
// nothing waits on setPosition(), whose promise stalls in hidden tabs (requestAnimationFrame).

import {Chessboard, COLOR, INPUT_EVENT_TYPE, BORDER_TYPE} from "../vendor/cm-chessboard/src/Chessboard.js"
import {Markers} from "../vendor/cm-chessboard/src/extensions/markers/Markers.js"
import {Arrows} from "../vendor/cm-chessboard/src/extensions/arrows/Arrows.js"
import {PromotionDialog, PROMOTION_DIALOG_RESULT_TYPE} from "../vendor/cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js"
import {Chess} from "../vendor/chess.js/dist/esm/chess.js"

// Marker and arrow types are matched by reference: module-level constants.
const MARK_SELECT = {class: "marker-select", slice: "markerSquare"}
const MARK_LAST = {class: "marker-last", slice: "markerSquare"}
const MARK_CHECK = {class: "marker-check", slice: "markerCheck"}
export const ARROW = {
  p1: {class: "arrow-p1"},
  p2: {class: "arrow-p2"},
  p3: {class: "arrow-p3"},
  p4: {class: "arrow-p4"},
  engine: {class: "arrow-engine"}
}
const UCI = /^([a-h][1-8])([a-h][1-8])([qrbn])?$/
// A piece letter right after a first- or last-rank square can only name a promotion piece.
const NAMED_PIECE = /[18]=?([qrbn])$/i

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
}

export function kingInCheck(chess) {
  if (!chess.inCheck()) return null
  const turn = chess.turn()
  for (const row of chess.board()) {
    for (const cell of row) {
      if (cell && cell.type === "k" && cell.color === turn) return cell.square
    }
  }
  return null
}

// The squares of a SAN (or chess.js permissive) move in `fen`, or null if it is not legal.
function sanSquares(fen, san) {
  try {
    const mv = new Chess(fen).move(san)
    return {from: mv.from, to: mv.to}
  } catch {
    return null  // chess.js 1.x throws on illegal or unparsable moves
  }
}

export class BoardView {
  constructor(element, onMove) {
    this.chess = new Chess()
    this.onMove = onMove
    this.animate = !reducedMotion()
    this._orientation = "white"
    this.board = new Chessboard(element, {
      position: this.chess.fen(),
      assetsUrl: "/static/vendor/cm-chessboard/assets/",
      style: {
        cssClass: "cca",
        borderType: BORDER_TYPE.none,
        showCoordinates: true,
        pieces: {file: "/static/vendor/cburnett-commons/cburnett.svg"},
        animationDuration: this.animate ? 200 : 0
      },
      extensions: [
        {class: Markers, props: {autoMarkers: MARK_SELECT, sprite: "/static/img/markers.svg"}},
        {class: Arrows},
        {class: PromotionDialog, props: {language: "en"}}
      ]
    })
    // cm-chessboard gives its <svg> role="img" but no accessible name (axe: svg-img-alt, WCAG
    // 1.1.1). Name it after the visually hidden, translated "Chess board" heading.
    this.board.view.svg.setAttribute("aria-labelledby", "board-heading")
  }

  // Tracked here: cm-chessboard only updates its own orientation when the (rAF-driven)
  // turn animation runs, which is late, or never in a hidden tab.
  get orientation() {
    return this._orientation
  }

  setOrientation(color) {
    this._orientation = color === "black" ? "black" : "white"
    this._turnTowards()
  }

  // cm-chessboard takes one turn at a time and ignores a request made during a turn; such a
  // request (a new game started while the previous flip still runs) is applied after it.
  _turnTowards() {
    const want = this._orientation === "black" ? COLOR.black : COLOR.white
    if (this.board.boardTurning || this.board.getOrientation() === want) return
    this.board.setOrientation(want, this.animate).then(() => this._turnTowards())
  }

  // Draw a position with its last move, check highlight and arrows. Loads chess.js too.
  show(fen, {lastMove = null, arrows = [], animate = true} = {}) {
    this.chess.load(fen)
    this.board.setPosition(fen, animate && this.animate)
    this.board.removeMarkers(MARK_LAST)
    this.board.removeMarkers(MARK_CHECK)
    if (lastMove) {
      this.board.addMarker(MARK_LAST, lastMove.from)
      this.board.addMarker(MARK_LAST, lastMove.to)
    }
    const king = kingInCheck(this.chess)
    if (king) this.board.addMarker(MARK_CHECK, king)
    this.setArrows(arrows)
  }

  setArrows(arrows) {
    this.board.removeArrows()
    for (const a of arrows) this.board.addArrow(a.type, a.from, a.to)
  }

  enable(color) {
    this.disable()
    this.board.enableMoveInput((event) => this._input(event), color === "black" ? COLOR.black : COLOR.white)
  }

  disable() {
    if (this.board.isMoveInputEnabled()) this.board.disableMoveInput()
    this.board.removeLegalMovesMarkers()
    this.board.removeMarkers(MARK_SELECT)
  }

  // Keyboard entry: SAN ("Nf3", "exd5", "O-O", "e8=Q") or UCI ("e2e4", "e7e8q").
  // Returns {move} (a chess.js move object) for a legal move; {needsPiece: {san, uci}} for a
  // promotion typed without its piece ("a8", "axb8", "a7a8"), with the queen promotion as the
  // example to show; null if the text is not a legal move here. The promotion piece is only
  // ever the one the text names: chess.js's permissive parser would pick a knight for "axb8".
  parse(text) {
    const clean = String(text || "").trim()
    if (!clean) return null
    const fen = this.chess.fen()
    let from, to, piece
    const m = UCI.exec(clean.toLowerCase())
    if (m) {
      [, from, to, piece] = m
    } else {
      const body = clean.replace(/[+#]?[!?]*$/, "")  // check marks and annotations do not matter
      const named = NAMED_PIECE.exec(body)
      piece = named ? named[1].toLowerCase() : undefined
      const san = named ? body.slice(0, named.index + 1) + "=" + piece.toUpperCase() : body
      // chess.js only finds the squares; "a8" alone is no SAN move for it, "a8=Q" is.
      const found = sanSquares(fen, san) || (piece ? null : sanSquares(fen, san + "=Q"))
      if (!found) return null
      ;({from, to} = found)
    }
    const legal = new Chess(fen).moves({verbose: true}).filter((mv) => mv.from === from && mv.to === to)
    if (!legal.length) return null
    if (!legal[0].promotion) return piece ? null : {move: legal[0]}  // "e2e4q" is no UCI move
    if (!piece) {
      const queen = legal.find((mv) => mv.promotion === "q")
      return {needsPiece: {san: queen.san, uci: from + to + "q"}}
    }
    const move = legal.find((mv) => mv.promotion === piece)
    return move ? {move} : null
  }

  // The move-entry field: plays the typed move if it is legal and complete. Returns null once
  // it is played, else the message to show as {key, params} (an i18n key and its values).
  enter(text) {
    const parsed = this.parse(text)
    if (!parsed) return {key: "err_illegal", params: {move: String(text || "").trim()}}
    if (parsed.needsPiece) return {key: "err_promo_piece", params: parsed.needsPiece}
    this.play(parsed.move)
    return null
  }

  // Play a parsed (legal) move on the board and report it.
  play(move) {
    this.chess.move({from: move.from, to: move.to, promotion: move.promotion})
    this.board.setPosition(this.chess.fen(), this.animate)
    this.board.removeArrows()
    this.onMove(move)
  }

  _input(event) {
    const board = this.board
    switch (event.type) {
      case INPUT_EVENT_TYPE.moveInputStarted: {
        board.removeLegalMovesMarkers()
        const moves = this.chess.moves({square: event.squareFrom, verbose: true})
        board.addLegalMovesMarkers(moves)
        return moves.length > 0
      }
      case INPUT_EVENT_TYPE.validateMoveInput: {
        board.removeLegalMovesMarkers()
        const legal = this.chess.moves({square: event.squareFrom, verbose: true}).filter((m) => m.to === event.squareTo)
        if (!legal.length) return false
        if (legal.some((m) => m.promotion)) {
          const color = this.chess.turn() === "w" ? COLOR.white : COLOR.black
          board.showPromotionDialog(event.squareTo, color, (result) => {
            if (result && result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
              const piece = result.piece.charAt(1)
              const move = legal.find((m) => m.promotion === piece)
              if (move) {
                this.play(move)
                return
              }
            }
            board.setPosition(this.chess.fen(), this.animate)  // canceled: the pawn goes back
          })
          return true
        }
        const move = legal[0]
        // cm-chessboard moves the piece itself right after this returns; sync the full
        // position (castling rook, en passant) once its move input has finished.
        event.chessboard.state.moveInputProcess.then(() => this.play(move))
        return true
      }
      case INPUT_EVENT_TYPE.moveInputCanceled:
        board.removeLegalMovesMarkers()
        return undefined
      default:
        return undefined
    }
  }
}
