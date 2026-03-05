#!/bin/bash

VENV_DIR="$HOME/.local/share/devils-advocate/venv"
BIN_DIR="$HOME/.local/bin"
DVAD_BIN="$VENV_DIR/bin/dvad"
MIN_PYTHON="3.12"
PORT=8411

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

# ─── Phase 1: Stop the running service BEFORE touching anything ────────────
if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    if systemctl --user is-active dvad-gui.service >/dev/null 2>&1; then
        OLD_PID=$(systemctl --user show dvad-gui.service -p MainPID --value 2>/dev/null)
        echo "[pre-install] Stopping running service (pid $OLD_PID)..."
        systemctl --user stop dvad-gui.service 2>/dev/null || true
        # Clear any start-limit-hit state from previous failed restarts
        systemctl --user reset-failed dvad-gui.service 2>/dev/null || true
        # Wait for the port to be released
        for i in 1 2 3 4 5; do
            if ! ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
                break
            fi
            echo "[pre-install] Waiting for port $PORT to be released..."
            sleep 1
        done
    else
        # Service exists but isn't running - clear failed state anyway
        systemctl --user reset-failed dvad-gui.service 2>/dev/null || true
    fi
fi

# ─── Phase 2: Record what we have before the install ───────────────────────
OLD_VERSION="(none)"
if [ -x "$DVAD_BIN" ]; then
    OLD_VERSION=$("$VENV_DIR/bin/python" -c "from importlib.metadata import version; print(version('devils-advocate'))" 2>/dev/null || echo "(unknown)")
fi
echo "[pre-install] Currently installed version: $OLD_VERSION"

# ─── Phase 3: Create venv if needed ───────────────────────────────────────
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/pip" ]; then
    echo "Existing venv found at $VENV_DIR - upgrading..."
else
    [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
    echo "Creating venv at $VENV_DIR..."
    mkdir -p "$(dirname "$VENV_DIR")"
    if ! "$PYTHON" -m venv "$VENV_DIR"; then
        echo "Error: Failed to create virtual environment." >&2
        echo "Try: sudo apt install python3.12-venv" >&2
        exit 1
    fi
fi

# ─── Phase 4: Install the package ─────────────────────────────────────────
echo "Installing devils-advocate from PyPI..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true

if ! "$VENV_DIR/bin/pip" install --no-cache-dir --force-reinstall devils-advocate; then
    echo "" >&2
    echo "Error: pip install failed. Check the output above for details." >&2
    exit 1
fi

# ─── Phase 5: Verify what pip actually installed ──────────────────────────
NEW_VERSION=$("$VENV_DIR/bin/python" -c "from importlib.metadata import version; print(version('devils-advocate'))" 2>/dev/null || echo "(unknown)")
DIST_INFO=$(find "$VENV_DIR/lib" -maxdepth 3 -type d -name "devils_advocate-*.dist-info" 2>/dev/null | head -1)
echo "[post-install] Installed version: $NEW_VERSION"
echo "[post-install] dist-info: $DIST_INFO"

if [ "$OLD_VERSION" = "$NEW_VERSION" ] && [ "$OLD_VERSION" != "(none)" ]; then
    echo "[WARNING] Version did not change ($OLD_VERSION -> $NEW_VERSION)."
    echo "          The latest version may not be published to PyPI yet."
    echo "          Check: pip index versions devils-advocate"
fi

# Purge stale bytecode so a service restart loads fresh code
find "$VENV_DIR/lib" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# ─── Phase 6: Symlink into ~/.local/bin ───────────────────────────────────
mkdir -p "$BIN_DIR"
if [ -L "$BIN_DIR/dvad" ] || [ ! -e "$BIN_DIR/dvad" ]; then
    ln -sf "$DVAD_BIN" "$BIN_DIR/dvad"
    echo "Symlinked dvad -> $BIN_DIR/dvad"
else
    echo "Warning: $BIN_DIR/dvad already exists and is not a symlink. Skipping." >&2
    echo "You can run dvad directly at: $DVAD_BIN" >&2
fi

# ─── Phase 7: Set up and start the systemd service ────────────────────────
SYSTEMD_OK=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    echo "Setting up systemd user service..."
    if "$DVAD_BIN" install --force; then
        SYSTEMD_OK=true
    else
        echo "  (systemd service setup skipped - configure later with: dvad install)"
    fi
fi

# ─── Phase 8: Health check ────────────────────────────────────────────────
if [ "$SYSTEMD_OK" = true ]; then
    echo ""
    echo "[health-check] Waiting for service to respond..."
    HEALTHY=false
    for i in 1 2 3 4 5 6; do
        sleep 1
        RESPONSE=$(curl -sf "http://127.0.0.1:${PORT}/api/version" 2>/dev/null || true)
        if [ -n "$RESPONSE" ]; then
            RUNNING_VERSION=$(echo "$RESPONSE" | "$VENV_DIR/bin/python" -c "import sys,json; print(json.load(sys.stdin).get('installed','?'))" 2>/dev/null || echo "?")
            RUNNING_PID=$(echo "$RESPONSE" | "$VENV_DIR/bin/python" -c "import sys,json; print(json.load(sys.stdin).get('pid','?'))" 2>/dev/null || echo "?")
            echo "[health-check] Service responding: version=$RUNNING_VERSION pid=$RUNNING_PID"
            if [ "$RUNNING_VERSION" = "$NEW_VERSION" ]; then
                HEALTHY=true
            else
                echo "[health-check] WARNING: Running version ($RUNNING_VERSION) != installed version ($NEW_VERSION)"
            fi
            break
        fi
    done
    if [ "$HEALTHY" = false ] && [ -z "$RESPONSE" ]; then
        echo "[health-check] WARNING: Service not responding after 6s."
        echo "  Check: systemctl --user status dvad-gui.service"
        echo "  Logs:  journalctl --user -u dvad-gui.service --since '1 min ago'"
    fi
fi

# ─── Done ─────────────────────────────────────────────────────────────────
# Check if ~/.local/bin is in PATH
PATH_OK=true
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    PATH_OK=false
fi

echo ""
echo "================================================"
echo "  Devil's Advocate installed successfully!"
echo "  $OLD_VERSION -> $NEW_VERSION"
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
