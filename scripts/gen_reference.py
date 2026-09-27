"""Generate ``docs/reference.md``: every technical parameter of CCA, derived from the code.

Usage::

    uv run python scripts/gen_reference.py            # (re)write docs/reference.md
    uv run python scripts/gen_reference.py --check    # exit 1 + unified diff if it is stale

Every value, name, type, default and description in the output is read when it runs: from the
imported ``cca`` package (which must be the one in this checkout's ``src/``) and its source
(parsed with ``ast`` for docstrings, field docstrings, constants and environment variable
reads), ``pyproject.toml``, ``engines/stockfish.lock.json``, ``scripts/fetch_stockfish.py``,
``scripts/audit_deps.py`` and the other ``scripts/*.py``. Only the short sentences that
introduce each section are written here. The output is deterministic: no timestamps, LF line
endings, UTF-8, stable ordering.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import difflib
import functools
import html
import importlib
import importlib.util
import inspect
import io
import json
import operator
import re
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from importlib import resources
from pathlib import Path, PurePath
from types import FunctionType, ModuleType
from typing import Any, TypeGuard, get_type_hints

import cca
from cca import cli, config
from cca.agent import AgentConfig
from cca.chaos.baseline import AR1Params
from cca.chaos.lorenz import LorenzParams
from cca.engines import maia2_human, stockfish
from cca.neuro.controller import Persona
from cca.neuro.neuromodulation import NeuroParams
from cca.timing.think_time import ThinkTimeParams
from cca.uci import protocol

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "reference.md"

CONFIG_CLASSES: tuple[type[Any], ...] = (
    AgentConfig,
    Persona,
    NeuroParams,
    ThinkTimeParams,
    LorenzParams,
    AR1Params,
)
DATA_MODEL_MODULE = "cca.core.types"
EQUATION_MODULES = (
    "cca.agent",
    "cca.core.types",
    "cca.neuro.controller",
    "cca.neuro.neuromodulation",
    "cca.policy.pikl",
    "cca.policy.exploit",
    "cca.policy.distributions",
    "cca.chaos.lorenz",
    "cca.chaos.baseline",
    "cca.timing.think_time",
)
_UCI_KEYWORDS = frozenset({"name", "type", "default", "min", "max", "var"})
_ENV_METHODS = frozenset({"get", "pop", "setdefault"})
_Scope = tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, ...]
# A CommonMark code span: a backtick run closed by the next run of exactly the same length.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)
# The address in CPython's default repr (``<function f at 0x7f...>``): it differs per process.
_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+(?=>)")
_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
}


# ---------------------------------------------------------------- markdown helpers
def fmt_value(value: object) -> str:
    """Platform-independent literal for a default value (``Path`` is shown POSIX-style)."""
    if isinstance(value, PurePath):
        text = f"Path({value.as_posix()!r})"
    elif isinstance(value, tuple):
        inner = ", ".join(fmt_value(v) for v in value)
        text = f"({inner},)" if len(value) == 1 else f"({inner})"
    elif isinstance(value, list):
        text = "[" + ", ".join(fmt_value(v) for v in value) + "]"
    elif isinstance(value, (set, frozenset)):
        items = sorted(fmt_value(v) for v in value)
        text = "{" + ", ".join(items) + "}" if items else f"{type(value).__name__}()"
    elif isinstance(value, Mapping):
        text = "{" + ", ".join(f"{fmt_value(k)}: {fmt_value(v)}" for k, v in value.items()) + "}"
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        args = (f"{f.name}={fmt_value(getattr(value, f.name))}" for f in dataclasses.fields(value))
        text = f"{type(value).__name__}({', '.join(args)})"
    elif isinstance(value, (str, bytes)):
        text = repr(value)
    else:
        text = stable_repr(value)
    return text


def stable_repr(value: object) -> str:
    """``repr`` without memory addresses, so the output is the same in every process."""
    return _ADDRESS.sub("", repr(value))


def code(text: str) -> str:
    """Inline code span that survives backticks inside ``text``."""
    if not text:
        return "*(empty)*"
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if "`" in (text[0], text[-1]) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def block(text: str, lang: str = "text") -> list[str]:
    """Fenced code block whose fence is longer than any backtick run in ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", *text.splitlines(), fence]


def inline(text: str) -> str:
    """Markdown inline text that renders as written, code spans left verbatim.

    Outside code spans ``&``, ``<`` and ``>`` become entities: help texts such as
    ``<auto> = $CCA_STOCKFISH`` would otherwise be raw HTML tags, which GitHub drops.
    """
    out: list[str] = []
    pos = 0
    for span in _CODE_SPAN.finditer(text):
        out.extend([html.escape(text[pos : span.start()], quote=False), span.group()])
        pos = span.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def _cell(text: str) -> str:
    return inline(" ".join(text.split())).replace("|", r"\|")


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """GitHub-flavoured Markdown table (HTML and pipes escaped, newlines folded)."""
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    out.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return out


