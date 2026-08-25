#!/bin/bash

# Target directory path
TARGET_DIR="$HOME/Documents/HAM/GreenHeron"
APP_SCRIPT="rt21_network_controller.py"
VENV_DIR="$TARGET_DIR/.venv"

echo "=================================================="
echo "      GreenHeron RT-21 Launching (Stable)       "
echo "=================================================="

cd "$TARGET_DIR" || exit 1

# Activate the working virtual environment
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "❌ Error: Virtual environment not found. Please run the setup commands first."
    exit 1
fi

echo "🚀 Launching Application via Python $(python3 --version)..."
python3 "$APP_SCRIPT"

# Deactivate after app closes
deactivate

