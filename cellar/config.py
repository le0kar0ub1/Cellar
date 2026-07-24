"""Configuration loading.

Resolution order, highest precedence first:

  1. --config <path>          (error if the file does not exist)
  2. $CELLAR_CONFIG           (error if set but the file does not exist)
  3. $XDG_CONFIG_HOME/cellar/config.toml
     (~/.config/cellar/config.toml when XDG_CONFIG_HOME is unset)
  4. /etc/cellar/config.toml  (system-wide defaults)
  5. built-in defaults

No other locations are probed. `cellar config path` prints the file in use.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DAYS = 7
DEFAULT_STALE_DAYS = 30
DEFAULT_HOLDS_FILE = "/etc/pacman.d/cellar-holds.conf"
DEFAULT_HOLDS_MAX_AGE_DAYS = 7
SYSTEM_CONFIG = Path("/etc/cellar/config.toml")

_TOP_LEVEL_KEYS = {
    "days",
    "helper",
    "stale_days",
    "holds_file",
    "holds_max_age_days",
    "packages",
}
_PACKAGE_KEYS = {"trust", "days"}


class ConfigError(Exception):
    """The configuration is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class PackageOverride:
    trust: bool = False
    days: int | None = None


@dataclass
class Config:
    days: int = DEFAULT_DAYS
    helper: str | None = None  # None = auto-detect (paru, then yay)
    stale_days: int = DEFAULT_STALE_DAYS
    holds_file: Path = Path(DEFAULT_HOLDS_FILE)
    holds_max_age_days: int = DEFAULT_HOLDS_MAX_AGE_DAYS
    packages: dict[str, PackageOverride] = field(default_factory=dict)
    path: Path | None = None  # file the values came from; None = built-in defaults

    def required_days(self, name: str) -> int:
        override = self.packages.get(name)
        if override is not None and override.days is not None:
            return override.days
        return self.days

    def is_trusted(self, name: str) -> bool:
        override = self.packages.get(name)
        return override is not None and override.trust


def resolve_path(cli_path: str | None) -> Path | None:
    """Return the config file to use, or None for built-in defaults."""
    if cli_path:
        path = Path(cli_path)
        if not path.is_file():
            raise ConfigError(f"--config: no such file: {path}")
        return path
    env = os.environ.get("CELLAR_CONFIG")
    if env:
        path = Path(env)
        if not path.is_file():
            raise ConfigError(f"$CELLAR_CONFIG points to a missing file: {path}")
        return path
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(xdg) / "cellar" / "config.toml"
    if path.is_file():
        return path
    if SYSTEM_CONFIG.is_file():
        return SYSTEM_CONFIG
    return None


def load(cli_path: str | None = None) -> Config:
    path = resolve_path(cli_path)
    if path is None:
        return Config()
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return _parse(raw, path)


def _parse(raw: dict, path: Path) -> Config:
    # Reject unknown keys: a typo in a security gate's config should be
    # loud, not silently ignored.
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")

    config = Config(path=path)
    config.days = _non_negative_int(raw, "days", config.days, path)
    config.stale_days = _non_negative_int(raw, "stale_days", config.stale_days, path)
    config.holds_max_age_days = _non_negative_int(
        raw, "holds_max_age_days", config.holds_max_age_days, path
    )

    helper = raw.get("helper")
    if helper is not None:
        if helper not in ("paru", "yay"):
            raise ConfigError(f"{path}: helper must be 'paru' or 'yay', got {helper!r}")
        config.helper = helper

    holds_file = raw.get("holds_file")
    if holds_file is not None:
        if not isinstance(holds_file, str) or not holds_file:
            raise ConfigError(f"{path}: holds_file must be a non-empty string")
        config.holds_file = Path(holds_file)

    packages = raw.get("packages", {})
    if not isinstance(packages, dict):
        raise ConfigError(f"{path}: [packages] must be a table")
    for name, entry in packages.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: [packages.{name}] must be a table")
        unknown = set(entry) - _PACKAGE_KEYS
        if unknown:
            raise ConfigError(
                f"{path}: [packages.{name}]: unknown key(s): {', '.join(sorted(unknown))}"
            )
        trust = entry.get("trust", False)
        if not isinstance(trust, bool):
            raise ConfigError(f"{path}: [packages.{name}].trust must be a boolean")
        days = entry.get("days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0):
            raise ConfigError(f"{path}: [packages.{name}].days must be a non-negative integer")
        config.packages[name] = PackageOverride(trust=trust, days=days)
    return config


def _non_negative_int(raw: dict, key: str, default: int, path: Path) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{path}: {key} must be a non-negative integer")
    return value
