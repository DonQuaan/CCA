"""Read the Render Blueprint (render.yaml) without a YAML library: its command and image tag.

deploy/render/service.py (the deploy's options check) and docker/constrained.py (the run under
the free plan's limits) read the Blueprint here, so both see the same command; docker/tests
checks the result against PyYAML. Standard library only.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Words of dockerCommand: no quotes, $, #, globs or separators, so a shell and a split on spaces
# give the same argument list.
WORD = re.compile(r"[A-Za-z0-9._/=+-]+")
_KEY = re.compile(r"^(?P<indent> *)dockerCommand:(?P<rest>.*)$")
_URL = re.compile(r"^ +url: *(?P<url>\S+) *$")


class BlueprintError(ValueError):
    """The Blueprint is not in the form this module reads; the message says why."""


def command_words(text: str) -> list[str]:
    """The words of ``dockerCommand`` in a Blueprint that has exactly one.

    Reads the two YAML forms the Blueprint may use: a plain one-line value, or a folded block
    (``>`` or ``>-``) of equally indented lines, which YAML joins with single spaces.

    Raises:
        BlueprintError: no or several ``dockerCommand`` keys, another form, or a word with a
            character outside ``WORD``.
    """
    lines = text.splitlines()
    found = [(i, m) for i, line in enumerate(lines) if (m := _KEY.match(line))]
    if len(found) != 1:
        raise BlueprintError(f"the Blueprint must have one dockerCommand, found {len(found)}")
    index, match = found[0]
    rest = match["rest"].strip()
    if rest in {">", ">-"}:
        block: list[str] = []
        for line in lines[index + 1 :]:
            indent = len(line) - len(line.lstrip(" "))
            if line.strip() and indent <= len(match["indent"]):
                break
            block.append(line)
        while block and not block[-1].strip():
            block.pop()
        indents = {len(line) - len(line.lstrip(" ")) for line in block}
        if not block or len(indents) != 1 or any(not line.strip() for line in block):
            raise BlueprintError(
                "dockerCommand's folded block must be non-empty lines with one indentation"
            )
        words = " ".join(line.strip() for line in block).split()
    elif rest and rest[0] not in "'\"|>&*!{[%@`#" and " #" not in rest:
        words = rest.split()
    else:
        raise BlueprintError(
            "dockerCommand must be plain words, on its line or in a folded block (>-)"
        )
    bad = [w for w in words if not WORD.fullmatch(w)]
    if bad:
        raise BlueprintError(f"dockerCommand words must match {WORD.pattern}: {bad[:3]!r:.120}")
    return words


def options(words: Iterable[str]) -> set[str]:
    """The option names among ``words``: ``--name`` (also from ``--name=value``) and ``-x``.

    A value such as ``8765`` or ``-1``, and a bare ``--``, is not an option.
    """
    found = set()
    for word in words:
        name = word.split("=", 1)[0]
        if (name.startswith("--") and len(name) > 2) or (name[:1] == "-" and name[1:2].isalpha()):
            found.add(name)
    return found


def image_tag(text: str, image: str) -> str:
    """The tag of ``image`` in the Blueprint's one ``url:`` line (``image: {url: IMAGE:TAG}``).

    Raises:
        BlueprintError: no or several ``url:`` lines, or one that is not ``IMAGE:TAG``.
    """
    urls = [m["url"] for line in text.splitlines() if (m := _URL.match(line))]
    if len(urls) != 1:
        raise BlueprintError(f"the Blueprint must have one image url, found {len(urls)}")
    name, sep, tag = urls[0].rpartition(":")
    if name != image or not sep or not tag:
        raise BlueprintError(f"the Blueprint's image is {urls[0]!r:.120}, not {image}:<tag>")
    return tag
