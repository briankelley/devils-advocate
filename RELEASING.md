# Releasing

## Version Bumps

1. Update `__version__` in `src/devils_advocate/__init__.py`
2. Update `version` in `pyproject.toml`
3. Commit: `git commit -am "release: vX.Y.Z"`
4. Tag: `git tag vX.Y.Z`

## Build and Publish

```bash
pip install build twine
python -m build
twine upload dist/*
```

## Pre-release Checklist

- [ ] All tests pass: `pytest`
- [ ] Version strings match in `__init__.py` and `pyproject.toml`
- [ ] CHANGELOG updated (if maintained)
- [ ] No debug prints or development-only code
