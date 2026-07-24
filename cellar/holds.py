"""The opt-in pacman ignore file written by `cellar sync-holds`.

Rules:
  * A dedicated file under /etc/pacman.d/, included from pacman.conf —
    never pacman.conf itself, never the file holding manual ignores.
  * Refuse to touch an existing file that cellar did not write.
  * The file carries its generation time; consumers warn (and tell the
    user to regenerate or remove it) once it is older than the freshness
    window, because pacman will keep honoring stale holds forever.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from .core import SECONDS_PER_DAY

MARKER = "# Managed by cellar. Do not edit"
GENERATED_PREFIX = "# Generated-epoch:"


class HoldsFileError(Exception):
    """The holds file cannot be safely written or trusted."""


def render(held: list[str], now: float, path: Path) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(now))
    lines = [
        f"{MARKER} — regenerate with `cellar sync-holds`, delete to opt out.",
        f"{GENERATED_PREFIX} {int(now)} ({stamp})",
        "#",
        "# Include this file from the [options] section of /etc/pacman.conf:",
        f"#   Include = {path}",
        "#",
    ]
    if held:
        lines.extend(f"IgnorePkg = {name}" for name in sorted(held))
    else:
        lines.append("# No packages are currently held.")
    return "\n".join(lines) + "\n"


def write(path: Path, held: list[str], now: float) -> None:
    if path.exists():
        try:
            existing = path.read_text(errors="replace")
        except OSError as exc:
            raise HoldsFileError(f"cannot read {path}: {exc}") from exc
        if MARKER not in existing:
            raise HoldsFileError(
                f"{path} exists but was not written by cellar; refusing to overwrite it"
            )
    content = render(held, now, path)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
        tmp_path = None
    except PermissionError as exc:
        raise HoldsFileError(
            f"cannot write {path} (root required — try `sudo cellar sync-holds`)"
        ) from exc
    except OSError as exc:
        raise HoldsFileError(f"cannot write {path}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def generated_at(path: Path) -> float | None:
    """Epoch timestamp recorded in the file header, or None."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(GENERATED_PREFIX):
            try:
                return float(line[len(GENERATED_PREFIX) :].split()[0])
            except (ValueError, IndexError):
                return None
    return None


def staleness_warning(path: Path, now: float, max_age_days: int) -> str | None:
    """A message if the holds file exists but is past its freshness window."""
    if not path.exists():
        return None
    timestamp = generated_at(path)
    if timestamp is None:
        return (
            f"holds file {path} has no readable timestamp; "
            "regenerate it with `cellar sync-holds` or delete it"
        )
    age_days = (now - timestamp) / SECONDS_PER_DAY
    if age_days > max_age_days:
        return (
            f"holds file {path} is {age_days:.0f} days old (freshness window: "
            f"{max_age_days}d); pacman is still honoring these stale holds. "
            "Re-run `sudo cellar sync-holds` or delete the file."
        )
    return None
