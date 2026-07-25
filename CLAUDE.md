# Devil's Advocate — agent instructions

## "Rebuild" (standing definition — invocable as the `/rebuild` skill)

When Brian says **rebuild**, run the entire release chain to completion, unattended:

1. **Dispatch the publish workflow**: `gh workflow run publish.yml --repo briankelley/devils-advocate`. This is pre-authorized — it builds the package, bumps the patch version, and publishes to PyPI. Do not stop to ask.
2. **Watch the run to completion** (`gh run watch` or poll `gh run list`). It also pushes a `build: bump version to X.Y.Z [skip ci]` commit; pull it into the local repo afterward.
3. **Reinstall locally the way Brian runs it**: execute `./install.sh`, which force-reinstalls from PyPI into `~/.local/share/devils-advocate/venv` (the `dvad` on PATH). The repo's `.venv` is an editable dev install and needs no rebuild.
4. **Validate the version bump landed locally**: confirm the installer's reported version matches the workflow's new version (and does not warn "Version did not change"). PyPI propagation can lag a minute — retry the install once or twice if the old version comes back.

A rebuild is not done until step 4 passes.
