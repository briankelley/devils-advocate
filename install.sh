#!/bin/bash

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
    echo "Existing venv found at $VENV_DIR - upgrading..."
else
    echo "Creating venv at $VENV_DIR..."
    mkdir -p "$(dirname "$VENV_DIR")"
    if ! "$PYTHON" -m venv "$VENV_DIR"; then
        echo "Error: Failed to create virtual environment." >&2
        echo "Try: sudo apt install python3.12-venv" >&2
        exit 1
    fi
fi

# Install/upgrade
echo "Installing devils-advocate..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true

if ! "$VENV_DIR/bin/pip" install --upgrade devils-advocate; then
    echo "" >&2
    echo "Error: pip install failed. Check the output above for details." >&2
    exit 1
fi

# Symlink into ~/.local/bin
mkdir -p "$BIN_DIR"
if [ -L "$BIN_DIR/dvad" ] || [ ! -e "$BIN_DIR/dvad" ]; then
    ln -sf "$DVAD_BIN" "$BIN_DIR/dvad"
    echo "Symlinked dvad -> $BIN_DIR/dvad"
else
    echo "Warning: $BIN_DIR/dvad already exists and is not a symlink. Skipping." >&2
    echo "You can run dvad directly at: $DVAD_BIN" >&2
fi

# Set up systemd user service
SYSTEMD_OK=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    echo "Setting up systemd user service..."
    if "$DVAD_BIN" install --force; then
        SYSTEMD_OK=true
    else
        echo "  (systemd service setup skipped - configure later with: dvad install)"
    fi
fi

# Check if ~/.local/bin is in PATH
PATH_OK=true
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    PATH_OK=false
fi

echo ""
echo "================================================"
echo "  Devil's Advocate installed successfully!"
echo "================================================"
echo ""

if [ "$PATH_OK" = false ]; then
    echo "  Next steps:"
    echo ""
    echo "    1. Log out and back in (adds dvad to your PATH)"
    echo "    2. Run: dvad gui"
    echo "    3. Open http://localhost:8411 to finish setup"
    echo ""
elif [ "$SYSTEMD_OK" = true ]; then
    echo "  The dashboard is running at:"
    echo ""
    echo "    http://localhost:8411"
    echo ""
    echo "  Open that URL to finish setup."
    echo ""
else
    echo "  Next step:"
    echo ""
    echo "    Run: dvad gui"
    echo "    Then open http://localhost:8411 to finish setup."
    echo ""
fi
