"""Command-line interface.

Exit codes: 0 on success — including when packages are held; a hold is a
normal outcome, not an error. Non-zero only on actual failure: AUR
unreachable, malformed config, helper not found, invalid arguments (2,
from argparse). `cellar upgrade` propagates the helper's exit code.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shlex
import subprocess
import sys
import time

from . import __version__, aur, config as config_mod, core, holds, pacman, state
from .core import Report, Status

EXIT_OK = 0
EXIT_FAILURE = 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        return _fail(str(exc))
    except aur.AurError as exc:
        return _fail(
            f"{exc}\n"
            "The AUR could not be consulted, so every AUR upgrade is held "
            "(fail safe). Nothing was upgraded."
        )
    except pacman.PacmanError as exc:
        return _fail(str(exc))
    except holds.HoldsFileError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="config file to use")
    common.add_argument(
        "--days", type=int, metavar="N", help="aging threshold in days (overrides config)"
    )
    common.add_argument(
        "--helper", choices=pacman.HELPERS, help="AUR helper to use (overrides config)"
    )

    parser = argparse.ArgumentParser(
        prog="cellar",
        description="Enforce an aging period on AUR package upgrades.",
    )
    parser.add_argument(
        "--version", action="version", version=f"cellar {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser(
        "check",
        parents=[common],
        help="show installed AUR packages and their aging status (read-only)",
    )
    p.add_argument(
        "--all", action="store_true", help="also show up-to-date and non-AUR packages"
    )
    p.set_defaults(func=cmd_check)

    p = sub.add_parser(
        "upgrade",
        parents=[common],
        help="run the AUR helper with aging packages held via --ignore",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the helper command instead of running it",
    )
    p.add_argument(
        "--force",
        action="append",
        metavar="PKG",
        help="bypass the hold on PKG for this run only (repeatable)",
    )
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser(
        "status", parents=[common], help="detailed aging status for one package"
    )
    p.add_argument("package")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "sync-holds",
        parents=[common],
        help="write current holds to a pacman ignore file (opt-in)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the file content instead of writing it",
    )
    p.set_defaults(func=cmd_sync_holds)

    p = sub.add_parser("config", parents=[common], help="configuration helpers")
    p.add_argument("action", choices=("path",), help="'path': print the config file in use")
    p.set_defaults(func=cmd_config)

    return parser


# ---------------------------------------------------------------- commands


def cmd_check(args) -> int:
    now = time.time()
    cfg = _load_config(args)
    reports = _gather(cfg, now)
    state.update(reports, now)

    missing = [r for r in reports if r.status is Status.NOT_IN_AUR]
    if args.all:
        shown = reports
    else:
        shown = [
            r
            for r in reports
            if r.status in (Status.AGING, Status.READY)
            or (r.status is Status.IGNORED and r.outdated)
        ]

    if not reports:
        print("No foreign packages installed.")
        return EXIT_OK
    if shown:
        _print_table(shown, now)
    else:
        print("All AUR packages are up-to-date.")

    _print_summary(reports)
    if missing and not args.all:
        _warn(
            f"{len(missing)} foreign package(s) not found on the AUR, skipped: "
            + ", ".join(r.name for r in missing)
        )
    _warn_stale_holds(reports, cfg, now)
    _warn_stale_holds_file(cfg, now)
    return EXIT_OK


def cmd_upgrade(args) -> int:
    now = time.time()
    cfg = _load_config(args)
    # Resolve the helper before touching the network so a missing helper
    # fails fast and clearly.
    helper = pacman.find_helper(cfg.helper)
    reports = _gather(cfg, now)
    state.update(reports, now)

    forced = set(args.force or [])
    aging = {r.name: r for r in reports if r.status is Status.AGING}
    for name in sorted(forced - set(aging)):
        _warn(f"--force {name}: package is not currently held; nothing to bypass")
    held = [r for r in aging.values() if r.name not in forced]
    bypassed = sorted(forced & set(aging))

    if held:
        print(f"Held (aging, {len(held)}):")
        for r in held:
            remaining = r.remaining_days(now)
            left = f"{remaining:.1f}d remaining" if remaining is not None else "no timestamp"
            stale = "  [STALE — see `cellar check`]" if r.is_stale(now, cfg.stale_days) else ""
            print(f"  {r.name} {r.installed} -> {r.latest}  ({left}){stale}")
    if bypassed:
        print("Bypassed this run (--force): " + ", ".join(bypassed))

    _warn_stale_holds(reports, cfg, now)
    _warn_stale_holds_file(cfg, now)

    cmd = [helper, "-Syu"] + [f"--ignore={r.name}" for r in held]
    if args.dry_run:
        print("dry run: " + shlex.join(cmd))
        return EXIT_OK
    print("Running: " + shlex.join(cmd))
    return subprocess.run(cmd).returncode


def cmd_status(args) -> int:
    now = time.time()
    cfg = _load_config(args)
    name = args.package
    installed = pacman.foreign_packages()
    if name not in installed:
        return _fail(
            f"{name} is not an installed foreign package (see `pacman -Qm`)"
        )
    ignored = pacman.ignored_packages()
    info = aur.fetch_info([name])
    report = core.evaluate(
        {name: installed[name]}, info, cfg, ignored, now, pacman.vercmp
    )[0]
    state.annotate([report])

    def row(label: str, value: str) -> None:
        print(f"{label:<18} {value}")

    row("Package:", report.name)
    row("Installed:", report.installed)
    row("AUR latest:", report.latest or "—")
    if report.last_modified is not None:
        updated = datetime.datetime.fromtimestamp(
            report.last_modified, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        age = report.age_days(now)
        row("Last AUR update:", f"{updated} ({age:.1f}d ago)")
    if report.status in (Status.AGING, Status.READY):
        source = (
            "per-package override"
            if cfg.packages.get(name) and cfg.packages[name].days is not None
            else "default"
        )
        row("Aging threshold:", f"{report.required_days}d ({source})")
    row("Status:", _status_text(report, now))
    if report.status is Status.AGING:
        row("Remaining:", f"{report.remaining_days(now):.1f}d")
        held = report.held_days(now)
        if held is not None:
            row("Held for:", f"{held:.1f}d")
        if report.is_stale(now, cfg.stale_days):
            _warn(
                f"{name} has been held for more than {cfg.stale_days} days. "
                "Review the AUR diff manually; if it looks clean, "
                f"`cellar upgrade --force {name}`."
            )
    if report.trusted:
        row("Trust:", "always-trusted in config (aging bypassed)")
    if report.note:
        row("Note:", report.note)
    if report.status is not Status.NOT_IN_AUR:
        row("AUR page:", aur.AUR_PACKAGE_URL.format(name=name))
    return EXIT_OK


def cmd_sync_holds(args) -> int:
    now = time.time()
    cfg = _load_config(args)
    reports = _gather(cfg, now)
    state.update(reports, now)
    held = [r.name for r in reports if r.status is Status.AGING]

    if args.dry_run:
        print(f"dry run: would write to {cfg.holds_file}:\n")
        print(holds.render(held, now, cfg.holds_file), end="")
        return EXIT_OK

    holds.write(cfg.holds_file, held, now)
    if held:
        print(f"Wrote {len(held)} hold(s) to {cfg.holds_file}: " + ", ".join(sorted(held)))
    else:
        print(f"No packages currently held; wrote an empty holds file to {cfg.holds_file}.")
    _print_include_hint(cfg)
    return EXIT_OK


def cmd_config(args) -> int:
    # args.action can only be "path" (argparse enforces choices).
    path = config_mod.resolve_path(args.config)
    print(path if path is not None else "(built-in defaults)")
    return EXIT_OK


# ----------------------------------------------------------------- helpers


def _load_config(args) -> config_mod.Config:
    cfg = config_mod.load(args.config)
    if args.days is not None:
        if args.days < 0:
            raise config_mod.ConfigError("--days must be a non-negative integer")
        cfg.days = args.days
    if args.helper:
        cfg.helper = args.helper
    return cfg


def _gather(cfg: config_mod.Config, now: float) -> list[Report]:
    installed = pacman.foreign_packages()
    if not installed:
        return []
    ignored = pacman.ignored_packages()
    info = aur.fetch_info(sorted(installed))
    return core.evaluate(installed, info, cfg, ignored, now, pacman.vercmp)


def _print_table(reports: list[Report], now: float) -> None:
    headers = ("PACKAGE", "INSTALLED", "LATEST", "UPDATED", "STATUS")
    rows = []
    for r in reports:
        age = r.age_days(now)
        rows.append(
            (
                r.name,
                r.installed,
                r.latest or "—",
                f"{age:.1f}d" if age is not None else "—",
                _status_text(r, now),
            )
        )
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r, row in zip(reports, rows):
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        print(_colorize(r, line.rstrip()))


def _status_text(report: Report, now: float) -> str:
    if report.status is Status.AGING:
        remaining = report.remaining_days(now)
        if remaining is None:
            return "aging (no AUR timestamp — held)"
        return f"aging ({remaining:.1f}d left)"
    if report.status is Status.READY:
        return "ready (trusted)" if report.trusted else "ready"
    if report.status is Status.IGNORED:
        return (
            "ignored (pacman IgnorePkg; update available)"
            if report.outdated
            else "ignored (pacman IgnorePkg)"
        )
    if report.status is Status.NOT_IN_AUR:
        return "not in AUR"
    return "up-to-date"


def _print_summary(reports: list[Report]) -> None:
    counts = {status: 0 for status in Status}
    for r in reports:
        counts[r.status] += 1
    parts = [
        f"{counts[status]} {label}"
        for status, label in (
            (Status.AGING, "aging"),
            (Status.READY, "ready"),
            (Status.UP_TO_DATE, "up-to-date"),
            (Status.IGNORED, "ignored"),
            (Status.NOT_IN_AUR, "not in AUR"),
        )
        if counts[status]
    ]
    print("\n" + ", ".join(parts))


def _warn_stale_holds(reports: list[Report], cfg: config_mod.Config, now: float) -> None:
    for r in reports:
        if r.is_stale(now, cfg.stale_days):
            _warn(
                f"{r.name} has been held for {r.held_days(now):.0f} days — its aging "
                "clock keeps resetting (fast-moving package). Review the AUR diff "
                f"manually; if it looks clean: `cellar upgrade --force {r.name}`."
            )


def _warn_stale_holds_file(cfg: config_mod.Config, now: float) -> None:
    message = holds.staleness_warning(cfg.holds_file, now, cfg.holds_max_age_days)
    if message:
        _warn(message)


def _print_include_hint(cfg: config_mod.Config) -> None:
    include_line = f"Include = {cfg.holds_file}"
    try:
        if str(cfg.holds_file) in open("/etc/pacman.conf").read():
            return
    except OSError:
        pass
    print(
        "Note: /etc/pacman.conf does not appear to include this file yet.\n"
        f"Add to its [options] section:\n  {include_line}"
    )


def _use_color(stream) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def _colorize(report: Report, line: str) -> str:
    if not _use_color(sys.stdout):
        return line
    codes = {Status.AGING: "33", Status.READY: "32"}  # yellow / green
    code = codes.get(report.status)
    return f"\033[{code}m{line}\033[0m" if code else line


def _warn(message: str) -> None:
    prefix = "\033[33mwarning:\033[0m" if _use_color(sys.stderr) else "warning:"
    print(f"{prefix} {message}", file=sys.stderr)


def _fail(message: str) -> int:
    prefix = "\033[31merror:\033[0m" if _use_color(sys.stderr) else "error:"
    print(f"{prefix} {message}", file=sys.stderr)
    return EXIT_FAILURE
