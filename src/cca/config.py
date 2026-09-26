"""Load personas and agent configurations from TOML (strict: unknown keys are errors)."""

from __future__ import annotations

import dataclasses
import math
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

from cca.agent import AgentConfig
from cca.chaos.lorenz import LorenzParams
from cca.neuro.controller import Persona
from cca.neuro.neuromodulation import NeuroParams
from cca.timing.think_time import ThinkTimeParams

T = TypeVar("T")

_SECTIONS: dict[str, type[Any]] = {
    "persona": Persona,
    "neuro": NeuroParams,
    "lorenz": LorenzParams,
    "timing": ThinkTimeParams,
}


class ConfigError(ValueError):
    """Invalid configuration file or value."""


def _build(cls: type[T], data: dict[str, Any], where: str) -> T:
    names = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(data) - set(names))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")
    for key, value in data.items():
        default = getattr(cls(), key)
        if dataclasses.is_dataclass(default):
            raise ConfigError(f"{where}.{key}: use the [{key}] section instead")
        if isinstance(default, bool) != isinstance(value, bool):
            raise ConfigError(f"{where}.{key}: expected {type(default).__name__}")
        if isinstance(default, (int, float)) and not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{key}: expected a number")
        if (
            isinstance(default, int)
            and not isinstance(default, bool)
            and not isinstance(value, int)
        ):
            raise ConfigError(f"{where}.{key}: expected an integer")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"{where}.{key}: must be finite")
        if isinstance(default, str) and not isinstance(value, str):
            raise ConfigError(f"{where}.{key}: expected a string")
    return cls(**data)


def list_personas() -> list[str]:
    """Names of the personas shipped with CCA."""
    folder = resources.files("cca.personas")
    return sorted(
        p.name.removesuffix(".toml") for p in folder.iterdir() if p.name.endswith(".toml")
    )


def load_persona(name_or_path: str | Path) -> Persona:
    """Load a shipped persona by name, or a persona TOML file by path."""
    path = Path(name_or_path)
    if path.suffix == ".toml" and path.is_file():
        text = path.read_text(encoding="utf-8")
        where = str(path)
    else:
        name = str(name_or_path)
        if name not in list_personas():
            raise ConfigError(f"unknown persona {name!r}; available: {list_personas()}")
        text = resources.files("cca.personas").joinpath(f"{name}.toml").read_text(encoding="utf-8")
        where = f"persona:{name}"
    return _build(Persona, tomllib.loads(text), where)


def load_agent_config(path: str | Path) -> AgentConfig:
    """Load a full agent configuration (sections: agent, persona, neuro, lorenz, timing).

    ``[persona]`` may contain ``preset = "<name>"`` to start from a shipped persona.
    """
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    unknown = sorted(set(raw) - {"agent", *_SECTIONS})
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {unknown}")
    parts: dict[str, Any] = {}
    for section, cls in _SECTIONS.items():
        data = dict(raw.get(section, {}))
        if section == "persona" and "preset" in data:
            base = dataclasses.asdict(load_persona(data.pop("preset")))
            base.update(data)
            data = base
        parts[section] = _build(cls, data, f"{path}:{section}")
    agent = dict(raw.get("agent", {}))
    top = _build(AgentConfig, agent, f"{path}:agent") if agent else AgentConfig()
    return dataclasses.replace(top, **parts)
