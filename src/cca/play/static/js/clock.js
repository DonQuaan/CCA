// cca play - clock display - part of CCA (Apache-2.0).
// Pure functions (no DOM), so they can be unit-tested outside a browser.

// Milliseconds left on `color`'s clock at time `now` (performance.now()), for a server state
// that ARRIVED at `receivedAt`. The server reports each clock as of its response and delays the
// start of the side to move by `starts_in_ms` (CCA's emulated thinking time). Both are measured
// from the response, so `receivedAt` must be the arrival time even when the page shows the state
// later (after the "thinking" delay); anchoring at display time would freeze the clock that long.
export function remainingMs(state, color, receivedAt, now) {
  const c = state.clocks
  const base = c[color + "_ms"]
  if (c.running !== color || state.game_over) return base
  const elapsed = now - receivedAt - c.starts_in_ms
  return Math.max(0, base - Math.max(0, elapsed))
}
