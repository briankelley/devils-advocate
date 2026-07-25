---
name: rebuild
description: Run the full dvad release chain to completion, unattended — publish workflow → PyPI → local reinstall → version verification. MANDATORY at every close-out (owner's word, 2026-07-24); also runs whenever the owner says "rebuild".
---

# /rebuild — the dvad release chain

This chain is pre-authorized and runs unattended, start to finish. Do not stop
to ask between steps. It is not done until step 4 passes.

**Mandatory at close-out (owner's word, 2026-07-24):** every close-out in this
repo ends with this chain. The one exemption: a close whose landing touched no
public-tree file states that plainly and skips — there is nothing to release,
and an empty version bump is noise, not a release.

1. **Dispatch the publish workflow:**
   `gh workflow run publish.yml --repo briankelley/devils-advocate`
   It builds the package, bumps the patch version, publishes to PyPI, and
   pushes a `build: bump version to X.Y.Z [skip ci]` commit.
2. **Watch the run to completion:**
   `gh run watch <run-id> --repo briankelley/devils-advocate --exit-status`
   (get the id from `gh run list --workflow=publish.yml --limit 1`). On
   success, pull the bump commit into the local repo: `git pull --ff-only`.
3. **Reinstall locally the way the owner runs it:** execute `./install.sh`,
   which force-reinstalls from PyPI into `~/.local/share/devils-advocate/venv`
   (the `dvad` on PATH). The repo's `.venv` is an editable dev install and
   needs no rebuild.
4. **Verify the bump landed:**
   `~/.local/share/devils-advocate/venv/bin/dvad --version` must report the
   workflow's new version, and the installer must not warn "Version did not
   change". PyPI propagation can lag a minute — retry the install once or
   twice if the old version comes back.

Report the old → new version pair when done. A failed or skipped step is
reported as exactly that — never as done.