def _one_line(doc: str | None) -> str:
    return " ".join(doc.split()) if doc else ""


# ---------------------------------------------------------------- source access
@functools.cache
def _source(path: Path) -> tuple[str, ast.Module]:
    text = path.read_text(encoding="utf-8")
    return text, ast.parse(text, filename=str(path))


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _package_files() -> list[Path]:
    return sorted((ROOT / "src" / "cca").rglob("*.py"), key=_rel)


def _module_name(path: Path) -> str:
    parts = list(path.resolve().relative_to((ROOT / "src").resolve()).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_path(module: ModuleType) -> Path:
    if module.__file__ is None:
        raise RuntimeError(f"{module.__name__} has no source file")
    return Path(module.__file__)


def _load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>`` (a file name) without running its ``main()``.

    The module is in ``sys.modules`` while it executes, because a dataclass looks its module up
    there, and is removed again afterwards so nothing leaks into the importing process.
    """
    module_name = f"cca_reference_{Path(name).stem}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scripts/{name}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _check_source_tree() -> None:
    """Refuse to document a ``cca`` that is not this checkout's ``src/cca``."""
    imported = _module_path(cca).resolve().parent
    expected = (ROOT / "src" / "cca").resolve()
    if imported != expected:
        raise SystemExit(
            f"cca was imported from {imported}, not {expected}: install this checkout "
            "(uv sync) before generating its reference"
        )


# ---------------------------------------------------------------- 1. package
def section_package() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    build = pyproject.get("build-system", {})
    rows = [
        ["Distribution", code(project["name"])],
        ["Version (`cca.__version__`)", code(cca.__version__)],
        ["Python", code(project.get("requires-python", ""))],
        ["Licence", code(str(project.get("license", "")))],
        ["Build backend", code(build.get("build-backend", ""))],
    ]
    rows.extend(["Dependency", code(dep)] for dep in project.get("dependencies", []))
    extras = project.get("optional-dependencies", {})
    rows.extend([f"Extra `{name}`", code(dep)] for name in sorted(extras) for dep in extras[name])
    scripts = project.get("scripts", {})
    rows.extend([f"Console script `{name}`", code(scripts[name])] for name in sorted(scripts))
    groups = pyproject.get("dependency-groups", {})
    rows.extend(
        [f"Dependency group `{name}`", code(dep)] for name in sorted(groups) for dep in groups[name]
    )
    return [
        "## 1. Package",
        "",
        "Read from `pyproject.toml`; the version from the imported package.",
        "",
        *table(["Item", "Value"], rows),
    ]


# ---------------------------------------------------------------- 2. pinned artefacts
def _stockfish_assets() -> list[str]:
    lock = json.loads((ROOT / "engines" / "stockfish.lock.json").read_text(encoding="utf-8"))
    fetch = ROOT / "scripts" / "fetch_stockfish.py"
    download = _load_script(fetch.name).DOWNLOAD
    rows = []
    for key in sorted(lock):
        tag, _, name = key.partition("/")
        url = download.format(tag=tag, name=name) if name else ""
        rows.append([code(key), code(lock[key]), url])
    return [
        "### 2.1 Stockfish release assets",
        "",
        "Pins from `engines/stockfish.lock.json`; URLs from `DOWNLOAD` in",
        "`scripts/fetch_stockfish.py`.",
        "",
        *table(["Asset (`tag/file`)", "SHA-256", "Download URL"], rows),
        "",
        "How the pins are enforced (docstring of `scripts/fetch_stockfish.py`):",
        "",
        *block(ast.get_docstring(_source(fetch)[1]) or ""),
    ]


def _maia2_checkpoints() -> list[str]:
    pins = maia2_human.PINNED_SHA256
    rows = [[code(kind), code(pins[kind])] for kind in sorted(pins)]
    weights = _rel(maia2_human.PROJECT_WEIGHTS)
    verify = _function_node(_module_path(maia2_human), "verify_checkpoint")
    return [
        "### 2.2 Maia-2 checkpoints",
        "",
        "`cca.engines.maia2_human.PINNED_SHA256`, enforced by `verify_checkpoint`:",
        inline(_one_line(ast.get_docstring(verify))),
        f"Default folder: `{weights}/` (see `CCA_WEIGHTS`).",
        "",
        *table(["Model type", "SHA-256"], rows),
    ]


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    _, tree = _source(path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise RuntimeError(f"{_display(path)} has no top-level function {name}")


def _win_rate_model() -> list[str]:
    path = _module_path(stockfish)
    text, _ = _source(path)
    node = _function_node(path, "sf19_expected_score")
    doc = ast.get_docstring(node) or ""
    source = ast.get_source_segment(text, node) or ""
    rows = [
        [str(i), code(repr(a)), code(repr(b))]
        for i, (a, b) in enumerate(zip(stockfish._WR_A, stockfish._WR_B, strict=True))
    ]
    return [
        "### 2.3 Stockfish 17-19 win-rate model",
        "",
        "Used when the engine gives no WDL. Coefficients `cca.engines.stockfish._WR_A` / `_WR_B`",
        "(polynomial in the clamped, normalised material `m`, highest power first):",
        "",
        *table(["i", "`_WR_A[i]`", "`_WR_B[i]`"], rows),
        "",
        "Formula (docstring of `sf19_expected_score`):",
        "",
        *block(doc),
        "",
        "Implementation:",
        "",
        *block(source, "python"),
    ]


def section_artefacts() -> list[str]:
    return [
        "## 2. Pinned artefacts",
        "",
        *_stockfish_assets(),
        "",
        *_maia2_checkpoints(),
        "",
        *_win_rate_model(),
    ]


# ---------------------------------------------------------------- 3. CLI
def _factory_order(fn: FunctionType, factory: str, bound: Mapping[str, object]) -> list[str]:
    """Closure variables in the factory's parameter order when it resolves, else sorted."""
    target: object = sys.modules.get(fn.__module__)
    for part in factory.split("."):
        target = getattr(target, part, None)
    first: list[str] = []
    if callable(target):
        first = [p for p in inspect.signature(target).parameters if p in bound]
    return [*first, *sorted(set(bound) - set(first))]


def type_name(fn: object) -> str:
    """Name of an argparse ``type=`` callable; a closure shows the factory call that made it.

    ``type=_bounded_int(0, 65535)`` is an inner function called ``parse``: it is shown as
    ``int via _bounded_int(lo=0, hi=65535)`` (its return annotation and the values it captured).
    """
    name = getattr(fn, "__name__", None)
    if not isinstance(name, str):
        return stable_repr(fn)
    if not isinstance(fn, FunctionType) or ".<locals>." not in fn.__qualname__:
        return name
    returns = inspect.signature(fn).return_annotation
    if returns is not inspect.Signature.empty:
        name = returns if isinstance(returns, str) else getattr(returns, "__name__", str(returns))
    bound = inspect.getclosurevars(fn).nonlocals
    if not bound:
        return name
    factory = fn.__qualname__.rsplit(".<locals>.", 1)[0]
    args = ", ".join(f"{k}={fmt_value(bound[k])}" for k in _factory_order(fn, factory, bound))
    return f"{name} via {factory}({args})"


def _arg_kind(action: argparse.Action) -> str:
    if action.nargs == 0:
        name = type(action).__name__.strip("_").removesuffix("Action")
        return f"flag ({name})"
    kind = "str" if action.type is None else type_name(action.type)
    if action.nargs is not None:
        kind += f", nargs={action.nargs}"
    return kind


def _arg_default(action: argparse.Action) -> str:
    if action.required:
        return "*required*"
    if action.default is argparse.SUPPRESS:
        return ""
    return code(fmt_value(action.default))


def _arg_help(parser: argparse.ArgumentParser, action: argparse.Action) -> str:
    if action.help is None:
        return ""
    if action.help == argparse.SUPPRESS:
        return "(hidden from `--help`)"
    if "%" in action.help:
        return parser._get_formatter()._expand_help(action)
    return action.help


def _arg_row(parser: argparse.ArgumentParser, action: argparse.Action) -> list[str]:
    flag = ", ".join(action.option_strings) or str(action.metavar or action.dest)
    choices = ", ".join(code(fmt_value(c)) for c in action.choices) if action.choices else ""
    return [
        code(flag),
        _arg_kind(action),
        _arg_default(action),
        choices,
        _arg_help(parser, action),
    ]


def _option_key(action: argparse.Action) -> str:
    return max(action.option_strings, key=len).lstrip("-").lower()


def _blocks(*blocks: list[str]) -> list[str]:
    """Join non-empty blocks of lines with exactly one blank line between them."""
    out: list[str] = []
    for b in blocks:
        if b:
            out.extend([*([""] if out else []), *b])
    return out


def _parser_text(parser: argparse.ArgumentParser, text: str | None) -> str:
    """A description or epilog as ``--help`` prints it: ``%(prog)s`` expanded, one line."""
    if text and "%(prog)" in text:
        text = text % {"prog": parser.prog}
    return _one_line(text)


def cli_section(
    parser: argparse.ArgumentParser, title: str | None = None, summary: str = ""
) -> list[str]:
    """One heading per (sub)command with a table of its arguments, recursing into subparsers."""
    title = title or parser.prog
    description = _parser_text(parser, parser.description)
    epilog = _parser_text(parser, parser.epilog)
    actions = [a for a in parser._actions if not isinstance(a, argparse._HelpAction)]
    subs = [a for a in actions if isinstance(a, argparse._SubParsersAction)]
    plain = [a for a in actions if not isinstance(a, argparse._SubParsersAction)]
    positionals = [a for a in plain if not a.option_strings]
    options = sorted((a for a in plain if a.option_strings), key=_option_key)
    rows = [_arg_row(parser, a) for a in (*positionals, *options)]
    args = table(["Argument", "Type", "Default", "Choices", "Help"], rows) if rows else []
    lines = _blocks(
        [f"### `{title}`"],
        [inline(summary)] if summary else [],
        [inline(description)] if description and description != summary else [],
        args or ([] if subs else ["No arguments."]),
        [inline(epilog)] if epilog else [],
    )
    for sub in subs:
        # Every parser, including those added without help= (absent from _choices_actions);
        # an alias maps to the same parser object as its command, which is registered first.
        by_parser: dict[int, list[str]] = {}
        for name, child in sub._name_parser_map.items():
            by_parser.setdefault(id(child), []).append(name)
        commands = sorted((names[0], names[1:]) for names in by_parser.values())
        helps = {a.dest: a.help or "" for a in sub._choices_actions}
        shown = ", ".join(code(name) for name, _ in commands)
        lines = _blocks(lines, [f"Subcommands: {shown}."])
        for name, aliases in commands:
            summary_line = helps.get(name, "")
            if aliases:
                summary_line += f" (aliases: {', '.join(code(a) for a in aliases)})"
            child_lines = cli_section(
                sub._name_parser_map[name], f"{title} {name}", summary_line.strip()
            )
            lines = _blocks(lines, child_lines)
    return lines


def section_cli() -> list[str]:
    return [
        "## 3. Command-line interface",
        "",
        "Introspected from `cca.cli.build_parser()`; `-h/--help` is omitted everywhere.",
        "",
        *cli_section(cli.build_parser()),
    ]


# ---------------------------------------------------------------- 4. UCI
@dataclasses.dataclass(frozen=True, slots=True)
class UciOption:
    """One ``option name ...`` line of the UCI handshake."""

    name: str
    kind: str
    default: str | None = None
    min: str | None = None
    max: str | None = None
    vars: tuple[str, ...] = ()


def parse_uci_option(line: str) -> UciOption:
    """Parse ``option name <n> type <t> [default <d>] [min <x>] [max <y>] [var <v>]*``."""
    tokens = line.split()
    if tokens[:2] != ["option", "name"]:
        raise ValueError(f"not a UCI option line: {line!r}")
    fields: dict[str, list[str]] = {}
    values: list[list[str]] = []
    current: list[str] = []
    for word in tokens[1:]:
        if word in _UCI_KEYWORDS:
            current = []
            if word == "var":
                values.append(current)
            else:
                fields[word] = current
        else:
            current.append(word)

    def joined(key: str) -> str | None:
        return " ".join(fields[key]) if key in fields else None

    name, kind = joined("name"), joined("type")
    if not name or not kind:
        raise ValueError(f"UCI option line without name or type: {line!r}")
    return UciOption(
        name=name,
        kind=kind,
        default=joined("default"),
        min=joined("min"),
        max=joined("max"),
        vars=tuple(" ".join(v) for v in values),
    )


def uci_handshake() -> list[str]:
    """Run ``uci`` / ``quit`` through a real :class:`UciServer` (``uci`` starts no engine)."""
    out = io.StringIO()
    protocol.UciServer(stdin=io.StringIO("uci\nquit\n"), stdout=out).serve()
    lines = out.getvalue().splitlines()
    if "uciok" not in lines:
        raise RuntimeError(f"the UCI handshake did not answer uciok: {lines}")
    return lines


def _uci_help() -> dict[str, str]:
    raw = getattr(protocol, "UCI_OPTION_HELP", None)
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def section_uci() -> list[str]:
    lines = uci_handshake()
    ids = [line.removeprefix("id ").partition(" ") for line in lines if line.startswith("id ")]
    options = [parse_uci_option(line) for line in lines if line.startswith("option ")]
    helps = _uci_help()
    rows = [
        [
            code(o.name),
            o.kind,
            "" if o.default is None else code(o.default),
            "" if o.min is None else code(o.min),
            "" if o.max is None else code(o.max),
            ", ".join(code(v) for v in o.vars),
            helps.get(o.name, ""),
        ]
        for o in options
    ]
    out = [
        "## 4. UCI options",
        "",
        "Captured from a real in-process handshake (`uci` then `quit` through",
        "`cca.uci.protocol.UciServer`), in the order the engine advertises them. Descriptions",
        "come from `cca.uci.protocol.UCI_OPTION_HELP` when the module defines it.",
        "",
        *table(["Identifier", "Value"], [[code(k), code(v)] for k, _, v in ids]),
        "",
        *table(["Option", "Type", "Default", "Min", "Max", "Values", "Description"], rows),
    ]
    placeholders = sorted(getattr(protocol, "_PLACEHOLDERS", ()))
    if placeholders:
        shown = ", ".join(code(p) for p in placeholders)
        out.extend(["", f"Default placeholders that mean *unset*: {shown}."])
    orphans = sorted(set(helps) - {o.name for o in options})
    if orphans:
        shown = ", ".join(code(n) for n in orphans)
        out.extend(["", f"`UCI_OPTION_HELP` entries with no advertised option: {shown}."])
    return out


# ---------------------------------------------------------------- 5-7. dataclasses
def _class_node(cls: type[Any]) -> tuple[str, ast.ClassDef]:
    path = _module_path(sys.modules[cls.__module__])
    text, tree = _source(path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            return text, node
    raise RuntimeError(f"{_display(path)} has no top-level class {cls.__name__}")


def field_sources(cls: type[Any]) -> dict[str, tuple[str, str]]:
    """``{field: (annotation as written, attribute docstring or "")}`` from the class source."""
    text, node = _class_node(cls)
    found: dict[str, tuple[str, str]] = {}
    body = node.body
    for i, stmt in enumerate(body):
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        annotation = " ".join((ast.get_source_segment(text, stmt.annotation) or "").split())
        nxt = body[i + 1] if i + 1 < len(body) else None
        doc = ""
        if (
            isinstance(nxt, ast.Expr)
            and isinstance(nxt.value, ast.Constant)
            and isinstance(nxt.value.value, str)
        ):
            doc = nxt.value.value
        found[stmt.target.id] = (annotation, _one_line(doc))
    return found


def field_default(f: dataclasses.Field[Any]) -> str:
    """Rendered default of a dataclass field (``*required*`` when it has none)."""
    if f.default is not dataclasses.MISSING:
        return code(fmt_value(f.default))
    if f.default_factory is not dataclasses.MISSING:
        value = f.default_factory()
        if _is_default_instance(value):
            return code(f"{type(value).__name__}()")
        return code(fmt_value(value))
    return "*required*"


def _is_default_instance(value: object) -> bool:
    """True for a dataclass instance equal to its class's no-argument construction."""
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return False
    try:
        return bool(value == type(value)())
    except TypeError:  # the class has required fields
        return False


def dataclass_section(cls: type[Any], toml_sections: Sequence[str] = ()) -> list[str]:
    """Heading, class docstring and a field table for one dataclass."""
    _, node = _class_node(cls)
    sources = field_sources(cls)
    lines = [f"### `{cls.__name__}`", "", f"`{cls.__module__}.{cls.__qualname__}`"]
    if toml_sections:
        shown = ", ".join(code(f"[{name}]") for name in toml_sections)
        lines[-1] += f"; TOML section {shown}"
    lines[-1] += "."
    doc = ast.get_docstring(node)
    if doc:
        lines.extend(["", inline(_one_line(doc))])
    rows = []
    for f in dataclasses.fields(cls):
        annotation, field_doc = sources.get(f.name, ("", ""))
        rows.append([code(f.name), code(annotation), field_default(f), field_doc])
    lines.extend(["", *table(["Field", "Type", "Default", "Description"], rows)])
    return lines


def _is_dataclass_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id == "dataclass"
    return isinstance(target, ast.Attribute) and target.attr == "dataclass"


def discover_dataclasses() -> list[type[Any]]:
    """Every public top-level dataclass in ``src/cca``, in file then source order."""
    found: list[type[Any]] = []
    for path in _package_files():
        _, tree = _source(path)
        names = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and not node.name.startswith("_")
            and any(_is_dataclass_decorator(d) for d in node.decorator_list)
        ]
        if names:
            module = importlib.import_module(_module_name(path))
            found.extend(getattr(module, name) for name in names)
    return found


def config_file_sections() -> list[tuple[str, type[Any]]]:
    """``(TOML section, dataclass)`` for each top-level section ``load_agent_config`` accepts.

    The accepted names are the set literal the loader subtracts from the file's keys
    (``set(raw) - {"agent", *_SECTIONS}``). A string in it is loaded into the loader's return
    type; a ``*mapping`` contributes that module-level ``{section: class}`` mapping. Any other
    shape is an error, so a rewritten loader cannot be documented half-way.
    """
    loader = config.load_agent_config
    node = _function_node(_module_path(config), loader.__name__)
    found = [
        n.right
        for n in ast.walk(node)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) and isinstance(n.right, ast.Set)
    ]
    where = f"cca.config.{loader.__name__}"
    if len(found) != 1:
        raise RuntimeError(f"{where}: expected one `set(raw) - {{...}}` of accepted sections")
    returns = get_type_hints(loader)["return"]
    sections: list[tuple[str, type[Any]]] = []
    for element in found[0].elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            sections.append((element.value, returns))
        elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
            mapping = getattr(config, element.value.id)
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{where}: {element.value.id} is not a mapping")
            sections.extend(mapping.items())
        else:
            raise TypeError(f"{where}: cannot read section {ast.unparse(element)}")
    names = [name for name, _ in sections]
    if len(set(names)) != len(names) or sum(cls is returns for _, cls in sections) != 1:
        raise RuntimeError(f"{where}: ambiguous sections {names}")
    if not all(isinstance(n, str) and dataclasses.is_dataclass(c) for n, c in sections):
        raise RuntimeError(f"{where}: every section must map a name to a dataclass")
    return sections


def _config_file() -> list[str]:
    loader = config.load_agent_config
    module_doc = ast.get_docstring(_source(_module_path(config))[1]) or ""
    loader_doc = ast.get_docstring(_function_node(_module_path(config), loader.__name__)) or ""
    rows = [[code(f"[{name}]"), code(cls.__name__)] for name, cls in config_file_sections()]
    return [
        "### Configuration file",
        "",
        f"`cca.config.{loader.__name__}` reads one TOML file. The docstrings of `cca.config` and",
        "of that function, verbatim:",
        "",
        *block(module_doc),
        "",
        *block(loader_doc),
        "",
        "The top-level sections it accepts, read from its source, and the class whose fields",
        "each one sets:",
        "",
        *table(["TOML section", "Class"], rows),
    ]


def section_dataclasses() -> list[str]:
    sections: dict[type[Any], list[str]] = {}
    for name, cls in config_file_sections():
        sections.setdefault(cls, []).append(name)
    everything = discover_dataclasses()
    model = [c for c in everything if c.__module__ == DATA_MODEL_MODULE]
    other = [c for c in everything if c not in CONFIG_CLASSES and c not in model]
    lines = [
        "## 5. Configuration",
        "",
        "Field types and descriptions are read from the class source (the string literal after",
        "a field is its description); defaults from the dataclass fields themselves.",
        "",
        *_config_file(),
    ]
    for cls in CONFIG_CLASSES:
        lines.extend(["", *dataclass_section(cls, sections.get(cls, []))])
    lines.extend(["", "## 6. Data model", "", f"Every dataclass in `{DATA_MODEL_MODULE}`."])
    for cls in model:
        lines.extend(["", *dataclass_section(cls)])
    lines.extend(["", "## 7. Other dataclasses", "", "Every other public dataclass in `cca`."])
    for cls in other:
        lines.extend(["", *dataclass_section(cls)])
    return lines


# ---------------------------------------------------------------- 8. personas
def toml_description(text: str) -> str:
    """The leading ``#`` comment block of a TOML file, as one line."""
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("#"):
            break
        lines.append(stripped.lstrip("#").strip())
    return _one_line(" ".join(lines))


def section_personas() -> list[str]:
    folder = resources.files("cca.personas")
    names = config.list_personas()
    texts = {n: folder.joinpath(f"{n}.toml").read_text(encoding="utf-8") for n in names}
    loaded = {n: config.load_persona(n) for n in names}
    overridden = {n: set(tomllib.loads(texts[n])) for n in names}
    rows = []
    for f in dataclasses.fields(Persona):
        row = [code(f.name), field_default(f)]
        for n in names:
            value = code(fmt_value(getattr(loaded[n], f.name)))
            row.append(f"**{value}**" if f.name in overridden[n] else value)
        rows.append(row)
    lines = [
        "## 8. Shipped personas",
        "",
        "`src/cca/personas/*.toml` loaded with `cca.config.load_persona`. **Bold** = set in the",
        "TOML file; plain = the `Persona` default. Descriptions are the files' header comments.",
        "",
        *table(["Field", "Default", *names], rows),
        "",
    ]
    lines.extend(
        f"- `{n}`: {inline(toml_description(texts[n])) or '(no description)'}" for n in names
    )
    return lines


# ---------------------------------------------------------------- 9. equations
def section_equations() -> list[str]:
    lines = [
        "## 9. Model equations",
        "",
        "Module docstrings, verbatim: they state the formulas the code implements.",
    ]
    for name in EQUATION_MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:  # a parent package is missing
            spec = None
        if spec is None or spec.origin is None:
            continue
        _, tree = _source(Path(spec.origin))
        doc = ast.get_docstring(tree)
        if doc:
            lines.extend(["", f"### `{name}`", "", *block(doc)])
    return lines


# ---------------------------------------------------------------- 10. constants
def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_numeric(value: object) -> bool:
    """A number, or a tuple/list of numbers, ``None`` and such tuples with at least one number."""
    if isinstance(value, (tuple, list)):
        return any(map(_is_numeric, value)) and all(v is None or _is_numeric(v) for v in value)
    return _is_number(value)


def _operand(node: ast.expr) -> int | float:
    value = fold_constant(node)
    if not _is_number(value):
        raise ValueError(f"arithmetic on a non-number: {ast.dump(node)}")
    return value


def fold_constant(node: ast.expr) -> object:
    """Value of an expression built only from numbers, ``None``, tuples/lists and arithmetic.

    Raises ``ValueError`` for anything else (a name, a call, a string, ...). Nothing is
    executed: the operators are applied to the folded operands, as importing the module would.
    """
    if isinstance(node, ast.Constant) and (node.value is None or _is_number(node.value)):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        items = [fold_constant(item) for item in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_operand(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        return _BINARY_OPS[type(node.op)](_operand(node.left), _operand(node.right))
    raise ValueError(f"not a constant number expression: {ast.dump(node)}")


def numeric_constants(path: Path) -> list[tuple[str, str, str]]:
    """``(name, value, expression)`` of the top-level numeric constants of one module.

    A constant is ``NAME = <expr>`` where ``<expr>`` folds (:func:`fold_constant`) to a number
    or a tuple/list of numbers and ``None``. ``expression`` is the source text when it is
    computed (``64 * 1024``) and empty when it is a plain literal.
    """
    text, tree = _source(path)
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            folded = fold_constant(value)
        except ValueError:  # a name, a call, a string, ...
            continue
        if not _is_numeric(folded):
            continue
        try:
            ast.literal_eval(value)
            expression = ""
        except ValueError:  # computed, e.g. 64 * 1024
            expression = " ".join((ast.get_source_segment(text, value) or "").split())
        found.append((target.id, fmt_value(folded), expression))
    return found


def section_constants() -> list[str]:
    rows = [
        [code(_module_name(path)), code(name), code(value), code(expr) if expr else ""]
        for path in _package_files()
        for name, value, expr in numeric_constants(path)
    ]
    return [
        "## 10. Module-level numeric constants",
        "",
        "Top-level assignments in `src/cca` whose value is a number, or a tuple/list of numbers",
        "and `None`, written as literals or as arithmetic on them (folded here; the expression",
        "is shown). Values computed from other names or from calls are not listed.",
        "",
        *table(["Module", "Name", "Value", "Expression"], rows),
    ]


# ---------------------------------------------------------------- 11. environment
@dataclasses.dataclass(frozen=True, slots=True, order=True)
class EnvRead:
    """One place where the code reads (or writes) an environment variable."""

    name: str
    where: str
    line: int
    function: str
    access: str
    summary: str


def _is_environ(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _walk(node: ast.AST, scope: _Scope = ()) -> Iterator[tuple[ast.AST, _Scope]]:
    yield node, scope
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        scope = (*scope, node)
    for child in ast.iter_child_nodes(node):
        yield from _walk(child, scope)


def _env_access(node: ast.AST) -> tuple[ast.expr, str] | None:
    """``(key expression, access kind)`` if ``node`` touches ``os.environ`` / ``getenv``."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _ENV_METHODS
            and _is_environ(func.value)
        ):
            return node.args[0], f"environ.{func.attr}()"
        if (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            return node.args[0], "getenv()"
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        kind = "environ[...]" if isinstance(node.ctx, ast.Load) else "environ[...] (write)"
        return node.slice, kind
    if (
        isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        and any(_is_environ(c) for c in node.comparators)
    ):
        return node.left, "in environ"
    return None


def env_reads(text: str, where: str) -> list[EnvRead]:
    """Every environment variable access in one source file."""
    found = []
    for node, scope in _walk(ast.parse(text)):
        hit = _env_access(node)
        if hit is None:
            continue
        key, access = hit
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = key.value
        else:
            name = f"<dynamic: {ast.get_source_segment(text, key)}>"
        funcs = [s for s in scope if not isinstance(s, ast.ClassDef)]
        summary = (ast.get_docstring(funcs[-1]) or "").strip().split("\n")[0] if funcs else ""
        function = ".".join(s.name for s in scope) or "<module>"
        line = getattr(node, "lineno", 0)
        found.append(EnvRead(name, where, line, function, access, summary))
    return found


def section_environment() -> list[str]:
    files = [*_package_files(), *sorted((ROOT / "scripts").glob("*.py"), key=_rel)]
    reads = sorted(r for path in files for r in env_reads(_source(path)[0], _rel(path)))
    rows = [
        [code(r.name), code(f"{r.where}:{r.line}"), code(r.function), r.access, r.summary]
        for r in reads
    ]
    return [
        "## 11. Environment variables",
        "",
        "Every `os.environ` / `os.getenv` access in `src/cca` and `scripts/`, found with `ast`.",
        "The summary is the first docstring line of the enclosing function.",
        "",
        *table(["Variable", "Where", "Function", "Access", "Function summary"], rows),
    ]


# ---------------------------------------------------------------- 12. supply chain
def section_security() -> list[str]:
    audit = _load_script("audit_deps.py")
    accepted, review_by = audit.ACCEPTED, audit.REVIEW_BY
    if not isinstance(review_by, date) or not isinstance(accepted, Mapping):
        raise TypeError("scripts/audit_deps.py: REVIEW_BY must be a date, ACCEPTED a mapping")
    rows = [[code(str(k)), str(accepted[k])] for k in sorted(accepted)]
    _, tree = _source(ROOT / "scripts" / "audit_deps.py")
    return [
        "## 12. Security and supply chain",
        "",
        "Accepted advisories of the dependency audit (`scripts/audit_deps.py`). The audit fails",
        f"after `REVIEW_BY` = {code(review_by.isoformat())}, forcing a re-assessment.",
        "",
        *table(["Advisory", "Why it is accepted"], rows),
        "",
        "Audit method (docstring of `scripts/audit_deps.py`):",
        "",
        *block(ast.get_docstring(tree) or ""),
        "",
        "Pinned binaries and weights: section 2.",
    ]


# ---------------------------------------------------------------- document
HEADER = """\
# CCA technical reference

<!-- GENERATED FILE: do not edit by hand. Source: scripts/gen_reference.py -->

This file is generated by `scripts/gen_reference.py`. Every value, name, type, default and
description in it is read when the file is generated, from: the imported `cca` package and its
source in `src/cca`, `pyproject.toml`, `engines/stockfish.lock.json`,
`scripts/fetch_stockfish.py`, `scripts/audit_deps.py` and the other `scripts/*.py`. Only the
short sentences that introduce each section are written in the generator.
**Do not edit this file**: change the code, then regenerate.

- Regenerate: `uv run python scripts/gen_reference.py`
- Check: `uv run python scripts/gen_reference.py --check` exits 1 and prints a unified diff when
  this file is stale; `tests/unit/test_reference.py` runs the same check.

Contents: 1 Package · 2 Pinned artefacts · 3 Command-line interface · 4 UCI options ·
5 Configuration · 6 Data model · 7 Other dataclasses · 8 Shipped personas · 9 Model equations ·
10 Module-level numeric constants · 11 Environment variables · 12 Security and supply chain
"""


def render() -> str:
    """The whole reference document (deterministic, LF, no trailing whitespace)."""
    _check_source_tree()
    parts = [
        HEADER.rstrip("\n").splitlines(),
        section_package(),
        section_artefacts(),
        section_cli(),
        section_uci(),
        section_dataclasses(),
        section_personas(),
        section_equations(),
        section_constants(),
        section_environment(),
        section_security(),
    ]
    lines = [line.rstrip() for part in parts for line in [*part, ""]]
    return "\n".join(lines).rstrip("\n") + "\n"


def _display(path: Path) -> str:
    try:
        return _rel(path)
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate docs/reference.md from the code.")
    ap.add_argument("--check", action="store_true", help="exit 1 with a diff if the file is stale")
    ap.add_argument("--output", type=Path, default=OUTPUT, help="file to write or check")
    args = ap.parse_args(argv)
    target: Path = args.output
    text = render()
    if args.check:
        current = target.read_bytes().decode("utf-8") if target.is_file() else ""
        if current == text:
            print(f"{_display(target)} is up to date")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"{_display(target)} (on disk)",
            tofile=f"{_display(target)} (generated)",
        )
        sys.stdout.writelines(d if d.endswith("\n") else d + "\n" for d in diff)
        print(
            f"{_display(target)} is stale: run `uv run python scripts/gen_reference.py`",
            file=sys.stderr,
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    print(f"wrote {_display(target)}")
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
