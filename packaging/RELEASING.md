# Release checklist

The AUR repo (`ssh://aur@aur.archlinux.org/cellar.git`) contains only
`PKGBUILD` and `.SRCINFO`, synced from this directory. It only accepts the
`master` branch, and rejects pushes where `.SRCINFO` is out of sync.

1. Bump the version in `cellar/__init__.py`, `pyproject.toml`, and
   `PKGBUILD` (`pkgver`; reset `pkgrel=1`).
2. `python -m unittest discover -s tests`
3. Commit, tag, push:

   ```console
   git commit -am "Release X.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

4. Update checksum and metadata (never publish a placeholder or `SKIP`):

   ```console
   cd packaging
   updpkgsums
   makepkg --printsrcinfo > .SRCINFO
   ```

5. Build and smoke-test in a clean chroot ([devtools](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot)):

   ```console
   pacman -S devtools
   CHROOT=$HOME/chroot
   mkdir -p "$CHROOT"
   mkarchroot "$CHROOT/root" base-devel     # create the chroot (once)
   arch-nspawn "$CHROOT/root" pacman -Syu   # keep it up to date
   makechrootpkg -c -n -r "$CHROOT"         # build + namcap, from packaging/
   # install and run it inside the build copy, not on the host:
   arch-nspawn "$CHROOT/$USER" --bind="$PWD" pacman -U --noconfirm "$PWD"/cellar-*.pkg.tar.zst
   arch-nspawn "$CHROOT/$USER" cellar --version
   arch-nspawn "$CHROOT/$USER" cellar check
   ```

6. Sync to the AUR:

   ```console
   git clone ssh://aur@aur.archlinux.org/cellar.git aur-cellar  # first time only
   cp PKGBUILD .SRCINFO cellar.install aur-cellar/
   cd aur-cellar
   git add PKGBUILD .SRCINFO cellar.install
   git commit -m "Update to X.Y.Z"
   git push origin master
   ```

7. Commit the synced `PKGBUILD`/`.SRCINFO` back to this repo, and confirm
   the release end to end: `paru -S cellar`.

Packaging-only fix (no new tarball): bump `pkgrel` instead, skip steps 3
and `updpkgsums`, regenerate `.SRCINFO`, push.
