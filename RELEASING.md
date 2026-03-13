# Releasing

## Version Bumps

1. Update `__version__` in `src/devils_advocate/__init__.py`
2. Update `version` in `pyproject.toml`
3. Commit: `git commit -am "release: vX.Y.Z"`
4. Tag: `git tag vX.Y.Z`

## Build and Publish

performed by github actions. not local. deploys to PyPI

## Pre-release Checklist

- [ ] Targeted tests for updated test cases or changed application code pass: `pytest`
- [ ] Version strings match in `__init__.py` and `pyproject.toml`
- [ ] CHANGELOG updated (if maintained)
- [ ] No debug prints or development-only code
