"""Layered configuration resolution for the stock quote service.

Resolution order (highest precedence wins):

1. Environment variables (``STOCK_*``)
2. ``config.toml`` (path overridable via ``STOCK_CONFIG_PATH``)
3. Built-in defaults below

Missing keys degrade to the default value; the resolution of every key is
recorded so the /status page can show its source (env/toml/default) and flag
defaulted keys. Invalid values raise ``ConfigError`` at startup, naming the
offending key and the expected type.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("stock_service.config")

LOG_PREFIX = "config"


class ConfigError(Exception):
    """Raised at startup when a configuration value cannot be parsed."""


@dataclass(frozen=True)
class ConfigSpec:
    key: str
    expected: str
    default: Any
    description: str


@dataclass(frozen=True)
class ResolvedKey:
    key: str
    value: Any
    source: str  # "env" | "toml" | "default"
    expected: str
    description: str

    @property
    def defaulted(self) -> bool:
        return self.source == "default"


@dataclass
class Settings:
    values: dict[str, Any]
    resolved: dict[str, ResolvedKey]
    config_path: Path | None
    warnings: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        return self.values[key]

    def display(self, key: str) -> str:
        value = self.values[key]
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)


SPECS: dict[str, ConfigSpec] = {
    "upstream.provider": ConfigSpec(
        "upstream.provider",
        "one of: yfinance, fake",
        "yfinance",
        "Quote data provider used for upstream fetches.",
    ),
    "upstream.request_timeout": ConfigSpec(
        "upstream.request_timeout",
        "positive number (seconds)",
        8.0,
        "Timeout for a single upstream quote fetch.",
    ),
    "cache.ttl_seconds": ConfigSpec(
        "cache.ttl_seconds",
        "positive number (seconds)",
        60.0,
        "Time-to-live of cached quote entries.",
    ),
    "cache.dir": ConfigSpec(
        "cache.dir",
        "filesystem path string",
        "cache",
        "Directory for on-disk cache entries.",
    ),
    "watchlist": ConfigSpec(
        "watchlist",
        "non-empty comma-separated list of symbols",
        ["AAPL", "MSFT", "GOOG"],
        "Default symbols prefetched by POST /prefetch.",
    ),
    "prefetch.lock_timeout": ConfigSpec(
        "prefetch.lock_timeout",
        "non-negative number (seconds)",
        15.0,
        "Max time to wait on an in-flight fetch for the same key.",
    ),
}

ENV_PREFIX = "STOCK_"
SECTION_KEYS = {
    "upstream": {"provider", "request_timeout"},
    "cache": {"ttl_seconds", "dir"},
    "prefetch": {"lock_timeout"},
}
KNOWN_SECTIONS = set(SECTION_KEYS) | {"watchlist"}


def env_name(key: str) -> str:
    return ENV_PREFIX + key.replace(".", "_").upper()


def _coerce_float(raw: Any, key: str, expected: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"invalid configuration for {key}: expected {expected}, got {raw!r}"
        ) from None


def _validate(key: str, value: Any) -> Any:
    spec = SPECS[key]
    if key == "upstream.provider":
        if value not in ("yfinance", "fake"):
            raise ConfigError(
                f"invalid configuration for {key}: expected {spec.expected}, got {value!r}"
            )
        return value
    if key in ("upstream.request_timeout", "cache.ttl_seconds", "prefetch.lock_timeout"):
        number = _coerce_float(value, key, spec.expected)
        valid = number >= 0.0 if key == "prefetch.lock_timeout" else number > 0.0
        if not valid:
            raise ConfigError(
                f"invalid configuration for {key}: expected {spec.expected}, got {value!r}"
            )
        return number
    if key == "cache.dir":
        text = str(value).strip()
        if not text:
            raise ConfigError(
                f"invalid configuration for {key}: expected {spec.expected}, got {value!r}"
            )
        return text
    if key == "watchlist":
        if isinstance(value, str):
            items = [part.strip().upper() for part in value.split(",") if part.strip()]
        elif isinstance(value, list):
            items = [str(part).strip().upper() for part in value if str(part).strip()]
        else:
            raise ConfigError(
                f"invalid configuration for {key}: expected {spec.expected}, got {value!r}"
            )
        if not items:
            raise ConfigError(
                f"invalid configuration for {key}: expected {spec.expected}, got {value!r}"
            )
        return items
    return value


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _toml_value(table: dict[str, Any], key: str) -> Any:
    section, _, leaf = key.partition(".")
    if not leaf:
        return table.get(key, _MISSING)
    section_table = table.get(section)
    if not isinstance(section_table, dict) or leaf not in section_table:
        return _MISSING
    return section_table[leaf]


def _load_toml(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not path.exists():
        warnings.append(
            f"{LOG_PREFIX}: config file not found at {path}; using defaults/env only"
        )
        logger.warning("config file not found at %s; using defaults/env only", path)
        return {}, warnings
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to parse config file {path}: {exc}") from exc

    for name, content in data.items():
        if name not in KNOWN_SECTIONS:
            logger.warning("config: unknown key %r in %s ignored", name, path)
            warnings.append(
                f"{LOG_PREFIX}: unknown configuration key {name!r} in {path} ignored"
            )
        elif isinstance(content, dict):
            for leaf in content:
                if leaf not in SECTION_KEYS.get(name, set()):
                    full = f"{name}.{leaf}"
                    logger.warning("config: unknown key %r in %s ignored", full, path)
                    warnings.append(
                        f"{LOG_PREFIX}: unknown configuration key {full!r} in {path} ignored"
                    )
    return data, warnings


def load_settings(config_path: str | os.PathLike[str] | None = None,
                  environ: dict[str, str] | None = None) -> Settings:
    """Resolve settings with precedence env > toml > defaults.

    Raises ``ConfigError`` on invalid values or an unreadable config file.
    """
    env = os.environ if environ is None else environ
    path_arg = (
        config_path if config_path is not None else env.get(env_name("config_path"))
    )
    path = Path(path_arg) if path_arg else Path("config.toml")

    toml_data, warnings = _load_toml(path)
    resolved: dict[str, ResolvedKey] = {}
    values: dict[str, Any] = {}

    for key, spec in SPECS.items():
        env_var = env_name(key)
        if env_var in env:
            raw: Any = env[env_var]
            source = "env"
        else:
            candidate = _toml_value(toml_data, key)
            if candidate is not _MISSING:
                raw = candidate
                source = "toml"
            else:
                raw = spec.default
                source = "default"

        value = _validate(key, raw)

        resolved[key] = ResolvedKey(
            key=key,
            value=value,
            source=source,
            expected=spec.expected,
            description=spec.description,
        )
        values[key] = value
        if source == "default":
            message = (
                f"{LOG_PREFIX}: key {key} not set in env or {path.name}; "
                f"falling back to default {value!r}"
            )
            warnings.append(message)
            logger.warning(message)
        else:
            logger.info("config %s = %r (source=%s)", key, value, source)

    return Settings(
        values=values, resolved=resolved, config_path=path, warnings=warnings
    )
