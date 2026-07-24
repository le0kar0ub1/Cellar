"""The gate itself: version gate first, then age gate.

Order matters. If the installed version already matches the AUR's latest,
the package is up-to-date and LastModified is irrelevant — a cosmetic
PKGBUILD edit must not make a current package look "aging". Only when the
versions differ does the age of the latest AUR update decide whether the
upgrade is released or held. The clock resets on every new version: cellar
holds the installed version entirely until the latest has aged past the
threshold (see the README for why intermediate versions are not stepped
through).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable

SECONDS_PER_DAY = 86400


class Status(enum.Enum):
    UP_TO_DATE = "up-to-date"
    AGING = "aging"
    READY = "ready"
    NOT_IN_AUR = "not-in-aur"
    IGNORED = "ignored"


@dataclass
class Report:
    name: str
    installed: str
    status: Status
    latest: str | None = None
    last_modified: int | None = None  # epoch of the latest AUR metadata push
    required_days: int | None = None
    trusted: bool = False
    outdated: bool = False
    held_since: float | None = None  # epoch when this hold was first observed
    note: str = ""

    def age_days(self, now: float) -> float | None:
        """Age of the latest AUR update, in days."""
        if self.last_modified is None:
            return None
        return (now - self.last_modified) / SECONDS_PER_DAY

    def remaining_days(self, now: float) -> float | None:
        """Days until the aging threshold is met (0 if already met)."""
        if self.required_days is None or self.last_modified is None:
            return None
        remaining = self.required_days - (now - self.last_modified) / SECONDS_PER_DAY
        return max(0.0, remaining)

    def held_days(self, now: float) -> float | None:
        if self.held_since is None:
            return None
        return (now - self.held_since) / SECONDS_PER_DAY

    def is_stale(self, now: float, stale_days: int) -> bool:
        """Held so long the user should review the diff and decide by hand."""
        held = self.held_days(now)
        return self.status is Status.AGING and held is not None and held > stale_days


def evaluate(
    installed: dict[str, str],
    aur_info: dict[str, dict],
    config,
    ignored: frozenset[str],
    now: float,
    vercmp: Callable[[str, str], int],
) -> list[Report]:
    """Produce one Report per installed foreign package, sorted by name."""
    reports = []
    for name in sorted(installed):
        current = installed[name]
        info = aur_info.get(name)
        latest = info.get("Version") if info else None
        last_modified = info.get("LastModified") if info else None

        # The user's own pacman IgnorePkg always wins; cellar never gates
        # (or upgrades) a package the user explicitly pinned.
        if name in ignored:
            outdated = latest is not None and vercmp(current, latest) < 0
            reports.append(
                Report(
                    name,
                    current,
                    Status.IGNORED,
                    latest=latest,
                    last_modified=last_modified,
                    outdated=outdated,
                    note="in pacman IgnorePkg",
                )
            )
            continue

        if info is None:
            reports.append(
                Report(
                    name,
                    current,
                    Status.NOT_IN_AUR,
                    note="not found on the AUR (locally built?)",
                )
            )
            continue

        # 1. Version gate. Installed version equal to (or newer than) the
        #    AUR's latest → up-to-date; no aging applies.
        if vercmp(current, latest) >= 0:
            reports.append(
                Report(
                    name,
                    current,
                    Status.UP_TO_DATE,
                    latest=latest,
                    last_modified=last_modified,
                )
            )
            continue

        # 2. Age gate. Versions differ; the latest update must have aged.
        report = Report(
            name,
            current,
            Status.AGING,
            latest=latest,
            last_modified=last_modified,
            required_days=config.required_days(name),
            trusted=config.is_trusted(name),
            outdated=True,
        )
        if report.trusted:
            report.status = Status.READY
            report.note = "trusted in config; aging bypassed"
        elif last_modified is None:
            # Fail safe: no timestamp means we cannot prove the update has
            # aged, so it stays held.
            report.note = "AUR reported no LastModified; holding (fail safe)"
        elif now - last_modified >= report.required_days * SECONDS_PER_DAY:
            report.status = Status.READY
        reports.append(report)
    return reports
