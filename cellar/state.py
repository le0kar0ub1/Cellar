"""Hold tracking, for the stale-hold warning.

A fast-moving package can keep resetting its aging clock and end up held
indefinitely. To detect that, cellar remembers when it first saw each
(package, installed version) pair held, in
$XDG_STATE_HOME/cellar/holds.json (~/.local/state by default).

This is bookkeeping only: it never influences whether a package is held,
so all writes are best-effort and failures are silent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .core import Report, Status


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "cellar" / "holds.json"


def load() -> dict:
    try:
        with open(state_path(), "rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def annotate(reports: list[Report]) -> None:
    """Set held_since on aging reports from stored state, without writing."""
    state = load()
    for report in reports:
        if report.status is not Status.AGING:
            continue
        entry = state.get(report.name)
        if isinstance(entry, dict) and entry.get("installed") == report.installed:
            since = entry.get("since")
            if isinstance(since, (int, float)):
                report.held_since = float(since)


def update(reports: list[Report], now: float) -> None:
    """Record first-held timestamps for current holds, drop the rest.

    The clock is keyed on the installed version: it keeps running while
    the AUR side moves from one too-young version to the next, and resets
    only when the package is actually upgraded (or released).
    """
    state = load()
    new_state: dict[str, dict] = {}
    for report in reports:
        if report.status is not Status.AGING:
            continue
        entry = state.get(report.name)
        if not (
            isinstance(entry, dict)
            and entry.get("installed") == report.installed
            and isinstance(entry.get("since"), (int, float))
        ):
            entry = {"installed": report.installed, "since": now}
        new_state[report.name] = entry
        report.held_since = float(entry["since"])
    _save(new_state)


def _save(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass  # never let bookkeeping break the gate
