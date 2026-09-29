"""Accessibility guards for the ``cca play`` page (WCAG 2.x AA).

The page was audited with axe-core (WCAG 2.0/2.1/2.2 A and AA rules) in both themes and both
languages; these tests pin what that audit fixed so a later CSS or markup change cannot undo it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "cca" / "play" / "static"
CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
BOARD_JS = (STATIC / "js" / "board.js").read_text(encoding="utf-8")
MAIN_JS = (STATIC / "js" / "main.js").read_text(encoding="utf-8")

TEXT_TOKENS = ("--text", "--muted", "--bad", "--warn", "--good", "--accent")
SURFACES = ("--bg", "--surface", "--surface-2", "--surface-3", "--accent-soft")
TOKEN = re.compile(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b")


def _themes() -> dict[str, dict[str, str]]:
    light_block = CSS.split("@media")[0]
    dark_rule = r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root[^{]*\{(.*?)\}"
    dark = re.search(dark_rule, CSS, re.DOTALL)
    assert dark is not None, "dark theme block not found"
    light = dict(TOKEN.findall(light_block))
    return {"light": light, "dark": {**light, **dict(TOKEN.findall(dark.group(1)))}}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_formula_matches_wcag() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_text_colour_meets_aa_on_every_surface(theme: str) -> None:
    tokens = _themes()[theme]
    failures = [
        f"{fg} {tokens[fg]} on {bg} {tokens[bg]}: {contrast(tokens[fg], tokens[bg]):.2f}"
        for fg in TEXT_TOKENS
        for bg in SURFACES
        if contrast(tokens[fg], tokens[bg]) < 4.5
    ]
    assert not failures, failures


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_button_text_on_accent_meets_aa(theme: str) -> None:
    tokens = _themes()[theme]
    assert contrast(tokens["--on-accent"], tokens["--accent"]) >= 4.5


def test_board_svg_has_an_accessible_name() -> None:
    # cm-chessboard renders <svg role="img"> without a name (axe: svg-img-alt).
    assert 'setAttribute("aria-labelledby", "board-heading")' in BOARD_JS
    assert 'id="board-heading"' in HTML


def test_scrollable_candidate_table_is_keyboard_reachable() -> None:
    # A horizontally scrolling region must be focusable (axe: scrollable-region-focusable).
    wrap = re.search(r'<div class="table-wrap"([^>]*)>', HTML)
    assert wrap is not None
    attrs = wrap.group(1)
    assert 'tabindex="0"' in attrs
    assert 'role="region"' in attrs
    label = re.search(r'aria-labelledby="([^"]+)"', attrs)
    assert label is not None
    assert f'id="{label.group(1)}"' in HTML


def test_the_ask_banner_is_announced_when_it_appears() -> None:
    # The "Ask CCA to move" banner appears without the focus moving to it: a status message
    # must reach assistive technology anyway (WCAG 4.1.3), through the page's polite live
    # region, once, when the banner goes from hidden to shown (not on every render).
    announcer = re.search(r"<div ([^>]*)id=\"announcer\"([^>]*)>", HTML)
    assert announcer is not None
    attrs = announcer.group(1) + announcer.group(2)
    assert 'aria-live="polite"' in attrs
    assert 'aria-atomic="true"' in attrs
    assert re.search(r'<div class="ask-banner" id="ask-banner" hidden>', HTML)
    render = MAIN_JS.split("function renderControls() {", 1)[1].split("\n}\n", 1)[0]
    shown = re.search(
        r'const ask = \$\("ask-banner"\)\n\s*const asking = canAsk\(ui\)\n'
        r"\s*if \(asking && ask\.hidden\) "
        r'announce\(`\$\{t\("ask_text"\)\} \$\{t\("ask_cca"\)\}\.`\)\n'
        r"\s*ask\.hidden = !asking",
        render,
    )
    assert shown, render
    announce = MAIN_JS.split("function announce(text) {", 1)[1].split("\n}\n", 1)[0]
    assert '$("announcer")' in announce
