# Release checklist

This directory mirrors the AUR package for visibility; the AUR repo itself
(`ssh://aur@aur.archlinux.org/cellar.git`) contains only `PKGBUILD` and
`.SRCINFO`, synced from here at release time.

1. Bump the version in `cellar/__init__.py`, `pyproject.toml`, and
   `pkgver` in `PKGBUILD`.
2. Run the tests: `python -m unittest discover -s tests`.
3. Commit, tag `vX.Y.Z`, push the tag, and create the GitHub release.
4. Fill in the real checksum now that the tarball exists:

   ```console
   $ updpkgsums            # or: makepkg -g
   ```

   Never publish with a placeholder or `SKIP` checksum — the whole point of
   this tool is not trusting unverified sources.
5. Regenerate `.SRCINFO`:

   ```console
   $ makepkg --printsrcinfo > .SRCINFO
   ```

6. Build-test locally (`makepkg -si`), ideally in a clean chroot
   (`pkgctl build`).
7. Copy `PKGBUILD` and `.SRCINFO` into the AUR repo clone, commit, push.
