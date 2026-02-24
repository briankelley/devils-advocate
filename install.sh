#!/bin/bash
set -e

VENV_DIR="$HOME/.local/share/devils-advocate/venv"
BIN_DIR="$HOME/.local/bin"
DVAD_BIN="$VENV_DIR/bin/dvad"
MIN_PYTHON="3.12"

echo "Devil's Advocate installer"
echo "=========================="
echo ""

# Find Python 3.12+
PYTHON=""
for candidate in python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        if [ -n "$version" ]; then
            major="${version%%.*}"
            minor="${version#*.}"
            if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
                PYTHON="$(command -v "$candidate")"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python $MIN_PYTHON or later is required but not found." >&2
    echo "Install it with your package manager (e.g., sudo apt install python3.12)" >&2
    exit 1
fi

echo "Using $PYTHON ($("$PYTHON" --version))"

# Create venv
if [ -d "$VENV_DIR" ]; then
    echo "Existing venv found at $VENV_DIR — upgrading..."
else
    echo "Creating venv at $VENV_DIR..."
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Install/upgrade
echo "Installing devils-advocate..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1
"$VENV_DIR/bin/pip" install --upgrade devils-advocate

# Symlink into ~/.local/bin
mkdir -p "$BIN_DIR"
if [ -L "$BIN_DIR/dvad" ] || [ ! -e "$BIN_DIR/dvad" ]; then
    ln -sf "$DVAD_BIN" "$BIN_DIR/dvad"
    echo "Symlinked dvad -> $BIN_DIR/dvad"
else
    echo "Warning: $BIN_DIR/dvad already exists and is not a symlink. Skipping." >&2
    echo "You can run dvad directly at: $DVAD_BIN" >&2
fi

# Check if ~/.local/bin is in PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    echo ""
    echo "Note: $BIN_DIR is not in your PATH."
    echo "Add it by appending this to your ~/.bashrc or ~/.zshrc:"
    echo ""
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Then restart your terminal or run: source ~/.bashrc"
    echo ""
fi

# Set up systemd user service
if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    echo "Setting up systemd user service..."
    "$DVAD_BIN" install --force
else
    echo "Note: systemd user services not available. Start the GUI manually with: dvad gui"
fi

echo ""
echo "Installation complete!"
echo ""
echo "  CLI:       dvad review --mode plan --input plan.md --project myproject"
echo "  Dashboard: http://localhost:8411"
echo ""
