"""Thin wrappers around pacman's command-line tools."""

from __future__ import annotations

import shutil
import subprocess

HELPERS = ("paru", "yay")


class PacmanError(Exception):
    """A pacman tool is missing or failed."""


def foreign_packages() -> dict[str, str]:
    """Installed packages not found in any sync repo (pacman -Qm), name -> version."""
    try:
        proc = subprocess.run(
            ["pacman", "-Qm"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise PacmanError("pacman not found; cellar only works on Arch Linux") from exc
    # pacman -Qm exits 1 with empty stdout when there are no foreign
    # packages; it may also emit warnings on stderr while succeeding, so
    # only stderr *with* a failing exit code and no results is an error.
    if proc.returncode not in (0, 1) or (
        proc.returncode == 1 and not proc.stdout and proc.stderr.strip()
    ):
        raise PacmanError(f"pacman -Qm failed: {proc.stderr.strip() or proc.returncode}")
    packages: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, version = line.partition(" ")
        if name and version:
            packages[name] = version
    return packages


def ignored_packages() -> frozenset[str]:
    """The user's effective IgnorePkg set.

    pacman-conf resolves Include directives, so manual ignores are seen
    wherever they live. Best-effort: an empty set on failure only means
    cellar may list a package the user already ignores — pacman itself
    still honors the ignore during the actual upgrade.
    """
    try:
        out = _run(["pacman-conf", "IgnorePkg"])
    except PacmanError:
        return frozenset()
    return frozenset(out.split())


def vercmp(a: str, b: str) -> int:
    """Compare two package versions with pacman's vercmp: <0, 0, >0."""
    out = _run(["vercmp", a, b])
    try:
        return int(out.strip())
    except ValueError as exc:
        raise PacmanError(f"unexpected vercmp output: {out!r}") from exc


def find_helper(preferred: str | None = None) -> str:
    """Locate the AUR helper binary; paru is preferred, yay the fallback."""
    for name in (preferred,) if preferred else HELPERS:
        path = shutil.which(name)
        if path:
            return path
    wanted = preferred or " or ".join(HELPERS)
    raise PacmanError(
        f"AUR helper not found ({wanted}); install one or set `helper` in the config"
    )


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise PacmanError(f"required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else f"exit code {exc.returncode}"
        raise PacmanError(f"{' '.join(cmd)} failed: {detail}") from exc
    return proc.stdout
