#!/usr/bin/env bash
# Thin wrapper (v1.4). The real test is tests/run_offline_e2e.py — cross-platform, works on Windows with `python`.
set -euo pipefail
PKG="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
export PYTHONUTF8=1
exec "$PY" -X utf8 "$PKG/tests/run_offline_e2e.py" "$@"
