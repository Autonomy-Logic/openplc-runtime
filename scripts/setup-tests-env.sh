#!/usr/bin/env bash
set -e

# ===========================
# Pytest Environment Setup
# ===========================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/venvs/test-env"

echo "🚀 Setting up test environment for OpenPLC Runtime"
echo "📂 Project root: $PROJECT_ROOT"
echo "🐍 Virtualenv: $VENV_DIR"
echo "=============================="

if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "✅ Virtual environment already exists."
fi

source "$VENV_DIR/bin/activate"

echo "⬆️  Upgrading pip..."
pip install --upgrade pip

if [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    echo "📦 Installing dependencies from requirements.txt..."
    pip install -r "$PROJECT_ROOT/requirements.txt"
fi

# 4. Install pytest and project in editable mode
echo "🧪 Installing pytest and local package..."
pip install pytest
pip install -e "$PROJECT_ROOT"

if [ ! -f "$PROJECT_ROOT/pytest.ini" ]; then
    echo "⚙️  Creating default pytest.ini..."
    cat <<EOF > "$PROJECT_ROOT/pytest.ini"
[pytest]
minversion = 7.0
addopts = -v --maxfail=3 --disable-warnings
testpaths = tests
pythonpath = .
EOF
fi

# Existing conftest.py with fixtures is preserved; no need to create or overwrite.

echo "🧪 Running pytest..."
pytest -vvv

echo "✅ All done!"
