// Time-series strip drawn with inline SVG (no chart library) - part of CCA (Apache-2.0).
// Built with DOM APIs only: server data is never parsed as markup.

import {t} from "./i18n.js"

const NS = "http://www.w3.org/2000/svg"
const W = 100
const H = 30
const PAD = 3

const fmt = (v, digits = 2) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : "-")

// Each row: one or more lines (or bars) over CCA's moves; ranges grow to fit the data.
const ROWS = [
  {
    key: "q", label: "series_q", hint: "series_q_hint", min: 0, max: 1, ref: 0.5,
    lines: [{get: (h) => h.q_opt, cls: "s-qopt"}, {get: (h) => h.q_human, cls: "s-qhuman", dash: true}],
    value: (h) => fmt(h.q_opt, 3)
  },
  {key: "stress", label: "series_stress", hint: "series_stress_hint", min: 0, max: 1, lines: [{get: (h) => h.stress, cls: "s-stress"}], value: (h) => fmt(h.stress)},
  {key: "drive", label: "series_drive", hint: "series_drive_hint", min: -0.5, max: 0.5, ref: 0, lines: [{get: (h) => h.drive, cls: "s-drive"}], value: (h) => fmt(h.drive)},
  {key: "opp", label: "series_opp", hint: "series_opp_hint", min: 0, max: 1, lines: [{get: (h) => h.opp_stress, cls: "s-opp"}], value: (h) => fmt(h.opp_stress)},
  {
    key: "chaos", label: "series_chaos", hint: "series_chaos_hint", min: -1, max: 1, ref: 0,
    lines: [0, 1, 2].map((i) => ({get: (h) => (h.chaos || [])[i], cls: "s-u" + i})),
    value: (h) => (h.chaos || []).map((u) => fmt(u, 1)).join(" ")
  },
  {key: "think", label: "series_think", hint: "series_think_hint", min: 0, max: 1, bars: (h) => h.think_time, value: (h) => fmt(h.think_time, 1) + " s"}
]

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v))
  if (parent) parent.appendChild(node)
  return node
}

function range(row, history) {
  let lo = row.min
  let hi = row.max
  const values = []
  for (const h of history) {
    if (row.bars) values.push(row.bars(h))
    else for (const line of row.lines) values.push(line.get(h))
  }
  for (const v of values) {
    if (typeof v !== "number" || !Number.isFinite(v)) continue
    lo = Math.min(lo, v)
    hi = Math.max(hi, v)
  }
  if (row.ref === 0 && row.min < 0) {  // symmetric around zero
    const m = Math.max(Math.abs(lo), Math.abs(hi))
    lo = -m
    hi = m
  }
  return [lo, hi === lo ? lo + 1 : hi]
}

export function renderSeries(container, history, {selectedPly = null, onSelect = null} = {}) {
  container.replaceChildren()
  if (!history.length) {
    const p = document.createElement("p")
    p.className = "series-empty"
    p.textContent = t("series_empty")
    container.appendChild(p)
    return
  }
  const n = history.length
  const x = (i) => ((i + 0.5) / n) * W
  const last = history[n - 1]
  for (const row of ROWS) {
    const [lo, hi] = range(row, history)
    const y = (v) => H - PAD - ((v - lo) / (hi - lo)) * (H - 2 * PAD)
    const wrap = document.createElement("div")
    wrap.className = "series-row"
    const label = document.createElement("span")
    label.className = "series-label"
    label.textContent = t(row.label)
    label.title = t(row.hint)
    if (row.key === "q" || row.key === "chaos") {
      for (const line of row.lines) {
        const key = document.createElement("span")
        key.className = "key " + line.cls
        label.appendChild(key)
      }
    }
    const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": t(row.label)})
    if (row.ref !== undefined && row.ref >= lo && row.ref <= hi) {
      el("line", {x1: 0, x2: W, y1: y(row.ref), y2: y(row.ref), class: "ref"}, svg)
    }
    history.forEach((h, i) => {
      if (h.restart) el("line", {x1: x(i) - W / n / 2, x2: x(i) - W / n / 2, y1: 0, y2: H, class: "restart"}, svg)
      if (selectedPly !== null && h.ply === selectedPly) el("rect", {x: x(i) - W / n / 2, y: 0, width: W / n, height: H, class: "sel"}, svg)
    })
    if (row.bars) {
      const bw = Math.min(4, Math.max(0.6, (W / n) * 0.6))
      history.forEach((h, i) => {
        const v = row.bars(h)
        if (typeof v !== "number" || !Number.isFinite(v)) return
        el("rect", {x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(0.5, H - PAD - y(v)), class: "bar"}, svg)
      })
    } else {
      for (const line of row.lines) {
        let d = ""
        let pen = false
        history.forEach((h, i) => {
          const v = line.get(h)
          if (typeof v !== "number" || !Number.isFinite(v) || h.restart) pen = false
          if (typeof v !== "number" || !Number.isFinite(v)) return
          d += `${pen ? "L" : "M"}${x(i).toFixed(2)} ${y(v).toFixed(2)} `
          if (n === 1) d += `M${(x(i) - 3).toFixed(2)} ${y(v).toFixed(2)} L${(x(i) + 3).toFixed(2)} ${y(v).toFixed(2)} `
          pen = true
        })
        if (d) el("path", {d: d.trim(), class: "line " + line.cls + (line.dash ? " dash" : "")}, svg)
      }
    }
    history.forEach((h, i) => {
      const hit = el("rect", {x: x(i) - W / n / 2, y: 0, width: W / n, height: H, class: "hit"}, svg)
      const title = el("title", {}, hit)
      let value = row.value(h)
      if (row.key === "think") value += " " + t("compute_note", {s: fmt(h.compute_s, 1)})
      title.textContent = t("point_title", {n: h.move_number, san: h.san, value})
      if (onSelect) hit.addEventListener("click", () => onSelect(h.ply))
    })
    const val = document.createElement("span")
    val.className = "series-value"
    val.textContent = row.value(last)
    wrap.append(label, svg, val)
    container.appendChild(wrap)
  }
}
