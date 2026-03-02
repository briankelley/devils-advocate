# Fresh Install from TestPyPI (Vanilla VM)

These instructions assume a clean Linux system with Python 3.12+ and no pip installed.

## Steps

```bash
# 1. Install pip
sudo apt update && sudo apt install -y python3-pip

# 2. Install dependencies from real PyPI
pip install --break-system-packages click httpx pyyaml "rich[markdown]" fastapi "uvicorn[standard]" jinja2 python-multipart "ruamel.yaml"

# 3. Install dvad from TestPyPI
pip install --break-system-packages --no-deps --index-url https://test.pypi.org/simple/ devils-advocate==0.9.3

# 4. Log out and back in (adds ~/.local/bin to PATH)

# 5. Initialize config
dvad config --init

# 6. Set up API keys
#    Copy the example env and add your keys:
cp ~/.config/devils-advocate/.env.example ~/.config/devils-advocate/.env
#    Edit ~/.config/devils-advocate/.env with your API keys

# 7. Edit ~/.config/devils-advocate/models.yaml with your models and roles

# 8. Launch
dvad gui
```

## Notes

- The two-step install (steps 2-3) is required because TestPyPI has broken/squatted packages under real names (e.g. a fake `FASTAPI-1.0`). On production PyPI this will be a single `pip install devils-advocate` command.
- `--break-system-packages` is needed on modern distros (PEP 668). Fine for a throwaway test VM.
- The relog in step 4 is needed because `~/.local/bin` is added to PATH by `~/.profile` which only runs on login.
