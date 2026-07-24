# Cellar

**Enforce an aging period on AUR package upgrades.**

Cellar is a small gate that sits in front of your AUR helper. A package must
have been updated on the AUR at least *N* days ago (default: 7) before cellar
will let it be upgraded — a cooling-off window during which a malicious update
can be spotted and pulled by the community. Upgrades that haven't aged yet are
held at the installed version and passed to `paru`/`yay` via `--ignore`.

Cellar never builds packages itself. It only decides *when* your normal
upgrade flow is allowed to see an update.

## Security model and limitations — read this first

Cellar is **defense-in-depth, not a guarantee**:

* The delay only helps if someone actually reports the bad package during the
  window. The AUR has **no automatic vulnerability feed**; nothing scans
  packages on your behalf.
* Cellar **does not replace reading PKGBUILD diffs**. It buys time to do that;
  it does not do it for you.
* Cellar looks at AUR metadata (`Version`, `LastModified`) only. It cannot
  detect a compromise that ships inside an upstream release tarball, a
  maintainer account takeover that waits out the window, or anything else that
  survives *N* days of public scrutiny.
* By default the gate only protects the `cellar upgrade` path. If you keep
  running `paru -Syu` directly, see [Persistence](#persistence-sync-holds).

If you want a stronger posture, combine cellar with reading diffs
(`paru` shows them by default) and a minimal set of AUR packages.

## How it works

1. List installed foreign packages (`pacman -Qmq`) — packages not in any sync
   repo. Locally built packages that aren't on the AUR filter out naturally in
   the next step.
2. Batch-query the AUR RPC (`/rpc/v5/info`) for all of them at once.
3. **Version gate first.** If the installed version is equal to (or newer
   than) the AUR's latest, the package is up-to-date and no aging applies —
   `LastModified` is irrelevant. This is what prevents a cosmetic PKGBUILD
   edit from making a current package look "aging".
4. **Then age-gate.** If the versions differ, compute `now − LastModified`.
   Younger than the threshold → **hold**. Older → **allow**.
5. The clock resets on each new version. There is no version stepping — cellar
   holds the package entirely at whatever's installed until the *latest*
   version has aged past the threshold (see
   [Design decisions](#design-decisions)).
6. The actual upgrade is `paru -Syu` (or `yay`) with every held package passed
   as `--ignore`.

Cellar **fails safe**: if the AUR can't be reached, everything is held, the
failure is reported, and cellar exits non-zero. It never silently allows.

> **Nuance:** `LastModified` reflects the last AUR *metadata push*, not
> strictly a version bump — orphaning, adoption, or a comment-only PKGBUILD
> edit updates it too. The version gate in step 3 handles the common case
> (same version + fresh push = still up-to-date), but be aware that a
> metadata-only push *after* a version bump restarts that version's clock:
> cellar errs on the side of holding longer, never shorter.

## Install

From the AUR (once published):

```console
$ paru -S cellar
```

Or build the package yourself from this repo:

```console
$ cd packaging
$ makepkg -si
```

Or run straight from source (Python ≥ 3.11, stdlib only):

```console
$ python -m cellar check
```

## Usage

```console
$ cellar check                # read-only status of your AUR packages
$ cellar check --all          # include up-to-date and non-AUR packages
$ cellar upgrade              # paru/yay -Syu with aging packages --ignore'd
$ cellar upgrade --dry-run    # show what would run, run nothing
$ cellar upgrade --force pkg  # bypass the hold on pkg, this run only
$ cellar status pkg           # detail for one package
$ cellar sync-holds           # opt-in: write holds to a pacman ignore file
$ cellar config path          # print the config file in use
```

`cellar check` prints a table like:

```
PACKAGE   INSTALLED  LATEST     UPDATED  STATUS
somepkg   1.4.2-1    1.5.0-1    2.1d     aging (4.9d left)
otherpkg  0.9-1      1.0-1      12.3d    ready
```

By default only packages that aren't up-to-date are shown; `--all` shows
everything. Packages that exist locally but not on the AUR are warned about
and skipped. Packages in your pacman `IgnorePkg` are honored and never
touched.

Flags (per subcommand): `--days N`, `--helper paru|yay`, `--config PATH`,
`--dry-run` (upgrade, sync-holds), `--force PKG` (upgrade, repeatable),
`--all` (check).

### Exit codes

**0 on success — including when packages are held.** A hold is a normal
outcome, not an error. Non-zero only on actual failure: AUR unreachable,
malformed config, helper not found, invalid arguments. `cellar upgrade`
propagates the helper's exit code.

## Configuration

Config resolution, highest precedence first:

1. `--config <path>`
2. `$CELLAR_CONFIG`
3. `$XDG_CONFIG_HOME/cellar/config.toml`
   (`~/.config/cellar/config.toml` when `XDG_CONFIG_HOME` is unset)
4. `/etc/cellar/config.toml` (system-wide defaults)
5. built-in defaults

No other locations are probed. `cellar config path` prints the file actually
in use. CLI flags override config values.

```toml
# ~/.config/cellar/config.toml

days = 7            # aging threshold in days
helper = "paru"     # "paru" or "yay"; omit to auto-detect (paru, then yay)
stale_days = 30     # warn when a package has been held longer than this

# sync-holds settings (only relevant if you use `cellar sync-holds`)
holds_file = "/etc/pacman.d/cellar-holds.conf"
holds_max_age_days = 7   # warn when the holds file is older than this

# Per-package overrides
[packages.spotify]
trust = true        # always allow immediately; aging bypassed

[packages.some-fast-moving-tool]
days = 3            # custom aging period for this package
```

Notes:

* `--force` is a **per-invocation bypass only**; it never writes stored
  trust. Permanent trust goes in the config file, where it's explicit and
  auditable.
* `--days` overrides the global threshold; per-package `days` overrides still
  apply (they are more specific).
* Unknown config keys are rejected, loudly. A typo in a security gate's
  config should never be silently ignored.

## Stale holds

A fast-moving package can keep resetting its aging clock and end up held
indefinitely. Cellar tracks when each hold was first seen (in
`~/.local/state/cellar/holds.json`, bookkeeping only) and once a package has
been held past `stale_days` (default 30), `cellar check` warns prominently.
That's your cue to review the AUR diff manually and, if it's clean:

```console
$ cellar upgrade --force that-package
```

The failure mode is meant to prompt human review — not silent, indefinite
staleness.

## Persistence (sync-holds)

By default holds are **in-memory**: computed at runtime and passed as
`--ignore`. Clean, no root, nothing goes stale — but it only protects the
`cellar upgrade` path.

If you also run `paru -Syu` directly, `cellar sync-holds` is the opt-in
escape hatch. It writes the current holds to a dedicated file
(`/etc/pacman.d/cellar-holds.conf` by default; root required) that you
include from `/etc/pacman.conf`'s `[options]` section:

```ini
Include = /etc/pacman.d/cellar-holds.conf
```

Guarantees:

* Cellar writes **only** its own dedicated file — never `pacman.conf` itself,
  and never the file your manual ignores live in. It refuses to overwrite a
  file it didn't create.
* The file is headed `Managed by cellar. Do not edit`.
* The file records when it was generated. It goes stale if cellar isn't
  re-run — pacman would keep honoring old holds forever — so once it's older
  than `holds_max_age_days` (default 7), every cellar command warns loudly
  and tells you to re-run `sudo cellar sync-holds` or delete the file. Re-run
  it after each `cellar upgrade` (or from a timer) if you rely on it.

## Design decisions

### Version stepping was considered and rejected

"Why can't cellar install v2 — which has aged — on day 7, even though v3 just
came out?" Three reasons, all AUR-specific:

1. The RPC only exposes the **current** version. To install an older one,
   cellar would have to clone the package's AUR git repo and build old
   commits itself — turning a thin gate into a package builder with a much
   larger attack surface. (The tool whose job is distrust would become the
   thing you have to trust most.)
2. Old PKGBUILDs frequently don't build on a rolling distro.
3. It would systematically install code the maintainer has already
   superseded — sometimes *for security reasons*.

Trading unknown-malicious risk for known-broken risk is a bad trade. So the
clock resets on every new version, and the package stays at the installed
version until the latest has aged.

### Cellar holds cellar

Cellar gets no special treatment — being an AUR package, it gates its own
updates. That falls out of the design. The one consequence you should know:
**if a security advisory lands for cellar itself, its own patch is the one
update that can't reach you quickly.** In that case, run:

```console
$ cellar upgrade --force cellar
```

### Deferred to v2

Classifier signals beyond `LastModified` — the RPC also returns `OutOfDate`,
`NumVotes`, `Popularity`, and `Maintainer`. A recent maintainer change is
arguably a stronger supply-chain signal than an update timestamp, and could
flag a package or extend its aging period. Out of scope for v1.

## Development

Stdlib only (`urllib`, `json`, `tomllib`), Python ≥ 3.11. Both the code and
the PKGBUILD are deliberately small enough to audit in one sitting.

```console
$ python -m unittest discover -s tests    # unit + end-to-end tests
$ python -m cellar check                  # run from source
```

The suite is two layers, both dependency-free and hermetic:

* `tests/test_core.py` — unit tests for the gate logic (version gate, aging
  thresholds, overrides) and config validation.
* `tests/test_integration.py` — end-to-end tests that drive the real CLI:
  `pacman`/`vercmp`/`pacman-conf`/`paru` are stub executables on a private
  `PATH`, the AUR RPC is a local HTTP server on loopback, and all files live
  in a temp directory. No network, no pacman, and no root required — the
  suite also runs inside `makepkg`'s `check()` in a clean chroot.

Build and install the Arch package locally:

```console
$ cd packaging
$ makepkg -si
```

Ideally, test in a clean chroot ([devtools](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot)):

```console
$ pkgctl build    # or: extra-x86_64-build
```

## License

[MIT](LICENSE)
