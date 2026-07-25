# Changelog

Notable changes to cellar. Versions are git tags (`vX.Y.Z`); `pkgrel`-only
entries are AUR packaging fixes with no source change.

## [0.1.2] — TBD
- improve usability for packages that are frequently updated
- reduce test run time (check)
- ...

## [0.1.1] — 2026-07-25

### Added

- Usage errors now print the full help (top-level or per-subcommand) instead
  of a bare usage line, with the error message at the end.

### Changed

- `vercmp` is no longer spawned for byte-identical version strings — on a
  typical system, most packages are up-to-date, so `cellar check` now runs
  with almost no subprocess overhead.
- The batching integration test uses 40 long-named packages instead of 300,
  halving the test suite's runtime (it still proves multi-request batching).

### Fixed

- Packaging metadata modernized to PEP 639 (SPDX license string, no license
  classifier); silences setuptools deprecation warnings during build.
- makepkg/AUR build artifacts under `packaging/` are now gitignored.

## [0.1.0-2] — 2026-07-24 (packaging only)

- `build()`/`check()`/`package()` call `/usr/bin/python` explicitly: with a
  pyenv/conda shim first in `PATH`, `python` lacks the makedepends modules
  and would embed a broken script shebang.
- Added `cellar.install` with a `post_remove()` notice listing the runtime
  files pacman does not own (`~/.config/cellar/`, `~/.local/state/cellar/`,
  and `/etc/pacman.d/cellar-holds.conf` + its `Include` line for
  `sync-holds` users — remove the `Include` first).

## [0.1.0] — 2026-07-24

Initial release.

- `cellar check` — read-only table of installed AUR packages: installed vs
  latest AUR version, age of the latest update, status (aging / ready /
  up-to-date / ignored / not in AUR). Shows only actionable packages by
  default; `--all` shows everything.
- `cellar upgrade` — runs the AUR helper (`paru`, fallback `yay`) as
  `-Syu` with aging packages held via `--ignore`; prints what was held and
  the days remaining. Helper exit code is propagated.
- `cellar status <pkg>` — per-package detail (versions, last AUR update,
  threshold, time held, AUR page).
- `cellar sync-holds` — opt-in: writes current holds to a dedicated pacman
  ignore file (`/etc/pacman.d/cellar-holds.conf`) with a managed-by header
  and generation timestamp; refuses to touch files it didn't write; warns
  when the file outlives its freshness window.
- `cellar config path` — prints the config file in use.
- The gate: version gate first (equal or newer installed version is
  up-to-date regardless of `LastModified`), then age gate
  (`now − LastModified` vs the threshold, default 7 days). The clock resets
  on every new version; no version stepping.
- Fail safe: AUR unreachable or malformed responses hold everything and
  exit non-zero; packages absent from the AUR are warned about and skipped;
  pacman `IgnorePkg` entries are honored untouched.
- Stale-hold detection: holds are tracked in
  `~/.local/state/cellar/holds.json`; packages held past `stale_days`
  (default 30) are flagged prominently for manual review.
- Config: TOML at `--config` / `$CELLAR_CONFIG` /
  `$XDG_CONFIG_HOME/cellar/config.toml` / `/etc/cellar/config.toml`;
  global `days`, `helper`, `stale_days`, holds-file settings; per-package
  `trust` and `days` overrides; unknown keys rejected. CLI flags override
  config; `--force` is per-invocation only.
- Exit codes: 0 on success including holds; non-zero only on real failure;
  2 for invalid arguments.
- Tests: unit tests for the gate and config, plus a hermetic end-to-end
  suite (stub pacman/vercmp/pacman-conf/paru on a private `PATH`, local
  loopback AUR RPC server, temp-dir filesystem) that also runs in
  `makepkg check()`.
- Packaging: PKGBUILD (`arch=any`, MIT, paru/yay as optdepends),
  `.SRCINFO`, release checklist with clean-chroot build instructions.

[0.1.1]: https://github.com/le0kar0ub1/Cellar/releases/tag/v0.1.1
[0.1.0]: https://github.com/le0kar0ub1/Cellar/releases/tag/v0.1.0
