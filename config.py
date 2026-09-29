from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("stock_dash.config")

ENV_PREFIX = "STOCK_DASH_"
DEFAULT_TOML_PATH = "config.toml"


class ConfigError(Exception):
    """Raised when a configuration value is invalid. Startup must fail."""


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    value: Any
    source: str  # "env" | "toml" | "default"
    expected_type: str
    raw: Any = None

    @property
    def is_default(self) -> bool:
        return self.source == "default"


@dataclass(frozen=True)
class _Spec:
    key: str
    expected_type: str
    default: Any
    coerce: Callable[[Any], Any]


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise ValueError(f"expected str, got {type(value).__name__}")


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected int, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip())
    raise ValueError(f"expected int, got {type(value).__name__}")


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("expected float, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip())
    raise ValueError(f"expected float, got {type(value).__name__}")


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError(f"expected list[str] or comma-separated str, got {type(value).__name__}")


_SPECS: tuple[_Spec, ...] = (
    _Spec("cache_ttl_seconds", "float", 300.0, _coerce_float),
    _Spec("cache_dir", "str", ".cache/quotes", _coerce_str),
    _Spec("prefetch_symbols", "list[str]", ["AAPL", "MSFT", "GOOG"], _coerce_str_list),
    _Spec("upstream_timeout_seconds", "float", 5.0, _coerce_float),
    _Spec("upstream", "str", "mock", _coerce_str),
    _Spec("log_level", "str", "INFO", _coerce_str),
)


@dataclass
class Config:
    entries: dict[str, ConfigEntry] = field(default_factory=dict)

    def get(self, key: str) -> Any:
        return self.entries[key].value

    def table(self) -> list[ConfigEntry]:
        return [self.entries[spec.key] for spec in _SPECS]


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a TOML table at top level")
    return data


def load_config(
    env: dict[str, str] | None = None,
    toml_path: str | os.PathLike[str] | None = None,
) -> Config:
    """Resolve config as env > config.toml > built-in defaults.

    Invalid values raise ConfigError naming the key and the expected type;
    missing values fall back to defaults and are logged so the /status page
    can flag them.
    """
    env = os.environ if env is None else env
    if toml_path is None:
        toml_path = env.get(f"{ENV_PREFIX}CONFIG", DEFAULT_TOML_PATH)
    toml_values = _read_toml(Path(toml_path))

    entries: dict[str, ConfigEntry] = {}
    for spec in _SPECS:
        env_name = f"{ENV_PREFIX}{spec.key.upper()}"
        raw: Any = None
        source: str
        if env_name in env:
            raw, source = env[env_name], "env"
        elif spec.key in toml_values:
            raw, source = toml_values[spec.key], "toml"
        else:
            raw, source = spec.default, "default"
            logger.warning(
                "config key %r missing (env %s, toml key %r); falling back to default %r",
                spec.key, env_name, spec.key, spec.default,
            )
        try:
            value = spec.coerce(raw)
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f"invalid value for config key {spec.key!r} (source: {source}): "
                f"expected {spec.expected_type}, got {raw!r}"
            ) from exc
        entries[spec.key] = ConfigEntry(
            key=spec.key,
            value=value,
            source=source,
            expected_type=spec.expected_type,
            raw=raw,
        )
    return Config(entries=entries)
